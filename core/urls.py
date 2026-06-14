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
router.register(r'admin-logs', views.AdminLogViewSet, basename='admin-log')
router.register(r'voting-periods', views.VotingPeriodViewSet, basename='voting-periods')
router.register(r'book-reviews', views.BookReviewViewSet, basename='book-reviews')
router.register(r'notifications', views.NotificationViewSet, basename='notifications')

urlpatterns = [
    path('meetings/register/', views.MeetingRegistrationViewSet.as_view({'get': 'list', 'post': 'create'}), name='meeting-registration-list'),
    path('meetings/register/<int:pk>/mark-attendance/', views.MeetingRegistrationViewSet.as_view({'patch': 'mark_attendance'}), name='meeting-registration-mark-attendance'),
    path('recommendations/for-me/', views.RecommendationForMeView.as_view(), name='recommendations-for-me'),
    path('check-username/', views.CheckUsernameView.as_view(), name='check-username'),
    path('check-password/', views.CheckPasswordView.as_view(), name='check-password'),
    path('check-email/', views.CheckEmailView.as_view(), name='check-email'),
    path('voting-period/', views.CurrentVotingPeriodView.as_view(), name='voting-period'),
    path('votes/period-stats/', views.VotingPeriodStatsView.as_view(), name='votes-period-stats'),
    path('admin/statistics/', views.AdminStatisticsView.as_view(), name='admin-statistics'),
    path('meetings/register/<int:pk>/', views.MeetingRegistrationViewSet.as_view({'delete': 'destroy'}), name='meeting-registration-delete'),# добавляем URL для удаления регистрации на встречу
    path('', include(router.urls)),
]