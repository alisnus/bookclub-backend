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

    ROLE_CHOICES = [
        ('user', 'User'),
        ('admin', 'Admin'),
    ]

    role = models.CharField(#поле роли
        max_length=20,
        choices=ROLE_CHOICES,
        default='user',
        verbose_name='Role of user'
    )

    AVATAR_ICON_CHOICES = [
        ('default', 'Default'),
        ('book', 'Book'),
        ('star', 'Star'),
        ('owl', 'Owl'),
        ('cat', 'Cat'),
        ('coffee', 'Coffee'),
    ]

    avatar_icon = models.CharField(#поле аватарки
        max_length=50,
        choices=AVATAR_ICON_CHOICES,
        default='default',  # потом можно будет заменить на 'user1', 'book', 'star' и т.д, по дефолту стоит такая картинка
        verbose_name='Avatar'
    )
    #флажок для удаления пользователя( мягкого удаления )
    '''is_deleted = models.BooleanField(
        default=False,
        verbose_name='Soft-deleted (удалён)',
        help_text='Помечает пользователя как удалённого без потери данных'
    )'''
    #флажок для бана пользователя
    is_banned = models.BooleanField(
    default=False,
    verbose_name='Заблокирован (бан)',
    help_text='Если пользователь забанен, он не может авторизоваться'
    )   
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
    LECTURE_DISCUSSION = 'LECTURE_DISCUSSION', 'Lecture and Discussion'
    WORKSHOP = 'WORKSHOP', 'Workshop'
    MOVIE_SCREENING = 'MOVIE_SCREENING', 'Movie Screening'

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
    discussed_book = models.ForeignKey(# добавляем ссылку на книгу во встречу для рекомендательной системы
        'Book',  # строковая ссылка, т.к. Book определён ниже
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='meetings'
    )
    class Meta:
        ordering = ['-date']

    def clean(self):
        if self.status == MeetingStatus.UPCOMING and self.date < timezone.now():
            raise ValidationError("Дата предстоящей встречи не может быть в прошлом.")

        if self.max_attendees is not None:
            if self.max_attendees < 1:
                raise ValidationError("max_attendees должно быть не меньше 1.")
            if self.pk:
                current_attendees = self.registrations.count()
                if self.max_attendees < current_attendees:
                    raise ValidationError(
                        f"max_attendees не может быть меньше текущего числа участников ({current_attendees})."
                    )

        if self.discussed_book and self.discussed_book.is_archived:
            raise ValidationError("Нельзя привязать архивную книгу к встрече.")


    def __str__(self):
        return self.title

# Модель для записи на встречу
class MeetingRegistration(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='registrations')
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name='registrations')
    phone_number = models.CharField(max_length=20, null=False, blank=False)
    registered_at = models.DateTimeField(auto_now_add=True)
    is_attended = models.BooleanField(default=False, db_index=True)
    attended_at = models.DateTimeField(null=True, blank=True)
    attendance_marked_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='marked_attendance_records'
    )

    class Meta:
        unique_together = ('user', 'meeting')

    def __str__(self):
        return f"{self.user.email} - {self.meeting.title}"

# Модель для книг
class BookGenre(models.TextChoices):
    SCI_FI = 'Научная фантастика', 'Научная фантастика'
    FANTASY = 'Фэнтези', 'Фэнтези'
    HISTORY = 'История', 'История'
    MYSTERY = 'Детектив', 'Детектив'
    DRAMA = 'Драма', 'Драма'
    NOVEL = 'Роман', 'Роман'
    FICTION = 'Художественная литература', 'Художественная литература'
    ROMANCE = 'Романтика', 'Романтика'
    THRILLER = 'Триллер', 'Триллер'
    BIOGRAPHY = 'Биография', 'Биография'
    POETRY = 'Поэзия', 'Поэзия'
    NON_FICTION = 'Нон-фикшн', 'Нон-фикшн'


