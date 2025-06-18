#serializers.py
import re
import logging
from rest_framework import serializers
from django.utils import timezone
from .models import User, Meeting, MeetingRegistration, Book, BookVote, BookRating, ReviewQuestion, MeetingReview, VotingPeriod, MeetingReviewQuestion
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework import serializers
from .models import BookRating, Book, User

# Настройка логирования
logger = logging.getLogger(__name__)

#Преобразование данных из python в json и обратно(ДРФ)
# Сериализатор для модели User
class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id','username', 'email', 'first_name', 'last_name', 'created_at','is_staff', 'is_superuser']
        read_only_fields = ['id', 'created_at','is_staff', 'is_superuser']
# Сериализатор для регистрации нового пользователя через POST /api/register/.
class UserRegistrationSerializer(serializers.ModelSerializer):
    # Поле пароля: только для записи, обязательно, отображается как пароль в формах.
    password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})
    passwordConfirm = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})
    username = serializers.CharField(required=True)

    class Meta:
        # Указывает модель User для создания нового пользователя.
        model = User
        # Поля для регистрации: email, пароль, имя и фамилия.
        fields = ['username','email', 'password', 'passwordConfirm', 'first_name', 'last_name']
        extra_kwargs = {
            'email': {'required': True},
            'username': {'required': True},
            'first_name': {'required': True},
            'last_name': {'required': True},
        }

    def validate(self, data):
        # Проверка совпадения паролей
        if data['password'] != data['passwordConfirm']:
            raise serializers.ValidationError({'passwordConfirm': 'Пароли не совпадают'})
        # Проверка длины пароля
        if len(data['password']) <= 8:
            raise serializers.ValidationError({'password': 'Пароль должен быть длиннее 8 символов'})
        return data

    # Метод create переопределяет создание пользователя.
    def create(self, validated_data):
        validated_data.pop('passwordConfirm')
        # Создаёт пользователя с хешированным паролем через create_user.
        # validated_data — проверенные данные из JSON-запроса.
        user = User.objects.create_user(
            username=validated_data['username'],
            email=validated_data['email'],
            password=validated_data['password'],
            first_name=validated_data['first_name'],
            last_name=validated_data['last_name']
        )
        return user # Возвращает созданного пользователя для ответа API.

# Сериализатор для кастомизации JWT-токена при авторизации (POST /api/token/).
# Кастомный JWT-токен
class MyTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        email = attrs.get('email')
        password = attrs.get('password')

        # Проверка существования email
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            raise serializers.ValidationError({'detail': 'Неверный email'})

        # Проверка пароля
        if not user.check_password(password):
            raise serializers.ValidationError({'detail': 'Неверный пароль'})

        # Стандартная валидация токена
        data = super().validate(attrs)

        # Добавляем дополнительные данные в ответ
        data['username'] = self.user.username
        data['email'] = self.user.email
        data['first_name'] = self.user.first_name
        data['last_name'] = self.user.last_name
        data['is_staff'] = self.user.is_staff

        # Кастомизация payload токена
        refresh = RefreshToken.for_user(self.user)
        refresh['username'] = self.user.username  # Добавляем username в refresh-токен
        refresh['is_staff'] = self.user.is_staff  # Добавляем is_staff в refresh-токен
        access = refresh.access_token
        access['username'] = self.user.username  # Добавляем username в access-токен
        access['is_staff'] = self.user.is_staff  # Добавляем is_staff в access-токен

        data['refresh'] = str(refresh)
        data['access'] = str(access)

        return data

# Сериализатор для модели Meeting, преобразует данные встреч в JSON.
class MeetingSerializer(serializers.ModelSerializer):
    current_attendees = serializers.SerializerMethodField()

    class Meta:
        model = Meeting
        fields = ['id', 'title', 'date', 'location', 'description', 'status', 'type', 'poster', 'max_attendees', 'current_attendees']
        read_only_fields = ['id', 'current_attendees']

    def validate_date(self, value):
        if value < timezone.now():
            raise serializers.ValidationError("Дата встречи не может быть в прошлом.")
        return value

    def get_current_attendees(self, obj):
        return obj.registrations.count()
# Сериализатор для модели MeetingRegistration, обрабатывает регистрацию на встречу.
class MeetingRegistrationSerializer(serializers.ModelSerializer):
    # Вложенный сериализатор для отображения данных пользователя в ответах (только чтение).
    user = UserSerializer(read_only=True)
    # Вложенный сериализатор для отображения данных встречи в ответах (только чтение).
    meeting = MeetingSerializer(read_only=True)
    # Поле для записи ID встречи, ссылается на модель Meeting (для POST-запросов).
    meeting_id = serializers.PrimaryKeyRelatedField(queryset=Meeting.objects.all(), source='meeting', write_only=True)
    phone_number = serializers.CharField(max_length=20)

    class Meta:
        model = MeetingRegistration
        fields = ['id', 'user', 'meeting', 'meeting_id', 'phone_number', 'registered_at']
        read_only_fields = ['id', 'user', 'meeting', 'registered_at']

    def validate_phone_number(self, value):
        if not re.match(r'^\+\d{10,15}$', value):
            raise serializers.ValidationError("Номер телефона должен начинаться с '+' и содержать 10-15 цифр.")
        return value

    def validate(self, data):
        meeting = data['meeting']
        if meeting.status != 'UPCOMING':
            raise serializers.ValidationError("Регистрация возможна только на предстоящие встречи.")
        if meeting.max_attendees:
            current_count = meeting.registrations.count()
            if current_count >= meeting.max_attendees:
                raise serializers.ValidationError("Максимальное количество участников достигнуто.")
        return data

    def create(self, validated_data):
        request = self.context.get('request')
        user = request.user if request else None
        if not user:
            raise serializers.ValidationError("Пользователь не авторизован.")
        meeting = validated_data.pop('meeting')
        phone_number = validated_data.pop('phone_number')
        return MeetingRegistration.objects.create(user=user, meeting=meeting, phone_number=phone_number)
    
