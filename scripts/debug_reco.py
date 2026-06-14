from django import setup
import os
import sys
# Ensure project root is on sys.path so Django settings package is importable
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
	sys.path.insert(0, ROOT)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bookclub.settings')
setup()

from django.utils import timezone
from datetime import timedelta
from rest_framework.test import APIRequestFactory
from rest_framework.request import Request as DRFRequest
from core.models import User, Book, Meeting, MeetingRegistration, MeetingStatus, BookVote, BookRating, BookReview
from core.views import RecommendationForMeView

# Clean DB: (in test env we use test DB, but here running in project DB; be careful)
# We'll create new objects with unique titles to avoid collisions.

# Create users
user, _ = User.objects.get_or_create(username='dbg_user', defaults={'email': 'dbg_user@example.com', 'first_name': 'Dbg', 'last_name': 'User', 'role': 'user'})
neighbor, _ = User.objects.get_or_create(username='dbg_neighbor', defaults={'email': 'dbg_neighbor@example.com', 'first_name': 'Dbg', 'last_name': 'Neighbor', 'role': 'user'})

# Create books
book_a, _ = Book.objects.get_or_create(title='DBG Book A', defaults={'genre':'Sci-Fi', 'author':'Author A', 'publication_year':2020})
book_b, _ = Book.objects.get_or_create(title='DBG Book B', defaults={'genre':'Fantasy', 'author':'Author B', 'publication_year':2021})
book_c, _ = Book.objects.get_or_create(title='DBG Book C', defaults={'genre':'History', 'author':'Author C', 'publication_year':2022})

# Create meetings
now = timezone.now()
mt_target, _ = Meeting.objects.get_or_create(title='DBG Target completed', defaults={'date': now - timedelta(days=10), 'location': 'Room', 'status': MeetingStatus.COMPLETED, 'discussed_book': book_a})
mt_b, _ = Meeting.objects.get_or_create(title='DBG Neighbor B', defaults={'date': now - timedelta(days=9), 'location': 'Room', 'status': MeetingStatus.COMPLETED, 'discussed_book': book_b})
mt_c, _ = Meeting.objects.get_or_create(title='DBG Neighbor C', defaults={'date': now - timedelta(days=8), 'location': 'Room', 'status': MeetingStatus.COMPLETED, 'discussed_book': book_c})

# Registrations
MeetingRegistration.objects.get_or_create(user=user, meeting=mt_target, defaults={'phone_number': '+70000000001', 'is_attended': True, 'attended_at': timezone.now()})
MeetingRegistration.objects.get_or_create(user=neighbor, meeting=mt_target, defaults={'phone_number': '+70000000002', 'is_attended': True, 'attended_at': timezone.now()})
MeetingRegistration.objects.get_or_create(user=neighbor, meeting=mt_b, defaults={'phone_number': '+70000000003', 'is_attended': True, 'attended_at': timezone.now()})
MeetingRegistration.objects.get_or_create(user=neighbor, meeting=mt_c, defaults={'phone_number': '+70000000004', 'is_attended': False})

# Now run recommendation view internals
view = RecommendationForMeView()
transactions, weighted_profiles, negative_review_rating_books = view._build_user_signals()
attended_discussed_ids = view._get_attended_discussed_book_ids(user.id)

print('transactions:', {k: list(v) for k,v in transactions.items()})
print('weighted_profiles (sample):', {k: dict(v) for k,v in weighted_profiles.items()})
print('negative_review_rating_books:', {k: list(v) for k,v in negative_review_rating_books.items()})
print('attended_discussed_ids for user:', attended_discussed_ids)

# Get rules
rules, status = view._get_cached_rules(transactions=transactions, min_support=0.08, min_confidence=0.3, min_lift=1.0)
print('rules keys:', rules.keys(), 'status:', status)

# Now get final payload via view.get
factory = APIRequestFactory()
ws_request = factory.get('/api/recommendations-for-me')
# Ensure host is acceptable for build_absolute_uri
ws_request.META['HTTP_HOST'] = 'localhost'
ws_request.META['wsgi.url_scheme'] = 'http'
request = DRFRequest(ws_request)
request.user = user
resp = view.get(request)
print('status_code:', resp.status_code)
print('payload count:', resp.data.get('count'))
print('recommended ids:', [r['id'] for r in resp.data.get('recommendations', [])])

print('---done---')