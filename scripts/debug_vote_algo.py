"""
Debug the recommendation algorithm for a user who voted for drama/fantasy.
Shows: seed books, apriori rules, fallback logic, and final lane output.
"""
from core.models import User, BookVote, Book, BookRating, MeetingRegistration
from django.utils import timezone
from django.db.models import Q
from core.views import RecommendationForMeView
from efficient_apriori import apriori

# Hardcode for testing
import os
email = os.environ.get('DEBUG_USER_EMAIL', '888svet@mail.ru')

try:
    user = User.objects.get(email=email)
except User.DoesNotExist:
    print(f'User {email} not found')
    exit(1)

rv = RecommendationForMeView()

# Build signals (transactions, weighted_profiles, negative_review_rating_books)
transactions, weighted_profiles, negative_review_rating_books = rv._build_user_signals()

# User's seed books
user_vote_ids = set(BookVote.objects.filter(user_id=user.id).values_list('book_id', flat=True))
user_rating_ids = set(
    BookRating.objects.filter(user_id=user.id, rating__in=['GREEN', 'YELLOW']).values_list('book_id', flat=True)
)
    user_meeting_ids = set(
        MeetingRegistration.objects.filter(
            user_id=user.id,
            meeting__discussed_book__isnull=False,
            is_attended=True,
        ).filter(
            Q(meeting__status='COMPLETED') | Q(meeting__date__lte=timezone.now())
        ).values_list('meeting__discussed_book_id', flat=True)
    )

print(f"\n=== USER {user.id} {user.email} ===")
print(f"VOTE_IDS: {sorted(list(user_vote_ids))}")
if user_vote_ids:
    vote_books = Book.objects.filter(id__in=user_vote_ids).values('id', 'title', 'genre')
    for b in vote_books:
        print(f"  - {b['id']}: {b['title']} ({b['genre']})")
else:
    print("  (no votes)")

print(f"RATING_IDS: {sorted(list(user_rating_ids))}")
print(f"MEETING_IDS: {sorted(list(user_meeting_ids))}")

# Build full seen set
user_seen_ids = transactions.get(user.id, set()) | user_meeting_ids
attended_discussed_ids = user_meeting_ids
print(f"SEEN_TOTAL: {sorted(list(user_seen_ids))}")

# Mine rules
rules = rv._mine_rules_with_apriori(
    transactions=transactions,
    min_support=rv.DEFAULT_MIN_SUPPORT,
    min_confidence=rv.DEFAULT_MIN_CONFIDENCE,
    min_lift=rv.DEFAULT_MIN_LIFT,
)

print(f"\n=== APRIORI RULES (global) ===")
print(f"Total rules extracted: {sum(len(v) for v in rules.values())}")
print(f"\nRules FOR THIS USER'S VOTE SEEDS:")
for seed_id in user_vote_ids:
    rules_for_seed = rules.get(seed_id, [])
    seed_book = Book.objects.get(id=seed_id)
    print(f"\n  From BOOK {seed_id} ({seed_book.title} / {seed_book.genre}):")
    if rules_for_seed:
        for rule in rules_for_seed[:5]:  # Show first 5
            target_book = Book.objects.get(id=rule['book_id'])
            print(f"    -> {rule['book_id']} ({target_book.title}, {target_book.genre}): confidence={rule['confidence']:.2f}, lift={rule['lift']:.2f}")
    else:
        print(f"    (no rules)")

# Negative penalties
negative_source_ids = negative_review_rating_books.get(user.id, set())
negative_penalties = rv._build_negative_penalties(rules, negative_source_ids)
print(f"\n=== NEGATIVE PENALTIES ===")
print(f"Books with RED ratings: {negative_source_ids}")
print(f"Penalty targets: {len(negative_penalties)} books penalized")

# Now simulate the votes lane recommendation
print(f"\n=== VOTES LANE SIMULATION ===")
global_excluded = set(user_seen_ids)

votes_lane = rv._recommend_for_seed_group(
    request=None,
    rules=rules,
    seed_book_ids=user_vote_ids,
    user_seen_ids=user_seen_ids,
    negative_penalties=negative_penalties,
    limit=5,
    source_label='vote_based',
    excluded_book_ids=global_excluded,
    allow_broader_fallback=True,
)

print(f"\nFinal votes_lane (5 results):")
for i, book_dict in enumerate(votes_lane, 1):
    print(f"  {i}. {book_dict['id']}: {book_dict['title']} ({book_dict.get('genre', 'N/A')})")
    print(f"     apriori_score={book_dict.get('apriori_score', 0):.3f}")
    print(f"     reasons: {[r.get('source') for r in book_dict.get('reasons', [])]}")