# Сериализатор для модели Book, преобразует данные книг в JSON.
class BookSerializer(serializers.ModelSerializer):
    average_rating = serializers.SerializerMethodField()

    def get_average_rating(self, obj):
        ratings = obj.book_ratings.all()
        if not ratings:
            logger.debug(f"No ratings for book {obj.id}")
            return 0
        scores = {'GREEN': 5, 'YELLOW': 3, 'RED': 1}
        total = sum(scores[r.rating] for r in ratings)
        avg = total / len(ratings)
        rounded_avg = round(avg, 1)
        logger.debug(f"Book {obj.id}: ratings={len(ratings)}, total={total}, avg={avg}, rounded_avg={rounded_avg}")
        return rounded_avg

    class Meta:
        model = Book
        fields = ['id', 'title', 'genre', 'author', 'publication_year', 'is_discussed', 'cover', 'description', 'is_voting_candidate', 'average_rating']
        read_only_fields = ['id', 'average_rating']
# Сериализатор для модели BookVote, обрабатывает голосование за книги.
class BookVoteSerializer(serializers.ModelSerializer):
    book_id = serializers.PrimaryKeyRelatedField(queryset=Book.objects.all(), source='book')

    class Meta:
        model = BookVote
        fields = ['id', 'book_id', 'voted_at']
        read_only_fields = ['id', 'voted_at']
# Сериализатор для модели BookRating, обрабатывает оценку книг.
# core/serializers.py
class BookRatingSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)  # Для чтения, возвращает данные пользователя
    book = BookSerializer(read_only=True)  # Для чтения, возвращает данные книги
    book_id = serializers.PrimaryKeyRelatedField(queryset=Book.objects.all(), source='book', write_only=True)
    rating = serializers.ChoiceField(choices=BookRating.RATING_CHOICES)

    class Meta:
        model = BookRating
        fields = ['id', 'user', 'book', 'book_id', 'rating', 'rated_at']
        read_only_fields = ['id', 'user', 'book', 'rated_at']

    def validate(self, data):
        # Запрещаем отправку user_id в запросе
        if 'user_id' in self.initial_data:
            raise serializers.ValidationError({'user_id': 'Поле user_id не допускается'})
        return data
# Сериализатор для модели ReviewQuestion
class ReviewQuestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReviewQuestion
        fields = ['id', 'question_text', 'created_at']
        read_only_fields = ['id', 'created_at']

# Сериализатор для модели MeetingReviewQuestion
class MeetingReviewQuestionSerializer(serializers.ModelSerializer):
    question1 = ReviewQuestionSerializer(read_only=True)
    question2 = ReviewQuestionSerializer(read_only=True)
    question3 = ReviewQuestionSerializer(read_only=True)
    question1_id = serializers.PrimaryKeyRelatedField(queryset=ReviewQuestion.objects.all(), source='question1', write_only=True)
    question2_id = serializers.PrimaryKeyRelatedField(queryset=ReviewQuestion.objects.all(), source='question2', write_only=True)
    question3_id = serializers.PrimaryKeyRelatedField(queryset=ReviewQuestion.objects.all(), source='question3', write_only=True)

    class Meta:
        model = MeetingReviewQuestion
        fields = ['id', 'meeting', 'question1', 'question2', 'question3', 'question1_id', 'question2_id', 'question3_id']
        read_only_fields = ['id', 'meeting', 'question1', 'question2', 'question3']

    def validate(self, data):
        question_ids = [data['question1'].id, data['question2'].id, data['question3'].id]
        if len(set(question_ids)) != 3:
            raise serializers.ValidationError("Все вопросы должны быть разными.")
        return data

# Сериализатор для модели MeetingReview
class MeetingReviewSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    meeting = MeetingSerializer(read_only=True)
    question1 = ReviewQuestionSerializer(read_only=True, source='meeting.review_questions.question1')
    question2 = ReviewQuestionSerializer(read_only=True, source='meeting.review_questions.question2')
    question3 = ReviewQuestionSerializer(read_only=True, source='meeting.review_questions.question3')
    meeting_id = serializers.PrimaryKeyRelatedField(queryset=Meeting.objects.all(), source='meeting', write_only=True)
    question1_answer = serializers.ChoiceField(choices=[1, 2, 3, 4, 5], allow_null=True, required=False)
    question2_answer = serializers.ChoiceField(choices=[1, 2, 3, 4, 5], allow_null=True, required=False)
    question3_answer = serializers.ChoiceField(choices=[1, 2, 3, 4, 5], allow_null=True, required=False)

    class Meta:
        model = MeetingReview
        fields = [
            'id', 'user', 'meeting', 'question1', 'question1_answer',
            'question2', 'question2_answer', 'question3', 'question3_answer',
            'meeting_id', 'reviewed_at'
        ]
        read_only_fields = ['id', 'user', 'meeting', 'question1', 'question2', 'question3', 'reviewed_at']

    def validate(self, data):
        meeting = data.get('meeting')
        if not hasattr(meeting, 'review_questions'):
            raise serializers.ValidationError("Для этой встречи не заданы вопросы для отзыва.")
        return data

# Сериализатор для модели VotingPeriod
class VotingPeriodSerializer(serializers.ModelSerializer):
    class Meta:
        model = VotingPeriod
        fields = ['id', 'start_date', 'end_date', 'is_active']
        read_only_fields = ['id']