class Book(models.Model):
    title = models.CharField(max_length=200, null=False, blank=False)
    genre = models.CharField(max_length=50, choices=BookGenre.choices, null=False, blank=False)
    author = models.CharField(max_length=100, null=False, blank=False)
    publication_year = models.PositiveIntegerField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    is_discussed = models.BooleanField(default=False, null=False)
    cover = models.ImageField(upload_to='covers/', null=True, blank=True)
    is_voting_candidate = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)

    def __str__(self):
        return self.title

    def clean(self):
        if self.is_archived and self.is_voting_candidate:
            raise ValidationError("Архивная книга не может участвовать в голосовании.")

        if self.is_voting_candidate:
            count = Book.objects.filter(is_voting_candidate=True).exclude(id=self.id).count()
            if count >= 8:
                raise ValidationError("Можно выбрать не более 8 книг для голосования. Снимите флаг с другой книги.")
        
        active_period = VotingPeriod.objects.filter(is_active=True).first()
        if active_period and self.pk:
            old = Book.objects.get(pk=self.pk)
            if old.is_voting_candidate != self.is_voting_candidate:
                raise ValidationError(
                    "Нельзя менять статус кандидата на голосование во время активного периода голосования."
                )

    def save(self, *args, **kwargs):
        self.clean()  # Вызываем валидацию перед сохранением
        super().save(*args, **kwargs)


class VotingMode(models.TextChoices):
    OPEN = 'open', 'Открытое голосование'
    CLOSED = 'closed', 'Закрытое голосование'

class VotingPeriod(models.Model):
    start_date = models.DateTimeField(null=False, blank=False)
    end_date = models.DateTimeField(null=False, blank=False)
    is_active = models.BooleanField(default=False)
    voting_mode = models.CharField(
        max_length=20,
        choices=VotingMode.choices,
        default=VotingMode.CLOSED,
        verbose_name='Режим голосования'
    )

    def __str__(self):
        return f"Voting Period: {self.start_date} - {self.end_date} ({self.voting_mode})"

    def save(self, *args, **kwargs):
        if self.is_active:
            VotingPeriod.objects.filter(is_active=True).exclude(id=self.id).update(is_active=False)
        super().save(*args, **kwargs)

    def clean(self):
        # Ensure start < end
        from django.core.exceptions import ValidationError
        if self.start_date >= self.end_date:
            raise ValidationError('Дата начала должна быть раньше даты окончания периода.')

        # Prevent overlapping periods (enforced at model level so admin UI also respects it)
        overlap_qs = VotingPeriod.objects.filter(
            start_date__lt=self.end_date,
            end_date__gt=self.start_date,
        )
        if self.pk:
            overlap_qs = overlap_qs.exclude(pk=self.pk)
        if overlap_qs.exists():
            raise ValidationError('Период голосования пересекается с существующим.')

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

class BookReview(models.Model):
    #Отзыв пользователя о книге (без редактирования, с модерацией админом)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='book_reviews')
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name='reviews')
    review_text = models.TextField()
    rating = models.ForeignKey(BookRating, on_delete=models.SET_NULL, null=True, blank=True, related_name='book_reviews')
    is_hidden = models.BooleanField(default=False)  # модерация админом
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['user', 'book']  # один отзыв на книгу от пользователя
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Отзыв {self.user.username} на '{self.book.title}'"


class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    meeting = models.ForeignKey(
        Meeting,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='notifications'
    )
    title = models.CharField(max_length=200)
    message = models.TextField()
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.email} | {self.title}"
    
#Модель для записи всех важных действий администратора
class AdminLog(models.Model):
    admin = models.ForeignKey(#какой администратор совершил действие
        'User',
        on_delete=models.SET_NULL,#если админа удаляю, логи не теряются
        null=True,
        blank=True,#поле можем оставить пустым
        related_name='admin_logs',#ну как алиас, как можем обратиться, чтобы получить все логи админа
        verbose_name='Admin'
    )

    action = models.CharField(#например созданное собрание, удалённая книга итп
        max_length=100,
        verbose_name='Type of action'
    )

    target = models.CharField(#название объекта, над которым было действие
        max_length=200,
        blank=True,
        verbose_name='Object'
    )

    target_id = models.PositiveIntegerField(#числовой айди объекта
        null=True,
        blank=True,
        verbose_name='ID of object'
    )

    registration = models.ForeignKey(
        'MeetingRegistration',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='admin_logs',
        verbose_name='Related registration'
    )

    timestamp = models.DateTimeField(#дата и время когда произошло
        auto_now_add=True,#автоматически ставим текущее время
        verbose_name='Time'
    )

    details = models.TextField(
        blank=True,
        verbose_name='Details'
    )

    class Meta:
        verbose_name = 'Log of admin'
        verbose_name_plural = 'Logs of admins'
        ordering = ['-timestamp']  # новые сверху

    def __str__(self):
        return f"{self.timestamp} | {self.admin} | {self.action}"
