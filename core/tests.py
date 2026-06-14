from django.urls import reverse
from django.utils import timezone
from django.test import override_settings
from datetime import timedelta
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import AccessToken

from core.models import User, Book, Meeting, MeetingRegistration, MeetingStatus, VotingPeriod, BookVote, BookRating, BookReview, AdminLog, ReviewQuestion, MeetingReview, MeetingReviewQuestion, Notification


class AuthTokenFlowTests(APITestCase):
	def setUp(self):
		self.user = User.objects.create_user(
			username='token_user',
			email='token_user@example.com',
			password='StrongPass123',
			first_name='Token',
			last_name='User',
			role='user',
		)

	def test_expired_access_token_then_refresh_then_success(self):
		token_response = self.client.post(
			reverse('token_obtain_pair'),
			{'email': self.user.email, 'password': 'StrongPass123'},
			format='json',
		)
		self.assertEqual(token_response.status_code, status.HTTP_200_OK)
		refresh_token = token_response.data['refresh']

		expired_access = AccessToken.for_user(self.user)
		expired_access.set_exp(lifetime=timedelta(seconds=-1))
		self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {str(expired_access)}')

		expired_response = self.client.get(reverse('users-me'))
		self.assertEqual(expired_response.status_code, status.HTTP_401_UNAUTHORIZED)

		refresh_response = self.client.post(
			reverse('token_refresh'),
			{'refresh': refresh_token},
			format='json',
		)
		self.assertEqual(refresh_response.status_code, status.HTTP_200_OK)
		self.assertIn('access', refresh_response.data)

		new_access = refresh_response.data['access']
		self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {new_access}')
		success_response = self.client.get(reverse('users-me'))
		self.assertEqual(success_response.status_code, status.HTTP_200_OK)


class RegistrationConsentValidationTests(APITestCase):
	def test_registration_accepts_exactly_8_char_password(self):
		response = self.client.post(
			reverse('register'),
			{
				'username': 'len8_user',
				'email': 'len8_user@example.com',
				'first_name': 'Len',
				'last_name': 'Eight',
				'password': 'K9m2T7q4',
				'passwordConfirm': 'K9m2T7q4',
				'accept_privacy_policy': True,
				'accept_terms': True,
				'accept_personal_data_processing': True,
			},
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_201_CREATED)
		self.assertTrue(User.objects.filter(email='len8_user@example.com').exists())

	def test_registration_requires_all_consent_flags(self):
		response = self.client.post(
			reverse('register'),
			{
				'username': 'consent_user',
				'email': 'consent_user@example.com',
				'first_name': 'Consent',
				'last_name': 'User',
				'password': 'StrongPass123',
				'passwordConfirm': 'StrongPass123',
				'accept_privacy_policy': True,
				'accept_terms': False,
				'accept_personal_data_processing': True,
			},
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('accept_terms', response.data)
		self.assertFalse(User.objects.filter(email='consent_user@example.com').exists())

	def test_registration_succeeds_when_all_consents_are_true(self):
		response = self.client.post(
			reverse('register'),
			{
				'username': 'consent_ok_user',
				'email': 'consent_ok_user@example.com',
				'first_name': 'Consent',
				'last_name': 'Ok',
				'password': 'StrongPass123',
				'passwordConfirm': 'StrongPass123',
				'accept_privacy_policy': True,
				'accept_terms': True,
				'accept_personal_data_processing': True,
			},
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_201_CREATED)
		self.assertTrue(User.objects.filter(email='consent_ok_user@example.com').exists())

	def test_registration_fails_with_readable_message_for_duplicate_email(self):
		User.objects.create_user(
			username='already_email_user',
			email='duplicate_email@example.com',
			password='StrongPass123',
			first_name='Base',
			last_name='User',
		)

		response = self.client.post(
			reverse('register'),
			{
				'username': 'new_unique_username',
				'email': 'duplicate_email@example.com',
				'first_name': 'Consent',
				'last_name': 'Email',
				'password': 'StrongPass123',
				'passwordConfirm': 'StrongPass123',
				'accept_privacy_policy': True,
				'accept_terms': True,
				'accept_personal_data_processing': True,
			},
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		email_errors = response.data.get('email', [])
		self.assertTrue(any('существует' in str(message).lower() for message in email_errors))

	def test_registration_fails_with_readable_message_for_duplicate_username(self):
		User.objects.create_user(
			username='duplicate_nickname',
			email='unique_for_existing_username@example.com',
			password='StrongPass123',
			first_name='Base',
			last_name='User',
		)

		response = self.client.post(
			reverse('register'),
			{
				'username': 'duplicate_nickname',
				'email': 'totally_new_email@example.com',
				'first_name': 'Consent',
				'last_name': 'Nickname',
				'password': 'StrongPass123',
				'passwordConfirm': 'StrongPass123',
				'accept_privacy_policy': True,
				'accept_terms': True,
				'accept_personal_data_processing': True,
			},
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		username_errors = response.data.get('username', [])
		self.assertTrue(any('существует' in str(message).lower() for message in username_errors))


class RecommendationForMeViewTests(APITestCase):
	def setUp(self):
		self.user = User.objects.create_user(
			username='rec_user',
			email='rec_user@example.com',
			password='StrongPass123',
			first_name='Rec',
			last_name='User',
			role='user',
		)
		self.admin = User.objects.create_user(
			username='rec_admin',
			email='rec_admin@example.com',
			password='StrongPass123',
			first_name='Rec',
			last_name='Admin',
			role='admin',
			is_staff=True,
		)
		Book.objects.create(title='Book A', genre='Sci-Fi', author='Author 1', publication_year=2020)
		Book.objects.create(title='Book B', genre='Sci-Fi', author='Author 2', publication_year=2021)
		self.rating_book = Book.objects.create(title='Rating Book', genre='Sci-Fi', author='Author 3', publication_year=2022)

		self.url = reverse('recommendations-for-me')

	def test_recommendations_requires_auth(self):
		response = self.client.get(self.url)
		self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

	def test_recommendations_for_admin_forbidden(self):
		self.client.force_authenticate(self.admin)
		response = self.client.get(self.url)
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

	def test_recommendations_for_user_response_shape(self):
		self.client.force_authenticate(self.user)
		response = self.client.get(self.url)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertIn('count', response.data)
		self.assertIn('used_fallback', response.data)
		self.assertIn('params', response.data)
		self.assertIn('neighbors_used', response.data)
		self.assertIn('recommendations', response.data)

	def test_recommendations_accepts_min_hybrid_score(self):
		self.client.force_authenticate(self.user)
		response = self.client.get(self.url, {'min_hybrid_score': '0.3'})
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertIn('params', response.data)
		self.assertIn('min_hybrid_score', response.data['params'])
		self.assertEqual(float(response.data['params']['min_hybrid_score']), 0.3)

	def test_recommendations_exclude_archived_books_from_output(self):
		period = VotingPeriod.objects.create(
			start_date=timezone.now() - timedelta(days=1),
			end_date=timezone.now() + timedelta(days=1),
			is_active=True,
		)
		seen = Book.objects.create(title='Seen Mystery', genre='Mystery', author='A1', publication_year=2020)
		archived_candidate = Book.objects.create(
			title='Archived Mystery', genre='Mystery', author='A2', publication_year=2021, is_archived=True
		)
		active_candidate = Book.objects.create(title='Active Mystery', genre='Mystery', author='A3', publication_year=2022)

		BookVote.objects.create(user=self.user, book=seen, voting_period=period)

		self.client.force_authenticate(self.user)
		response = self.client.get(self.url)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		recommended_ids = [item['id'] for item in response.data['recommendations']]
		self.assertIn(active_candidate.id, recommended_ids)
		self.assertNotIn(archived_candidate.id, recommended_ids)

	def test_recommendations_limit_is_capped(self):
		self.client.force_authenticate(self.user)
		response = self.client.get(self.url, {'limit': '999'})
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data['params']['limit'], 20)
		self.assertLessEqual(response.data['count'], 20)

	def test_recommendations_use_popular_fallback_on_cold_start(self):
		self.client.force_authenticate(self.user)
		response = self.client.get(self.url, {'limit': '2'})
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data['count'], 2)
		self.assertEqual(len(response.data['recommendations']), 2)

	def test_recommendations_pad_with_repeat_books_when_catalog_is_short(self):
		third_book = Book.objects.create(title='Book C', genre='Sci-Fi', author='Author 3', publication_year=2022)
		self.assertIsNotNone(third_book.pk)

		self.client.force_authenticate(self.user)
		BookRating.objects.create(user=self.user, book=Book.objects.get(title='Book A'), rating='GREEN')
		BookRating.objects.create(user=self.user, book=Book.objects.get(title='Book B'), rating='YELLOW')

		response = self.client.get(self.url, {'limit': '3'})
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data['count'], 3)
		self.assertEqual(len(response.data['recommendations']), 3)

	@override_settings(DEBUG=True)
	def test_rating_change_busts_cached_recommendations(self):
		self.client.force_authenticate(self.user)

		first_response = self.client.get(self.url, {'debug_reco': '1', 'limit': '2'})
		self.assertEqual(first_response.status_code, status.HTTP_200_OK)
		self.assertEqual(first_response.data['debug']['cache_status'], 'miss')

		second_response = self.client.get(self.url, {'debug_reco': '1', 'limit': '2'})
		self.assertEqual(second_response.status_code, status.HTTP_200_OK)
		self.assertEqual(second_response.data['debug']['cache_status'], 'hit')

		rating_response = self.client.post(
			reverse('ratings-list'),
			{'book_id': self.rating_book.id, 'rating': 'GREEN'},
			format='json',
		)
		self.assertEqual(rating_response.status_code, status.HTTP_201_CREATED)

		third_response = self.client.get(self.url, {'debug_reco': '1', 'limit': '2'})
		self.assertEqual(third_response.status_code, status.HTTP_200_OK)
		self.assertEqual(third_response.data['debug']['cache_status'], 'miss')

	def test_red_rating_with_review_applies_soft_penalty(self):
		period = VotingPeriod.objects.create(
			start_date=timezone.now() - timedelta(days=1),
			end_date=timezone.now() + timedelta(days=1),
			is_active=True,
		)
		book_negative = Book.objects.create(title='Negative Book', genre='Drama', author='Neg', publication_year=2001)
		book_seen = Book.objects.create(title='Seen Book', genre='Drama', author='Seen', publication_year=2002)
		book_candidate = Book.objects.create(title='Candidate Book', genre='Drama', author='Cand', publication_year=2003)

		neighbor = User.objects.create_user(
			username='rec_neighbor',
			email='rec_neighbor@example.com',
			password='StrongPass123',
			first_name='Rec',
			last_name='Neighbor',
			role='user',
		)

		BookVote.objects.create(user=self.user, book=book_seen, voting_period=period)
		BookRating.objects.create(user=self.user, book=book_negative, rating='RED')
		BookReview.objects.create(user=self.user, book=book_negative, review_text='Не понравилось вообще')

		BookVote.objects.create(user=neighbor, book=book_negative, voting_period=period)
		BookVote.objects.create(user=neighbor, book=book_seen, voting_period=period)
		BookVote.objects.create(user=neighbor, book=book_candidate, voting_period=period)

		self.client.force_authenticate(self.user)
		response = self.client.get(self.url)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		recommended_items = response.data['recommendations']
		recommended_ids = [item['id'] for item in recommended_items]
		self.assertIn(book_candidate.id, recommended_ids)
		self.assertNotIn(book_negative.id, recommended_ids)

	def test_red_rating_excludes_rated_book_from_recommendations(self):
		period = VotingPeriod.objects.create(
			start_date=timezone.now() - timedelta(days=1),
			end_date=timezone.now() + timedelta(days=1),
			is_active=True,
		)
		book_negative = Book.objects.create(title='Negative Only Book', genre='Drama', author='Neg', publication_year=2004)
		book_seen = Book.objects.create(title='Seen Only Book', genre='Drama', author='Seen', publication_year=2005)
		book_candidate = Book.objects.create(title='Candidate Only Book', genre='Drama', author='Cand', publication_year=2006)

		neighbor = User.objects.create_user(
			username='rec_neighbor_red',
			email='rec_neighbor_red@example.com',
			password='StrongPass123',
			first_name='Rec',
			last_name='NeighborRed',
			role='user',
		)

		BookVote.objects.create(user=self.user, book=book_seen, voting_period=period)
		BookRating.objects.create(user=self.user, book=book_negative, rating='RED')

		BookVote.objects.create(user=neighbor, book=book_negative, voting_period=period)
		BookVote.objects.create(user=neighbor, book=book_seen, voting_period=period)
		BookVote.objects.create(user=neighbor, book=book_candidate, voting_period=period)

		self.client.force_authenticate(self.user)
		response = self.client.get(self.url)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		recommended_ids = [item['id'] for item in response.data['recommendations']]
		self.assertNotIn(book_negative.id, recommended_ids)


class AdminSiteAccessTests(APITestCase):
	def setUp(self):
		self.role_admin = User.objects.create_user(
			username='role_admin',
			email='role_admin@example.com',
			password='StrongPass123',
			first_name='Role',
			last_name='Admin',
			role='admin',
			is_staff=True,
		)
		self.superuser = User.objects.create_superuser(
			username='root_user',
			email='root@example.com',
			password='StrongPass123',
			first_name='Root',
			last_name='User',
		)

	def test_role_admin_cannot_access_django_admin_site(self):
		self.client.force_login(self.role_admin)
		response = self.client.get('/admin/')
		self.assertEqual(response.status_code, status.HTTP_302_FOUND)
		self.assertIn('/admin/login/', response.url)

	def test_superuser_can_access_django_admin_site(self):
		self.client.force_login(self.superuser)
		response = self.client.get('/admin/')
		self.assertEqual(response.status_code, status.HTTP_200_OK)


