import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bookclub.settings')
django.setup()
from core.models import User, Book, BookVote, BookRating, MeetingRegistration
from django.test import TestCase, Client
from django.urls import reverse
from rest_framework.test import APIClient
import json

# Test with a real user who has activity
try:
    user = User.objects.get(email='888svet@mail.ru')
    
    # Use APIClient with force_authenticate
    client = APIClient()
    client.force_authenticate(user)
    
    # Get user activity
    user_votes = BookVote.objects.filter(user=user)
    user_ratings = BookRating.objects.filter(user=user)
    user_meetings = MeetingRegistration.objects.filter(user=user, is_attended=True)
    
    print(f'User: {user.email} (id: {user.id})')
    print(f'Votes: {user_votes.count()} - {list(user_votes.values_list("book__title", flat=True))}')
    print(f'Ratings (GREEN/YELLOW): {user_ratings.count()} - {list(user_ratings.values_list("book__title", flat=True))}')
    print(f'Meeting attendance: {user_meetings.count()}')
    
    # Direct query like in view
    votes_from_query = set(BookVote.objects.filter(user_id=user.id).values_list('book_id', flat=True))
    ratings_from_query = set(BookRating.objects.filter(user_id=user.id, rating__in=['GREEN', 'YELLOW']).values_list('book_id', flat=True))
    
    print(f'\nDirect queries:')
    print(f'Votes: {votes_from_query}')
    print(f'Ratings: {ratings_from_query}')
    
    # Use APIClient instead
    response = client.get(reverse('recommendations-for-me') + '?debug_reco=1')
    data = response.json() if hasattr(response, 'json') else response.data
    
    print(f'\nResponse status: {response.status_code}')
    print(f'Response keys: {list(data.keys())}')
    print(f'Total recommendations: {data["count"]}')
    print(f'Used fallback: {data["used_fallback"]}')
    
    if 'debug' in data:
        print(f'Debug info: {data["debug"]}')
    
    print(f'\nTop recommendations:')
    for i, rec in enumerate(data['recommendations'][:10], 1):
        print(f'  {i}. {rec["title"]} by {rec["author"]} (score: {rec.get("score", "N/A")})')
except User.DoesNotExist:
    print('User 888svet@mail.ru not found')

