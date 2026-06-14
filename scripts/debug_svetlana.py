from core.models import User, BookVote, BookRating, MeetingRegistration
from django.utils import timezone
from django.db.models import Q

email = '888svet@mail.ru'
try:
    u = User.objects.get(email=email)
except User.DoesNotExist:
    print('NO_USER', email)
else:
    votes = list(BookVote.objects.filter(user=u).values_list('book_id', flat=True))
    ratings = list(BookRating.objects.filter(user=u, rating__in=['GREEN','YELLOW']).values_list('book_id', flat=True))
    meetings = list(
        MeetingRegistration.objects.filter(
            user=u,
            meeting__discussed_book__isnull=False,
            is_attended=True,
        ).filter(
            Q(meeting__status='COMPLETED') | Q(meeting__date__lte=timezone.now())
        ).values_list('meeting__discussed_book_id', flat=True)
    )
    print('USER', u.id, u.username, u.email)
    print('VOTES_COUNT', len(votes), votes)
    print('RATINGS_COUNT', len(ratings), ratings)
    print('MEETINGS_COUNT', len(meetings), meetings)
    # Also print seen ids set used by reco
    transactions = {}
    def add_item(uid, bid):
        if uid is None or bid is None:
            return
        transactions.setdefault(uid, set()).add(bid)
    for row in BookVote.objects.values('user_id', 'book_id'):
        add_item(row['user_id'], row['book_id'])
    for row in BookRating.objects.filter(rating__in=['GREEN','YELLOW']).values('user_id', 'book_id', 'rating'):
        add_item(row['user_id'], row['book_id'])
    for row in BookRating.objects.values('user_id', 'book_id', 'rating'):
        pass
    for row in BookReview.objects.values('user_id', 'book_id'):
        uid = row['user_id']
        bid = row['book_id']
        add_item(uid, bid)
    for row in MeetingRegistration.objects.filter(
        meeting__discussed_book__isnull=False,
        is_attended=True,
    ).filter(
        Q(meeting__status='COMPLETED') | Q(meeting__date__lte=timezone.now())
    ).values('user_id', 'meeting__discussed_book_id'):
        add_item(row['user_id'], row['meeting__discussed_book_id'])
    seen = transactions.get(u.id, set())
    print('SEEN_TOTAL', len(seen), sorted(list(seen)))
    # Simulate fallback content-based for vote seeds
    from core.views import RecommendationForMeView
    rv = RecommendationForMeView()
    seed_vote_ids = set(votes)
    print('SIMULATE_FALLBACK_FOR_VOTES', seed_vote_ids)
    fb = rv._fallback_content_based(user_seen_ids=seed_vote_ids, limit=5, excluded_book_ids=set(seen))
    print('FALLBACK_CANDIDATES', list(fb.values_list('id', flat=True)))
    # Inspect seed books' genres/authors and possible candidate pool
    from core.models import Book
    seed_books = Book.objects.filter(id__in=seed_vote_ids)
    for b in seed_books:
        print('SEED_BOOK', b.id, b.title, 'genre=', b.genre, 'author=', b.author)

    # Candidate pool by genre/author (pre-filter)
    genres = set(seed_books.values_list('genre', flat=True))
    authors = set(seed_books.values_list('author', flat=True))
    print('SEED_GENRES', genres)
    print('SEED_AUTHORS', authors)
    all_candidates = Book.objects.exclude(id__in=seed_vote_ids).exclude(id__in=seen).filter(is_archived=False)
    by_genre = all_candidates.filter(genre__in=genres)
    by_author = all_candidates.filter(author__in=authors)
    print('CANDIDATES_BY_GENRE_IDS', list(by_genre.values_list('id', flat=True)))
    print('CANDIDATES_BY_AUTHOR_IDS', list(by_author.values_list('id', flat=True)))

    # Build rules from transactions and run the actual _recommend_for_seed_group to see final enriched votes lane
    rules = rv._mine_rules_with_apriori(transactions=transactions, min_support=rv.DEFAULT_MIN_SUPPORT, min_confidence=rv.DEFAULT_MIN_CONFIDENCE, min_lift=rv.DEFAULT_MIN_LIFT)
    negative_source_ids = {}
    negative_penalties = rv._build_negative_penalties(rules, negative_source_ids)
    votes_lane = rv._recommend_for_seed_group(request=None, rules=rules, seed_book_ids=seed_vote_ids, user_seen_ids=seen, negative_penalties=negative_penalties, limit=5, source_label='vote_based', excluded_book_ids=set(seen))
    print('VOTES_LANE_FINAL_IDS', [b['id'] for b in votes_lane])
