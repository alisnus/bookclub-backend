#serializers.py
import re
import logging
import bleach
from django.db import IntegrityError
from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from zoneinfo import ZoneInfo
from django.db import transaction
from .models import User, Meeting, MeetingRegistration, Book, BookVote, BookRating, ReviewQuestion, MeetingReview, \
    VotingPeriod, MeetingReviewQuestion, BookReview, AdminLog, MeetingStatus, Notification
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework import serializers
from .models import BookRating, Book, User

# Настройка логирования
logger = logging.getLogger(__name__)


PROFANITY_STEMS = [
    'бля', 'хер', 'хуй', 'пизд', 'еб', 'сук', 'мраз', 'урод', 'дебил', 'твар', 'шлюх', 'жоп', 'срат', 'пидор', 'гандон', 'говн', 'гавн'
]


def contains_profanity(text):
    normalized = (text or '').lower()
    return any(stem in normalized for stem in PROFANITY_STEMS)


NAME_MAX_LENGTH = 50


def validate_person_name(value, field_label):
    normalized = (value or '').strip()
    if not normalized:
        raise serializers.ValidationError(f'{field_label} не может быть пустым.')
    if len(normalized) > NAME_MAX_LENGTH:
        raise serializers.ValidationError(f'{field_label} не может быть длиннее {NAME_MAX_LENGTH} символов.')
    return normalized


def translate_password_errors(messages):
    translated = []
    for message in messages:
        lower = message.lower()
        if 'too common' in lower:
            translated.append('Введённый пароль слишком простой.')
            continue
        if 'entirely numeric' in lower:
            translated.append('Пароль не должен состоять только из цифр.')
            continue
        if 'too similar' in lower or 'similar' in lower:
            translated.append('Пароль слишком похож на имя пользователя, email или другие данные.')
            continue
        if 'too short' in lower:
            import re
            match = re.search(r'(?:at least) (\d+)', message)
            if match:
                translated.append(f'Пароль должен содержать минимум {match.group(1)} символов.')
            else:
                translated.append('Пароль слишком короткий.')
            continue
        translated.append(message)
    return translated

#Преобразование данных из python в json и обратно(ДРФ)
# Сериализатор для модели User
class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id','username', 'email', 'first_name', 'last_name', 'role', 'avatar_icon', 'is_active', 'created_at', 'is_staff', 'is_superuser', 'is_banned', 'date_joined']
        read_only_fields = ['id', 'created_at', 'date_joined', 'is_active', 'is_staff', 'is_superuser', 'is_banned']

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        request = self.context.get('request')
        #if request and not (request.user.role == 'admin' or request.user.is_staff):
            #ret.pop('is_deleted', None)  #скрываем is_deleted от обычных пользователей
            #ret.pop('is_banned', None)  #скрываем is_banned от обычных пользователей
        return ret
    
