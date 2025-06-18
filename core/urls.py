#core/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

# Создаём роутер для ViewSet'ов
router = DefaultRouter()
router.register(r'users', views.UserViewSet, basename='users')
router.register(r'books', views.BookViewSet, basename='books')
router.register(r'meetings', views.MeetingViewSet, basename='meetings')
router.register(r'review-questions', views.ReviewQuestionViewSet, basename='review-questions')
router.register(r'votes', views.BookVoteViewSet, basename='votes')
router.register(r'ratings', views.BookRatingViewSet, basename='ratings')
router.register(r'reviews', views.MeetingReviewViewSet, basename='reviews')
router.register(r'meetingreviewquestions', views.MeetingReviewQuestionViewSet, basename='meetingreviewquestions')

urlpatterns = [
    path('meetings/register/', views.MeetingRegistrationViewSet.as_view({'get': 'list', 'post': 'create'}), name='meeting-registration-list'),
    path('check-username/', views.CheckUsernameView.as_view(), name='check-username'),
    path('check-password/', views.CheckPasswordView.as_view(), name='check-password'),
    path('check-email/', views.CheckEmailView.as_view(), name='check-email'),
    path('voting-period/', views.VotingPeriodView.as_view(), name='voting-period'),
    path('', include(router.urls)),
]