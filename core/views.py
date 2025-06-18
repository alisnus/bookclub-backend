#views.py
from django.shortcuts import render, get_object_or_404
from rest_framework import generics
from rest_framework import viewsets, permissions, status
from rest_framework.views import APIView
from rest_framework.decorators import api_view, permission_classes, action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from datetime import datetime, timedelta
from django.db.models import Avg, Case, When, FloatField
from .models import User, Meeting, MeetingRegistration, Book, BookVote, BookRating, ReviewQuestion, MeetingReview, VotingPeriod, MeetingReviewQuestion
from rest_framework import serializers
from .serializers import (
    UserSerializer, UserRegistrationSerializer, MyTokenObtainPairSerializer,
    MeetingSerializer, MeetingRegistrationSerializer, BookSerializer,
    BookVoteSerializer, BookRatingSerializer, ReviewQuestionSerializer, MeetingReviewSerializer, VotingPeriodSerializer, MeetingReviewQuestionSerializer
)
import logging
# Настройка логирования
logger = logging.getLogger(__name__)

# Кастомная пагинация
class StandardResultsSetPagination(PageNumberPagination):
    page_size = 12  # Книг на странице
    page_size_query_param = 'page_size'
    max_page_size = 100

# Кастомное разрешение: админ или сам пользователь
class IsAdminOrSelf(permissions.BasePermission):#Проверка прав доступа к, например, профилю пользователя
    def has_object_permission(self, request, view, obj):
        return request.user.is_staff or obj == request.user#Если пользователь админ возвращаем True


# ViewSet для пользователей (список, профиль)
class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdminOrSelf]

    def get_queryset(self):
        if self.request.user.is_staff:
            return User.objects.all()
        return User.objects.filter(id=self.request.user.id)

    @action(detail=False, methods=['get'], permission_classes=[permissions.IsAuthenticated])
    def me(self, request):
        serializer = UserSerializer(request.user)
        return Response(serializer.data)

# Кастомный view для получения JWT-токена
class MyTokenObtainPairView(TokenObtainPairView):
    # Указываем кастомный сериализатор, который добавляет email и username в токен
    serializer_class = MyTokenObtainPairSerializer


# View для обновления JWT-токена
class TokenRefreshView(TokenRefreshView):
    # Использует стандартное поведение из rest_framework_simplejwt
    pass


# View для регистрации пользователя
class UserRegistrationView(APIView):
    # Разрешаем доступ всем (даже неавторизованным), чтобы любой мог зарегистрироваться.
    permission_classes = [permissions.AllowAny]

    # Метод обрабатывает POST-запрос с данными для регистрации (username, email, password...)
    def post(self, request):
        # Создаём сериализатор с данными из тела запроса (JSON).
        serializer = UserRegistrationSerializer(data=request.data)
        if serializer.is_valid():
            # Сохраняем нового пользователя в базу.
            serializer.save()
            # Возвращаем данные пользователя (username, email и т.д.) и статус 201 (Created).
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        # Если данные невалидны (например, email уже занят), возвращаем ошибки и статус 400 (Bad Request).
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

# View для проверки никнейма
class CheckUsernameView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username = request.data.get('username')
        if not username:
            return Response({'detail': 'Никнейм обязателен'}, status=status.HTTP_400_BAD_REQUEST)
        if User.objects.filter(username=username).exists():
            return Response({'detail': 'Никнейм уже занят'}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'detail': 'Никнейм доступен'}, status=status.HTTP_200_OK)

# View для проверки пароля
class CheckPasswordView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        password = request.data.get('password')
        if not password:
            return Response({'detail': 'Пароль обязателен'}, status=status.HTTP_400_BAD_REQUEST)
        if len(password) <= 8:
            return Response({'detail': 'Пароль должен быть длиннее 8 символов'}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'detail': 'Пароль корректен'}, status=status.HTTP_200_OK)

# View для проверки email
class CheckEmailView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        email = request.data.get('email')
        if not email:
            return Response({'detail': 'Email обязателен'}, status=status.HTTP_400_BAD_REQUEST)
        if User.objects.filter(email=email).exists():
            return Response({'detail': 'Email уже занят'}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'detail': 'Email доступен'}, status=status.HTTP_200_OK)