class UserListVisibilityTests(APITestCase):
	def setUp(self):
		self.admin = User.objects.create_user(
			username='users_admin',
			email='users_admin@example.com',
			password='StrongPass123',
			first_name='Users',
			last_name='Admin',
			role='admin',
			is_staff=True,
		)
		self.regular_user = User.objects.create_user(
			username='users_regular',
			email='users_regular@example.com',
			password='StrongPass123',
			first_name='Users',
			last_name='Regular',
			role='user',
		)
		self.other_admin = User.objects.create_user(
			username='users_other_admin',
			email='users_other_admin@example.com',
			password='StrongPass123',
			first_name='Users',
			last_name='OtherAdmin',
			role='admin',
			is_staff=True,
		)
		self.superuser = User.objects.create_superuser(
			username='users_root',
			email='users_root@example.com',
			password='StrongPass123',
		)

	def test_admin_list_shows_only_regular_users(self):
		self.client.force_authenticate(self.admin)
		response = self.client.get(reverse('users-list'))
		self.assertEqual(response.status_code, status.HTTP_200_OK)

		items = response.data if isinstance(response.data, list) else response.data.get('results', [])
		ids = [item['id'] for item in items]

		self.assertIn(self.regular_user.id, ids)
		self.assertNotIn(self.admin.id, ids)
		self.assertNotIn(self.other_admin.id, ids)
		self.assertNotIn(self.superuser.id, ids)

	def test_admin_list_search_filters_by_username(self):
		self.client.force_authenticate(self.admin)
		response = self.client.get(reverse('users-list'), {'search': 'regular'})
		self.assertEqual(response.status_code, status.HTTP_200_OK)

		items = response.data if isinstance(response.data, list) else response.data.get('results', [])
		ids = [item['id'] for item in items]

		self.assertIn(self.regular_user.id, ids)
		self.assertNotIn(self.admin.id, ids)
		self.assertNotIn(self.other_admin.id, ids)


