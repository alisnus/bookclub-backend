from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.db.models import Count, Q
from django.utils import timezone
from django.contrib import messages
from django.utils.html import format_html
import logging
from .models import (
    User, Meeting, MeetingRegistration, Book, BookVote,
    BookRating, ReviewQuestion, MeetingReview, VotingPeriod, MeetingReviewQuestion
)

# Настройка логирования
logger = logging.getLogger(__name__)

# Регистрация кастомной модели User
@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ('email', 'username', 'first_name', 'last_name', 'is_staff')
    list_filter = ('is_staff', 'is_superuser')
    search_fields = ('email', 'username', 'first_name', 'last_name')
    fieldsets = (
        (None, {'fields': ('email', 'username', 'password')}),
        ('Личная информация', {'fields': ('first_name', 'last_name')}),
        ('Права', {'fields': ('is_active', 'is_staff', 'is_superuser')}),
        ('Даты', {'fields': ('last_login', 'date_joined')}),
    )
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'email', 'first_name', 'last_name', 'password1', 'password2'),
        }),
    )
    ordering = ('email',)

# Регистрация модели Book
@admin.register(Book)
class BookAdmin(admin.ModelAdmin):
    list_display = ('title', 'author', 'genre', 'publication_year', 'is_discussed', 'is_voting_candidate', 'vote_count')
    list_filter = ('genre', 'is_discussed', 'is_voting_candidate', 'votes__voting_period')  # Добавляем фильтр по периоду
    search_fields = ('title', 'author')
    actions = ['mark_as_voting_candidate', 'remove_from_voting', 'get_top_voted_books', 'clear_top_voted_filter']

    def vote_count(self, obj):
        return getattr(obj, 'vote_count', 0)  # Используем аннотированное значение
    vote_count.short_description = 'Голосов'

    def mark_as_voting_candidate(self, request, queryset):
        if queryset.count() != 8:
            self.message_user(request, "Выберите ровно 8 книг для голосования.", level='error')
            return
        if queryset.filter(is_discussed=True).exists():
            self.message_user(request, "Выбраны книги, которые уже обсуждались.", level='error')
            return
        Book.objects.update(is_voting_candidate=False)
        queryset.update(is_voting_candidate=True)
        self.message_user(request, "Книги назначены для голосования.")
    mark_as_voting_candidate.short_description = "Назначить для голосования"

    def remove_from_voting(self, request, queryset):
        queryset.update(is_voting_candidate=False)
        self.message_user(request, "Книги удалены из голосования.")
    remove_from_voting.short_description = "Удалить из голосования"

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        period_id = request.GET.get('votes__voting_period__id__exact', None)
        if period_id:
            print(f"Filtering by period_id: {period_id}")
            queryset = queryset.annotate(
                vote_count=Count('votes', filter=Q(votes__voting_period_id=period_id), distinct=True)
            ).order_by('-vote_count', 'title')  # Сортировка по убыванию голосов
        else:
            # Суммируем голоса за все периоды, если нет фильтра
            queryset = queryset.annotate(
                vote_count=Count('votes', distinct=True)
            ).order_by('-vote_count', 'title')  # Сортировка по убыванию общей суммы голосов
            print("No period filter, counting all votes")
        return queryset

# Регистрация модели Meeting
@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    list_display = ('title', 'date', 'location', 'status', 'type', 'max_attendees', 'current_attendees', 'poster_preview')
    list_filter = ('status', 'type')
    search_fields = ('title', 'location')
    ordering = ['date']
    readonly_fields = ['poster_preview']

    def current_attendees(self, obj):
        return obj.registrations.count()
    current_attendees.short_description = 'Зарегистрировано'

    def poster_preview(self, obj):
        if obj.poster:
            return format_html('<img src="{}" style="max-height: 100px;" />', obj.poster.url)
        return "Нет изображения"
    poster_preview.short_description = 'Превью постера'

# Регистрация модели MeetingRegistration
@admin.register(MeetingRegistration)
class MeetingRegistrationAdmin(admin.ModelAdmin):
    list_display = ('user', 'meeting', 'phone_number', 'registered_at')
    list_filter = ('meeting',)
    search_fields = ('user__email', 'meeting__title')

# Регистрация модели BookVote
@admin.register(BookVote)
class BookVoteAdmin(admin.ModelAdmin):
    list_display = ('user', 'book', 'voted_at')
    list_filter = ('voting_period', 'book')
    search_fields = ('user__email', 'book__title')

# Регистрация модели BookRating
@admin.register(BookRating)
class BookRatingAdmin(admin.ModelAdmin):
    list_display = ('user', 'book', 'rating', 'rated_at')
    list_filter = ('rating', 'book')
    search_fields = ('user__email', 'book__title')

# Регистрация модели ReviewQuestion
@admin.register(ReviewQuestion)
class ReviewQuestionAdmin(admin.ModelAdmin):
    list_display = ('question_text', 'created_at')
    search_fields = ('question_text',)

# Регистрация модели MeetingReview
@admin.register(MeetingReview)
class MeetingReviewAdmin(admin.ModelAdmin):
    list_display = ('user', 'meeting', 'question1_answer', 'question2_answer', 'question3_answer', 'reviewed_at')
    list_filter = ('meeting',)
    search_fields = ('user__email', 'meeting__title')

# Регистрация модели MeetingReviewQuestion
@admin.register(MeetingReviewQuestion)
class MeetingReviewQuestionAdmin(admin.ModelAdmin):
    list_display = ('meeting', 'question1', 'question2', 'question3')
    list_filter = ('meeting',)
    search_fields = ('meeting__title', 'question1__question_text', 'question2__question_text', 'question3__question_text')
    fieldsets = (
        (None, {
            'fields': ('meeting', 'question1', 'question2', 'question3')
        }),
    )

# Регистрация модели VotingPeriod
@admin.register(VotingPeriod)
class VotingPeriodAdmin(admin.ModelAdmin):
    list_display = ('start_date', 'end_date', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('start_date', 'end_date')
    fieldsets = (
        (None, {'fields': ('start_date', 'end_date', 'is_active')}),
    )