# ViewSet для работы с книгами (список, создание, обновление, удаление).
class BookViewSet(viewsets.ModelViewSet):
    queryset = Book.objects.all()
    serializer_class = BookSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        queryset = self.queryset
        params = {
            'genre': self.request.query_params.get('genre'),
            'rating_color': self.request.query_params.get('rating_color'),
            'min_rating': self.request.query_params.get('min_rating'),
            'for_voting': self.request.query_params.get('for_voting'),
            'is_discussed': self.request.query_params.get('is_discussed'),
        }
        logger.debug(f"Applying filters: {params}")

        if params['genre']:
            queryset = queryset.filter(genre=params['genre'])

        if params['min_rating']:
            try:
                min_rating_float = float(params['min_rating'])
                queryset = queryset.annotate(
                    avg_rating=Avg(Case(
                        When(book_ratings__rating='GREEN', then=5),
                        When(book_ratings__rating='YELLOW', then=3),
                        When(book_ratings__rating='RED', then=1),
                        output_field=FloatField()
                    ))
                ).filter(avg_rating__gte=min_rating_float)
            except ValueError:
                logger.error(f"Invalid min_rating value: {params['min_rating']}")
                pass

        if params['rating_color'] in ['GREEN', 'YELLOW', 'RED']:
            queryset = queryset.filter(
                book_ratings__rating=params['rating_color'],
                book_ratings__user=self.request.user
            )

        if params['is_discussed'] is not None:
            is_discussed = params['is_discussed'].lower() == 'true'
            queryset = queryset.filter(is_discussed=is_discussed)

        if params['for_voting']:
            queryset = queryset.filter(is_discussed=False, is_voting_candidate=True).distinct()[:8]
            logger.debug(f"Books for voting: {list(queryset.values('id', 'title'))}")

        else:
            queryset = queryset.distinct()

        logger.debug(f"Filtered queryset count: {queryset.count()}")
        return queryset

    @action(detail=False, methods=['patch'], permission_classes=[permissions.IsAdminUser])
    def reset_voting(self, request):
        Book.objects.update(is_voting_candidate=False)
        return Response({'status': 'Все книги сняты с голосования'})

# ViewSet для работы со встречами (список, создание, обновление, удаление).
class MeetingViewSet(viewsets.ModelViewSet):
    queryset = Meeting.objects.all()
    serializer_class = MeetingSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        status = self.request.query_params.get('status')
        if status:
            return self.queryset.filter(status=status)
        return self.queryset.filter(status='UPCOMING', date__gte=timezone.now()).order_by('date')

    def retrieve(self, request, *args, **kwargs):
        instance = get_object_or_404(Meeting, pk=kwargs.get('pk'))
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

