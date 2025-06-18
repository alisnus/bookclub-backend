#models.py
from django.db import models
from django.contrib.auth.models import AbstractUser, UserManager
from django.core.exceptions import ValidationError
from django.utils import timezone
#связуемая единица между sql и django
# Кастомная модель пользователя
class CustomUserManager(UserManager):
    def create_user(self, username, email, password=None, **extra_fields):
        if not username:
            raise ValueError('Имя пользователя должно быть указано')
        if not email:
            raise ValueError('Email должен быть указан')
        return super().create_user(username, email, password, **extra_fields)

    def create_superuser(self, username, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        if extra_fields.get('is_staff') is not True:
            raise ValueError('Суперпользователь должен иметь is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Суперпользователь должен иметь is_superuser=True.')
        return super().create_superuser(username, email, password, **extra_fields)

class User(AbstractUser):#unique=True гарантирует, что email уникален в таблице core_user.
    email = models.EmailField(unique=True, null=False, blank=False) #Поле email используется как уникальный идентификатор вместо username.
    # Время создания пользователя
    created_at = models.DateTimeField(auto_now_add=True)
    username = models.CharField(max_length=150, unique=True, null=False, blank=False)
    # Указывает, что email используется для логина вместо username.
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username','first_name', 'last_name']
    objects = CustomUserManager()

    # Метод __str__ возвращает читаемое представление объекта.
    def __str__(self):
        return self.email

# Перечисления для статусов и типов встреч
class MeetingStatus(models.TextChoices):
    UPCOMING = 'UPCOMING', 'Upcoming'
    COMPLETED = 'COMPLETED', 'Completed'
    CANCELLED = 'CANCELLED', 'Canceled'

class MeetingType(models.TextChoices):
    BOOK_DISCUSSION = 'BOOK_DISCUSSION', 'Book Discussion'
    LECTURE = 'LECTURE', 'Lecture'
    WORKSHOP = 'WORKSHOP', 'Workshop'

# Модель для встреч
class Meeting(models.Model):
    title = models.CharField(max_length=200, null=False, blank=False)
    date = models.DateTimeField(null=False, blank=False)
    location = models.CharField(max_length=200, null=False, blank=False)
    description = models.TextField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=MeetingStatus.choices,
        default=MeetingStatus.UPCOMING,
        null=False,
        blank=False
    )
    type = models.CharField(
        max_length=20,
        choices=MeetingType.choices,
        default=MeetingType.BOOK_DISCUSSION,
        null=False,
        blank=False
    )
    poster = models.ImageField(upload_to='posters/', null=True, blank=True)  # Изменено на ImageField
    max_attendees = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ['-date']

    def __str__(self):
        return self.title

# Модель для записи на встречу
class MeetingRegistration(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='registrations')
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name='registrations')
    phone_number = models.CharField(max_length=20, null=False, blank=False)
    registered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'meeting')

    def __str__(self):
        return f"{self.user.email} - {self.meeting.title}"

# Модель для книг
class Book(models.Model):
    title = models.CharField(max_length=200, null=False, blank=False)
    genre = models.CharField(max_length=50, null=False, blank=False)
    author = models.CharField(max_length=100, null=False, blank=False)
    publication_year = models.PositiveIntegerField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    is_discussed = models.BooleanField(default=False, null=False)
    cover = models.ImageField(upload_to='covers/', null=True, blank=True)
    is_voting_candidate = models.BooleanField(default=False)

    def __str__(self):
        return self.title

    def clean(self):
        if self.is_voting_candidate:
            count = Book.objects.filter(is_voting_candidate=True).exclude(id=self.id).count()
            if count >= 8:
                raise ValidationError("Можно выбрать не более 8 книг для голосования. Снимите флаг с другой книги.")

    def save(self, *args, **kwargs):
        self.clean()  # Вызываем валидацию перед сохранением
        super().save(*args, **kwargs)

class VotingPeriod(models.Model):
    start_date = models.DateTimeField(null=False, blank=False)
    end_date = models.DateTimeField(null=False, blank=False)
    is_active = models.BooleanField(default=False)

    def __str__(self):
        return f"Voting Period: {self.start_date} - {self.end_date}"

    def save(self, *args, **kwargs):
        if self.is_active:
            VotingPeriod.objects.filter(is_active=True).exclude(id=self.id).update(is_active=False)
        super().save(*args, **kwargs)

# Модель для голосования за книги
class BookVote(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='votes')
    book = models.ForeignKey('Book', on_delete=models.CASCADE, related_name='votes')
    voting_period = models.ForeignKey(VotingPeriod, on_delete=models.CASCADE, related_name='votes')
    voted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'core_bookvote'
        indexes = [
            models.Index(fields=['user', 'book', 'voting_period', 'voted_at'], name='vote_unique_idx')
        ]
        unique_together = ('user', 'book', 'voting_period')  # Один голос на книгу

    def __str__(self):
        return f"{self.user.email} voted for {self.book.title}"

# Модель для оценок книг
class BookRating(models.Model):
    RATING_CHOICES = [
        ('GREEN', 'Green'),
        ('YELLOW', 'Yellow'),
        ('RED', 'Red'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='book_ratings')
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name='book_ratings')
    rating = models.CharField(max_length=10, choices=RATING_CHOICES, null=False, blank=False)
    rated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'book')

    def __str__(self):
        return f"{self.user.email} rated {self.book.title}: {self.rating}"

# Модель для вопросов отзывов
class ReviewQuestion(models.Model):
    question_text = models.CharField(max_length=200, null=False, blank=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.question_text

# Модель для связи вопросов с встречей
class MeetingReviewQuestion(models.Model):
    meeting = models.OneToOneField(Meeting, on_delete=models.CASCADE, related_name='review_questions')
    question1 = models.ForeignKey(ReviewQuestion, on_delete=models.CASCADE, related_name='meeting_question1')
    question2 = models.ForeignKey(ReviewQuestion, on_delete=models.CASCADE, related_name='meeting_question2')
    question3 = models.ForeignKey(ReviewQuestion, on_delete=models.CASCADE, related_name='meeting_question3')

    def __str__(self):
        return f"Questions for {self.meeting.title}"

# Модель для отзывов на встречи
class MeetingReview(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='meeting_reviews')
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name='reviews')
    question1_answer = models.PositiveIntegerField(choices=[(i, str(i)) for i in range(1, 6)], null=True, blank=True)
    question2_answer = models.PositiveIntegerField(choices=[(i, str(i)) for i in range(1, 6)], null=True, blank=True)
    question3_answer = models.PositiveIntegerField(choices=[(i, str(i)) for i in range(1, 6)], null=True, blank=True)
    reviewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'meeting')

    def __str__(self):
        return f"{self.user.email} reviewed {self.meeting.title}"
