import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bookclub.settings')
django.setup()
from django.test import RequestFactory
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from core.models import User, BookVote, BookRating, BookReview, MeetingRegistration
from core.views import RecommendationForMeView


def safe_get_user(identifier):
    try:
        return User.objects.get(username=identifier)
    except User.DoesNotExist:
        try:
            return User.objects.get(email=identifier)
        except User.DoesNotExist:
            return None


def title_list(qs):
    return list(qs.values_list('book__title', flat=True))


def analyze(user_identifier='vladimir'):
    user = safe_get_user(user_identifier)
    if not user:
        print(f"User '{user_identifier}' not found")
        return

    print(f"Analyzing user: id={user.id}, username={user.username}, email={user.email}")

    # Raw activity
    votes_qs = BookVote.objects.filter(user=user)
    ratings_all_qs = BookRating.objects.filter(user=user)
    ratings_pos_qs = BookRating.objects.filter(user=user, rating__in=['GREEN', 'YELLOW'])
    ratings_red_qs = BookRating.objects.filter(user=user, rating='RED')
    reviews_qs = BookReview.objects.filter(user=user)
    meetings_qs = MeetingRegistration.objects.filter(user=user, is_attended=True)

    print('\nActivity summary:')
    print('  votes:', votes_qs.count(), title_list(votes_qs))
    print('  ratings (all):', ratings_all_qs.count(), title_list(ratings_all_qs))
    print('  ratings (GREEN/YELLOW):', ratings_pos_qs.count(), title_list(ratings_pos_qs))
    print('  ratings (RED):', ratings_red_qs.count(), title_list(ratings_red_qs))
    print('  reviews:', reviews_qs.count())
    print('  meetings attended (discussed book):', meetings_qs.count())

    # Prepare a Request for view (with debug_reco=1)
    # Ensure ALLOWED_HOSTS includes the test host to avoid DisallowedHost errors
    from django.conf import settings
    try:
        settings.ALLOWED_HOSTS += ['testserver', 'localhost', '127.0.0.1']
    except Exception:
        settings.ALLOWED_HOSTS = ['testserver', 'localhost', '127.0.0.1']

    factory = RequestFactory()
    django_req = factory.get('/api/recommendations/for-me/?debug_reco=1')
    # Populate host/scheme so serializers that call build_absolute_uri work
    django_req.META['HTTP_HOST'] = 'localhost'
    django_req.META['wsgi.url_scheme'] = 'http'
    # We'll wrap into DRF Request and ensure .user is actual user
    drf_req = Request(django_req)
    drf_req.user = user

    view = RecommendationForMeView()

    # Build signals and rules
    transactions, weighted_profiles, negative_review_rating_books = view._build_user_signals()
    user_seen = transactions.get(user.id, set())
    print('\nSignals:')
    print('  seen set size:', len(user_seen), 'items:', user_seen)
    print('  weighted profile entries:', len(weighted_profiles.get(user.id, {})))
    print('  negative review/rating books for user:', negative_review_rating_books.get(user.id, set()))

    # Mine rules (use defaults from view)
    rules = view._mine_rules_with_apriori(
        transactions=transactions,
        min_support=view.DEFAULT_MIN_SUPPORT,
        min_confidence=view.DEFAULT_MIN_CONFIDENCE,
        min_lift=view.DEFAULT_MIN_LIFT,
    )
    # _mine_rules_with_apriori returns a dict lhs_book_id -> list(recs)
    total_rules = sum(len(v) for v in rules.values())
    print('\nApriori rules mined (total consequents):', total_rules)
    # Print rule summaries (limit to 40 printed consequents)
    printed = 0
    for lhs, recs in rules.items():
        for rec in recs:
            printed += 1
            print(f"  {printed}. {lhs} -> {rec['book_id']} (supp={rec.get('support')}, conf={rec.get('confidence'):.3f}, lift={rec.get('lift'):.3f})")
            if printed >= 40:
                break
        if printed >= 40:
            break

    # Negative penalties
    negative_source_ids = negative_review_rating_books.get(user.id, set())
    negative_penalties = view._build_negative_penalties(rules, negative_source_ids)
    print('\nNegative penalties for candidate books (top 20):')
    for bid, penalty in list(negative_penalties.items())[:20]:
        print(f'  book_id={bid} penalty={penalty:.3f}')

    # Evaluate recommendations with only votes seeds
    vote_seed = set(votes_qs.values_list('book_id', flat=True))
    rating_seed = set(ratings_pos_qs.values_list('book_id', flat=True))
    meeting_seed = set(meetings_qs.values_list('meeting__discussed_book_id', flat=True))

    print('\nSeed sets:')
    print('  vote_seed:', vote_seed)
    print('  rating_seed:', rating_seed)
    print('  meeting_seed:', meeting_seed)

    print('\nRunning recommendation with vote seeds only...')
    recs_votes_only = view._recommend_for_seed_group(
        request=drf_req,
        rules=rules,
        seed_book_ids=vote_seed,
        user_seen_ids=user_seen,
        negative_penalties=negative_penalties,
        limit=10,
        source_label='vote_based_debug',
        excluded_book_ids=set(user_seen),
        allow_broader_fallback=True,
    )
    print('  results count:', len(recs_votes_only))
    for i, r in enumerate(recs_votes_only, 1):
        print(f"    {i}. {r['title']} (id={r['id']}) score={r.get('score')} reasons={r.get('reasons')}")

    print('\nRunning recommendation with combined seeds (votes+ratings+meetings)...')
    combined_seeds = vote_seed | rating_seed | meeting_seed
    recs_combined = view._recommend_for_seed_group(
        request=drf_req,
        rules=rules,
        seed_book_ids=combined_seeds,
        user_seen_ids=user_seen,
        negative_penalties=negative_penalties,
        limit=10,
        source_label='combined_debug',
        excluded_book_ids=set(user_seen),
        allow_broader_fallback=True,
    )
    print('  results count:', len(recs_combined))
    for i, r in enumerate(recs_combined, 1):
        print(f"    {i}. {r['title']} (id={r['id']}) score={r.get('score')} reasons={r.get('reasons')}")

    print('\nFinal endpoint call (view.get) to see full payload:')
    full_resp = view.get(drf_req)
    payload = full_resp.data
    print('  payload keys:', list(payload.keys()))
    print('  payload count:', payload['count'])
    print('  used_fallback:', payload['used_fallback'])
    print('  top recommendations:')
    for i, r in enumerate(payload['recommendations'][:10], 1):
        print(f"    {i}. {r['title']} (id={r['id']}) score={r.get('score')} reasons={r.get('reasons')}")


if __name__ == '__main__':
    analyze('vladimir')