# ViewSet для регистрации на встречу
# C:\Users\gajda\PycharmProjects\bookclub\core\views.py (только MeetingRegistrationViewSet)
class MeetingRegistrationViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def list(self, request):
        meeting_id = request.query_params.get('meeting_id')
        if not meeting_id:
            return Response({'detail': 'Параметр meeting_id обязателен'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            registrations = MeetingRegistration.objects.filter(
                user=request.user,
                meeting_id=meeting_id
            )
            serializer = MeetingRegistrationSerializer(registrations, many=True)
            return Response(serializer.data)
        except ValueError:
            return Response({'detail': 'Некорректный meeting_id'}, status=status.HTTP_400_BAD_REQUEST)

    def create(self, request):
        serializer = MeetingRegistrationSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

# ViewSet для голосования за книги
class BookVoteViewSet(viewsets.ModelViewSet):
    queryset = BookVote.objects.all()
    serializer_class = BookVoteSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = self.queryset.filter(user=self.request.user)  # Фильтруем по текущему пользователю
        params = {
            'voting_period_id': self.request.query_params.get('voting_period_id'),
        }
        logger.debug(f"Applying filters: {params}")

        if params['voting_period_id']:
            try:
                queryset = queryset.filter(voting_period_id=params['voting_period_id'])
                logger.debug(f"Filtered by voting_period_id: {params['voting_period_id']}")
            except ValueError:
                logger.error(f"Invalid voting_period_id: {params['voting_period_id']}")

        logger.debug(f"Filtered queryset count: {queryset.count()}")
        return queryset

    def perform_create(self, serializer):
        today = timezone.now()
        active_period = VotingPeriod.objects.filter(
            start_date__lte=today,
            end_date__gte=today,
            is_active=True
        ).first()
        if not active_period:
            raise ValidationError("Голосование закрыто: нет активного периода.")

        user = self.request.user
        existing_votes = BookVote.objects.filter(
            user=user,
            voting_period=active_period
        )
        if existing_votes.count() >= 2:
            raise ValidationError("Вы уже проголосовали за 2 книги в этом периоде.")
        book = serializer.validated_data['book']
        if existing_votes.filter(book=book).exists():
            raise ValidationError("Вы уже проголосовали за эту книгу в этом периоде.")
        if not book.is_voting_candidate:
            raise ValidationError("Эта книга не доступна для голосования.")

        serializer.save(user=user, voting_period=active_period)

# ViewSet для оценки книг (зелёная, жёлтая, красная)
class BookRatingViewSet(viewsets.ModelViewSet):
    queryset = BookRating.objects.all()
    serializer_class = BookRatingSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        try:
            book_id = self.request.data.get('book_id')
            if not book_id:
                raise ValidationError('Поле book_id обязательно')
            book = Book.objects.get(id=book_id)
            existing_rating = BookRating.objects.filter(user=self.request.user, book=book).first()
            if existing_rating:
                existing_rating.rating = self.request.data.get('rating')
                existing_rating.save()
                return Response(self.serializer_class(existing_rating).data)
            serializer.save(user=self.request.user, book=book)
        except ObjectDoesNotExist:
            raise ValidationError('Книга с указанным ID не найдена')


# ViewSet для вопросов отзывов
class ReviewQuestionViewSet(viewsets.ModelViewSet):
    queryset = ReviewQuestion.objects.all()
    serializer_class = ReviewQuestionSerializer
    permission_classes = [permissions.IsAuthenticated]

# ViewSet для вопросов встреч
class MeetingReviewQuestionViewSet(viewsets.ModelViewSet):
    queryset = MeetingReviewQuestion.objects.all()
    serializer_class = MeetingReviewQuestionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = self.queryset
        meeting_id = self.request.query_params.get('meeting_id')
        if meeting_id:
            queryset = queryset.filter(meeting_id=meeting_id)
        return queryset

# ViewSet для отзывов на встречи
class MeetingReviewViewSet(viewsets.ModelViewSet):
    queryset = MeetingReview.objects.all()
    serializer_class = MeetingReviewSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        user = self.request.user
        meeting = serializer.validated_data['meeting']
        if not MeetingRegistration.objects.filter(user=user, meeting=meeting).exists():
            raise ValidationError("Вы не зарегистрированы на эту встречу.")
        if meeting.status != 'COMPLETED':
            raise ValidationError("Отзывы можно оставлять только для завершённых встреч.")
        if MeetingReview.objects.filter(user=user, meeting=meeting).exists():
            raise ValidationError("Вы уже оставили отзыв на эту встречу.")
        serializer.save(user=user)

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def by_meeting(self, request):
        meeting_id = request.query_params.get('meeting_id')
        if not meeting_id:
            raise ValidationError("Параметр meeting_id обязателен.")
        try:
            meeting = Meeting.objects.get(id=meeting_id)
        except Meeting.DoesNotExist:
            raise ValidationError("Встреча не найдена.")
        reviews = self.queryset.filter(meeting=meeting)
        serializer = self.serializer_class(reviews, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def by_user(self, request):
        reviews = self.queryset.filter(user=request.user)
        meeting_id = request.query_params.get('meeting_id')
        if meeting_id:
            try:
                meeting = Meeting.objects.get(id=meeting_id)
                reviews = reviews.filter(meeting=meeting)
            except Meeting.DoesNotExist:
                raise ValidationError("Встреча не найдена.")
        serializer = self.serializer_class(reviews, many=True)
        return Response(serializer.data)

# View для проверки текущего периода голосования
class VotingPeriodView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        today = timezone.now()
        active_period = VotingPeriod.objects.filter(
            start_date__lte=today,
            end_date__gte=today,
            is_active=True
        ).first()
        if active_period:
            serializer = VotingPeriodSerializer(active_period)
            return Response({
                'id': active_period.id,  # Добавляем id
                'is_active': True,
                'start_date': serializer.data['start_date'],
                'end_date': serializer.data['end_date']
            })
        return Response({
            'is_active': False,
            'message': 'Голосование ещё не открыто или уже закрыто'
        })

# View для информации о клубе (главная страница, только для авторизованных)
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def club_info(request):
    # Текущий месяц
    today = timezone.now()
    start_of_month = datetime(today.year, today.month, 1, tzinfo=timezone.get_current_timezone())
    end_of_month = (start_of_month + timedelta(days=31)).replace(day=1) - timedelta(seconds=1)

    # Все UPCOMING встречи за месяц
    upcoming_meetings = Meeting.objects.filter(
        status='UPCOMING',
        date__gte=timezone.now(),
        date__lte=end_of_month
    ).order_by('-date')

    meeting_serializer = MeetingSerializer(upcoming_meetings, many=True)
    data = {
        "welcome_message": "Добро пожаловать в Книжный клуб! Обсуждаем лучшие книги вместе!",
        "why_us": [
            {
                "title": "Глубокий Анализ",
                "description": "Мы не просто читаем книги, а разбираем их смысл, контекст и идеи, чтобы лучше понимать мир и себя.",
                "icon": "Star"
            },
            {
                "title": "Разнообразие жанров",
                "description": "Мы не ограничиваемся классикой – в программе есть современная проза, нон-фикшн, мировая литература и даже эксперименты с формой.",
                "icon": "Book"
            },
            {
                "title": "Экспертные лекторы",
                "description": "Лекции проводят специалисты с реальным опытом и страстью к литературе, что делает обсуждения содержательными и вдохновляющими.",
                "icon": "Person"
            },
            {
                "title": "Дружелюбная атмосфера",
                "description": "У нас нет места снобизму или формальностям – только искренний интерес к книгам и поддержка каждого участника.",
                "icon": "Group"
            },
            {
                "title": "Живое общение",
                "description": "Наши встречи – это диалог, где каждый может высказать своё мнение и услышать разные точки зрения.",
                "icon": "Event"
            }
        ],
        "faq": [
            {
                "question": "Что такое книжный клуб?",
                "answer": "Место, где любители книг собираются для обсуждения и обмена идеями."
            },
            {
                "question": "Как выбираются книги для обсуждения?",
                "answer": "Каждый месяц администратор предлагает список, участники голосуют – и выбираются две книги для обсуждения на месяц."
            },
            {
                "question": "Как часто проходят встречи?",
                "answer": "Мы встречаемся 5–6 раз в месяц. Два раза – обсуждаем книги, остальные встречи посвящены лекциям, разговорам о литературе и другим форматам."
            },
            {
                "question": "Где посмотреть расписание встреч?",
                "answer": "Все даты, темы и форматы – в нашей ленте будущих встреч ниже на сайте, там же можно зарегистрироваться на мероприятие."
            },
            {
                "question": "Что нужно знать новичку?",
                "answer": "На наших встречах свобода слова. Мы не поднимаем руки, чтобы поделиться мыслями, у нас нет чёткой очереди, а иногда мы даже перебиваем."
            }
        ],
        "lecturers": [
            {
                "name": "Анна Миронова",
                "bio": "Литературовед\n– Специалист по Достоевскому, Толстому и Булгакову. Объясняет классику через психологию и эпоху, сравнивает с кино.",
                "avatar": "/static/images/anna.jpg"
            },
            {
                "name": "Пётр Исаев",
                "bio": "Исследователь постколониальной литературы\n– Жил в ЮАР, Британии и Индии. Пишет о языке, памяти и власти. Разбирает тексты Нгуги, Рой и Ачебе.",
                "avatar": "/static/images/petr.jpg"
            },
            {
                "name": "Мария Кравцова",
                "bio": "Независимый критик, популяризатор нон-фикшн\n– Пишет о науке, мышлении и книгах. Любит нон-фикшн – от нейронауки до урбанистики.",
                "avatar": "/static/images/maria.jpg"
            },
            {
                "name": "Игорь Кузнецов",
                "bio": "Историк литературы, знаток забытых сюжетов\n– Рассказывает о вычеркнутых книгах и мифах. Объясняет, как литература отражает эпоху.",
                "avatar": "/static/images/igor.jpg"
            }
        ],
        "location": "Офис 305, ул. Либнехта, д. 22, Челябинск",
        "upcoming_meetings": meeting_serializer.data
    }
    return Response(data)