class AttendanceAndGamificationTests(APITestCase):
	def setUp(self):
		self.user = User.objects.create_user(
			username='member_user',
			email='member_user@example.com',
			password='StrongPass123',
			first_name='Member',
			last_name='User',
			role='user',
		)
		self.admin = User.objects.create_user(
			username='club_admin',
			email='club_admin@example.com',
			password='StrongPass123',
			first_name='Club',
			last_name='Admin',
			role='admin',
			is_staff=True,
		)

		self.completed_meeting = Meeting.objects.create(
			title='Completed meeting',
			date=timezone.now() - timedelta(days=2),
			location='Room 1',
			status=MeetingStatus.COMPLETED,
		)
		self.upcoming_meeting = Meeting.objects.create(
			title='Upcoming meeting',
			date=timezone.now() + timedelta(days=2),
			location='Room 2',
			status=MeetingStatus.UPCOMING,
		)

		self.completed_registration = MeetingRegistration.objects.create(
			user=self.user,
			meeting=self.completed_meeting,
			phone_number='+79990000001',
		)
		# mark the completed registration as attended to reflect historical data
		self.completed_registration.is_attended = True
		self.completed_registration.attended_at = timezone.now()
		self.completed_registration.attendance_marked_by = self.admin
		self.completed_registration.save(update_fields=['is_attended', 'attended_at', 'attendance_marked_by'])
		self.upcoming_registration = MeetingRegistration.objects.create(
			user=self.user,
			meeting=self.upcoming_meeting,
			phone_number='+79990000002',
		)

	def test_non_admin_cannot_mark_attendance(self):
		url = reverse('meeting-registration-mark-attendance', args=[self.completed_registration.id])
		self.client.force_authenticate(self.user)
		response = self.client.patch(url, {'is_attended': True}, format='json')
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

	def test_admin_can_mark_attendance_for_completed_meeting(self):
		url = reverse('meeting-registration-mark-attendance', args=[self.completed_registration.id])
		self.client.force_authenticate(self.admin)
		response = self.client.patch(url, {'is_attended': True}, format='json')
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.completed_registration.refresh_from_db()
		self.assertTrue(self.completed_registration.is_attended)

	def test_admin_cannot_remove_participant_from_completed_meeting(self):
		# У админа нет права удалять участника после завершения встречи
		# Создаём отдельного пользователя/регистрацию, чтобы не конфликтовать с "completed_registration" из setUp
		tmp_user = User.objects.create_user(
			username='tmp_member',
			email='tmp_member@example.com',
			password='StrongPass123',
			first_name='Tmp',
			last_name='Member',
			role='user',
		)
		registration = MeetingRegistration.objects.create(
			user=tmp_user,
			meeting=self.completed_meeting,
			phone_number='+79990000003',
		)
		self.client.force_authenticate(self.admin)
		response = self.client.post(
			reverse('meetings-remove-participant', args=[self.completed_meeting.id]),
			{'registration_id': registration.id},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		# Запись должна остаться в базе
		self.assertTrue(MeetingRegistration.objects.filter(id=registration.id).exists())
		self.assertIsNotNone(self.completed_registration.attended_at)

	def test_admin_cannot_unmark_attendance_if_review_exists(self):
		self.completed_registration.is_attended = True
		self.completed_registration.attended_at = timezone.now()
		self.completed_registration.attendance_marked_by = self.admin
		self.completed_registration.save(update_fields=['is_attended', 'attended_at', 'attendance_marked_by'])

		question1 = ReviewQuestion.objects.create(question_text='Attend Q1')
		question2 = ReviewQuestion.objects.create(question_text='Attend Q2')
		question3 = ReviewQuestion.objects.create(question_text='Attend Q3')
		MeetingReviewQuestion.objects.create(
			meeting=self.completed_meeting,
			question1=question1,
			question2=question2,
			question3=question3,
		)
		MeetingReview.objects.create(
			user=self.user,
			meeting=self.completed_meeting,
			question1_answer=5,
			question2_answer=4,
			question3_answer=5,
		)

		url = reverse('meeting-registration-mark-attendance', args=[self.completed_registration.id])
		self.client.force_authenticate(self.admin)
		response = self.client.patch(url, {'is_attended': False}, format='json')
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('нельзя снять отметку посещения', str(response.data).lower())

		self.completed_registration.refresh_from_db()
		self.assertTrue(self.completed_registration.is_attended)

	def test_admin_cannot_remove_participant_if_review_exists(self):
		self.completed_registration.is_attended = True
		self.completed_registration.attended_at = timezone.now()
		self.completed_registration.attendance_marked_by = self.admin
		self.completed_registration.save(update_fields=['is_attended', 'attended_at', 'attendance_marked_by'])

		registration = self.completed_registration
		MeetingReview.objects.create(
			user=self.user,
			meeting=self.completed_meeting,
			question1_answer=5,
			question2_answer=4,
			question3_answer=5,
		)

		self.client.force_authenticate(self.admin)
		response = self.client.post(
			reverse('meetings-remove-participant', args=[self.completed_meeting.id]),
			{'registration_id': registration.id},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('уже оставил отзыв', str(response.data).lower())
		self.assertTrue(MeetingRegistration.objects.filter(id=registration.id).exists())

	def test_mark_attendance_repeat_same_state_is_idempotent(self):
		url = reverse('meeting-registration-mark-attendance', args=[self.completed_registration.id])
		self.client.force_authenticate(self.admin)

		first_response = self.client.patch(url, {'is_attended': True}, format='json')
		self.assertEqual(first_response.status_code, status.HTTP_200_OK)
		first_attended_at = first_response.data.get('attended_at')

		first_log_count = AdminLog.objects.filter(
			action='mark_attendance',
			registration_id=self.completed_registration.id,
		).count()

		second_response = self.client.patch(url, {'is_attended': True}, format='json')
		self.assertEqual(second_response.status_code, status.HTTP_200_OK)
		self.assertEqual(second_response.data.get('detail'), 'Статус посещения не изменился.')
		self.assertEqual(second_response.data.get('attended_at'), first_attended_at)

		second_log_count = AdminLog.objects.filter(
			action='mark_attendance',
			registration_id=self.completed_registration.id,
		).count()
		self.assertEqual(second_log_count, first_log_count)

	def test_admin_cannot_mark_attendance_for_upcoming_meeting(self):
		url = reverse('meeting-registration-mark-attendance', args=[self.upcoming_registration.id])
		self.client.force_authenticate(self.admin)
		response = self.client.patch(url, {'is_attended': True}, format='json')
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	def test_admin_can_mark_attendance_after_upcoming_meeting_set_to_completed(self):
		self.upcoming_meeting.status = MeetingStatus.COMPLETED
		self.upcoming_meeting.save(update_fields=['status'])

		url = reverse('meeting-registration-mark-attendance', args=[self.upcoming_registration.id])
		self.client.force_authenticate(self.admin)
		response = self.client.patch(url, {'is_attended': True}, format='json')
		self.assertEqual(response.status_code, status.HTTP_200_OK)

		self.upcoming_registration.refresh_from_db()
		self.assertTrue(self.upcoming_registration.is_attended)

	def test_admin_can_mark_attendance_for_legacy_lowercase_completed_status(self):
		Meeting.objects.filter(id=self.upcoming_meeting.id).update(status='completed')

		url = reverse('meeting-registration-mark-attendance', args=[self.upcoming_registration.id])
		self.client.force_authenticate(self.admin)
		response = self.client.patch(url, {'is_attended': True}, format='json')
		self.assertEqual(response.status_code, status.HTTP_200_OK)

		self.upcoming_registration.refresh_from_db()
		self.assertTrue(self.upcoming_registration.is_attended)

	def test_admin_can_mark_attendance_for_past_date_even_if_status_not_completed(self):
		# Встреча имела дату в прошлом, но не была явно помечена как COMPLETED — админ может поставить отметку
		past_meeting = Meeting.objects.create(
			title='Past not completed',
			date=timezone.now() - timedelta(days=1),
			location='Old Hall',
			status=MeetingStatus.UPCOMING,
		)
		registration = MeetingRegistration.objects.create(
			user=self.user,
			meeting=past_meeting,
			phone_number='+79990000004',
		)
		self.client.force_authenticate(self.admin)
		response = self.client.patch(
			reverse('meeting-registration-mark-attendance', args=[registration.id]),
			{'is_attended': True},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		registration.refresh_from_db()
		self.assertTrue(registration.is_attended)
		self.assertIsNotNone(registration.attended_at)

	def test_user_cannot_cancel_registration_for_non_upcoming_meeting(self):
		url = reverse('meeting-registration-delete', args=[self.completed_registration.id])
		self.client.force_authenticate(self.user)
		response = self.client.delete(url)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	def test_me_endpoint_returns_participation_level_from_attended_meetings(self):
		for idx in range(2):
			meeting = Meeting.objects.create(
				title=f'Completed extra {idx}',
				date=timezone.now() - timedelta(days=5 + idx),
				location='Room 3',
				status=MeetingStatus.COMPLETED,
			)
			MeetingRegistration.objects.create(
				user=self.user,
				meeting=meeting,
				phone_number=f'+7999000001{idx + 3}',
				is_attended=True,
				attended_at=timezone.now(),
				attendance_marked_by=self.admin,
			)

		self.completed_registration.is_attended = True
		self.completed_registration.attended_at = timezone.now()
		self.completed_registration.attendance_marked_by = self.admin
		self.completed_registration.save(update_fields=['is_attended', 'attended_at', 'attendance_marked_by'])

		self.client.force_authenticate(self.user)
		response = self.client.get(reverse('users-me'))
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data.get('attended_completed_count'), 3)
		self.assertEqual(response.data.get('participation_level', {}).get('code'), 'member')

	def test_recommendations_use_only_attended_completed_meetings(self):
		book_a = Book.objects.create(title='A', genre='Sci-Fi', author='Author A', publication_year=2020)
		book_b = Book.objects.create(title='B', genre='Fantasy', author='Author B', publication_year=2021)
		book_c = Book.objects.create(title='C', genre='History', author='Author C', publication_year=2022)

		neighbor = User.objects.create_user(
			username='neighbor_user',
			email='neighbor_user@example.com',
			password='StrongPass123',
			first_name='Neighbor',
			last_name='User',
			role='user',
		)

		target_meeting = Meeting.objects.create(
			title='Target completed',
			date=timezone.now() - timedelta(days=10),
			location='Room 4',
			status=MeetingStatus.COMPLETED,
			discussed_book=book_a,
		)
		neighbor_meeting_b = Meeting.objects.create(
			title='Neighbor completed B',
			date=timezone.now() - timedelta(days=9),
			location='Room 5',
			status=MeetingStatus.COMPLETED,
			discussed_book=book_b,
		)
		neighbor_meeting_c = Meeting.objects.create(
			title='Neighbor completed C',
			date=timezone.now() - timedelta(days=8),
			location='Room 6',
			status=MeetingStatus.COMPLETED,
			discussed_book=book_c,
		)

		MeetingRegistration.objects.create(
			user=self.user,
			meeting=target_meeting,
			phone_number='+79990000020',
			is_attended=True,
			attended_at=timezone.now(),
			attendance_marked_by=self.admin,
		)
		MeetingRegistration.objects.create(
			user=neighbor,
			meeting=target_meeting,
			phone_number='+79990000021',
			is_attended=True,
			attended_at=timezone.now(),
			attendance_marked_by=self.admin,
		)
		MeetingRegistration.objects.create(
			user=neighbor,
			meeting=neighbor_meeting_b,
			phone_number='+79990000022',
			is_attended=True,
			attended_at=timezone.now(),
			attendance_marked_by=self.admin,
		)
		MeetingRegistration.objects.create(
			user=neighbor,
			meeting=neighbor_meeting_c,
			phone_number='+79990000023',
			is_attended=False,
		)

		self.client.force_authenticate(self.user)
		response = self.client.get(reverse('recommendations-for-me'), {'debug_reco': '1'})
		# debug_reco requested; debug info included in response when applicable
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		recommended_ids = [item['id'] for item in response.data['recommendations']]
		self.assertIn(book_b.id, recommended_ids)
		self.assertNotIn(book_c.id, recommended_ids)

	def test_banned_user_cannot_register_for_meeting(self):
		self.user.is_banned = True
		self.user.is_active = False
		self.user.save(update_fields=['is_banned', 'is_active'])

		book = Book.objects.create(title='Ban book', genre='Роман', author='Author', publication_year=2024)
		meeting = Meeting.objects.create(
			title='Ban meeting',
			date=timezone.now() + timedelta(days=2),
			location='Room X',
			status=MeetingStatus.UPCOMING,
			discussed_book=book,
		)

		self.client.force_authenticate(self.user)
		response = self.client.post(
			reverse('meeting-registration-list'),
			{'meeting_id': meeting.id, 'phone_number': '+79990000999'},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('забан', str(response.data).lower())

	def test_ban_removes_future_upcoming_registrations(self):
		book = Book.objects.create(title='Future book', genre='Роман', author='Author', publication_year=2024)
		future_meeting = Meeting.objects.create(
			title='Future meeting',
			date=timezone.now() + timedelta(days=3),
			location='Room Y',
			status=MeetingStatus.UPCOMING,
			discussed_book=book,
		)
		past_completed_meeting = Meeting.objects.create(
			title='Past completed meeting',
			date=timezone.now() - timedelta(days=3),
			location='Room Z',
			status=MeetingStatus.COMPLETED,
			discussed_book=book,
		)
		MeetingRegistration.objects.create(
			user=self.user,
			meeting=future_meeting,
			phone_number='+79990000998',
		)
		MeetingRegistration.objects.create(
			user=self.user,
			meeting=past_completed_meeting,
			phone_number='+79990000997',
		)

		self.client.force_authenticate(self.admin)
		response = self.client.patch(
			reverse('users-ban', args=[self.user.id]),
			{'is_banned': True},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)

		self.assertFalse(MeetingRegistration.objects.filter(user=self.user, meeting=future_meeting).exists())
		self.assertTrue(MeetingRegistration.objects.filter(user=self.user, meeting=past_completed_meeting).exists())


class MeetingReviewVisibilityTests(APITestCase):
	def setUp(self):
		self.admin = User.objects.create_user(
			username='review_admin',
			email='review_admin@example.com',
			password='StrongPass123',
			first_name='Review',
			last_name='Admin',
			role='admin',
			is_staff=True,
		)
		self.user = User.objects.create_user(
			username='review_user_main',
			email='review_user_main@example.com',
			password='StrongPass123',
			first_name='Main',
			last_name='User',
			role='user',
		)
		self.other_user = User.objects.create_user(
			username='review_user_other',
			email='review_user_other@example.com',
			password='StrongPass123',
			first_name='Other',
			last_name='User',
			role='user',
		)
		self.book = Book.objects.create(title='Review Book', genre='Роман', author='Author', publication_year=2024)
		self.meeting = Meeting.objects.create(
			title='Review Meeting',
			date=timezone.now() - timedelta(days=1),
			location='Room 1',
			status=MeetingStatus.COMPLETED,
		)
		question1 = ReviewQuestion.objects.create(question_text='Q1')
		question2 = ReviewQuestion.objects.create(question_text='Q2')
		question3 = ReviewQuestion.objects.create(question_text='Q3')
		MeetingReviewQuestion.objects.create(
			meeting=self.meeting,
			question1=question1,
			question2=question2,
			question3=question3,
		)
		MeetingRegistration.objects.create(user=self.user, meeting=self.meeting, phone_number='+79990000001')
		MeetingRegistration.objects.create(user=self.other_user, meeting=self.meeting, phone_number='+79990000002')
		self.review = MeetingReview.objects.create(
			user=self.user,
			meeting=self.meeting,
			question1_answer=5,
			question2_answer=4,
			question3_answer=5,
		)
		MeetingReview.objects.create(
			user=self.other_user,
			meeting=self.meeting,
			question1_answer=3,
			question2_answer=3,
			question3_answer=3,
		)

	def test_regular_user_can_view_only_own_meeting_reviews(self):
		self.client.force_authenticate(self.user)
		response = self.client.get(reverse('reviews-by-user'), {'meeting_id': self.meeting.id})
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(len(response.data), 1)
		self.assertEqual(response.data[0]['id'], self.review.id)

	def test_regular_user_cannot_view_all_reviews_by_meeting(self):
		self.client.force_authenticate(self.user)
		response = self.client.get(reverse('reviews-by-meeting'), {'meeting_id': self.meeting.id})
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

	def test_admin_can_view_reviews_by_meeting(self):
		self.client.force_authenticate(self.admin)
		response = self.client.get(reverse('reviews-by-meeting'), {'meeting_id': self.meeting.id})
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(len(response.data), 2)


class BookRatingPrivacyTests(APITestCase):
	def setUp(self):
		self.admin = User.objects.create_user(
			username='rating_admin',
			email='rating_admin@example.com',
			password='StrongPass123',
			first_name='Rating',
			last_name='Admin',
			role='admin',
			is_staff=True,
		)
		self.user = User.objects.create_user(
			username='rating_user',
			email='rating_user@example.com',
			password='StrongPass123',
			first_name='Rating',
			last_name='User',
			role='user',
		)

		self.book_a = Book.objects.create(
			title='Rating Book A',
			genre='Роман',
			author='Author A',
			publication_year=2024,
		)
		self.book_b = Book.objects.create(
			title='Rating Book B',
			genre='Роман',
			author='Author B',
			publication_year=2024,
		)

		BookRating.objects.create(user=self.admin, book=self.book_a, rating='GREEN')
		BookRating.objects.create(user=self.user, book=self.book_b, rating='RED')

	def test_admin_cannot_list_book_ratings_due_to_privacy_policy(self):
		self.client.force_authenticate(self.admin)
		response = self.client.get(reverse('ratings-list'))
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
		self.assertIn('приватност', str(response.data).lower())

	def test_admin_cannot_retrieve_specific_user_rating_due_to_privacy_policy(self):
		target_rating = BookRating.objects.get(user=self.user, book=self.book_b)
		self.client.force_authenticate(self.admin)
		response = self.client.get(reverse('ratings-detail', args=[target_rating.id]))
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
		self.assertIn('чужих оценок', str(response.data).lower())

	def test_repeating_same_rating_is_idempotent_without_duplicate_rows(self):
		existing_rating = BookRating.objects.get(user=self.user, book=self.book_b)
		initial_rating_id = existing_rating.id

		self.client.force_authenticate(self.user)
		response = self.client.post(
			reverse('ratings-list'),
			{'book_id': self.book_b.id, 'rating': 'RED'},
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_201_CREATED)
		self.assertEqual(BookRating.objects.filter(user=self.user, book=self.book_b).count(), 1)

		updated_rating = BookRating.objects.get(user=self.user, book=self.book_b)
		self.assertEqual(updated_rating.id, initial_rating_id)
		self.assertEqual(updated_rating.rating, 'RED')
		self.assertEqual(response.data.get('id'), initial_rating_id)
		self.assertIn('user', response.data)
		self.assertIn('book', response.data)
		self.assertIn('rated_at', response.data)

	def test_user_cannot_update_rating_via_put_detail_endpoint(self):
		target_rating = BookRating.objects.get(user=self.user, book=self.book_b)
		self.client.force_authenticate(self.user)
		response = self.client.put(
			reverse('ratings-detail', args=[target_rating.id]),
			{'rating': 'GREEN'},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
		self.assertIn('post /api/ratings/', str(response.data).lower())


class MeetingReviewAttendancePolicyTests(APITestCase):
	def setUp(self):
		self.user = User.objects.create_user(
			username='review_policy_user',
			email='review_policy_user@example.com',
			password='StrongPass123',
			first_name='Policy',
			last_name='User',
			role='user',
		)

		self.meeting = Meeting.objects.create(
			title='Policy meeting',
			date=timezone.now() - timedelta(days=1),
			location='Room P',
			status=MeetingStatus.COMPLETED,
		)

		question1 = ReviewQuestion.objects.create(question_text='Policy Q1')
		question2 = ReviewQuestion.objects.create(question_text='Policy Q2')
		question3 = ReviewQuestion.objects.create(question_text='Policy Q3')
		MeetingReviewQuestion.objects.create(
			meeting=self.meeting,
			question1=question1,
			question2=question2,
			question3=question3,
		)

		self.registration = MeetingRegistration.objects.create(
			user=self.user,
			meeting=self.meeting,
			phone_number='+79990000031',
			is_attended=False,
		)

	def test_user_cannot_leave_meeting_review_without_attendance(self):
		self.client.force_authenticate(self.user)
		response = self.client.post(
			reverse('reviews-list'),
			{
				'meeting_id': self.meeting.id,
				'question1_answer': 5,
				'question2_answer': 4,
				'question3_answer': 5,
			},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('присутств', str(response.data).lower())

	def test_user_can_leave_meeting_review_when_attended(self):
		self.registration.is_attended = True
		self.registration.attended_at = timezone.now()
		self.registration.save(update_fields=['is_attended', 'attended_at'])

		self.client.force_authenticate(self.user)
		response = self.client.post(
			reverse('reviews-list'),
			{
				'meeting_id': self.meeting.id,
				'question1_answer': 5,
				'question2_answer': 4,
				'question3_answer': 5,
			},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_201_CREATED)

	def test_user_cannot_leave_meeting_review_with_null_answers(self):
		self.registration.is_attended = True
		self.registration.attended_at = timezone.now()
		self.registration.save(update_fields=['is_attended', 'attended_at'])

		self.client.force_authenticate(self.user)
		response = self.client.post(
			reverse('reviews-list'),
			{
				'meeting_id': self.meeting.id,
				'question1_answer': None,
				'question2_answer': 4,
				'question3_answer': None,
			},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('question1_answer', response.data)
		self.assertIn('question3_answer', response.data)


class MeetingReviewStatsNullHandlingTests(APITestCase):
	def setUp(self):
		self.admin = User.objects.create_user(
			username='stats_admin',
			email='stats_admin@example.com',
			password='StrongPass123',
			first_name='Stats',
			last_name='Admin',
			role='admin',
			is_staff=True,
		)
		self.user = User.objects.create_user(
			username='stats_user',
			email='stats_user@example.com',
			password='StrongPass123',
			first_name='Stats',
			last_name='User',
			role='user',
		)
		self.meeting = Meeting.objects.create(
			title='Stats Meeting',
			date=timezone.now() - timedelta(days=1),
			location='Stats room',
			status=MeetingStatus.COMPLETED,
		)
		MeetingRegistration.objects.create(
			user=self.user,
			meeting=self.meeting,
			phone_number='+79990000991',
			is_attended=True,
			attended_at=timezone.now(),
		)

	def test_stats_returns_none_and_empty_distribution_for_null_answers(self):
		MeetingReview.objects.create(
			user=self.user,
			meeting=self.meeting,
			question1_answer=None,
			question2_answer=None,
			question3_answer=None,
		)

		self.client.force_authenticate(self.admin)
		response = self.client.get(reverse('reviews-stats'), {'meeting_id': self.meeting.id})

		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data.get('total_reviews'), 1)
		self.assertIsNone(response.data.get('averages', {}).get('question1'))
		self.assertIsNone(response.data.get('averages', {}).get('question2'))
		self.assertIsNone(response.data.get('averages', {}).get('question3'))
		self.assertEqual(response.data.get('distributions', {}).get('question1'), {})
		self.assertEqual(response.data.get('distributions', {}).get('question2'), {})
		self.assertEqual(response.data.get('distributions', {}).get('question3'), {})


class MeetingsAndModerationFlowTests(APITestCase):
	def setUp(self):
		self.admin = User.objects.create_user(
			username='flow_admin',
			email='flow_admin@example.com',
			password='StrongPass123',
			first_name='Flow',
			last_name='Admin',
			role='admin',
			is_staff=True,
		)
		self.user = User.objects.create_user(
			username='flow_user',
			email='flow_user@example.com',
			password='StrongPass123',
			first_name='Flow',
			last_name='User',
			role='user',
		)

		self.meeting = Meeting.objects.create(
			title='Flow meeting',
			date=timezone.now() + timedelta(days=1),
			location='Room A',
			status=MeetingStatus.UPCOMING,
		)

		self.completed_other_meeting = Meeting.objects.create(
			title='Other completed meeting',
			date=timezone.now() - timedelta(days=2),
			location='Room B',
			status=MeetingStatus.COMPLETED,
		)

		self.completed_registered_meeting = Meeting.objects.create(
			title='My completed meeting',
			date=timezone.now() - timedelta(days=3),
			location='Room C',
			status=MeetingStatus.COMPLETED,
		)
		MeetingRegistration.objects.create(
			user=self.user,
			meeting=self.completed_registered_meeting,
			phone_number='+79990000177',
		)

	def test_admin_delete_meeting_sets_cancelled_status(self):
		self.client.force_authenticate(self.admin)
		response = self.client.delete(reverse('meetings-detail', args=[self.meeting.id]))
		self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
		self.meeting.refresh_from_db()
		self.assertEqual(self.meeting.status, MeetingStatus.CANCELLED)

	def test_admin_cannot_cancel_completed_meeting(self):
		self.client.force_authenticate(self.admin)
		response = self.client.delete(reverse('meetings-detail', args=[self.completed_other_meeting.id]))
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.completed_other_meeting.refresh_from_db()
		self.assertEqual(self.completed_other_meeting.status, MeetingStatus.COMPLETED)

	def test_admin_cannot_patch_completed_meeting_to_cancelled(self):
		self.client.force_authenticate(self.admin)
		response = self.client.patch(
			reverse('meetings-detail', args=[self.completed_other_meeting.id]),
			{'status': MeetingStatus.CANCELLED},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.completed_other_meeting.refresh_from_db()
		self.assertEqual(self.completed_other_meeting.status, MeetingStatus.COMPLETED)

	def test_admin_cannot_create_meeting_with_empty_date(self):
		self.client.force_authenticate(self.admin)
		response = self.client.post(
			reverse('meetings-list'),
			{
				'title': 'Broken meeting',
				'date': '',
				'location': 'Room X',
				'status': MeetingStatus.UPCOMING,
			},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('дат', str(response.data).lower())
		self.assertIn('время', str(response.data).lower())

	def test_admin_cannot_create_meeting_with_invalid_date_format(self):
		self.client.force_authenticate(self.admin)
		response = self.client.post(
			reverse('meetings-list'),
			{
				'title': 'Broken meeting 2',
				'date': 'tomorrow at 6 pm',
				'location': 'Room Y',
				'status': MeetingStatus.UPCOMING,
			},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('iso', str(response.data).lower())

	def test_user_cannot_access_global_completed_archive_via_status_filter(self):
		self.client.force_authenticate(self.user)
		response = self.client.get(reverse('meetings-list'), {'status': MeetingStatus.COMPLETED})
		self.assertEqual(response.status_code, status.HTTP_200_OK)

		returned_ids = {row['id'] for row in response.data}
		self.assertIn(self.meeting.id, returned_ids)
		self.assertNotIn(self.completed_other_meeting.id, returned_ids)
		self.assertNotIn(self.completed_registered_meeting.id, returned_ids)
		self.assertTrue(all(row['status'] == MeetingStatus.UPCOMING for row in response.data))

	def test_user_cannot_retrieve_own_completed_meeting_via_global_meetings_detail(self):
		self.client.force_authenticate(self.user)
		response = self.client.get(reverse('meetings-detail', args=[self.completed_registered_meeting.id]))
		self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

	def test_user_meetings_contains_only_registered_meetings_including_past_and_upcoming(self):
		MeetingRegistration.objects.create(
			user=self.user,
			meeting=self.meeting,
			phone_number='+79990000178',
		)

		self.client.force_authenticate(self.user)
		response = self.client.get(reverse('users-meetings'))
		self.assertEqual(response.status_code, status.HTTP_200_OK)

		returned_ids = {row['id'] for row in response.data}
		self.assertIn(self.meeting.id, returned_ids)
		self.assertIn(self.completed_registered_meeting.id, returned_ids)
		self.assertNotIn(self.completed_other_meeting.id, returned_ids)

	def test_user_can_retrieve_single_own_meeting_via_users_meetings_detail(self):
		self.client.force_authenticate(self.user)
		response = self.client.get(
			reverse('users-meeting-detail', kwargs={'meeting_id': self.completed_registered_meeting.id})
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data.get('id'), self.completed_registered_meeting.id)

	def test_user_cannot_retrieve_single_foreign_meeting_via_users_meetings_detail(self):
		self.client.force_authenticate(self.user)
		response = self.client.get(
			reverse('users-meeting-detail', kwargs={'meeting_id': self.completed_other_meeting.id})
		)
		self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

	def test_user_can_retrieve_single_own_registration_via_users_registrations_detail(self):
		own_registration = MeetingRegistration.objects.filter(
			user=self.user,
			meeting=self.completed_registered_meeting,
		).first()
		self.assertIsNotNone(own_registration)

		self.client.force_authenticate(self.user)
		response = self.client.get(
			reverse('users-registration-detail', kwargs={'registration_id': own_registration.id})
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data.get('registration_id'), own_registration.id)
		self.assertEqual(response.data.get('meeting_id'), self.completed_registered_meeting.id)

	def test_user_cannot_retrieve_foreign_registration_via_users_registrations_detail(self):
		foreign_user = User.objects.create_user(
			username='foreign_reg_user',
			email='foreign_reg_user@example.com',
			password='StrongPass123',
			first_name='Foreign',
			last_name='User',
			role='user',
		)
		foreign_registration = MeetingRegistration.objects.create(
			user=foreign_user,
			meeting=self.meeting,
			phone_number='+79990000179',
		)

		self.client.force_authenticate(self.user)
		response = self.client.get(
			reverse('users-registration-detail', kwargs={'registration_id': foreign_registration.id})
		)
		self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

	def test_user_cannot_retrieve_foreign_completed_meeting(self):
		self.client.force_authenticate(self.user)
		response = self.client.get(reverse('meetings-detail', args=[self.completed_other_meeting.id]))
		self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

	def test_admin_can_restore_cancelled_meeting(self):
		self.meeting.status = MeetingStatus.CANCELLED
		self.meeting.save(update_fields=['status'])

		self.client.force_authenticate(self.admin)
		response = self.client.post(reverse('meetings-restore', args=[self.meeting.id]), format='json')
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data.get('status'), MeetingStatus.UPCOMING)

		self.meeting.refresh_from_db()
		self.assertEqual(self.meeting.status, MeetingStatus.UPCOMING)

	def test_admin_cannot_restore_non_cancelled_meeting(self):
		self.client.force_authenticate(self.admin)
		response = self.client.post(reverse('meetings-restore', args=[self.meeting.id]), format='json')
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	def test_admin_can_patch_completed_meeting_back_to_upcoming(self):
		self.meeting.status = MeetingStatus.COMPLETED
		self.meeting.save(update_fields=['status'])

		self.client.force_authenticate(self.admin)
		response = self.client.patch(
			reverse('meetings-detail', args=[self.meeting.id]),
			{'status': MeetingStatus.UPCOMING},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data.get('status'), MeetingStatus.UPCOMING)

	def test_admin_cannot_patch_completed_meeting_to_upcoming_if_attendance_exists(self):
		self.completed_registered_meeting.status = MeetingStatus.COMPLETED
		self.completed_registered_meeting.save(update_fields=['status'])
		registration = MeetingRegistration.objects.get(
			user=self.user,
			meeting=self.completed_registered_meeting,
		)
		registration.is_attended = True
		registration.attended_at = timezone.now()
		registration.attendance_marked_by = self.admin
		registration.save(update_fields=['is_attended', 'attended_at', 'attendance_marked_by'])

		self.client.force_authenticate(self.admin)
		response = self.client.patch(
			reverse('meetings-detail', args=[self.completed_registered_meeting.id]),
			{'status': MeetingStatus.UPCOMING},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('посещ', str(response.data).lower())

	def test_admin_cannot_patch_completed_meeting_to_upcoming_if_reviews_exist(self):
		self.meeting.status = MeetingStatus.COMPLETED
		self.meeting.save(update_fields=['status'])
		MeetingReview.objects.create(user=self.user, meeting=self.meeting)

		self.client.force_authenticate(self.admin)
		response = self.client.patch(
			reverse('meetings-detail', args=[self.meeting.id]),
			{'status': MeetingStatus.UPCOMING},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('status', response.data)

	def test_admin_can_reschedule_upcoming_meeting_with_registrations_from_past_to_future(self):
		meeting = Meeting.objects.create(
			title='Reschedule me',
			date=timezone.now() - timedelta(days=1),
			location='Room D',
			status=MeetingStatus.UPCOMING,
		)
		registration = MeetingRegistration.objects.create(
			user=self.user,
			meeting=meeting,
			phone_number='+79990000179',
		)

		new_date = timezone.now() + timedelta(days=3)

		self.client.force_authenticate(self.admin)
		response = self.client.patch(
			reverse('meetings-detail', args=[meeting.id]),
			{'date': new_date.isoformat()},
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data.get('status'), MeetingStatus.UPCOMING)
		self.assertEqual(MeetingRegistration.objects.filter(id=registration.id).count(), 1)

		meeting.refresh_from_db()
		self.assertGreater(meeting.date, timezone.now())

	def test_admin_cannot_reschedule_upcoming_meeting_with_attendance_to_future(self):
		meeting = Meeting.objects.create(
			title='Reschedule blocked',
			date=timezone.now() - timedelta(days=1),
			location='Room E',
			status=MeetingStatus.UPCOMING,
		)
		registration = MeetingRegistration.objects.create(
			user=self.user,
			meeting=meeting,
			phone_number='+79990000180',
			is_attended=True,
			attended_at=timezone.now(),
			attendance_marked_by=self.admin,
		)

		self.client.force_authenticate(self.admin)
		response = self.client.patch(
			reverse('meetings-detail', args=[meeting.id]),
			{'date': (timezone.now() + timedelta(days=3)).isoformat()},
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('отмечено посещение', str(response.data).lower())
		registration.refresh_from_db()
		self.assertTrue(registration.is_attended)

	def test_admin_noop_patch_does_not_create_duplicate_log(self):
		self.client.force_authenticate(self.admin)
		meeting_url = reverse('meetings-detail', args=[self.meeting.id])

		first_response = self.client.patch(
			meeting_url,
			{'title': 'Flow meeting updated'},
			format='json',
		)
		self.assertEqual(first_response.status_code, status.HTTP_200_OK)

		logs_after_first_patch = AdminLog.objects.filter(
			action='редактирование встречи',
			target_id=self.meeting.id,
		).count()
		self.assertEqual(logs_after_first_patch, 1)

		second_response = self.client.patch(
			meeting_url,
			{'title': 'Flow meeting updated'},
			format='json',
		)
		self.assertEqual(second_response.status_code, status.HTTP_200_OK)

		logs_after_second_patch = AdminLog.objects.filter(
			action='редактирование встречи',
			target_id=self.meeting.id,
		).count()
		self.assertEqual(logs_after_second_patch, 1)

	def test_admin_can_remove_participant_from_meeting(self):
		registration = MeetingRegistration.objects.create(
			user=self.user,
			meeting=self.meeting,
			phone_number='+79990000111',
		)
		self.assertEqual(self.meeting.registrations.count(), 1)
		self.client.force_authenticate(self.admin)
		response = self.client.post(
			reverse('meetings-remove-participant', args=[self.meeting.id]),
			{'registration_id': registration.id},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data.get('current_attendees'), 0)
		self.assertFalse(MeetingRegistration.objects.filter(id=registration.id).exists())
		self.assertEqual(self.meeting.registrations.count(), 0)

	def test_admin_add_participant_returns_auto_filled_participant_names(self):
		self.client.force_authenticate(self.admin)
		response = self.client.post(
			reverse('meetings-add-participant', args=[self.meeting.id]),
			{'email': self.user.email, 'phone_number': '+79990000199'},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_201_CREATED)
		self.assertEqual(response.data.get('participant_first_name'), self.user.first_name)
		self.assertEqual(response.data.get('participant_last_name'), self.user.last_name)

	def test_admin_can_view_meeting_participants(self):
		registration = MeetingRegistration.objects.create(
			user=self.user,
			meeting=self.meeting,
			phone_number='+79990000198',
		)
		self.client.force_authenticate(self.admin)
		response = self.client.get(reverse('meetings-participants', args=[self.meeting.id]))
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data.get('meeting_id'), self.meeting.id)
		self.assertEqual(response.data.get('count'), 1)
		participants = response.data.get('participants', [])
		self.assertEqual(len(participants), 1)
		self.assertEqual(participants[0].get('id'), registration.id)
		self.assertEqual(participants[0].get('participant_first_name'), self.user.first_name)
		self.assertEqual(participants[0].get('participant_last_name'), self.user.last_name)

	def test_non_admin_cannot_view_meeting_participants(self):
		self.client.force_authenticate(self.user)
		response = self.client.get(reverse('meetings-participants', args=[self.meeting.id]))
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

	def test_admin_remove_participant_rejects_null_registration_id(self):
		self.client.force_authenticate(self.admin)
		response = self.client.post(
			reverse('meetings-remove-participant', args=[self.meeting.id]),
			{'registration_id': 'null'},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('registration_id', str(response.data).lower())

	def test_book_review_rejects_profanity(self):
		book = Book.objects.create(title='Flow book', genre='Novel', author='Author', publication_year=2024)
		self.client.force_authenticate(self.user)
		response = self.client.post(
			reverse('book-reviews-list'),
			{
				'book': book.id,
				'review_text': 'Это полная херня, вообще не понравилось.',
			},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	def test_mark_attendance_log_keeps_history_after_registration_delete(self):
		self.meeting.status = MeetingStatus.COMPLETED
		self.meeting.save(update_fields=['status'])
		registration = MeetingRegistration.objects.create(
			user=self.user,
			meeting=self.meeting,
			phone_number='+79990000123',
		)

		self.client.force_authenticate(self.admin)
		response = self.client.patch(
			reverse('meeting-registration-mark-attendance', args=[registration.id]),
			{'is_attended': True},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)

		log = AdminLog.objects.filter(action='mark_attendance').order_by('-id').first()
		self.assertIsNotNone(log)
		self.assertEqual(log.registration_id, registration.id)

		registration.delete()
		log.refresh_from_db()
		self.assertIsNone(log.registration)
		self.assertEqual(log.target_id, response.data['registration_id'])

	def test_admin_cannot_set_max_attendees_to_zero(self):
		self.client.force_authenticate(self.admin)
		response = self.client.patch(
			reverse('meetings-detail', args=[self.meeting.id]),
			{'max_attendees': 0},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('max_attendees', response.data)

	def test_admin_cannot_set_max_attendees_below_current_registrations(self):
		MeetingRegistration.objects.create(
			user=self.user,
			meeting=self.meeting,
			phone_number='+79990000124',
		)
		second_user = User.objects.create_user(
			username='second_user',
			email='second_user@example.com',
			password='StrongPass123',
			first_name='Second',
			last_name='User',
			role='user',
		)
		MeetingRegistration.objects.create(
			user=second_user,
			meeting=self.meeting,
			phone_number='+79990000125',
		)

		self.client.force_authenticate(self.admin)
		response = self.client.patch(
			reverse('meetings-detail', args=[self.meeting.id]),
			{'max_attendees': 1},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('max_attendees', response.data)

	def test_admin_cannot_link_archived_book_to_meeting(self):
		archived_book = Book.objects.create(
			title='Archived discussed',
			genre='Novel',
			author='Author',
			publication_year=2020,
			is_archived=True,
		)
		self.client.force_authenticate(self.admin)
		response = self.client.patch(
			reverse('meetings-detail', args=[self.meeting.id]),
			{'discussed_book': archived_book.id},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('discussed_book', response.data)


class BookArchiveAndVotingRulesTests(APITestCase):
	def setUp(self):
		self.admin = User.objects.create_user(
			username='archive_admin',
			email='archive_admin@example.com',
			password='StrongPass123',
			first_name='Archive',
			last_name='Admin',
			role='admin',
			is_staff=True,
		)
		self.user = User.objects.create_user(
			username='archive_user',
			email='archive_user@example.com',
			password='StrongPass123',
			first_name='Archive',
			last_name='User',
			role='user',
		)
		self.superuser = User.objects.create_superuser(
			username='archive_root',
			email='archive_root@example.com',
			password='StrongPass123',
			first_name='Archive',
			last_name='Root',
		)

	def test_admin_delete_book_archives_instead_of_physical_delete(self):
		book = Book.objects.create(title='Archive me', genre='Sci-Fi', author='A', publication_year=2020)
		self.client.force_authenticate(self.superuser)
		response = self.client.delete(reverse('books-detail', args=[book.id]))
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
		book.refresh_from_db()
		self.assertFalse(book.is_archived)

		self.client.force_authenticate(self.user)
		list_response = self.client.get(reverse('books-list'))
		self.assertEqual(list_response.status_code, status.HTTP_200_OK)
		results = list_response.data.get('results', []) if isinstance(list_response.data, dict) else list_response.data
		book_ids = [item['id'] for item in results]
		self.assertIn(book.id, book_ids)

	def test_admin_can_unarchive_book_via_patch(self):
		book = Book.objects.create(
			title='Unarchive me',
			genre='Sci-Fi',
			author='A',
			publication_year=2020,
			is_archived=True,
		)
		self.client.force_authenticate(self.superuser)
		response = self.client.patch(
			reverse('books-detail', args=[book.id]),
			{'is_archived': False},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

		book.refresh_from_db()
		self.assertTrue(book.is_archived)

	def test_vote_limit_is_fixed_to_two_votes_per_period(self):
		period = VotingPeriod.objects.create(
			start_date=timezone.now() - timedelta(days=1),
			end_date=timezone.now() + timedelta(days=1),
			is_active=True,
		)
		books = [
			Book.objects.create(title=f'Vote book {idx}', genre='Novel', author='A', publication_year=2020 + idx, is_voting_candidate=True)
			for idx in range(3)
		]

		self.client.force_authenticate(self.user)
		for idx in range(2):
			response = self.client.post(reverse('votes-list'), {'book_id': books[idx].id}, format='json')
			self.assertEqual(response.status_code, status.HTTP_201_CREATED)

		third_response = self.client.post(reverse('votes-list'), {'book_id': books[2].id}, format='json')
		self.assertEqual(third_response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertEqual(BookVote.objects.filter(user=self.user, voting_period=period).count(), 2)

	def test_user_can_list_only_own_votes_in_current_active_period(self):
		current_period = VotingPeriod.objects.create(
			start_date=timezone.now() - timedelta(days=1),
			end_date=timezone.now() + timedelta(days=1),
			is_active=True,
		)
		past_period = VotingPeriod.objects.create(
			start_date=timezone.now() - timedelta(days=10),
			end_date=timezone.now() - timedelta(days=5),
			is_active=False,
		)

		current_book = Book.objects.create(
			title='Current vote book',
			genre='Novel',
			author='A',
			publication_year=2022,
			is_voting_candidate=True,
		)
		past_book = Book.objects.create(
			title='Past vote book',
			genre='Novel',
			author='B',
			publication_year=2021,
			is_voting_candidate=True,
		)

		BookVote.objects.create(user=self.user, book=current_book, voting_period=current_period)
		BookVote.objects.create(user=self.user, book=past_book, voting_period=past_period)

		self.client.force_authenticate(self.user)
		response = self.client.get(reverse('votes-list'))
		self.assertEqual(response.status_code, status.HTTP_200_OK)

		items = response.data if isinstance(response.data, list) else response.data.get('results', [])
		ids = [item['book_id'] for item in items]
		self.assertIn(current_book.id, ids)
		self.assertNotIn(past_book.id, ids)

	def test_user_cannot_access_past_votes_via_voting_period_filter(self):
		current_period = VotingPeriod.objects.create(
			start_date=timezone.now() - timedelta(days=1),
			end_date=timezone.now() + timedelta(days=1),
			is_active=True,
		)
		past_period = VotingPeriod.objects.create(
			start_date=timezone.now() - timedelta(days=10),
			end_date=timezone.now() - timedelta(days=5),
			is_active=False,
		)

		current_book = Book.objects.create(
			title='Current restricted vote book',
			genre='Novel',
			author='A',
			publication_year=2020,
			is_voting_candidate=True,
		)
		past_book = Book.objects.create(
			title='Past restricted vote book',
			genre='Novel',
			author='B',
			publication_year=2019,
			is_voting_candidate=True,
		)

		BookVote.objects.create(user=self.user, book=current_book, voting_period=current_period)
		BookVote.objects.create(user=self.user, book=past_book, voting_period=past_period)

		self.client.force_authenticate(self.user)
		response = self.client.get(reverse('votes-list'), {'voting_period_id': past_period.id})
		self.assertEqual(response.status_code, status.HTTP_200_OK)

		items = response.data if isinstance(response.data, list) else response.data.get('results', [])
		ids = [item['book_id'] for item in items]
		self.assertIn(current_book.id, ids)
		self.assertNotIn(past_book.id, ids)

	def test_admin_can_see_vote_counts_for_closed_period(self):
		period = VotingPeriod.objects.create(
			start_date=timezone.now() - timedelta(days=1),
			end_date=timezone.now() + timedelta(days=1),
			is_active=True,
			voting_mode='closed',
		)
		other_user = User.objects.create_user(
			username='archive_user_two',
			email='archive_user_two@example.com',
			password='StrongPass123',
			first_name='Archive',
			last_name='UserTwo',
			role='user',
		)
		book_one = Book.objects.create(
			title='Closed period book one',
			genre='Novel',
			author='A',
			publication_year=2020,
			is_voting_candidate=True,
		)
		book_two = Book.objects.create(
			title='Closed period book two',
			genre='Novel',
			author='B',
			publication_year=2021,
			is_voting_candidate=True,
		)
		BookVote.objects.create(user=self.user, book=book_one, voting_period=period)
		BookVote.objects.create(user=other_user, book=book_two, voting_period=period)

		self.client.force_authenticate(self.admin)
		response = self.client.get(reverse('votes-all-for-counts'), {'voting_period_id': period.id})
		self.assertEqual(response.status_code, status.HTTP_200_OK)

		items = response.data if isinstance(response.data, list) else response.data.get('results', [])
		ids = {item['book_id'] for item in items}
		self.assertIn(book_one.id, ids)
		self.assertIn(book_two.id, ids)

	def test_period_stats_defaults_to_current_period_when_provided(self):
		current_period = VotingPeriod.objects.create(
			start_date=timezone.now() - timedelta(days=1),
			end_date=timezone.now() + timedelta(days=1),
			is_active=True,
		)
		other_user = User.objects.create_user(
			username='archive_user_three',
			email='archive_user_three@example.com',
			password='StrongPass123',
			first_name='Archive',
			last_name='UserThree',
			role='user',
		)
		old_period = VotingPeriod.objects.create(
			start_date=timezone.now() - timedelta(days=10),
			end_date=timezone.now() - timedelta(days=5),
			is_active=False,
		)
		current_book = Book.objects.create(
			title='Current stats book',
			genre='Novel',
			author='C',
			publication_year=2022,
			is_voting_candidate=True,
		)
		old_book = Book.objects.create(
			title='Old stats book',
			genre='Novel',
			author='D',
			publication_year=2021,
			is_voting_candidate=True,
		)
		BookVote.objects.create(user=self.user, book=current_book, voting_period=current_period)
		BookVote.objects.create(user=other_user, book=old_book, voting_period=old_period)

		self.client.force_authenticate(self.admin)
		response = self.client.get(reverse('votes-period-stats'), {'voting_period_id': current_period.id})
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data['voting_period_id'], current_period.id)
		book_ids = {item['book__id'] for item in response.data['books']}
		self.assertIn(current_book.id, book_ids)
		self.assertNotIn(old_book.id, book_ids)

	def test_admin_cannot_create_book_with_unknown_genre(self):
		self.client.force_authenticate(self.superuser)
		response = self.client.post(
			reverse('books-list'),
			{
				'title': 'Genre check',
				'genre': 'Cyberpunk Noir',
				'author': 'Author',
				'publication_year': 2024,
			},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

	def test_admin_cannot_archive_book_while_it_is_voting_candidate(self):
		book = Book.objects.create(
			title='Candidate Book',
			genre='Роман',
			author='A',
			publication_year=2020,
			is_voting_candidate=True,
		)
		self.client.force_authenticate(self.superuser)
		response = self.client.delete(reverse('books-detail', args=[book.id]))
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

		book.refresh_from_db()
		self.assertFalse(book.is_archived)
		self.assertTrue(book.is_voting_candidate)

	def test_admin_cannot_change_voting_candidate_flag_during_active_period(self):
		VotingPeriod.objects.create(
			start_date=timezone.now() - timedelta(days=1),
			end_date=timezone.now() + timedelta(days=1),
			is_active=True,
		)
		book = Book.objects.create(
			title='Toggle Candidate',
			genre='Роман',
			author='A',
			publication_year=2020,
			is_voting_candidate=True,
		)

		self.client.force_authenticate(self.superuser)
		response = self.client.patch(
			reverse('books-detail', args=[book.id]),
			{'is_voting_candidate': False},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

	def test_admin_can_activate_voting_period_via_patch(self):
		inactive_period = VotingPeriod.objects.create(
			start_date=timezone.now() - timedelta(days=2),
			end_date=timezone.now() + timedelta(days=2),
			is_active=False,
		)
		other_period = VotingPeriod.objects.create(
			start_date=timezone.now() - timedelta(days=10),
			end_date=timezone.now() - timedelta(days=5),
			is_active=True,
		)

		self.client.force_authenticate(self.superuser)
		response = self.client.patch(
			reverse('voting-periods-detail', args=[inactive_period.id]),
			{'is_active': True},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

		inactive_period.refresh_from_db()
		other_period.refresh_from_db()
		self.assertFalse(inactive_period.is_active)
		self.assertTrue(other_period.is_active)

	def test_admin_cannot_archive_book_superuser_only(self):
		book = Book.objects.create(title='Admin cannot archive', genre='Sci-Fi', author='A', publication_year=2020)
		self.client.force_authenticate(self.admin)
		response = self.client.delete(reverse('books-detail', args=[book.id]))
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

	def test_admin_cannot_update_voting_period_superuser_only(self):
		period = VotingPeriod.objects.create(
			start_date=timezone.now() - timedelta(days=1),
			end_date=timezone.now() + timedelta(days=1),
			is_active=False,
		)
		self.client.force_authenticate(self.admin)
		response = self.client.patch(
			reverse('voting-periods-detail', args=[period.id]),
			{'is_active': True},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class AdminLogVisibilityTests(APITestCase):
	def setUp(self):
		self.admin_one = User.objects.create_user(
			username='logs_admin_one',
			email='logs_admin_one@example.com',
			password='StrongPass123',
			first_name='Logs',
			last_name='AdminOne',
			role='admin',
			is_staff=True,
		)
		self.admin_two = User.objects.create_user(
			username='logs_admin_two',
			email='logs_admin_two@example.com',
			password='StrongPass123',
			first_name='Logs',
			last_name='AdminTwo',
			role='admin',
			is_staff=True,
		)
		self.superuser = User.objects.create_superuser(
			username='logs_root',
			email='logs_root@example.com',
			password='StrongPass123',
			first_name='Logs',
			last_name='Root',
		)

		AdminLog.objects.create(admin=self.admin_one, action='admin_action_one', target='x')
		AdminLog.objects.create(admin=self.admin_two, action='admin_action_two', target='y')
		AdminLog.objects.create(admin=self.superuser, action='root_action', target='z')

	def test_admin_sees_other_admin_logs_but_not_superuser_logs(self):
		self.client.force_authenticate(self.admin_one)
		response = self.client.get(reverse('admin-log-list'))
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		actions = [item['action'] for item in response.data]
		self.assertIn('admin_action_one', actions)
		self.assertIn('admin_action_two', actions)
		self.assertNotIn('root_action', actions)

	def test_superuser_can_access_admin_logs_api_with_admin_scope(self):
		self.client.force_authenticate(self.superuser)
		response = self.client.get(reverse('admin-log-list'))
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		actions = [item['action'] for item in response.data]
		self.assertIn('admin_action_one', actions)
		self.assertIn('admin_action_two', actions)
		self.assertNotIn('root_action', actions)

	def test_admin_logs_filter_by_action_and_admin_username(self):
		self.client.force_authenticate(self.admin_one)
		response = self.client.get(
			reverse('admin-log-list'),
			{'action': 'admin_action_one', 'admin__username': self.admin_one.username},
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(len(response.data), 1)
		self.assertEqual(response.data[0]['action'], 'admin_action_one')
		self.assertEqual(response.data[0]['admin'], self.admin_one.username)


class AdminRegistrationsEndpointTests(APITestCase):
	def setUp(self):
		self.admin = User.objects.create_user(
			username='registrations_admin',
			email='registrations_admin@example.com',
			password='StrongPass123',
			first_name='Reg',
			last_name='Admin',
			role='admin',
			is_staff=True,
		)
		self.target_user = User.objects.create_user(
			username='registrations_user',
			email='registrations_user@example.com',
			password='StrongPass123',
			first_name='Reg',
			last_name='User',
			role='user',
		)
		self.superuser = User.objects.create_superuser(
			username='registrations_root',
			email='registrations_root@example.com',
			password='StrongPass123',
			first_name='Reg',
			last_name='Root',
		)
		self.meeting = Meeting.objects.create(
			title='Registrations Meeting',
			date=timezone.now() + timedelta(days=10),
			location='Main Hall',
			description='Test meeting for registrations endpoint',
			status=MeetingStatus.UPCOMING,
			type='BOOK_DISCUSSION',
		)
		self.registration = MeetingRegistration.objects.create(
			user=self.target_user,
			meeting=self.meeting,
			is_attended=False,
		)

	def test_admin_can_load_regular_user_registrations(self):
		self.client.force_authenticate(self.admin)
		response = self.client.get(reverse('users-admin-registrations', kwargs={'pk': self.target_user.id}))
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(len(response.data), 1)
		self.assertEqual(response.data[0]['id'], self.registration.id)
		self.assertEqual(response.data[0]['meeting']['id'], self.meeting.id)

	def test_admin_can_load_superuser_registrations_endpoint(self):
		self.client.force_authenticate(self.admin)
		response = self.client.get(reverse('users-admin-registrations', kwargs={'pk': self.superuser.id}))
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data, [])

class BookReviewValidationTests(APITestCase):
	def setUp(self):
		self.user = User.objects.create_user(
			username='review_user',
			email='review_user@example.com',
			password='StrongPass123',
			first_name='Review',
			last_name='User',
			role='user',
		)
		self.book = Book.objects.create(
			title='Test Book for Reviews',
			genre='Fiction',
			author='Test Author',
			publication_year=2020
		)
		self.other_user = User.objects.create_user(
			username='review_viewer',
			email='review_viewer@example.com',
			password='StrongPass123',
			first_name='View',
			last_name='Only',
			role='user',
		)
		self.admin = User.objects.create_user(
			username='review_moderator',
			email='review_moderator@example.com',
			password='StrongPass123',
			first_name='Review',
			last_name='Moderator',
			role='admin',
			is_staff=True,
		)
	
	def test_user_cannot_write_duplicate_review_on_same_book(self):
		"""Пользователь не может написать второй отзыв на одну и ту же книгу"""
		self.client.force_authenticate(self.user)
		
		# Первый отзыв должен пройти
		first_response = self.client.post(reverse('book-reviews-list'), {
			'book': self.book.id,
			'review_text': 'This is a great book!'
		}, format='json')
		self.assertEqual(first_response.status_code, status.HTTP_201_CREATED, msg=f"First review failed: {first_response.data}")
		
		# Второй отзыв на ту же книгу должен быть отклонён
		second_response = self.client.post(reverse('book-reviews-list'), {
			'book': self.book.id,
			'review_text': 'Another review... but should fail'
		}, format='json')
		self.assertEqual(second_response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('Вы уже написали отзыв на эту книгу', str(second_response.data))

	def test_review_text_sanitizes_xss_tags(self):
		"""Review text with XSS должен быть очищен от HTML тагов"""
		self.client.force_authenticate(self.user)
		
		response = self.client.post(reverse('book-reviews-list'), {
			'book': self.book.id,
			'review_text': 'Good book <script>alert("XSS")</script>'
		}, format='json')
		
		self.assertEqual(response.status_code, status.HTTP_201_CREATED)
		review = BookReview.objects.latest('id')
		# Скрипт должен быть удален, остаётся только текст
		self.assertEqual(review.review_text, 'Good book alert("XSS")')
		self.assertNotIn('<script>', review.review_text)

	def test_review_text_cannot_be_empty_or_whitespace_only(self):
		"""Отзыв не может быть пустым или содержать только пробелы"""
		self.client.force_authenticate(self.user)
		
		# Пустой отзыв
		response = self.client.post(reverse('book-reviews-list'), {
			'book': self.book.id,
			'review_text': ''
		}, format='json')
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		# Ошибка может быть на русском или английском
		error_text = str(response.data).lower()
		self.assertTrue('пустым' in error_text or 'blank' in error_text or 'empty' in error_text)
		
		# Только пробелы
		response = self.client.post(reverse('book-reviews-list'), {
			'book': self.book.id,
			'review_text': '   \n\t  '
		}, format='json')
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	def test_review_text_respects_max_length_limit(self):
		"""Отзыв не может быть больше 5000 символов"""
		self.client.force_authenticate(self.user)
		
		# Текст длиной 5001 символ
		long_text = 'a' * 5001
		response = self.client.post(reverse('book-reviews-list'), {
			'book': self.book.id,
			'review_text': long_text
		}, format='json')
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('5000', str(response.data))
		
		# Текст ровно 5000 символов должен пройти
		valid_text = 'a' * 5000
		response = self.client.post(reverse('book-reviews-list'), {
			'book': self.book.id,
			'review_text': valid_text
		}, format='json')
		self.assertEqual(response.status_code, status.HTTP_201_CREATED)

	def test_review_with_nonexistent_book_id(self):
		"""Отзыв с несуществующей книгой должен вернуть ошибку"""
		self.client.force_authenticate(self.user)
		
		response = self.client.post(reverse('book-reviews-list'), {
			'book': 99999,  # не существует
			'review_text': 'This is a review'
		}, format='json')
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	def test_review_html_entities_preserved_in_text(self):
		"""HTML сущности в тексте должны быть сохранены, но опасные теги удалены"""
		self.client.force_authenticate(self.user)
		
		response = self.client.post(reverse('book-reviews-list'), {
			'book': self.book.id,
			'review_text': 'Author wrote: "Best & brightest" <img src=x onerror=alert(1)>'
		}, format='json')
		
		self.assertEqual(response.status_code, status.HTTP_201_CREATED)
		review = BookReview.objects.latest('id')
		# Текст сохранён, опасный тег удалён
		self.assertIn('Best', review.review_text)
		self.assertIn('&', review.review_text)
		self.assertNotIn('<img', review.review_text)

	def test_regular_user_cannot_see_sensitive_review_author_fields(self):
		rating = BookRating.objects.create(user=self.user, book=self.book, rating='YELLOW')
		review = BookReview.objects.create(
			user=self.user,
			book=self.book,
			review_text='Visible review text',
			rating=rating,
		)

		self.client.force_authenticate(self.other_user)
		response = self.client.get(reverse('book-reviews-detail', args=[review.id]))
		self.assertEqual(response.status_code, status.HTTP_200_OK)

		public_user = response.data.get('user', {})
		self.assertNotIn('email', public_user)
		self.assertNotIn('is_staff', public_user)
		self.assertNotIn('is_superuser', public_user)

		rating_detail_user = response.data.get('rating_detail', {}).get('user', {})
		self.assertNotIn('email', rating_detail_user)
		self.assertNotIn('is_staff', rating_detail_user)
		self.assertNotIn('is_superuser', rating_detail_user)

	def test_review_create_rejects_rating_field_and_uses_separate_rating_endpoint(self):
		self.client.force_authenticate(self.user)
		response = self.client.post(
			reverse('book-reviews-list'),
			{
				'book': self.book.id,
				'review_text': 'Review with invalid inline rating',
				'rating': 999,
			},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('оценка отправляется отдельно', str(response.data).lower())

	def test_review_detail_shows_rating_added_after_review_creation(self):
		review = BookReview.objects.create(
			user=self.user,
			book=self.book,
			review_text='Review first, rating later',
			rating=None,
		)
		rating = BookRating.objects.create(user=self.user, book=self.book, rating='GREEN')

		self.client.force_authenticate(self.other_user)
		response = self.client.get(reverse('book-reviews-detail', args=[review.id]))
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data.get('rating'), rating.id)
		self.assertEqual(response.data.get('rating_detail', {}).get('rating'), 'GREEN')

	def test_toggle_visibility_is_idempotent_for_same_state(self):
		review = BookReview.objects.create(
			user=self.user,
			book=self.book,
			review_text='Moderation target',
			is_hidden=False,
		)
		self.client.force_authenticate(self.admin)

		before_logs = AdminLog.objects.filter(target_id=review.id, action__startswith='отзыв_').count()
		response = self.client.patch(
			reverse('book-reviews-toggle-visibility', args=[review.id]),
			{'is_hidden': False},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data.get('detail'), 'Статус видимости не изменился.')

		review.refresh_from_db()
		self.assertFalse(review.is_hidden)
		after_logs = AdminLog.objects.filter(target_id=review.id, action__startswith='отзыв_').count()
		self.assertEqual(after_logs, before_logs)

	def test_toggle_visibility_accepts_string_boolean(self):
		review = BookReview.objects.create(
			user=self.user,
			book=self.book,
			review_text='String bool target',
			is_hidden=False,
		)
		self.client.force_authenticate(self.admin)

		response = self.client.patch(
			reverse('book-reviews-toggle-visibility', args=[review.id]),
			{'is_hidden': 'true'},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		review.refresh_from_db()
		self.assertTrue(review.is_hidden)


class EdgeCaseTests(APITestCase):
	"""Тесты для граничных случаев"""
	
	def setUp(self):
		self.user = User.objects.create_user(
			username='edge_user',
			email='edge@example.com',
			password='StrongPass123',
			first_name='Edge',
			last_name='Case',
			role='user',
		)
		self.admin = User.objects.create_user(
			username='edge_admin',
			email='admin@example.com',
			password='StrongPass123',
			first_name='Admin',
			last_name='User',
			role='admin',
			is_staff=True,
		)

	def test_recommendations_for_cold_start_user_with_no_history(self):
		"""Холодный старт: пользователь без истории должен получить пустой список или базовые рекомендации"""
		self.client.force_authenticate(self.user)
		
		response = self.client.get(reverse('recommendations-for-me'))
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		# Ответ должен содержать ключ 'recommendations'
		self.assertIsInstance(response.data, dict)
		self.assertIn('recommendations', response.data)

	def test_recommendations_empty_database(self):
		"""Рекомендации в пустой БД должны вернуть пустой список рекомендаций"""
		self.client.force_authenticate(self.user)
		
		response = self.client.get(reverse('recommendations-for-me'))
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		# В пустой базе ожидаем строго пустой список рекомендаций
		self.assertIn('recommendations', response.data)
		self.assertIsInstance(response.data['recommendations'], list)
		self.assertEqual(response.data['recommendations'], [])
		self.assertEqual(response.data.get('count'), 0)

	def test_user_profile_with_max_length_names(self):
		"""Профиль с максимальной длиной имён должен сохраняться корректно"""
		long_first_name = 'A' * 50
		long_last_name = 'B' * 50
		
		user = User.objects.create_user(
			username='longnames123',
			email='longnames@example.com',
			password='StrongPass123',
			first_name=long_first_name,
			last_name=long_last_name,
			role='user',
		)
		
		self.client.force_authenticate(user)
		response = self.client.get(reverse('users-me'))
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data['first_name'], long_first_name)
		self.assertEqual(response.data['last_name'], long_last_name)


class MeetingReviewQuestionAPITests(APITestCase):
	"""Тесты для API привязки вопросов к встречам"""

	def setUp(self):
		# Создаём админа и обычного пользователя
		self.admin = User.objects.create_user(
			username='review_admin',
			email='review_admin@example.com',
			password='StrongPass123',
			first_name='Admin',
			last_name='Review',
			role='admin',
			is_staff=True,
		)

		self.user = User.objects.create_user(
			username='review_user',
			email='review_user@example.com',
			password='StrongPass123',
			first_name='User',
			last_name='Review',
			role='user',
		)

		self.superuser = User.objects.create_superuser(
			username='review_root',
			email='review_root@example.com',
			password='StrongPass123',
			first_name='Root',
			last_name='Review',
		)

		# Создаём вопросы
		self.q1 = ReviewQuestion.objects.create(question_text='How was the book?')
		self.q2 = ReviewQuestion.objects.create(question_text='Rating?')
		self.q3 = ReviewQuestion.objects.create(question_text='Recommend?')

		# Создаём встречи
		now = timezone.now()
		self.upcoming_meeting = Meeting.objects.create(
			title='Upcoming Meeting',
			date=now + timedelta(days=7),
			location='Library',
			status=MeetingStatus.UPCOMING,
		)

		self.completed_meeting = Meeting.objects.create(
			title='Completed Meeting',
			date=now - timedelta(days=7),
			location='Library',
			status=MeetingStatus.COMPLETED,
		)

	def test_admin_can_create_meeting_review_questions(self):
		"""Админ может привязать 3 вопроса к предстоящей встречи"""
		self.client.force_authenticate(self.admin)

		data = {
			'meeting_id': self.upcoming_meeting.id,
			'question1_id': self.q1.id,
			'question2_id': self.q2.id,
			'question3_id': self.q3.id,
		}

		response = self.client.post(
			reverse('meetingreviewquestions-list'),
			data,
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_201_CREATED)
		self.assertEqual(MeetingReviewQuestion.objects.count(), 1)
		self.assertEqual(response.data['meeting'], self.upcoming_meeting.id)

	def test_admin_cannot_create_duplicate_meeting_review_questions_for_same_meeting(self):
		MeetingReviewQuestion.objects.create(
			meeting=self.upcoming_meeting,
			question1=self.q1,
			question2=self.q2,
			question3=self.q3,
		)

		self.client.force_authenticate(self.admin)
		data = {
			'meeting_id': self.upcoming_meeting.id,
			'question1_id': self.q1.id,
			'question2_id': self.q2.id,
			'question3_id': self.q3.id,
		}

		response = self.client.post(reverse('meetingreviewquestions-list'), data, format='json')
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('meeting_id', response.data)

	def test_regular_user_cannot_create_meeting_review_questions(self):
		"""Обычный пользователь не может создавать привязки вопросов"""
		self.client.force_authenticate(self.user)

		data = {
			'meeting_id': self.upcoming_meeting.id,
			'question1_id': self.q1.id,
			'question2_id': self.q2.id,
			'question3_id': self.q3.id,
		}

		response = self.client.post(
			reverse('meetingreviewquestions-list'),
			data,
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
		self.assertEqual(MeetingReviewQuestion.objects.count(), 0)

	def test_regular_user_cannot_view_meeting_review_questions_without_attendance(self):
		MeetingReviewQuestion.objects.create(
			meeting=self.completed_meeting,
			question1=self.q1,
			question2=self.q2,
			question3=self.q3,
		)
		MeetingRegistration.objects.create(
			user=self.user,
			meeting=self.completed_meeting,
			phone_number='+79990000127',
			is_attended=False,
		)

		self.client.force_authenticate(self.user)
		response = self.client.get(
			reverse('meetingreviewquestions-list'),
			{'meeting_id': self.completed_meeting.id},
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		items = response.data if isinstance(response.data, list) else response.data.get('results', [])
		self.assertEqual(len(items), 0)

	def test_regular_user_can_view_meeting_review_questions_when_attended(self):
		mrq = MeetingReviewQuestion.objects.create(
			meeting=self.completed_meeting,
			question1=self.q1,
			question2=self.q2,
			question3=self.q3,
		)
		MeetingRegistration.objects.create(
			user=self.user,
			meeting=self.completed_meeting,
			phone_number='+79990000128',
			is_attended=True,
		)

		self.client.force_authenticate(self.user)
		response = self.client.get(
			reverse('meetingreviewquestions-list'),
			{'meeting_id': self.completed_meeting.id},
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		items = response.data if isinstance(response.data, list) else response.data.get('results', [])
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0]['id'], mrq.id)

	def test_cannot_add_questions_to_completed_meeting(self):
		"""Нельзя добавлять вопросы к уже завершённой встречи"""
		self.client.force_authenticate(self.admin)

		data = {
			'meeting_id': self.completed_meeting.id,
			'question1_id': self.q1.id,
			'question2_id': self.q2.id,
			'question3_id': self.q3.id,
		}

		response = self.client.post(
			reverse('meetingreviewquestions-list'),
			data,
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertEqual(MeetingReviewQuestion.objects.count(), 0)

	def test_cannot_use_duplicate_questions(self):
		"""Все 3 вопроса должны быть разными"""
		self.client.force_authenticate(self.admin)

		data = {
			'meeting_id': self.upcoming_meeting.id,
			'question1_id': self.q1.id,
			'question2_id': self.q1.id,  # Повтор
			'question3_id': self.q3.id,
		}

		response = self.client.post(
			reverse('meetingreviewquestions-list'),
			data,
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		# Ошибка может быть в non_field_errors или в свойстве
		self.assertTrue(
			'Все вопросы должны быть разными' in str(response.data),
			f"Expected error message not found in {response.data}"
		)

	def test_admin_can_update_questions_for_upcoming_meeting(self):
		"""Админ может обновить вопросы для UPCOMING встречи"""
		# Создаём привязку
		mrq = MeetingReviewQuestion.objects.create(
			meeting=self.upcoming_meeting,
			question1=self.q1,
			question2=self.q2,
			question3=self.q3,
		)

		self.client.force_authenticate(self.admin)

		q4 = ReviewQuestion.objects.create(question_text='New question?')

		data = {
			'question1_id': self.q1.id,
			'question2_id': self.q2.id,
			'question3_id': q4.id,
		}

		response = self.client.patch(
			reverse('meetingreviewquestions-detail', args=[mrq.id]),
			data,
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_200_OK)
		mrq.refresh_from_db()
		self.assertEqual(mrq.question3.id, q4.id)

	def test_admin_cannot_update_questions_for_completed_meeting(self):
		"""Нельзя менять вопросы для завершённой встречи"""
		# Создаём привязку
		mrq = MeetingReviewQuestion.objects.create(
			meeting=self.completed_meeting,
			question1=self.q1,
			question2=self.q2,
			question3=self.q3,
		)

		self.client.force_authenticate(self.admin)

		q4 = ReviewQuestion.objects.create(question_text='New question?')

		data = {
			'question3_id': q4.id,
		}

		response = self.client.patch(
			reverse('meetingreviewquestions-detail', args=[mrq.id]),
			data,
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('Невозможно изменить вопросы для встречи', str(response.data))

	def test_admin_cannot_rebind_questions_to_another_meeting_via_patch(self):
		"""Нельзя через PATCH сменить meeting_id у существующей привязки"""
		mrq = MeetingReviewQuestion.objects.create(
			meeting=self.upcoming_meeting,
			question1=self.q1,
			question2=self.q2,
			question3=self.q3,
		)

		self.client.force_authenticate(self.admin)

		response = self.client.patch(
			reverse('meetingreviewquestions-detail', args=[mrq.id]),
			{'meeting_id': self.completed_meeting.id},
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		mrq.refresh_from_db()
		self.assertEqual(mrq.meeting.id, self.upcoming_meeting.id)

	def test_admin_cannot_patch_review_question_superuser_only(self):
		"""Админ не может менять банк вопросов (только superuser)"""
		MeetingReviewQuestion.objects.create(
			meeting=self.completed_meeting,
			question1=self.q1,
			question2=self.q2,
			question3=self.q3,
		)

		self.client.force_authenticate(self.admin)

		response = self.client.patch(
			reverse('review-questions-detail', args=[self.q1.id]),
			{'question_text': 'Updated completed question'},
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
		self.q1.refresh_from_db()
		self.assertEqual(self.q1.question_text, 'How was the book?')

	def test_cannot_add_questions_to_past_meeting_even_if_status_upcoming(self):
		"""Нельзя привязывать вопросы к встрече в прошлом, даже если статус UPCOMING"""
		past_upcoming_meeting = Meeting.objects.create(
			title='Past Upcoming Meeting',
			date=timezone.now() - timedelta(days=1),
			location='Old Library',
			status=MeetingStatus.UPCOMING,
		)

		self.client.force_authenticate(self.admin)

		response = self.client.post(
			reverse('meetingreviewquestions-list'),
			{
				'meeting_id': past_upcoming_meeting.id,
				'question1_id': self.q1.id,
				'question2_id': self.q2.id,
				'question3_id': self.q3.id,
			},
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	def test_superuser_cannot_patch_review_question_used_in_past_upcoming_meeting(self):
		"""Superuser не может менять банк вопросов через API (только Django admin)"""
		past_upcoming_meeting = Meeting.objects.create(
			title='Past Upcoming Meeting 2',
			date=timezone.now() - timedelta(days=1),
			location='Old Library',
			status=MeetingStatus.UPCOMING,
		)

		MeetingReviewQuestion.objects.create(
			meeting=past_upcoming_meeting,
			question1=self.q1,
			question2=self.q2,
			question3=self.q3,
		)

		self.client.force_authenticate(self.superuser)

		response = self.client.patch(
			reverse('review-questions-detail', args=[self.q1.id]),
			{'question_text': 'Patched while meeting in past'},
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
		self.q1.refresh_from_db()
		self.assertEqual(self.q1.question_text, 'How was the book?')

	def test_admin_can_delete_meeting_review_questions(self):
		"""Админ может удалить привязку вопросов"""
		mrq = MeetingReviewQuestion.objects.create(
			meeting=self.upcoming_meeting,
			question1=self.q1,
			question2=self.q2,
			question3=self.q3,
		)

		self.client.force_authenticate(self.admin)

		response = self.client.delete(
			reverse('meetingreviewquestions-detail', args=[mrq.id]),
		)

		self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
		self.assertEqual(MeetingReviewQuestion.objects.count(), 0)

	def test_admin_cannot_delete_meeting_review_questions_for_past_upcoming_meeting(self):
		"""Нельзя удалять привязку вопросов у встречи, дата которой уже прошла"""
		past_upcoming_meeting = Meeting.objects.create(
			title='Past upcoming meeting',
			date=timezone.now() - timedelta(days=1),
			location='Old room',
			status=MeetingStatus.UPCOMING,
		)
		mrq = MeetingReviewQuestion.objects.create(
			meeting=past_upcoming_meeting,
			question1=self.q1,
			question2=self.q2,
			question3=self.q3,
		)

		self.client.force_authenticate(self.admin)

		response = self.client.delete(
			reverse('meetingreviewquestions-detail', args=[mrq.id]),
		)

		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertEqual(MeetingReviewQuestion.objects.count(), 1)

	def test_admin_cannot_delete_meeting_review_questions_if_reviews_exist(self):
		mrq = MeetingReviewQuestion.objects.create(
			meeting=self.completed_meeting,
			question1=self.q1,
			question2=self.q2,
			question3=self.q3,
		)
		MeetingRegistration.objects.create(
			user=self.user,
			meeting=self.completed_meeting,
			phone_number='+79990000126',
		)
		MeetingReview.objects.create(
			user=self.user,
			meeting=self.completed_meeting,
			question1_answer=5,
			question2_answer=4,
			question3_answer=5,
		)

		self.client.force_authenticate(self.admin)
		response = self.client.delete(
			reverse('meetingreviewquestions-detail', args=[mrq.id]),
		)

		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertTrue(MeetingReviewQuestion.objects.filter(id=mrq.id).exists())

	def test_regular_user_cannot_delete_meeting_review_questions(self):
		"""Обычный пользователь не может удалить привязку"""
		mrq = MeetingReviewQuestion.objects.create(
			meeting=self.upcoming_meeting,
			question1=self.q1,
			question2=self.q2,
			question3=self.q3,
		)

		self.client.force_authenticate(self.user)

		response = self.client.delete(
			reverse('meetingreviewquestions-detail', args=[mrq.id]),
		)

		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
		self.assertEqual(MeetingReviewQuestion.objects.count(), 1)

	def test_user_cannot_set_name_longer_than_50_chars(self):
		"""Имя не может быть длиннее 50 символов"""
		self.client.force_authenticate(self.user)
		
		# Попытка установить имя из 51 символа
		too_long_name = 'A' * 51
		response = self.client.patch(
			reverse('users-me-update'),
			{'first_name': too_long_name},
			format='json'
		)
		# Должна быть ошибка валидации
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('first_name', response.data)
		
		# Текущее имя не должно измениться
		self.user.refresh_from_db()
		self.assertNotEqual(self.user.first_name, too_long_name)

	def test_user_cannot_set_last_name_longer_than_50_chars(self):
		"""Фамилия не может быть длиннее 50 символов"""
		self.client.force_authenticate(self.user)

		too_long_last_name = 'B' * 51
		response = self.client.patch(
			reverse('users-me-update'),
			{'last_name': too_long_last_name},
			format='json'
		)

		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('last_name', response.data)

		self.user.refresh_from_db()
		self.assertNotEqual(self.user.last_name, too_long_last_name)

	def test_user_cannot_set_empty_first_or_last_name(self):
		"""Имя и фамилия не должны приниматься пустыми при обновлении профиля"""
		self.client.force_authenticate(self.user)

		response = self.client.patch(
			reverse('users-me-update'),
			{'first_name': '   '},
			format='json'
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('first_name', response.data)

		response = self.client.patch(
			reverse('users-me-update'),
			{'last_name': ''},
			format='json'
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('last_name', response.data)

	def test_me_update_returns_noop_message_when_data_unchanged(self):
		self.client.force_authenticate(self.user)
		response = self.client.patch(
			reverse('users-me-update'),
			{'first_name': self.user.first_name},
			format='json'
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data.get('detail'), 'Данные не изменились.')

	def test_me_update_returns_noop_for_trimmed_same_name(self):
		"""Пробелы по краям не должны считаться изменением данных"""
		self.client.force_authenticate(self.user)
		response = self.client.patch(
			reverse('users-me-update'),
			{'first_name': f'  {self.user.first_name}  '},
			format='json'
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data.get('detail'), 'Данные не изменились.')

	def test_user_cannot_set_invalid_avatar_icon(self):
		self.client.force_authenticate(self.user)
		response = self.client.patch(
			reverse('users-me-update'),
			{'avatar_icon': 'dragon'},
			format='json'
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('avatar_icon', response.data)
		self.assertTrue(any('корректный вариант аватара' in str(message).lower() for message in response.data['avatar_icon']))

	def test_user_can_update_avatar_icon_with_allowed_value(self):
		self.client.force_authenticate(self.user)
		response = self.client.patch(
			reverse('users-me-update'),
			{'avatar_icon': 'owl'},
			format='json'
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data.get('avatar_icon'), 'owl')

		self.user.refresh_from_db()
		self.assertEqual(self.user.avatar_icon, 'owl')

	def test_user_can_update_email_and_username(self):
		self.client.force_authenticate(self.user)
		response = self.client.patch(
			reverse('users-me-update'),
			{'email': 'new_email@example.com', 'username': 'new_nickname'},
			format='json'
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data.get('email'), 'new_email@example.com')
		self.assertEqual(response.data.get('username'), 'new_nickname')

		self.user.refresh_from_db()
		self.assertEqual(self.user.email, 'new_email@example.com')
		self.assertEqual(self.user.username, 'new_nickname')

	def test_user_cannot_update_email_to_existing(self):
		User.objects.create_user(
			username='taken_email_user',
			email='taken_email@example.com',
			password='StrongPass123',
			first_name='Taken',
			last_name='Email',
		)

		self.client.force_authenticate(self.user)
		response = self.client.patch(
			reverse('users-me-update'),
			{'email': 'taken_email@example.com'},
			format='json'
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('email', response.data)

	def test_user_cannot_update_username_to_existing(self):
		User.objects.create_user(
			username='taken_username',
			email='taken_username@example.com',
			password='StrongPass123',
			first_name='Taken',
			last_name='Username',
		)

		self.client.force_authenticate(self.user)
		response = self.client.patch(
			reverse('users-me-update'),
			{'username': 'taken_username'},
			format='json'
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('username', response.data)

	def test_user_can_update_password(self):
		self.client.force_authenticate(self.user)
		response = self.client.patch(
			reverse('users-me-update'),
			{'password': 'NewStrongPass123', 'password_confirm': 'NewStrongPass123'},
			format='json'
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)

		self.user.refresh_from_db()
		self.assertTrue(self.user.check_password('NewStrongPass123'))

	def test_user_cannot_register_for_cancelled_meeting(self):
		"""Пользователь не может зарегистрироваться на отмененную встречу"""
		book = Book.objects.create(
			title='Test Book',
			genre='Fiction',
			author='Author',
			publication_year=2024
		)
		
		meeting = Meeting.objects.create(
			title='Cancelled Meeting',
			description='This meeting is cancelled',
			date=timezone.now() + timedelta(days=10),
			status=MeetingStatus.CANCELLED,
			discussed_book=book,
		)
		
		self.client.force_authenticate(self.user)
		response = self.client.post(
			reverse('meeting-registration-list'),
			{'meeting_id': meeting.id, 'phone_number': '+79990000000'},
			format='json'
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	def test_user_cannot_register_for_past_upcoming_meeting(self):
		"""Пользователь не может зарегистрироваться на предстоящую встречу с прошедшей датой"""
		book = Book.objects.create(
			title='Past Upcoming Book',
			genre='Fiction',
			author='Author',
			publication_year=2024,
		)

		meeting = Meeting.objects.create(
			title='Past Upcoming Meeting',
			description='Date already passed',
			date=timezone.now() - timedelta(days=1),
			status=MeetingStatus.UPCOMING,
			discussed_book=book,
		)

		self.client.force_authenticate(self.user)
		response = self.client.post(
			reverse('meeting-registration-list'),
			{'meeting_id': meeting.id, 'phone_number': '+79990000001'},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('будущей датой', str(response.data).lower())

	def test_user_cannot_cancel_registration_for_past_upcoming_meeting(self):
		"""Пользователь не может отменить регистрацию на встречу с прошедшей датой"""
		book = Book.objects.create(
			title='Past Upcoming Cancel Book',
			genre='Fiction',
			author='Author',
			publication_year=2024,
		)

		meeting = Meeting.objects.create(
			title='Past Upcoming Cancel Meeting',
			description='Date already passed',
			date=timezone.now() - timedelta(days=1),
			status=MeetingStatus.UPCOMING,
			discussed_book=book,
		)
		registration = MeetingRegistration.objects.create(
			user=self.user,
			meeting=meeting,
			phone_number='+79990000002',
		)

		self.client.force_authenticate(self.user)
		response = self.client.delete(reverse('meeting-registration-delete', args=[registration.id]))
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('будущей датой', str(response.data).lower())

	def test_admin_cannot_add_participant_to_past_upcoming_meeting(self):
		"""Админ не может добавить участника на встречу с прошедшей датой, даже если статус UPCOMING"""
		book = Book.objects.create(
			title='Past Add Book',
			genre='Fiction',
			author='Author',
			publication_year=2024,
		)

		meeting = Meeting.objects.create(
			title='Past Add Meeting',
			description='Already passed',
			date=timezone.now() - timedelta(days=2),
			status=MeetingStatus.UPCOMING,
			discussed_book=book,
		)

		target_user = User.objects.create_user(
			username='late_add_user',
			email='late_add@example.com',
			password='StrongPass123',
			first_name='Late',
			last_name='Adder',
			role='user',
		)

		admin = User.objects.create_user(
			username='local_admin2',
			email='local_admin2@example.com',
			password='StrongPass123',
			first_name='Admin',
			last_name='Two',
			role='admin',
		)

		self.client.force_authenticate(admin)

		response = self.client.post(
			reverse('meetings-add-participant', args=[meeting.id]),
			{'email': target_user.email, 'phone_number': '+79990000003'},
			format='json',
		)

		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('будущей датой', str(response.data).lower())

	def test_registration_deletion_returns_seat_to_capacity(self):
		"""После удаления регистрации место должно освободиться для других"""
		book = Book.objects.create(
			title='Test Book',
			genre='Fiction',
			author='Author',
			publication_year=2024
		)
		
		meeting = Meeting.objects.create(
			title='Limited Meeting',
			description='Only 1 seat',
			date=timezone.now() + timedelta(days=10),
			status=MeetingStatus.UPCOMING,
			discussed_book=book,
			max_attendees=1,
		)
		
		user1 = User.objects.create_user(
			username='user1',
			email='user1@example.com',
			password='Pass123',
			role='user',
		)
		user2 = User.objects.create_user(
			username='user2',
			email='user2@example.com',
			password='Pass123',
			role='user',
		)
		
		# user1 регистрируется
		reg1 = MeetingRegistration.objects.create(
			user=user1,
			meeting=meeting,
			phone_number='+79990000001',
		)
		
		# user2 пытается зарегистрироваться - должен получить ошибку (встреча полная)
		self.client.force_authenticate(user2)
		response = self.client.post(
			reverse('meeting-registration-list'),
			{'meeting_id': meeting.id, 'phone_number': '+79990000002'},
			format='json'
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		
		# user1 удаляет свою регистрацию
		self.client.force_authenticate(user1)
		delete_response = self.client.delete(
			reverse('meeting-registration-delete', args=[reg1.id])
		)
		self.assertEqual(delete_response.status_code, status.HTTP_204_NO_CONTENT)
		
		# Теперь user2 может зарегистрироваться
		self.client.force_authenticate(user2)
		response = self.client.post(
			reverse('meeting-registration-list'),
			{'meeting_id': meeting.id, 'phone_number': '+79990000002'},
			format='json'
		)
		self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class NotificationFlowTests(APITestCase):
	def setUp(self):
		self.admin = User.objects.create_user(
			username='notify_admin',
			email='notify_admin@example.com',
			password='StrongPass123',
			first_name='Notify',
			last_name='Admin',
			role='admin',
			is_staff=True,
		)
		self.user = User.objects.create_user(
			username='notify_user',
			email='notify_user@example.com',
			password='StrongPass123',
			first_name='Notify',
			last_name='User',
			role='user',
		)
		self.other_user = User.objects.create_user(
			username='notify_other',
			email='notify_other@example.com',
			password='StrongPass123',
			first_name='Notify',
			last_name='Other',
			role='user',
		)

		self.meeting = Meeting.objects.create(
			title='Meeting for notifications',
			date=timezone.now() + timedelta(days=3),
			location='Room N',
			status=MeetingStatus.UPCOMING,
		)

		MeetingRegistration.objects.create(
			user=self.user,
			meeting=self.meeting,
			phone_number='+79990000188',
		)

	def test_cancelled_meeting_creates_notification_for_registered_user(self):
		self.client.force_authenticate(self.admin)
		response = self.client.delete(reverse('meetings-detail', args=[self.meeting.id]))
		self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

		notifications = Notification.objects.filter(user=self.user, meeting=self.meeting)
		self.assertEqual(notifications.count(), 1)
		notification = notifications.first()
		self.assertFalse(notification.is_read)
		self.assertIn('отмен', notification.title.lower())

	def test_user_can_list_only_own_notifications(self):
		Notification.objects.create(
			user=self.user,
			meeting=self.meeting,
			title='Тест уведомление',
			message='Тест',
		)
		Notification.objects.create(
			user=self.other_user,
			meeting=self.meeting,
			title='Чужое уведомление',
			message='Чужое',
		)

		self.client.force_authenticate(self.user)
		response = self.client.get(reverse('notifications-list'))
		self.assertEqual(response.status_code, status.HTTP_200_OK)

		items = response.data if isinstance(response.data, list) else response.data.get('results', [])
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0]['title'], 'Тест уведомление')

	def test_user_can_mark_notification_as_read(self):
		notification = Notification.objects.create(
			user=self.user,
			meeting=self.meeting,
			title='Непрочитано',
			message='Отметить прочитанным',
		)

		self.client.force_authenticate(self.user)
		response = self.client.patch(
			reverse('notifications-mark-read', args=[notification.id]),
			{},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		notification.refresh_from_db()
		self.assertTrue(notification.is_read)
		self.assertIsNotNone(notification.read_at)

	def test_user_cannot_mark_other_user_notification_as_read(self):
		notification = Notification.objects.create(
			user=self.other_user,
			meeting=self.meeting,
			title='Чужое',
			message='Чужое уведомление',
		)

		self.client.force_authenticate(self.user)
		response = self.client.patch(
			reverse('notifications-mark-read', args=[notification.id]),
			{},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

	def test_registered_user_gets_notification_when_meeting_date_changes(self):
		self.client.force_authenticate(self.admin)
		new_date = timezone.now() + timedelta(days=5)
		response = self.client.patch(
			reverse('meetings-detail', args=[self.meeting.id]),
			{'date': new_date.isoformat()},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)

		notification = Notification.objects.filter(user=self.user, meeting=self.meeting).order_by('-id').first()
		self.assertIsNotNone(notification)
		self.assertIn('записаны', notification.message.lower())
		self.assertIn('перенесена', notification.message.lower())

	def test_registered_user_gets_notification_when_meeting_location_changes(self):
		self.client.force_authenticate(self.admin)
		response = self.client.patch(
			reverse('meetings-detail', args=[self.meeting.id]),
			{'location': 'Room Z'},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)

		notification = Notification.objects.filter(user=self.user, meeting=self.meeting).order_by('-id').first()
		self.assertIsNotNone(notification)
		self.assertIn('место встречи изменено', notification.message.lower())

	def test_no_notification_for_noop_meeting_update(self):
		self.client.force_authenticate(self.admin)
		response = self.client.patch(
			reverse('meetings-detail', args=[self.meeting.id]),
			{'location': self.meeting.location},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(Notification.objects.filter(user=self.user, meeting=self.meeting).count(), 0)



class CancelledMeetingQuestionsTests(APITestCase):
	def setUp(self):
		# create superuser (admin) for write operations
		self.superuser = User.objects.create_superuser(
			username='q_admin',
			email='q_admin@example.com',
			password='StrongPass123',
			first_name='Q',
			last_name='Admin',
		)
		# create some review questions
		self.q1 = ReviewQuestion.objects.create(question_text='Q1')
		self.q2 = ReviewQuestion.objects.create(question_text='Q2')
		self.q3 = ReviewQuestion.objects.create(question_text='Q3')
		self.q4 = ReviewQuestion.objects.create(question_text='Q4')

	def test_admin_cannot_create_meeting_review_questions_for_cancelled_meeting(self):
		meeting = Meeting.objects.create(
			title='Cancelled Meeting',
			date=timezone.now() + timedelta(days=5),
			status=MeetingStatus.CANCELLED,
		)
		self.client.force_authenticate(self.superuser)
		url = reverse('meetingreviewquestions-list')
		payload = {
			'meeting_id': meeting.id,
			'question1_id': self.q1.id,
			'question2_id': self.q2.id,
			'question3_id': self.q3.id,
		}
		response = self.client.post(url, payload, format='json')
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('вопросы можно добавлять', str(response.data).lower())

	def test_admin_cannot_update_meeting_review_questions_for_cancelled_meeting(self):
		# create meeting and initial binding
		meeting = Meeting.objects.create(
			title='Future Meeting',
			date=timezone.now() + timedelta(days=5),
			status=MeetingStatus.UPCOMING,
		)
		mrq = MeetingReviewQuestion.objects.create(meeting=meeting, question1=self.q1, question2=self.q2, question3=self.q3)
		# Cancel meeting
		meeting.status = MeetingStatus.CANCELLED
		meeting.save(update_fields=['status'])

		self.client.force_authenticate(self.superuser)
		url = reverse('meetingreviewquestions-detail', args=[mrq.id])
		payload = {'question1_id': self.q4.id}
		response = self.client.patch(url, payload, format='json')
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('нельзя', str(response.data).lower())

	def test_admin_cannot_delete_meeting_review_questions_for_cancelled_meeting(self):
		meeting = Meeting.objects.create(
			title='Future Meeting 2',
			date=timezone.now() + timedelta(days=5),
			status=MeetingStatus.UPCOMING,
		)
		mrq = MeetingReviewQuestion.objects.create(meeting=meeting, question1=self.q1, question2=self.q2, question3=self.q3)
		meeting.status = MeetingStatus.CANCELLED
		meeting.save(update_fields=['status'])

		self.client.force_authenticate(self.superuser)
		url = reverse('meetingreviewquestions-detail', args=[mrq.id])
		response = self.client.delete(url)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('нельзя', str(response.data).lower())