#Сериализаторы для личного кабинета(что именно ращрешаем менять пользователю и что отображаем в личном кабинете)
class UserMeSerializer(serializers.ModelSerializer):
    first_name = serializers.CharField(
        required=False,
        max_length=NAME_MAX_LENGTH,
        allow_blank=False,
        trim_whitespace=True,
        error_messages={
            'blank': 'Имя не может быть пустым.',
            'max_length': f'Имя не может быть длиннее {NAME_MAX_LENGTH} символов.',
        },
    )
    last_name = serializers.CharField(
        required=False,
        max_length=NAME_MAX_LENGTH,
        allow_blank=False,
        trim_whitespace=True,
        error_messages={
            'blank': 'Фамилия не может быть пустой.',
            'max_length': f'Фамилия не может быть длиннее {NAME_MAX_LENGTH} символов.',
        },
    )
    avatar_icon = serializers.ChoiceField(
        required=False,
        choices=User.AVATAR_ICON_CHOICES,
        error_messages={
            'invalid_choice': 'Выберите корректный вариант аватара.',
        },
    )
    email = serializers.EmailField(
        required=False,
        allow_blank=False,
        trim_whitespace=True,
        error_messages={
            'blank': 'Email не может быть пустым.',
            'invalid': 'Введите корректный email.',
        },
    )
    username = serializers.CharField(
        required=False,
        max_length=150,
        allow_blank=False,
        trim_whitespace=True,
        error_messages={
            'blank': 'Псевдоним не может быть пустым.',
            'max_length': 'Псевдоним не может быть длиннее 150 символов.',
        },
    )
    password = serializers.CharField(
        required=False,
        write_only=True,
        min_length=8,
        allow_blank=False,
        trim_whitespace=True,
        style={'input_type': 'password'},
        error_messages={
            'blank': 'Пароль не может быть пустым.',
            'min_length': 'Пароль должен содержать не менее 8 символов.',
        },
    )
    password_confirm = serializers.CharField(
        required=False,
        write_only=True,
        min_length=8,
        allow_blank=False,
        trim_whitespace=True,
        style={'input_type': 'password'},
        error_messages={
            'blank': 'Подтверждение пароля не может быть пустым.',
            'min_length': 'Подтверждение пароля должно содержать не менее 8 символов.',
        },
    )

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'role', 'avatar_icon', 'date_joined', 'password', 'password_confirm']
        read_only_fields = ['id', 'role', 'date_joined']

    def validate_first_name(self, value):
        return validate_person_name(value, 'Имя')

    def validate_last_name(self, value):
        return validate_person_name(value, 'Фамилия')
    
    def validate_email(self, value):
        if not value:
            return value
        # Проверяем, не занята ли эта почта другим пользователем
        request = self.context.get('request')
        user = request.user if request and hasattr(request, 'user') else None
        exclude_id = user.id if user else None
        if User.objects.filter(email=value).exclude(id=exclude_id).exists():
            raise serializers.ValidationError('Этот email уже зарегистрирован.')
        return value
    
    def validate_username(self, value):
        if not value:
            return value
        # Проверяем, не занят ли этот username другим пользователем
        request = self.context.get('request')
        user = request.user if request and hasattr(request, 'user') else None
        exclude_id = user.id if user else None
        if User.objects.filter(username=value).exclude(id=exclude_id).exists():
            raise serializers.ValidationError('Этот username уже занят.')
        return value
    
    def validate(self, data):
        password = data.get('password')
        password_confirm = data.get('password_confirm')
        
        # Если пароль передан, требуем подтверждение
        if password and not password_confirm:
            raise serializers.ValidationError({'password_confirm': 'Подтверждение пароля обязательно при изменении пароля.'})
        
        # Проверяем совпадение паролей
        if password and password_confirm and password != password_confirm:
            raise serializers.ValidationError({'password_confirm': 'Пароли не совпадают.'})

        if password:
            try:
                validate_password(password, self.instance)
            except DjangoValidationError as exc:
                raise serializers.ValidationError({'password': translate_password_errors(list(exc.messages))})
        
        return data
    
    def update(self, instance, validated_data):
        # Извлекаем пароль, если он передан
        password = validated_data.pop('password', None)
        password_confirm = validated_data.pop('password_confirm', None)
        
        # Обновляем остальные поля
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        # Обновляем пароль, если передан
        if password:
            instance.set_password(password)
        
        instance.save()
        return instance


class UserMeetingSerializer(serializers.ModelSerializer):
    date_in_past = serializers.SerializerMethodField()

    def get_date_in_past(self, obj):
        try:
            return obj.date <= timezone.now()
        except Exception:
            return False
    class Meta:
        model = Meeting
        fields = ['id', 'title', 'date', 'status', 'location', 'date_in_past']

# Сериализатор для регистрации нового пользователя через POST /api/register/.
class UserRegistrationSerializer(serializers.ModelSerializer):
    # Поле пароля: только для записи, обязательно, отображается как пароль в формах.
    password = serializers.CharField(
        write_only=True,
        required=True,
        min_length=8,
        style={'input_type': 'password'},
        error_messages={
            'min_length': 'Пароль должен содержать не менее 8 символов.',
            'required': 'Пароль обязателен.',
            'blank': 'Пароль не может быть пустым.',
        },
    )
    passwordConfirm = serializers.CharField(
        write_only=True,
        required=True,
        min_length=8,
        style={'input_type': 'password'},
        error_messages={
            'min_length': 'Подтверждение пароля должно содержать не менее 8 символов.',
            'required': 'Подтверждение пароля обязательно.',
            'blank': 'Подтверждение пароля не может быть пустым.',
        },
    )
    email = serializers.EmailField(
        required=True,
        allow_blank=False,
        trim_whitespace=True,
        validators=[],
        error_messages={
            'required': 'Email обязателен.',
            'blank': 'Email не может быть пустым.',
            'invalid': 'Введите корректный email.',
        },
    )
    username = serializers.CharField(
        required=True,
        max_length=150,
        allow_blank=False,
        trim_whitespace=True,
        validators=[],
        error_messages={
            'blank': 'Псевдоним не может быть пустым.',
            'required': 'Псевдоним обязателен.',
            'max_length': 'Псевдоним не может быть длиннее 150 символов.',
        },
    )
    first_name = serializers.CharField(
        required=True,
        max_length=NAME_MAX_LENGTH,
        allow_blank=False,
        trim_whitespace=True,
        error_messages={
            'blank': 'Имя не может быть пустым.',
            'max_length': f'Имя не может быть длиннее {NAME_MAX_LENGTH} символов.',
        },
    )
    last_name = serializers.CharField(
        required=True,
        max_length=NAME_MAX_LENGTH,
        allow_blank=False,
        trim_whitespace=True,
        error_messages={
            'blank': 'Фамилия не может быть пустой.',
            'max_length': f'Фамилия не может быть длиннее {NAME_MAX_LENGTH} символов.',
        },
    )
    accept_privacy_policy = serializers.BooleanField(write_only=True, required=True)
    accept_terms = serializers.BooleanField(write_only=True, required=True)
    accept_personal_data_processing = serializers.BooleanField(write_only=True, required=True)

    class Meta:
        # Указывает модель User для создания нового пользователя.
        model = User
        # Поля для регистрации: email, пароль, имя и фамилия.
        fields = [
            'username',
            'email',
            'password',
            'passwordConfirm',
            'first_name',
            'last_name',
            'accept_privacy_policy',
            'accept_terms',
            'accept_personal_data_processing',
        ]
        extra_kwargs = {
            'username': {'required': True},
            'first_name': {'required': True},
            'last_name': {'required': True},
        }

    def validate(self, data):
        # Проверка совпадения паролей
        if data['password'] != data['passwordConfirm']:
            raise serializers.ValidationError({'passwordConfirm': 'Пароли не совпадают'})

        # Валидация пароля с проверкой на similarity к username и email
        from .models import User as TempUser
        temp_user = TempUser(
            username=data.get('username', ''),
            email=data.get('email', ''),
            first_name=data.get('first_name', ''),
            last_name=data.get('last_name', ''),
        )
        try:
            validate_password(data['password'], user=temp_user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': translate_password_errors(list(exc.messages))})

        required_consents = {
            'accept_privacy_policy': 'Нужно принять Политику конфиденциальности.',
            'accept_terms': 'Нужно принять Пользовательское соглашение.',
            'accept_personal_data_processing': 'Нужно принять Согласие на обработку данных.',
        }
        for field_name, message in required_consents.items():
            if data.get(field_name) is not True:
                raise serializers.ValidationError({field_name: message})

        return data

    def validate_first_name(self, value):
        return validate_person_name(value, 'Имя')

    def validate_last_name(self, value):
        return validate_person_name(value, 'Фамилия')

    def validate_username(self, value):
        normalized = (value or '').strip()
        if User.objects.filter(username__iexact=normalized).exists():
            raise serializers.ValidationError('Пользователь с таким псевдонимом уже существует.')
        return normalized

    def validate_email(self, value):
        normalized = (value or '').strip().lower()
        if User.objects.filter(email__iexact=normalized).exists():
            raise serializers.ValidationError('Пользователь с таким email уже существует.')
        return normalized

    # Метод create переопределяет создание пользователя.
    def create(self, validated_data):
        validated_data.pop('passwordConfirm')
        validated_data.pop('accept_privacy_policy', None)
        validated_data.pop('accept_terms', None)
        validated_data.pop('accept_personal_data_processing', None)
        # Создаёт пользователя с хешированным паролем через create_user.
        # validated_data — проверенные данные из JSON-запроса.
        try:
            user = User.objects.create_user(
                username=validated_data['username'],
                email=validated_data['email'],
                password=validated_data['password'],
                first_name=validated_data['first_name'],
                last_name=validated_data['last_name']
            )
        except IntegrityError:
            raise serializers.ValidationError(
                {
                    'email': 'Пользователь с таким email уже существует.',
                    'username': 'Пользователь с таким псевдонимом уже существует.',
                }
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
            raise serializers.ValidationError({'detail': 'Неверные данные для входа.'})

        # Новые проверки статуса
        if user.is_banned or not user.is_active:
            raise serializers.ValidationError({'detail': 'Аккаунт заблокирован. Обратитесь к администратору клуба.'})

        # Проверка пароля
        if not user.check_password(password):
            raise serializers.ValidationError({'detail': 'Неверные данные для входа.'})

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
# Сериализатор для модели Book, преобразует данные книг в JSON.
class BookSerializer(serializers.ModelSerializer):
    average_rating = serializers.SerializerMethodField()
    LEGACY_GENRE_ALIASES = {
        'sci-fi': 'Научная фантастика',
        'fantasy': 'Фэнтези',
        'history': 'История',
        'mystery': 'Детектив',
        'drama': 'Драма',
        'novel': 'Роман',
        'fiction': 'Художественная литература',
        'romance': 'Романтика',
        'thriller': 'Триллер',
        'biography': 'Биография',
        'poetry': 'Поэзия',
        'non-fiction': 'Нон-фикшн',
        'non fiction': 'Нон-фикшн',
    }

    def validate_genre(self, value):
        normalized = (value or '').strip()
        mapped = self.LEGACY_GENRE_ALIASES.get(normalized.lower())
        return mapped or normalized

    #average_rating = serializers.FloatField(read_only=True)
    def get_average_rating(self, obj):
        annotated = getattr(obj, 'average_rating', None)
        if annotated is not None:
            try:
                return round(float(annotated), 1)
            except (TypeError, ValueError):
                return 0.0
        ratings = list(obj.book_ratings.all())
        if not ratings:
            return 0.0
        scores = {'GREEN': 5, 'YELLOW': 3, 'RED': 1}
        total = sum(scores.get(r.rating, 0) for r in ratings)
        return round(total / len(ratings), 1)

    class Meta:
        model = Book
        fields = [
            'id', 'title', 'genre', 'author', 'publication_year', 'is_discussed',
            'cover', 'description', 'is_voting_candidate', 'is_archived', 'average_rating'
        ]
        read_only_fields = ['id', 'average_rating']
        
# Сериализатор для модели Meeting, преобразует данные встреч в JSON.
class MeetingSerializer(serializers.ModelSerializer):
    current_attendees = serializers.SerializerMethodField()
    date = serializers.DateTimeField(
        error_messages={
            'required': 'Укажите дату и время встречи.',
            'null': 'Укажите дату и время встречи.',
            'blank': 'Укажите дату и время встречи.',
            'invalid': 'Укажите дату и время в формате ISO, например: 2026-06-10T18:00:00+05:00.',
        }
    )
    discussed_book = BookSerializer(read_only=True)
    discussed_book_id = serializers.PrimaryKeyRelatedField(
        queryset=Book.objects.filter(is_archived=False),
        source='discussed_book',
        required=False,
        allow_null=True,
    )

    class Meta:
        model = Meeting
        fields = [
            'id',
            'title',
            'date',
            'location',
            'description',
            'status',
            'type',
            'poster',
            'max_attendees',
            'discussed_book',
            'discussed_book_id',
            'current_attendees',
        ]
        read_only_fields = ['id', 'current_attendees']

    def validate(self, data):
        instance = getattr(self, 'instance', None)
        meeting_status = data.get('status', instance.status if instance else MeetingStatus.UPCOMING)
        meeting_date = data.get('date', instance.date if instance else None)
        max_attendees = data.get('max_attendees', instance.max_attendees if instance else None)
        discussed_book = data.get('discussed_book', instance.discussed_book if instance else None)

        # Require timezone-aware datetimes from clients to avoid naive/aware comparison bugs.
        if meeting_date and not timezone.is_aware(meeting_date):
            raise serializers.ValidationError({'date': 'Пожалуйста, указывайте дату и время в timezone-aware ISO формате, например: 2026-06-10T18:00:00+05:00.'})

        if instance and instance.status == MeetingStatus.COMPLETED and meeting_status == MeetingStatus.CANCELLED:
            raise serializers.ValidationError({'status': 'Нельзя отменить завершённую встречу.'})

        if (
            instance
            and meeting_status == MeetingStatus.UPCOMING
            and instance.status != MeetingStatus.UPCOMING
            and MeetingRegistration.objects.filter(meeting=instance, is_attended=True).exists()
        ):
            raise serializers.ValidationError(
                {'status': 'Нельзя вернуть встречу в статус «Предстоящая», если по ней уже отмечено посещение.'}
            )

        if (
            instance
            and meeting_status == MeetingStatus.UPCOMING
            and instance.status != MeetingStatus.UPCOMING
            and MeetingReview.objects.filter(meeting=instance).exists()
        ):
            raise serializers.ValidationError(
                {'status': 'Нельзя вернуть встречу в статус «Предстоящая», если по ней уже есть отзывы.'}
            )

        if (
            instance
            and instance.status == MeetingStatus.UPCOMING
            and instance.date <= timezone.now()
            and meeting_date
            and meeting_date > timezone.now()
        ):
            if MeetingRegistration.objects.filter(meeting=instance, is_attended=True).exists():
                raise serializers.ValidationError(
                    {'date': 'Нельзя перенести встречу в будущее, если по ней уже отмечено посещение.'}
                )
            if MeetingReview.objects.filter(meeting=instance).exists():
                raise serializers.ValidationError(
                    {'date': 'Нельзя перенести встречу в будущее, если по ней уже есть отзывы.'}
                )

        if meeting_date and meeting_status == MeetingStatus.UPCOMING and meeting_date < timezone.now():
            raise serializers.ValidationError({'date': 'Дата предстоящей встречи не может быть в прошлом.'})

        # Жёсткий запрет: нельзя пометить встречу завершённой до её даты
        if meeting_status == MeetingStatus.COMPLETED and meeting_date and meeting_date > timezone.now():
            raise serializers.ValidationError({'status': 'Нельзя пометить встречу завершённой до даты её проведения.'})

        # Нельзя отменять встречу, если её дата уже прошла (включая случай UPCOMING с прошедшей датой)
        if meeting_status == MeetingStatus.CANCELLED:
            effective_date = meeting_date or (instance.date if instance else None)
            if effective_date and effective_date <= timezone.now():
                raise serializers.ValidationError({'status': 'Нельзя отменить встречу, которая уже прошла.'})

        if max_attendees is not None:
            if max_attendees < 1:
                raise serializers.ValidationError({'max_attendees': 'max_attendees должно быть не меньше 1.'})
            if instance and max_attendees < instance.registrations.count():
                raise serializers.ValidationError(
                    {'max_attendees': 'max_attendees не может быть меньше текущего числа участников.'}
                )

        if discussed_book and discussed_book.is_archived:
            raise serializers.ValidationError({'discussed_book': 'Нельзя привязать архивную книгу к встрече.'})

        return data

    def get_current_attendees(self, obj):
        return obj.registrations.count()
# Сериализатор для модели MeetingRegistration, обрабатывает регистрацию на встречу.
class MeetingRegistrationSerializer(serializers.ModelSerializer):
    # Вложенный сериализатор для отображения данных пользователя в ответах (только чтение).
    user = UserSerializer(read_only=True)
    participant_first_name = serializers.CharField(source='user.first_name', read_only=True)
    participant_last_name = serializers.CharField(source='user.last_name', read_only=True)
    # Вложенный сериализатор для отображения данных встречи в ответах (только чтение).
    meeting = MeetingSerializer(read_only=True)
    # Поле для записи ID встречи, ссылается на модель Meeting (для POST-запросов).
    meeting_id = serializers.PrimaryKeyRelatedField(queryset=Meeting.objects.all(), source='meeting', write_only=True)
    phone_number = serializers.CharField(max_length=20)

    class Meta:
        model = MeetingRegistration
        fields = [
            'id', 'user', 'meeting', 'meeting_id', 'phone_number', 'registered_at',
            'is_attended', 'attended_at', 'participant_first_name', 'participant_last_name'
        ]
        read_only_fields = ['id', 'user', 'meeting', 'registered_at', 'is_attended', 'attended_at']

    def validate_phone_number(self, value):
        if not re.match(r'^\+\d{10,15}$', value):
            raise serializers.ValidationError("Номер телефона должен начинаться с '+' и содержать 10-15 цифр.")
        return value

    def validate(self, data):
        meeting = data['meeting']
        if meeting.status != 'UPCOMING':
            raise serializers.ValidationError("Регистрация возможна только на предстоящие встречи.")
        if meeting.date <= timezone.now():
            raise serializers.ValidationError("Регистрация возможна только на предстоящие встречи с будущей датой.")
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
        if user.is_banned:
            raise serializers.ValidationError("Забаненный пользователь не может регистрироваться на встречи.")
        meeting = validated_data.pop('meeting')
        phone_number = validated_data.pop('phone_number')
        with transaction.atomic():
            locked_meeting = Meeting.objects.select_for_update().get(pk=meeting.pk)

            if locked_meeting.status != 'UPCOMING':
                raise serializers.ValidationError("Регистрация возможна только на предстоящие встречи.")
            if locked_meeting.date <= timezone.now():
                raise serializers.ValidationError("Регистрация возможна только на предстоящие встречи с будущей датой.")

            if MeetingRegistration.objects.filter(user=user, meeting=locked_meeting).exists():
                raise serializers.ValidationError("Вы уже зарегистрированы на эту встречу.")

            if locked_meeting.max_attendees is not None:
                current_count = locked_meeting.registrations.count()
                if current_count >= locked_meeting.max_attendees:
                    raise serializers.ValidationError("Максимальное количество участников достигнуто.")

            return MeetingRegistration.objects.create(user=user, meeting=locked_meeting, phone_number=phone_number)
    
# Сериализатор для модели Book, преобразует данные книг в JSON.
'''class BookSerializer(serializers.ModelSerializer):
    average_rating = serializers.SerializerMethodField()
    LEGACY_GENRE_ALIASES = {
        'sci-fi': 'Научная фантастика',
        'fantasy': 'Фэнтези',
        'history': 'История',
        'mystery': 'Детектив',
        'drama': 'Драма',
        'novel': 'Роман',
        'fiction': 'Художественная литература',
        'romance': 'Романтика',
        'thriller': 'Триллер',
        'biography': 'Биография',
        'poetry': 'Поэзия',
        'non-fiction': 'Нон-фикшн',
        'non fiction': 'Нон-фикшн',
    }

    def validate_genre(self, value):
        normalized = (value or '').strip()
        mapped = self.LEGACY_GENRE_ALIASES.get(normalized.lower())
        return mapped or normalized

    #average_rating = serializers.FloatField(read_only=True)
    def get_average_rating(self, obj):
        annotated = getattr(obj, 'average_rating', None)
        if annotated is not None:
            try:
                return round(float(annotated), 1)
            except (TypeError, ValueError):
                return 0.0
        ratings = list(obj.book_ratings.all())
        if not ratings:
            return 0.0
        scores = {'GREEN': 5, 'YELLOW': 3, 'RED': 1}
        total = sum(scores.get(r.rating, 0) for r in ratings)
        return round(total / len(ratings), 1)

    class Meta:
        model = Book
        fields = [
            'id', 'title', 'genre', 'author', 'publication_year', 'is_discussed',
            'cover', 'description', 'is_voting_candidate', 'is_archived', 'average_rating'
        ]
        read_only_fields = ['id', 'average_rating']'''
# Сериализатор для модели BookVote, обрабатывает голосование за книги.
class BookVoteSerializer(serializers.ModelSerializer):
    book_id = serializers.PrimaryKeyRelatedField(queryset=Book.objects.filter(is_archived=False), source='book')
    username = serializers.SerializerMethodField()

    class Meta:
        model = BookVote
        fields = ['id', 'book_id', 'voted_at', 'username']
        read_only_fields = ['id', 'voted_at', 'username']
    
    def get_username(self, obj):
        # Возвращаем username только в открытом режиме голосования
        voting_period = obj.voting_period
        if voting_period and voting_period.voting_mode == 'open':
            return obj.user.username
        # В закрытом режиме возвращаем None
        return None
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
    question1_id = serializers.PrimaryKeyRelatedField(
        queryset=ReviewQuestion.objects.all(), 
        source='question1', 
        write_only=True
    )
    question2_id = serializers.PrimaryKeyRelatedField(
        queryset=ReviewQuestion.objects.all(), 
        source='question2', 
        write_only=True
    )
    question3_id = serializers.PrimaryKeyRelatedField(
        queryset=ReviewQuestion.objects.all(), 
        source='question3', 
        write_only=True
    )
    meeting_id = serializers.IntegerField(write_only=True, required=False)

    class Meta:
        model = MeetingReviewQuestion
        fields = ['id', 'meeting', 'question1', 'question2', 'question3', 'question1_id', 'question2_id', 'question3_id', 'meeting_id']
        read_only_fields = ['id', 'meeting', 'question1', 'question2', 'question3']

    def validate_meeting_id(self, value):
        try:
            meeting = Meeting.objects.get(id=value)
        except Meeting.DoesNotExist:
            raise serializers.ValidationError("Meeting not found.")
        return meeting

    def validate(self, data):
        # Для CREATE: проверяем, что meeting_id передан
        if self.instance is None and 'meeting_id' not in self.initial_data:
            raise serializers.ValidationError({"meeting_id": "This field is required."})
        
        # Преобра meeting_id в meeting для сохранения
        if 'meeting_id' in data:
            data['meeting'] = data.pop('meeting_id')

        # Для UPDATE запрещаем менять саму встречу у уже существующей привязки
        if self.instance is not None and 'meeting' in data and data['meeting'].id != self.instance.meeting_id:
            raise serializers.ValidationError(
                "Нельзя изменять встречу у существующей привязки вопросов."
            )
        
        # Проверяем только если все три вопроса переданы (для CREATE операции)
        if 'question1' in data and 'question2' in data and 'question3' in data:
            question_ids = [data['question1'].id, data['question2'].id, data['question3'].id]
            if len(set(question_ids)) != 3:
                raise serializers.ValidationError("Все вопросы должны быть разными.")
        
        # Для CREATE: проверяем, что встреча только UPCOMING и не в прошлом
        if self.instance is None and 'meeting' in data:
            meeting = data['meeting']
            if MeetingReviewQuestion.objects.filter(meeting=meeting).exists():
                raise serializers.ValidationError(
                    {"meeting_id": "Для этой встречи вопросы уже привязаны."}
                )
            if meeting.status != 'UPCOMING':
                raise serializers.ValidationError(
                    "Вопросы можно добавлять только к встречам со статусом «Предстоящая»."
                )
            if meeting.date <= timezone.now():
                raise serializers.ValidationError(
                    "Нельзя привязывать вопросы к встрече, дата которой уже прошла."
                )
        
        # Для UPDATE: проверяем, что встреча не COMPLETED и не в прошлом
        if self.instance is not None:
            meeting = self.instance.meeting
            if meeting.status == 'COMPLETED':
                raise serializers.ValidationError(
                    "Невозможно изменить вопросы для встречи со статусом «Завершена»."
                )
            if meeting.date <= timezone.now():
                raise serializers.ValidationError(
                    "Невозможно изменить вопросы для встречи, дата которой уже прошла."
                )
        
        return data

# Сериализатор для модели MeetingReview
class MeetingReviewSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    meeting = MeetingSerializer(read_only=True)
    question1 = ReviewQuestionSerializer(read_only=True, source='meeting.review_questions.question1')
    question2 = ReviewQuestionSerializer(read_only=True, source='meeting.review_questions.question2')
    question3 = ReviewQuestionSerializer(read_only=True, source='meeting.review_questions.question3')
    meeting_id = serializers.PrimaryKeyRelatedField(queryset=Meeting.objects.all(), source='meeting', write_only=True)
    question1_answer = serializers.ChoiceField(choices=[1, 2, 3, 4, 5], allow_null=False, required=True)
    question2_answer = serializers.ChoiceField(choices=[1, 2, 3, 4, 5], allow_null=False, required=True)
    question3_answer = serializers.ChoiceField(choices=[1, 2, 3, 4, 5], allow_null=False, required=True)

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

        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user and user.is_authenticated:
            registration = MeetingRegistration.objects.filter(user=user, meeting=meeting).first()
            if not registration:
                raise serializers.ValidationError("Вы не зарегистрированы на эту встречу.")
            if not registration.is_attended:
                raise serializers.ValidationError(
                    "Отзывы могут оставлять только участники, присутствовавшие на встрече."
                )

        missing_answers = [
            field for field in ['question1_answer', 'question2_answer', 'question3_answer']
            if data.get(field) is None
        ]
        if missing_answers:
            raise serializers.ValidationError(
                {field: 'Это поле обязательно.' for field in missing_answers}
            )
        return data

# Сериализатор для модели VotingPeriod
class VotingPeriodSerializer(serializers.ModelSerializer):
    class Meta:
        model = VotingPeriod
        fields = ['id', 'start_date', 'end_date', 'is_active', 'voting_mode']
        read_only_fields = ['id']

# Сериализатор для модели BookReview
class BookReviewSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    rating = serializers.SerializerMethodField()
    rating_detail = serializers.SerializerMethodField()
    MIN_REVIEW_LENGTH = 50
    
    class Meta:
        model = BookReview
        fields = ['id', 'user', 'book', 'review_text', 'rating', 'rating_detail', 'is_hidden', 'created_at']
        read_only_fields = ['id', 'user', 'rating', 'rating_detail', 'is_hidden', 'created_at']

    def _resolve_current_rating(self, instance):
        return BookRating.objects.filter(user=instance.user, book=instance.book).first()

    def get_rating(self, instance):
        rating = self._resolve_current_rating(instance)
        return rating.id if rating else None

    def get_rating_detail(self, instance):
        rating = self._resolve_current_rating(instance)
        if rating is None:
            return None
        return BookRatingSerializer(rating, context=self.context).data

    def _is_admin_request(self):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        return getattr(request.user, 'role', None) == 'admin' or request.user.is_superuser

    def _public_user_payload(self, user_payload):
        if not isinstance(user_payload, dict):
            return user_payload
        allowed_fields = ['id', 'username', 'first_name', 'last_name', 'avatar_icon']
        return {field: user_payload.get(field) for field in allowed_fields}

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        if self._is_admin_request():
            return ret

        ret['user'] = self._public_user_payload(ret.get('user'))
        rating_detail = ret.get('rating_detail')
        if isinstance(rating_detail, dict):
            rating_detail['user'] = self._public_user_payload(rating_detail.get('user'))
        return ret

    def validate_review_text(self, value):
        # Санитизация: удаляем все HTML теги
        sanitized = bleach.clean(value, tags=[], strip=True)
        
        if contains_profanity(sanitized):
            raise serializers.ValidationError('Отзыв содержит недопустимую лексику.')
        
        if not sanitized.strip():
            raise serializers.ValidationError('Отзыв не может быть пустым.')

        if len(sanitized.strip()) < self.MIN_REVIEW_LENGTH:
            raise serializers.ValidationError(
                f'Отзыв должен быть не короче {self.MIN_REVIEW_LENGTH} символов.'
            )
        
        if len(sanitized) > 5000:
            raise serializers.ValidationError('Отзыв не может быть больше 5000 символов.')
        
        return sanitized

    def validate(self, data):
        if 'rating' in self.initial_data or 'rating_id' in self.initial_data:
            raise serializers.ValidationError(
                {'rating': 'Оценка отправляется отдельно через /api/ratings/.'}
            )

        # Проверяем, не существует ли уже отзыв от этого пользователя на эту книгу
        user = self.context['request'].user
        book = data.get('book')
        
        # При редактировании исключаем текущий объект из проверки
        if self.instance:
            existing = BookReview.objects.filter(
                user=user,
                book=book
            ).exclude(id=self.instance.id)
        else:
            existing = BookReview.objects.filter(user=user, book=book)
        
        if existing.exists():
            raise serializers.ValidationError(
                'Вы уже написали отзыв на эту книгу.'
            )
        
        return data


class NotificationSerializer(serializers.ModelSerializer):
    meeting_id = serializers.IntegerField(source='meeting.id', read_only=True)

    class Meta:
        model = Notification
        fields = ['id', 'title', 'message', 'is_read', 'created_at', 'read_at', 'meeting_id']
        read_only_fields = fields

#Сериализатор для Логов админа
class AdminLogSerializer(serializers.ModelSerializer):
    admin = serializers.CharField(source='admin.username', read_only=True)
    registration_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = AdminLog
        fields = [
            'id',
            'admin',           # кто сделал действие
            'action',          # 'создание встречи', 'удаление книги' и т.д.
            'target',          # человекочитаемое название объекта
            'target_id',       # ID объекта
            'registration_id', # связанная регистрация (если есть)
            'details',         # дополнительные детали (если есть)
            'timestamp'        # дата/время действия
        ]
        read_only_fields = fields  # всё только для чтения

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        chelyabinsk_time = timezone.localtime(instance.timestamp, ZoneInfo('Asia/Yekaterinburg'))
        ret['timestamp'] = chelyabinsk_time.isoformat()
        return ret
    
