#views.py
from urllib import request
from django.shortcuts import render, get_object_or_404
from rest_framework import generics
from rest_framework import viewsets, permissions, status
from rest_framework.views import APIView
from rest_framework.decorators import api_view, permission_classes, action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework.exceptions import ValidationError, PermissionDenied
from rest_framework.pagination import PageNumberPagination
from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from django.core.cache import cache
from django.conf import settings
from django.db import transaction
from django.db import IntegrityError
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import OrderingFilter
from datetime import datetime, timedelta
from django.db.models import Avg, Case, When, FloatField, Count, Max, Q
from django.db.models.functions import TruncDate
from efficient_apriori import apriori
from .models import User, Meeting, MeetingRegistration, MeetingStatus, Book, BookVote, BookRating, ReviewQuestion, MeetingReview, VotingPeriod, VotingMode, MeetingReviewQuestion, BookReview, AdminLog, Notification
from rest_framework import serializers
from .serializers import (
    UserSerializer, UserMeSerializer, UserMeetingSerializer, UserRegistrationSerializer, MyTokenObtainPairSerializer,
    MeetingSerializer, MeetingRegistrationSerializer, BookSerializer,
    BookVoteSerializer, BookRatingSerializer, ReviewQuestionSerializer, MeetingReviewSerializer, VotingPeriodSerializer,
    MeetingReviewQuestionSerializer, BookReviewSerializer, AdminLogSerializer, NotificationSerializer
)
import logging
# Настройка логирования
logger = logging.getLogger(__name__)


def safe_admin_log_target(value, max_length=200):
    if value is None:
        return ''
    if len(value) <= max_length:
        return value
    # Keep target field within DB limit to avoid DataError on long titles.
    return value[:max_length]


def get_user_participation_level(attended_completed_count):
    if attended_completed_count >= 12:
        return 'ambassador'
    if attended_completed_count >= 6:
        return 'active_reader'
    if attended_completed_count >= 3:
        return 'member'
    return 'newbie'


def is_effectively_completed(meeting):
    status_val = (meeting.status or '').strip().upper()
    return status_val == 'COMPLETED' or meeting.date <= timezone.now()


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
        if 'too similar' in lower:
            translated.append('Пароль слишком похож на данные пользователя.')
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


class RecommendationForMeView(APIView):
    permission_classes = [IsAuthenticated]

    # Пороги можно отдать в query params, но держим безопасные дефолты для старта.
    # Increased thresholds for small datasets to reduce noise from spurious patterns.
    DEFAULT_MIN_SUPPORT = 0.08
    DEFAULT_MIN_CONFIDENCE = 0.3
    DEFAULT_MIN_LIFT = 1.0
    DEFAULT_LIMIT = 10
    MAX_LIMIT = 20
    DEFAULT_TOP_K_NEIGHBORS = 10
    DEFAULT_APRIORI_WEIGHT = 0.6
    DEFAULT_COLLAB_WEIGHT = 0.4
    DEFAULT_MIN_HYBRID_SCORE = 0.0
    # Keep recommendations fresh after votes/ratings/reviews updates.
    # Set to 0 to disable payload cache. For modest traffic we enable a short
    # payload cache using Django's default cache (locmem by default) to reduce
    # repeated computation. TTL chosen as 60 seconds — short enough to remain
    # responsive to user actions but reduces load.
    DEFAULT_CACHE_TTL_SEC = 60
    DEFAULT_RULES_CACHE_TTL_SEC = 6 * 60 * 60
    WEIGHT_VOTE = 1.0
    WEIGHT_RATING_GREEN = 0.8
    WEIGHT_RATING_YELLOW = 0.5
    WEIGHT_BOOK_REVIEW = 0.7
    WEIGHT_MEETING_DISCUSSION = 0.4
    WEIGHT_REVIEW_WITH_GREEN = 1.2
    WEIGHT_REVIEW_WITH_YELLOW = 0.5
    WEIGHT_REVIEW_WITHOUT_RATING = 0.3
    NEGATIVE_REVIEW_PENALTY_FACTOR = 0.35
    DEFAULT_LANE_LIMIT = 5
    # Порог размера seed-сета, при котором применять ограничение apriori
    DEFAULT_APRIORI_SEED_SIZE_THRESHOLD = 15

    def _get_user_cache_bust_key(self, user_id):
        return f'reco:for-me:bust:{user_id}'

    def _get_user_cache_bust_version(self, user_id):
        cache_key = self._get_user_cache_bust_key(user_id)
        version = cache.get(cache_key)
        if version is None:
            cache.set(cache_key, 1, timeout=self.DEFAULT_CACHE_TTL_SEC)
            return 1
        try:
            return int(version)
        except (TypeError, ValueError):
            cache.set(cache_key, 1, timeout=self.DEFAULT_CACHE_TTL_SEC)
            return 1

    def bump_user_cache_bust_version(self, user_id):
        cache_key = self._get_user_cache_bust_key(user_id)
        try:
            cache.incr(cache_key)
        except Exception:
            current_version = self._get_user_cache_bust_version(user_id)
            cache.set(cache_key, current_version + 1, timeout=self.DEFAULT_CACHE_TTL_SEC)

    def _get_attended_discussed_book_ids(self, user_id):
        return set(
            MeetingRegistration.objects.filter(
                user_id=user_id,
                meeting__discussed_book__isnull=False,
                is_attended=True,
            ).filter(
                Q(meeting__status='COMPLETED') | Q(meeting__date__lte=timezone.now())
            ).values_list('meeting__discussed_book_id', flat=True)
        )

    def _build_user_signals(self):
        transactions = {}
        weighted_profiles = {}
        negative_review_rating_books = {}

        def add_item(uid, bid):
            if uid is None or bid is None:
                return
            transactions.setdefault(uid, set()).add(bid)

        def add_weight(uid, bid, weight):
            if uid is None or bid is None:
                return
            user_profile = weighted_profiles.setdefault(uid, {})
            user_profile[bid] = user_profile.get(bid, 0.0) + weight

        # 1) Голоса пользователя за книги
        for row in BookVote.objects.values('user_id', 'book_id'):
            add_item(row['user_id'], row['book_id'])
            add_weight(row['user_id'], row['book_id'], self.WEIGHT_VOTE)

        # 2) Рейтинги (берем позитивные/нейтральные сигналы)
        for row in BookRating.objects.filter(rating__in=['GREEN', 'YELLOW']).values('user_id', 'book_id', 'rating'):
            add_item(row['user_id'], row['book_id'])
            if row['rating'] == 'GREEN':
                add_weight(row['user_id'], row['book_id'], self.WEIGHT_RATING_GREEN)
            else:
                add_weight(row['user_id'], row['book_id'], self.WEIGHT_RATING_YELLOW)

        rating_map = {
            (row['user_id'], row['book_id']): row['rating']
            for row in BookRating.objects.values('user_id', 'book_id', 'rating')
        }

        # RED-рейтинг считаем явным негативным сигналом даже без отзыва.
        for row in BookRating.objects.filter(rating='RED').values('user_id', 'book_id'):
            negative_review_rating_books.setdefault(row['user_id'], set()).add(row['book_id'])

        # 3) Текстовые отзывы о книгах + связка с оценкой
        for row in BookReview.objects.values('user_id', 'book_id'):
            uid = row['user_id']
            bid = row['book_id']
            rating_value = rating_map.get((uid, bid))

            if rating_value == 'GREEN':
                add_item(uid, bid)
                add_weight(uid, bid, self.WEIGHT_REVIEW_WITH_GREEN)
            elif rating_value == 'YELLOW':
                add_item(uid, bid)
                add_weight(uid, bid, self.WEIGHT_REVIEW_WITH_YELLOW)
            elif rating_value == 'RED':
                # RED + отзыв трактуем как негативный сигнал: похожие книги отфильтруем ниже.
                negative_review_rating_books.setdefault(uid, set()).add(bid)
            else:
                add_item(uid, bid)
                add_weight(uid, bid, self.WEIGHT_REVIEW_WITHOUT_RATING)

        # 4) Встречи с привязкой к книге, которые пользователь посещал
        for row in MeetingRegistration.objects.filter(
            meeting__discussed_book__isnull=False,
            is_attended=True,
        ).filter(
            Q(meeting__status='COMPLETED') | Q(meeting__date__lte=timezone.now())
        ).values('user_id', 'meeting__discussed_book_id'):
            add_item(row['user_id'], row['meeting__discussed_book_id'])
            add_weight(row['user_id'], row['meeting__discussed_book_id'], self.WEIGHT_MEETING_DISCUSSION)

        return transactions, weighted_profiles, negative_review_rating_books

    def _get_rules_cache_version(self):
        vote_stats = BookVote.objects.aggregate(total=Count('id'), latest_id=Max('id'))
        rating_stats = BookRating.objects.aggregate(total=Count('id'), latest_id=Max('id'))
        review_stats = BookReview.objects.aggregate(total=Count('id'), latest_id=Max('id'))
        meeting_stats = MeetingRegistration.objects.filter(
            meeting__discussed_book__isnull=False,
            is_attended=True,
        ).filter(
            Q(meeting__status='COMPLETED') | Q(meeting__date__lte=timezone.now())
        ).aggregate(total=Count('id'), latest_id=Max('id'))

        return ':'.join([
            str(vote_stats.get('total') or 0),
            str(vote_stats.get('latest_id') or 0),
            str(rating_stats.get('total') or 0),
            str(rating_stats.get('latest_id') or 0),
            str(review_stats.get('total') or 0),
            str(review_stats.get('latest_id') or 0),
            str(meeting_stats.get('total') or 0),
            str(meeting_stats.get('latest_id') or 0),
        ])

    def _get_rules_cache_key(self, min_support, min_confidence, min_lift):
        return (
            'reco:rules:'
            f'{self._get_rules_cache_version()}:'
            f'{round(min_support, 4)}:{round(min_confidence, 4)}:{round(min_lift, 4)}'
        )

    def _get_cached_rules(self, transactions, min_support, min_confidence, min_lift):
        cache_key = self._get_rules_cache_key(min_support, min_confidence, min_lift)
        cached_rules = cache.get(cache_key)
        if cached_rules is not None:
            return cached_rules, 'hit'

        mined_rules = self._mine_rules_with_apriori(
            transactions=transactions,
            min_support=min_support,
            min_confidence=min_confidence,
            min_lift=min_lift,
        )
        cache.set(cache_key, mined_rules, timeout=self.DEFAULT_RULES_CACHE_TTL_SEC)
        return mined_rules, 'miss'

    def _mine_rules_with_apriori(self, transactions, min_support, min_confidence, min_lift):
        # Используем готовую библиотеку Apriori (association rules).
        tx = [tuple(sorted(items)) for items in transactions.values() if items]
        if not tx:
            return {}

        _, rules = apriori(
            tx,
            min_support=min_support,
            min_confidence=min_confidence,
            max_length=2,
        )

        out = {}
        for rule in rules:
            # Берем только правила A -> B с одним элементом слева и справа.
            if len(rule.lhs) != 1 or len(rule.rhs) != 1:
                continue
            if float(rule.lift) < min_lift:
                continue
            lhs_book_id = next(iter(rule.lhs))
            rhs_book_id = next(iter(rule.rhs))
            out.setdefault(lhs_book_id, []).append({
                'book_id': rhs_book_id,
                'confidence': float(rule.confidence),
                'lift': float(rule.lift),
                'support': float(rule.support),
            })
        return out

    def _fallback_content_based(self, user_seen_ids, limit, excluded_book_ids=None):
        # Fallback для холодного старта: похожесть по жанру/автору + качество.
        excluded_book_ids = excluded_book_ids or set()
        seen_books = Book.objects.filter(id__in=user_seen_ids)
        genres = set(seen_books.values_list('genre', flat=True))
        authors = set(seen_books.values_list('author', flat=True))

        if not genres and not authors:
            return self._fallback_popular_books(limit=limit, excluded_book_ids=excluded_book_ids)

        candidates = Book.objects.exclude(id__in=user_seen_ids).exclude(id__in=excluded_book_ids).filter(is_archived=False)
        if genres:
            candidates = candidates.filter(genre__in=genres) | Book.objects.exclude(id__in=user_seen_ids).exclude(id__in=excluded_book_ids).filter(author__in=authors, is_archived=False)
        elif authors:
            candidates = candidates.filter(author__in=authors)

        candidates = candidates.distinct().annotate(
            votes_count=Count('votes', distinct=True),
            avg_rating_num=Avg(
                Case(
                    When(book_ratings__rating='GREEN', then=5),
                    When(book_ratings__rating='YELLOW', then=3),
                    When(book_ratings__rating='RED', then=1),
                    output_field=FloatField()
                )
            )
        ).order_by('-avg_rating_num', '-votes_count', 'title')

        return candidates[:limit]

    def _fallback_popular_books(self, limit, excluded_book_ids=None):
        excluded_book_ids = excluded_book_ids or set()
        return Book.objects.exclude(id__in=excluded_book_ids).filter(is_archived=False).annotate(
            votes_count=Count('votes', distinct=True),
            avg_rating_num=Avg(
                Case(
                    When(book_ratings__rating='GREEN', then=5),
                    When(book_ratings__rating='YELLOW', then=3),
                    When(book_ratings__rating='RED', then=1),
                    output_field=FloatField()
                )
            )
        ).order_by('-avg_rating_num', '-votes_count', 'title')[:limit]

    def _fallback_repeat_books(self, user_seen_ids, limit, excluded_book_ids=None):
        excluded_book_ids = excluded_book_ids or set()
        repeat_ids = set(user_seen_ids) - set(excluded_book_ids)
        if not repeat_ids:
            return Book.objects.none()

        return Book.objects.filter(id__in=repeat_ids, is_archived=False).annotate(
            votes_count=Count('votes', distinct=True),
            avg_rating_num=Avg(
                Case(
                    When(book_ratings__rating='GREEN', then=5),
                    When(book_ratings__rating='YELLOW', then=3),
                    When(book_ratings__rating='RED', then=1),
                    output_field=FloatField()
                )
            )
        ).order_by('-avg_rating_num', '-votes_count', 'title')[:limit]

    def _normalize_score_map(self, score_map):
        if not score_map:
            return {}
        max_value = max(score_map.values())
        if max_value <= 0:
            return {k: 0.0 for k in score_map}
        return {k: (v / max_value) for k, v in score_map.items()}

    def _normalize_weights(self, apriori_weight, collab_weight):
        total = apriori_weight + collab_weight
        if total <= 0:
            return self.DEFAULT_APRIORI_WEIGHT, self.DEFAULT_COLLAB_WEIGHT
        return apriori_weight / total, collab_weight / total

    def _build_negative_penalties(self, rules, negative_book_ids):
        penalties = {}
        for source_book_id in negative_book_ids:
            for rec in rules.get(source_book_id, []):
                penalty = rec['confidence'] * rec['lift'] * self.NEGATIVE_REVIEW_PENALTY_FACTOR
                penalties[rec['book_id']] = penalties.get(rec['book_id'], 0.0) + penalty
        return penalties

    def _recommend_for_seed_group(
        self,
        request,
        rules,
        seed_book_ids,
        user_seed_weights,
        user_seen_ids,
        negative_penalties,
        limit,
        source_label,
        excluded_book_ids=None,
        allow_broader_fallback=False,
        allow_repeat=True,
    ):
        excluded_book_ids = excluded_book_ids or set()

        score_map = {}
        reasons_map = {}
        for seed_book_id in seed_book_ids:
            for rec in rules.get(seed_book_id, []):
                candidate_id = rec['book_id']
                if candidate_id in user_seen_ids or candidate_id in excluded_book_ids:
                    continue

                seed_weight = user_seed_weights.get(seed_book_id, 1.0)
                score = rec['confidence'] * rec['lift'] * seed_weight
                score_map[candidate_id] = score_map.get(candidate_id, 0.0) + score
                reasons_map.setdefault(candidate_id, []).append({
                    'source': source_label,
                    'from_book_id': seed_book_id,
                    'seed_weight': round(seed_weight, 3),
                    'confidence': round(rec['confidence'], 3),
                    'lift': round(rec['lift'], 3),
                    'support': round(rec.get('support', 0.0), 3),
                })

        for bid, penalty in negative_penalties.items():
            if bid not in score_map:
                continue
            score_map[bid] = max(0.0, score_map[bid] - penalty)
            reasons_map.setdefault(bid, []).append({
                'source': 'negative_feedback_penalty',
                'penalty': round(penalty, 3),
            })

        # Boost candidates that were recommended directly from this user's seed books.
        # This makes recommendations that come from the user's own choices rank higher
        # than fallback or incidental apriori hits. Boost factor is configurable
        # via `seed_boost` query param for quick experiments (default 1.5).
        try:
            seed_boost = float(request.query_params.get('seed_boost', 1.5))
        except Exception:
            seed_boost = 1.5
        seed_set = set(seed_book_ids or [])
        if seed_boost and seed_boost != 1.0 and seed_set:
            for cid in list(score_map.keys()):
                reasons = reasons_map.get(cid, [])
                if any(r.get('from_book_id') in seed_set for r in reasons if r.get('from_book_id') is not None):
                    old = score_map.get(cid, 0.0)
                    new = old * seed_boost
                    score_map[cid] = new
                    reasons_map.setdefault(cid, []).append({'source': 'seed_origin_boost', 'multiplier': round(seed_boost, 2)})

        ranked_ids = [bid for bid, _ in sorted(score_map.items(), key=lambda x: x[1], reverse=True)]
        selected_ids = ranked_ids[:limit]

        books_by_id = {
            b.id: b
            for b in Book.objects.filter(id__in=selected_ids, is_archived=False)
        }
        ordered_books = [books_by_id[bid] for bid in selected_ids if bid in books_by_id]
        serialized = BookSerializer(ordered_books, many=True, context={'request': request}).data

        normalized_apriori = self._normalize_score_map(score_map)
        enriched = []
        for item in serialized:
            bid = item['id']
            item['apriori_score'] = round(score_map.get(bid, 0.0), 3)
            item['collab_score'] = 0.0
            item['hybrid_score'] = round(score_map.get(bid, 0.0), 3)
            item['normalized_apriori_score'] = round(normalized_apriori.get(bid, 0.0), 3)
            item['normalized_collab_score'] = 0.0
            item['reasons'] = reasons_map.get(bid, [])[:3]
            # If any reason has 'from_book_id' it's an apriori-derived recommendation
            if any(r.get('from_book_id') is not None for r in item['reasons']):
                item['recommendation_type'] = 'apriori'
            else:
                # fallback reasons are handled later, but mark unknown for now
                item['recommendation_type'] = 'unknown'
            enriched.append(item)

        if len(enriched) < limit:
            needed = limit - len(enriched)
            fallback_excluded = set(user_seen_ids) | set(excluded_book_ids) | {item['id'] for item in enriched}
            fallback_books = self._fallback_content_based(
                user_seen_ids=seed_book_ids,
                limit=needed,
                excluded_book_ids=fallback_excluded,
            )
            fallback_data = BookSerializer(fallback_books, many=True, context={'request': request}).data
            # If seed-based fallback produced nothing, optionally try a broader
            # fallback based on the user's overall seen set to improve recall.
            if not fallback_data and allow_broader_fallback and seed_book_ids and set(seed_book_ids) != set(user_seen_ids):
                broader_books = self._fallback_content_based(
                    user_seen_ids=user_seen_ids,
                    limit=needed,
                    excluded_book_ids=fallback_excluded,
                )
                fallback_data = BookSerializer(broader_books, many=True, context={'request': request}).data
            if len(fallback_data) < needed:
                popular_books = self._fallback_popular_books(
                    limit=needed - len(fallback_data),
                    excluded_book_ids=fallback_excluded | {item['id'] for item in fallback_data},
                )
                popular_data = BookSerializer(popular_books, many=True, context={'request': request}).data
                for item in popular_data:
                    item['apriori_score'] = 0.0
                    item['collab_score'] = 0.0
                    item['hybrid_score'] = 0.0
                    item['normalized_apriori_score'] = 0.0
                    item['normalized_collab_score'] = 0.0
                    item['reasons'] = [{'source': 'fallback', 'type': 'popular'}]
                    penalty_value = negative_penalties.get(item['id'])
                    if penalty_value is not None:
                        item['reasons'].append({
                            'source': 'negative_feedback_penalty',
                            'penalty': round(penalty_value, 3),
                        })
                fallback_data.extend(popular_data)
            if len(fallback_data) < needed and allow_repeat:
                repeat_books = self._fallback_repeat_books(
                    user_seen_ids=user_seen_ids,
                    limit=needed - len(fallback_data),
                    excluded_book_ids={item['id'] for item in fallback_data},
                )
                repeat_data = BookSerializer(repeat_books, many=True, context={'request': request}).data
                for item in repeat_data:
                    item['apriori_score'] = 0.0
                    item['collab_score'] = 0.0
                    item['hybrid_score'] = 0.0
                    item['normalized_apriori_score'] = 0.0
                    item['normalized_collab_score'] = 0.0
                    item['reasons'] = [{'source': 'fallback', 'type': 'repeat'}]
                    penalty_value = negative_penalties.get(item['id'])
                    if penalty_value is not None:
                        item['reasons'].append({
                            'source': 'negative_feedback_penalty',
                            'penalty': round(penalty_value, 3),
                        })
                fallback_data.extend(repeat_data)

            for item in fallback_data:
                if not item.get('reasons'):
                    item['reasons'] = [{'source': 'fallback', 'type': source_label}]
                item['apriori_score'] = 0.0
                item['collab_score'] = 0.0
                item['hybrid_score'] = 0.0
                item['normalized_apriori_score'] = 0.0
                item['normalized_collab_score'] = 0.0
                penalty_value = negative_penalties.get(item['id'])
                if penalty_value is not None and not any(r.get('source') == 'negative_feedback_penalty' for r in item['reasons']):
                    item['reasons'].append({
                        'source': 'negative_feedback_penalty',
                        'penalty': round(penalty_value, 3),
                    })
                # set recommendation_type according to fallback subtype
                first = item['reasons'][0] if item['reasons'] else {}
                if first.get('source') == 'fallback':
                    t = first.get('type')
                    if t == 'popular':
                        item['recommendation_type'] = 'popular'
                    elif t == 'repeat':
                        item['recommendation_type'] = 'repeat'
                    else:
                        item['recommendation_type'] = 'content'
                else:
                    item['recommendation_type'] = 'unknown'
            enriched.extend(fallback_data)

        return enriched

    def get(self, request):
        try:
            min_support = float(request.query_params.get('min_support', self.DEFAULT_MIN_SUPPORT))
            min_confidence = float(request.query_params.get('min_confidence', self.DEFAULT_MIN_CONFIDENCE))
            min_lift = float(request.query_params.get('min_lift', self.DEFAULT_MIN_LIFT))
            limit = int(request.query_params.get('limit', self.DEFAULT_LIMIT))
            top_k_neighbors = int(request.query_params.get('top_k_neighbors', self.DEFAULT_TOP_K_NEIGHBORS))
            apriori_weight = float(request.query_params.get('apriori_weight', self.DEFAULT_APRIORI_WEIGHT))
            collab_weight = float(request.query_params.get('collab_weight', self.DEFAULT_COLLAB_WEIGHT))
            min_hybrid_score = float(request.query_params.get('min_hybrid_score', self.DEFAULT_MIN_HYBRID_SCORE))
            lane_limit = int(request.query_params.get('lane_limit', self.DEFAULT_LANE_LIMIT))
        except ValueError:
            return Response({'detail': 'Некорректные query-параметры.'}, status=status.HTTP_400_BAD_REQUEST)

        if limit < 1:
            limit = 1
        if limit > self.MAX_LIMIT:
            limit = self.MAX_LIMIT
        if lane_limit < 1:
            lane_limit = 1
        if lane_limit > 10:
            lane_limit = 10

        apriori_weight, collab_weight = self._normalize_weights(apriori_weight, collab_weight)

        # Админский аккаунт не получает персональные рекомендации.
        if is_admin_user(request.user):
            return Response(
                {'detail': 'Персональные рекомендации недоступны для администраторского аккаунта.'},
                status=status.HTTP_403_FORBIDDEN
            )

        cache_key = (
            f"reco:for-me:{request.user.id}:"
            f"{self._get_user_cache_bust_version(request.user.id)}:"
            f"{min_support}:{min_confidence}:{min_lift}:{limit}:{top_k_neighbors}:"
            f"{round(apriori_weight, 4)}:{round(collab_weight, 4)}:{min_hybrid_score}:{lane_limit}"
        )
        use_cache = self.DEFAULT_CACHE_TTL_SEC > 0
        debug_reco = settings.DEBUG and request.query_params.get('debug_reco', '0') in ['1', 'true', 'yes']
        try:
            allow_repeat = request.query_params.get('allow_repeat', '1') in ['1', 'true', 'yes']
        except Exception:
            allow_repeat = True
        if use_cache:
            cached_payload = cache.get(cache_key)
            if cached_payload is not None:
                if debug_reco:
                    cached_payload = dict(cached_payload)
                    cached_payload['debug'] = {'cache_status': 'hit'}
                return Response(cached_payload)

        transactions, weighted_profiles, negative_review_rating_books = self._build_user_signals()
        user_seen_ids = transactions.get(request.user.id, set())
        attended_discussed_ids = self._get_attended_discussed_book_ids(request.user.id)
        user_seen_ids = set(user_seen_ids) | attended_discussed_ids

        # Debug prints to help diagnose recommendation issues in tests
        try:
            tx_debug = {k: list(v) for k, v in transactions.items()}
            logger.debug("Recommendation debug: user_id=%s transactions=%s", request.user.id, tx_debug)
            logger.debug("attended_discussed_ids=%s user_seen_ids=%s", list(attended_discussed_ids), list(user_seen_ids))
            # debug_reco handled via payload['debug'] below
        except Exception:
            pass

        rules, rules_cache_status = self._get_cached_rules(
            transactions=transactions,
            min_support=min_support,
            min_confidence=min_confidence,
            min_lift=min_lift,
        )
        try:
            logger.debug("Apriori rules keys: %s", list(rules.keys()))
        except Exception:
            pass
        negative_source_ids = negative_review_rating_books.get(request.user.id, set())
        negative_penalties = self._build_negative_penalties(rules, negative_source_ids)

        # Precompute a simple neighbor-based book set for diagnostics and light fallback
        try:
            seed_set = set((user_rating_ids | user_vote_ids | user_meeting_ids))
            neighbor_uids = [uid for uid, items in transactions.items() if uid != request.user.id and seed_set & set(items)]
            neighbor_book_ids = set(
                MeetingRegistration.objects.filter(
                    user_id__in=neighbor_uids,
                    meeting__discussed_book__isnull=False,
                    is_attended=True,
                ).values_list('meeting__discussed_book_id', flat=True)
            )
        except Exception:
            neighbor_uids = []
            neighbor_book_ids = set()

        # Простая схема: три независимые ленты без collaborative filtering.
        user_vote_ids = set(BookVote.objects.filter(user_id=request.user.id).values_list('book_id', flat=True))
        user_rating_ids = set(
            BookRating.objects.filter(user_id=request.user.id, rating__in=['GREEN', 'YELLOW']).values_list('book_id', flat=True)
        )
        user_meeting_ids = set(attended_discussed_ids)

        # Simplified: combine all seed books and recommend top 10 books
        all_seeds = (user_rating_ids | user_vote_ids | user_meeting_ids) - set(negative_source_ids)

        # Нормализуем веса seed-книг пользователя, чтобы GREEN/YELLOW давали
        # разный вклад в итоговый ранжирующий score.
        raw_seed_profile = weighted_profiles.get(request.user.id, {})
        raw_seed_weights = {bid: float(raw_seed_profile.get(bid, 0.0)) for bid in all_seeds}
        max_seed_weight = max(raw_seed_weights.values()) if raw_seed_weights else 0.0
        if max_seed_weight > 0:
            user_seed_weights = {
                bid: (raw_seed_weights.get(bid, 0.0) / max_seed_weight) if raw_seed_weights.get(bid, 0.0) > 0 else 1.0
                for bid in all_seeds
            }
        else:
            user_seed_weights = {bid: 1.0 for bid in all_seeds}

        seed_counts = {
            'votes': len(user_vote_ids),
            'ratings': len(user_rating_ids),
            'meetings': len(user_meeting_ids),
            'seen_total': len(user_seen_ids),
            'all_seeds': len(all_seeds),
            'negative_seeds_excluded': len(negative_source_ids),
        }

        # Diagnostics: how many rules were mined, and simple neighbor count (users with overlap)
        try:
            rules_count = sum(len(v) for v in rules.values())
        except Exception:
            rules_count = 0

        try:
            user_seeds = set(all_seeds)
            neighbors_count = 0
            for uid, items in transactions.items():
                if uid == request.user.id:
                    continue
                if user_seeds & set(items):
                    neighbors_count += 1
        except Exception:
            neighbors_count = 0

        # Decide strategy: use apriori when user has enough signals, otherwise content-first
        interactions = seed_counts.get('votes', 0) + seed_counts.get('ratings', 0) + seed_counts.get('meetings', 0)
        use_apriori = False
        if interactions >= 5 and seed_counts.get('all_seeds', 0) >= 3 and rules_count >= 3:
            use_apriori = True

        recommendations = []
        used_fallback = False
        chosen_strategy = 'apriori' if use_apriori else 'content_first'

        if use_apriori:
            excluded_book_ids = set(user_seen_ids) | set(negative_source_ids)
            recommendations = self._recommend_for_seed_group(
                request=request,
                rules=rules,
                seed_book_ids=all_seeds,
                user_seed_weights=user_seed_weights,
                user_seen_ids=user_seen_ids,
                negative_penalties=negative_penalties,
                limit=limit,
                #limit=10,  # Return up to 10 best recommendations
                source_label='user_activity',
                excluded_book_ids=excluded_book_ids,
                allow_broader_fallback=True,
                allow_repeat=allow_repeat,
            )
            # If apriori produced too few personalized items, try relaxed thresholds once
            apriori_count = sum(1 for r in recommendations if r.get('recommendation_type') == 'apriori')
            relaxed_used = False
            if apriori_count < 3:
                try:
                    relaxed_support = float(request.query_params.get('relaxed_min_support', 0.05))
                    relaxed_confidence = float(request.query_params.get('relaxed_min_confidence', 0.2))
                except Exception:
                    relaxed_support = 0.05
                    relaxed_confidence = 0.2

                # Only attempt if relaxed thresholds are actually more permissive
                if relaxed_support < min_support or relaxed_confidence < min_confidence:
                    new_rules, new_rules_status = self._get_cached_rules(
                        transactions=transactions,
                        min_support=relaxed_support,
                        min_confidence=relaxed_confidence,
                        min_lift=min_lift,
                    )
                    # Recompute penalties and recommendations with relaxed rules
                    new_negative_penalties = self._build_negative_penalties(new_rules, negative_source_ids)
                    new_recommendations = self._recommend_for_seed_group(
                        request=request,
                        rules=new_rules,
                        seed_book_ids=all_seeds,
                        user_seed_weights=user_seed_weights,
                        user_seen_ids=user_seen_ids,
                        negative_penalties=new_negative_penalties,
                        limit=limit,
                        source_label='user_activity',
                        excluded_book_ids=excluded_book_ids,
                        allow_broader_fallback=True,
                        allow_repeat=allow_repeat,
                    )
                    # If relaxed produced more apriori items, use it
                    new_apriori_count = sum(1 for r in new_recommendations if r.get('recommendation_type') == 'apriori')
                    if new_apriori_count > apriori_count:
                        recommendations = new_recommendations
                        rules = new_rules
                        negative_penalties = new_negative_penalties
                        rules_cache_status = f"{rules_cache_status}->relaxed"
                        rules_count = sum(len(v) for v in rules.values())
                        relaxed_used = True
        else:
            # Content-first flow: seed-based content fallback, then broader, then popular, then repeat
            fallback_excluded = set(user_seen_ids) | set(negative_source_ids)
            fallback_data = []

            # 1) Пробуем content-based по seed-книгам
            if all_seeds:
                fallback_books = self._fallback_content_based(
                    user_seen_ids=all_seeds,
                    limit=limit,
                    excluded_book_ids=fallback_excluded,
                )
                fallback_data = BookSerializer(fallback_books, many=True, context={'request': request}).data

            # 2) Если не хватает, пробуем более широкий content-based (по всем seen)
            if len(fallback_data) < limit and set(all_seeds) != set(user_seen_ids):
                broader_books = self._fallback_content_based(
                    user_seen_ids=user_seen_ids,
                    limit=limit - len(fallback_data),
                    excluded_book_ids=fallback_excluded | {item['id'] for item in fallback_data},
                )
                broader_data = BookSerializer(broader_books, many=True, context={'request': request}).data
                fallback_data.extend(broader_data)

            # 3) Если всё ещё не хватает — добиваем популярными книгами (без neighbour_used)
            if len(fallback_data) < limit:
                needed = limit - len(fallback_data)
                popular_books = self._fallback_popular_books(
                    limit=needed,
                    excluded_book_ids=fallback_excluded | {item['id'] for item in fallback_data},
                )
                popular_data = BookSerializer(popular_books, many=True, context={'request': request}).data
                for item in popular_data:
                    item['reasons'] = [{'source': 'fallback', 'type': 'popular'}]
                    item['recommendation_type'] = 'popular'
                    # добавим пенальти, если нужно
                    penalty_value = negative_penalties.get(item['id'])
                    if penalty_value is not None:
                        item['reasons'].append({
                            'source': 'negative_feedback_penalty',
                            'penalty': round(penalty_value, 3),
                        })
                fallback_data.extend(popular_data)

            # 4) Если всё ещё не хватает и разрешено повторять прочитанное
            if len(fallback_data) < limit and allow_repeat:
                needed = limit - len(fallback_data)
                repeat_books = self._fallback_repeat_books(
                    user_seen_ids=user_seen_ids,
                    limit=needed,
                    excluded_book_ids={item['id'] for item in fallback_data},
                )
                repeat_data = BookSerializer(repeat_books, many=True, context={'request': request}).data
                for item in repeat_data:
                    item['reasons'] = [{'source': 'fallback', 'type': 'repeat'}]
                    item['recommendation_type'] = 'repeat'
                    penalty_value = negative_penalties.get(item['id'])
                    if penalty_value is not None:
                        item['reasons'].append({
                            'source': 'negative_feedback_penalty',
                            'penalty': round(penalty_value, 3),
                        })
                fallback_data.extend(repeat_data)

            # Нормализация полей (заполняем нулями)
            for item in fallback_data:
                item.setdefault('apriori_score', 0.0)
                item.setdefault('collab_score', 0.0)
                item.setdefault('hybrid_score', 0.0)
                item.setdefault('normalized_apriori_score', 0.0)
                item.setdefault('normalized_collab_score', 0.0)
                if 'recommendation_type' not in item:
                    item['recommendation_type'] = 'content'

            recommendations = fallback_data[:limit]   # обрезаем до limit
            '''
            # Content-first flow: try seed-based content fallback, then neighbor-collab, broader, then popular, then repeat
            fallback_excluded = set(user_seen_ids) | set(negative_source_ids)
            fallback_books = self._fallback_content_based(
                user_seen_ids=all_seeds,
                limit=limit,
                excluded_book_ids=fallback_excluded,
            )
            fallback_data = BookSerializer(fallback_books, many=True, context={'request': request}).data
            # If content-based fallback didn't return enough, try a lightweight neighbor-collab
            # — recommend books that users with overlapping seen sets attended.
            neighbor_used = False
            if (not fallback_data) or len(fallback_data) < limit:
                try:
                    # find neighbor user ids who share at least one seed book
                    seed_set = set(all_seeds)
                    neighbor_uids = [uid for uid, items in transactions.items() if uid != request.user.id and seed_set & set(items)]
                    if neighbor_uids:
                        neighbor_book_ids = set(
                            MeetingRegistration.objects.filter(
                                user_id__in=neighbor_uids,
                                meeting__discussed_book__isnull=False,
                                is_attended=True,
                            ).values_list('meeting__discussed_book_id', flat=True)
                        ) - set(user_seen_ids) - set(negative_source_ids)
                        if neighbor_book_ids:
                            neighbor_books = Book.objects.filter(id__in=neighbor_book_ids, is_archived=False)[: (limit - len(fallback_data))]
                            neighbor_data = BookSerializer(neighbor_books, many=True, context={'request': request}).data
                            neighbor_used = True
                            for item in neighbor_data:
                                item['reasons'] = [{'source': 'collab', 'type': 'neighbor_attended'}]
                                item['recommendation_type'] = 'collab'
                            fallback_data = list(fallback_data) + neighbor_data
                except Exception:
                    logger.exception('Error while computing neighbor-collab fallback')
            # If seed-based fallback produced nothing, optionally try a broader
            # fallback based on the user's overall seen set to improve recall.
            if not fallback_data and set(all_seeds) != set(user_seen_ids):
                broader_books = self._fallback_content_based(
                    user_seen_ids=user_seen_ids,
                    limit=limit,
                    excluded_book_ids=fallback_excluded,
                )
                fallback_data = BookSerializer(broader_books, many=True, context={'request': request}).data

            # If not enough, add popular
            if len(fallback_data) < limit:
                popular_books = None
                # If neighbor collab already provided items, prefer those over global populars.
                if not neighbor_used:
                    popular_books = self._fallback_popular_books(limit=limit - len(fallback_data), excluded_book_ids=fallback_excluded | {item['id'] for item in fallback_data})
                if popular_books:
                    popular_data = BookSerializer(popular_books, many=True, context={'request': request}).data
                    for item in popular_data:
                        item['reasons'] = [{'source': 'fallback', 'type': 'popular'}]
                        item['recommendation_type'] = 'popular'
                    fallback_data.extend(popular_data)

            # If still not enough and repeat allowed
            if len(fallback_data) < limit and allow_repeat:
                repeat_books = self._fallback_repeat_books(user_seen_ids=user_seen_ids, limit=limit - len(fallback_data), excluded_book_ids={item['id'] for item in fallback_data})
                repeat_data = BookSerializer(repeat_books, many=True, context={'request': request}).data
                for item in repeat_data:
                    item['reasons'] = [{'source': 'fallback', 'type': 'repeat'}]
                    item['recommendation_type'] = 'repeat'
                fallback_data.extend(repeat_data)

            # Normalize and ensure fields
            for item in fallback_data:
                item['apriori_score'] = 0.0
                item['collab_score'] = 0.0
                item['hybrid_score'] = 0.0
                item['normalized_apriori_score'] = 0.0
                item['normalized_collab_score'] = 0.0
                if 'recommendation_type' not in item:
                    item['recommendation_type'] = 'content'

            recommendations = fallback_data[:limit]
        '''
        used_fallback = any(
            reason.get('source') == 'fallback' 
            for item in recommendations 
            for reason in item.get('reasons', [])
        )
        
        # Remove service-only metadata from API response unless debug_reco enabled.
        # This keeps payload smaller for normal client usage while preserving
        # full diagnostics when `debug_reco` is requested.
        '''if not debug_reco:
            _meta_keys = {
                'apriori_score', 'collab_score', 'hybrid_score',
                'normalized_apriori_score', 'normalized_collab_score',
                'reasons', 'recommendation_type'
            }
            for _itm in recommendations:
                for _k in _meta_keys:
                    _itm.pop(_k, None)'''

        payload = {
            'count': len(recommendations),
            'used_fallback': used_fallback,
            'params': {
                'min_support': min_support,
                'min_confidence': min_confidence,
                'min_lift': min_lift,
                'top_k_neighbors': top_k_neighbors,
                'apriori_weight': apriori_weight,
                'collab_weight': 0.0,
                'min_hybrid_score': min_hybrid_score,
                'limit': limit,
                'lane_limit': lane_limit,
            },
            'neighbors_used': [],
            # Simplified: single combined recommendations list
            'recommendations': recommendations,
        }
        if debug_reco:
            payload['debug'] = {
                'cache_status': 'miss' if use_cache else 'disabled',
                'rules_cache_status': rules_cache_status,
                'seed_counts': seed_counts,
                'rules_count': rules_count,
                'neighbors_count': neighbors_count,
                'allow_repeat': allow_repeat,
                'strategy': chosen_strategy,
            }
            # Add deeper diagnostics when requested
            # keep debug payload minimal and stable
        if use_cache:
            cache.set(cache_key, payload, timeout=self.DEFAULT_CACHE_TTL_SEC)
        return Response(payload)

def is_admin_user(user):
    return user.is_authenticated and (user.role == 'admin' or user.is_superuser)


def build_voting_period_stats(period_id=None):
    today = timezone.now()
    active_period = None

    if period_id:
        try:
            active_period = VotingPeriod.objects.get(id=int(period_id))
        except (VotingPeriod.DoesNotExist, ValueError):
            raise ValidationError("Период голосования не найден.")
    else:
        active_period = (
            VotingPeriod.objects.filter(votes__isnull=False)
            .distinct()
            .order_by('-end_date', '-start_date')
            .first()
        )
        if not active_period:
            active_period = VotingPeriod.objects.filter(
                start_date__lte=today,
                end_date__gte=today,
                is_active=True
            ).first()

    if not active_period:
        return None

    votes_qs = BookVote.objects.filter(voting_period=active_period)
    total_votes = votes_qs.count()

    books = list(
        votes_qs.values('book__id', 'book__title', 'book__cover').annotate(votes_count=Count('id')).order_by('-votes_count')
    )

    for b in books:
        b['percent'] = round((b['votes_count'] / total_votes * 100) if total_votes > 0 else 0, 1)

    return {
        'voting_period_id': active_period.id,
        'start_date': active_period.start_date,
        'end_date': active_period.end_date,
        'total_votes': total_votes,
        'books': books,
    }

class IsAdmin(permissions.BasePermission):
    #разрешение: доступ только для пользователей с role='admin'.
    #используется для опасных действий (удаление, смена ролей и т.д.).
    def has_permission(self, request, view):#главный метод, который дрф вызывает для каждого запроса (self - экземпляр класса request - объект запроса(юзер, дата метод итп) wiew -объект вью, контекст)
        #проверяем пользователь залогинен, если нет - сразу запрещаем всё
        return is_admin_user(request.user)#ключевая проверка: если пользователь админ или сууперюзер


class IsAdminOrReadOnly(permissions.BasePermission):
    #разрешение:- GET, HEAD, OPTIONS — всем авторизованным пользователям
    #- POST, PATCH, DELETE — только админам (role='admin')
    def has_permission(self, request, view):
        #SAFE_METHODS — это GET, HEAD, OPTIONS (только чтение)
        if request.method in permissions.SAFE_METHODS:#проверяем, если запрос только на чтение
            return request.user.is_authenticated#то достаточно быть просто авторизованным

        #для изменения — только админ
        return is_admin_user(request.user)


class IsAuthenticatedReadOnly(permissions.BasePermission):
    #разрешение: GET/HEAD/OPTIONS — любому авторизованному,
    #изменения через API запрещены (вне рамок API).
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return request.user.is_authenticated
        return False


class IsAdminReadOnly(permissions.BasePermission):
    # Чтение банка вопросов доступно админу,
    # изменения банка вопросов через API запрещены.
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return is_admin_user(request.user)
        return False

# Кастомная пагинация
class StandardResultsSetPagination(PageNumberPagination):
    page_size = 15  # Книг на странице
    page_size_query_param = 'page_size'
    max_page_size = 100

# Кастомное разрешение: админ или сам пользователь(сейчас не используется, тк в userviewset есть более сильная защита)
'''
class IsAdminOrSelf(permissions.BasePermission):#Проверка прав доступа к, например, профилю пользователя
    def has_object_permission(self, request, view, obj):
        return request.user.is_staff or obj == request.user#Если пользователь админ возвращаем True'''


# ViewSet для пользователей (список, профиль)
class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    #указываем, какие permissions использовать по умолчанию для всего ViewSet
    permission_classes = [IsAdminOrReadOnly]  #чтение всем, изменение только админам

    def get_queryset(self):#админ видит всех, но может исключать забаненных
        if is_admin_user(self.request.user):
            queryset = User.objects.filter(role='user', is_superuser=False)
            exclude_banned = self.request.query_params.get('exclude_banned')
            if exclude_banned and exclude_banned.lower() in ['true', '1', 'yes']:
                queryset = queryset.filter(is_banned=False)
            search = (self.request.query_params.get('search') or '').strip()
            if search:
                queryset = queryset.filter(
                    Q(username__icontains=search)
                    | Q(email__icontains=search)
                    | Q(first_name__icontains=search)
                    | Q(last_name__icontains=search)
                )
            return queryset.order_by('id')
        return User.objects.filter(id=self.request.user.id).order_by('id')
    
    def create(self, request, *args, **kwargs):
        #cоздание пользователей только через /api/register/
        return Response(
            {'detail': 'Создание пользователей через этот эндпоинт запрещено. Используйте /api/register/.'},
            status=status.HTTP_403_FORBIDDEN
        )

    #action для получения своего профиля (расширяем существующий me)
    @action(detail=False, methods=['get'], permission_classes=[permissions.IsAuthenticated])#@ - декоратор, detail = False -не требует id в URL, для текущего пользователя а не для /users/{id}/me/, только гет-запрос и вызвать могут только залогиненные(не гость)
    def me(self, request):
        serializer = UserSerializer(request.user)
        data = dict(serializer.data)

        # Статусы участия применяются только к обычным пользователям.
        if request.user.role == 'user':
            attended_completed_count = MeetingRegistration.objects.filter(
                user=request.user,
                is_attended=True,
            ).filter(
                Q(meeting__status='COMPLETED') | Q(meeting__date__lte=timezone.now())
            ).count()
            level_code = get_user_participation_level(attended_completed_count)
            level_titles = {
                'newbie': 'Новичок',
                'member': 'Участник',
                'active_reader': 'Активный читатель',
                'ambassador': 'Амбассадор клуба',
            }
            data['attended_completed_count'] = attended_completed_count
            data['participation_level'] = {
                'code': level_code,
                'title': level_titles[level_code],
            }
        return Response(data)

    #action для обновления своего профиля (расширяем существующий me)
    @action(detail=False, methods=['patch'], permission_classes=[IsAuthenticated])
    def me_update(self, request):
        serializer = UserMeSerializer(request.user, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)

        changed_fields = []
        for field, value in serializer.validated_data.items():
            if field in {'password', 'password_confirm'}:
                changed_fields.append(field)
                continue
            if getattr(request.user, field) != value:
                changed_fields.append(field)

        if not changed_fields:
            payload = dict(UserMeSerializer(request.user).data)
            payload['detail'] = 'Данные не изменились.'
            return Response(payload, status=status.HTTP_200_OK)

        serializer.save()
        return Response(serializer.data)

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def meetings(self, request):
        # история встреч, на которые пользователь был записан 
        meeting_ids = MeetingRegistration.objects.filter(user=request.user).values_list('meeting_id', flat=True)
        meetings = Meeting.objects.filter(id__in=meeting_ids).order_by('-date')
        serializer = UserMeetingSerializer(meetings, many=True)
        return Response(serializer.data)

    @action(
        detail=False,
        methods=['get'],
        permission_classes=[IsAuthenticated],
        url_path=r'meetings/(?P<meeting_id>[^/.]+)',
        url_name='meeting-detail',
    )
    def meeting_detail(self, request, meeting_id=None):
        own_meeting_ids = MeetingRegistration.objects.filter(
            user=request.user
        ).values_list('meeting_id', flat=True)
        meeting = get_object_or_404(Meeting.objects.filter(id__in=own_meeting_ids), pk=meeting_id)
        serializer = UserMeetingSerializer(meeting)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def registrations(self, request):
        # все регистрации пользователя на встречи, включая отмены
        regs = MeetingRegistration.objects.filter(user=request.user).select_related('meeting', 'meeting__discussed_book').order_by('-meeting__date')

        def build_poster_url(meeting_obj):
            if not meeting_obj.poster:
                return None
            try:
                return request.build_absolute_uri(meeting_obj.poster.url)
            except Exception:
                return None

        data = [
            {
                "id": r.id,
                "registration_id": r.id,
                "meeting_id": r.meeting.id,
                "phone_number": r.phone_number,
                "registered_at": r.registered_at,
                "title": r.meeting.title,
                "date": r.meeting.date,
                "status": r.meeting.status,
                "location": r.meeting.location,
                "poster": build_poster_url(r.meeting),
                "type": r.meeting.type,
                "is_attended": r.is_attended,
                "attended_at": r.attended_at,
                "date_in_past": r.meeting.date <= timezone.now(),
                "meeting": {
                    "id": r.meeting.id,
                    "title": r.meeting.title,
                    "date": r.meeting.date,
                    "location": r.meeting.location,
                    "status": r.meeting.status,
                    "poster": build_poster_url(r.meeting),
                    "date_in_past": r.meeting.date <= timezone.now(),
                    "type": r.meeting.type,
                }
            }
            for r in regs
        ]
        return Response(data)

    @action(
        detail=False,
        methods=['get'],
        permission_classes=[IsAuthenticated],
        url_path=r'registrations/(?P<registration_id>[^/.]+)',
        url_name='registration-detail',
    )
    def registration_detail(self, request, registration_id=None):
        registration = get_object_or_404(
            MeetingRegistration.objects.select_related('meeting').filter(user=request.user),
            pk=registration_id,
        )
        return Response(
            {
                "registration_id": registration.id,
                "meeting_id": registration.meeting.id,
                "title": registration.meeting.title,
                "date": registration.meeting.date,
                "status": registration.meeting.status,
                "is_attended": registration.is_attended,
                "attended_at": registration.attended_at,
            }
        )

    #action для смены роли другого пользователя (только админ!)
    @action(detail=True, methods=['patch'], permission_classes=[IsAdmin])#тут наоборот требуем id в URL, чтобы понимать над каким пользователем будем производить действие, только PATCH запрос, тк изменяем значение поля role, а не создаём новую роль, только админ может вызвать
    def set_role(self, request, pk=None):# pk=None - id пользователя из URL, DRF сам подставит
        # Управление ролями вынесено из API.
        return Response(
            {'detail': 'Смена ролей доступна только через админ-панель Django.'},
            status=status.HTTP_403_FORBIDDEN
        )

    #аction для soft-delete пользователя (только админ!)
    @action(detail=True, methods=['patch'], permission_classes=[IsAdmin])
    def delete_user(self, request, pk=None):
        # Управление удалением пользователей вынесено из API.
        return Response(
            {'detail': 'Удаление пользователей доступно только через админ-панель Django.'},
            status=status.HTTP_403_FORBIDDEN
        )
    
    @action(detail=True, methods=['patch'], permission_classes=[IsAdmin])
    def ban(self, request, pk=None):
        """
        Админ банит или разбанивает пользователя.
        - Нельзя банить себя.
        - Нельзя банить системные аккаунты.
        - Если is_banned не передан, флаг переключается (toggle).
        """
        user = self.get_object()

        if user == request.user:
            return Response({'detail': 'Нельзя забанить самого себя.'},
                            status=status.HTTP_403_FORBIDDEN)

        if user.is_superuser:
            return Response({'detail': 'Нельзя банить системные аккаунты.'},
                            status=status.HTTP_403_FORBIDDEN)
        
        if user.role == 'admin':
            return Response({'detail': 'Нельзя банить администраторов. Для админов доступен только Django-админ.'},
                            status=status.HTTP_403_FORBIDDEN)

        # Читаем желаемое состояние из тела запроса
        is_banned_param = request.data.get('is_banned', None)

        if is_banned_param is None:
            # Если не передано явно — просто переключаем
            new_is_banned = not user.is_banned
        else:
            # Преобразуем строку/булево к bool
            if isinstance(is_banned_param, bool):
                new_is_banned = is_banned_param
            else:
                new_is_banned = str(is_banned_param).lower() in ['true', '1', 'yes']

        old_is_banned = user.is_banned
        user.is_banned = new_is_banned
        # Опционально синхронизируем is_active, чтобы забаненный не был активным
        user.is_active = not user.is_banned
        user.save()

        if new_is_banned:
            now = timezone.now()
            removed_registrations = MeetingRegistration.objects.filter(
                user=user,
                meeting__status='UPCOMING',
                meeting__date__gt=now,
            ).count()
            MeetingRegistration.objects.filter(
                user=user,
                meeting__status='UPCOMING',
                meeting__date__gt=now,
            ).delete()
        else:
            removed_registrations = 0

        # Логируем действие
        AdminLog.objects.create(
            admin=request.user,
            action='ban_user' if new_is_banned else 'unban_user',
            target=f"Пользователь {user.username} (id={user.id})",
            target_id=user.id,
            details=f"is_banned: {old_is_banned} → {new_is_banned}; removed_future_registrations={removed_registrations}"
        )

        status_text = 'забанен' if new_is_banned else 'разбанен'
        return Response({'detail': f'Пользователь {status_text}.'})
    
    def update(self, request, *args, **kwargs):
        return Response(
            {'detail': 'Редактирование пользователей доступно только через админ-панель Django.'},
            status=status.HTTP_403_FORBIDDEN
        )

    def partial_update(self, request, *args, **kwargs):
        return Response(
            {'detail': 'Редактирование пользователей доступно только через админ-панель Django.'},
            status=status.HTTP_403_FORBIDDEN
        )

    @action(detail=True, methods=['get'], permission_classes=[IsAdmin], url_path='admin-registrations')
    def admin_registrations(self, request, pk=None):
        """
        Админ может смотреть регистрации любого пользователя.
        Возвращает paginated результат с встречами пользователя.
        """
        user = get_object_or_404(User.objects.all(), pk=pk)
        registrations = MeetingRegistration.objects.filter(user=user).select_related('meeting', 'meeting__discussed_book')
        
        # Пагинируем результат
        paginator = PageNumberPagination()
        paginated_regs = paginator.paginate_queryset(registrations, request)
        
        data = []
        for r in (paginated_regs or registrations):
            meeting_data = {
                "id": r.meeting.id,
                "title": r.meeting.title,
                "date": r.meeting.date,
                "location": r.meeting.location,
                "status": r.meeting.status,
            }
            if r.meeting.discussed_book:
                meeting_data["discussed_book"] = {
                    "title": r.meeting.discussed_book.title,
                    "author": r.meeting.discussed_book.author,
                }
            
            data.append({
                "id": r.id,
                "phone_number": r.phone_number,
                "registered_at": r.registered_at,
                "meeting": meeting_data,
                "is_attended": r.is_attended,
                "attended_at": r.attended_at,
            })
        
        return paginator.get_paginated_response(data) if paginated_regs else Response(data)

    def destroy(self, request, *args, **kwargs):
        return Response(
            {'detail': 'Удаление пользователей доступно только через админ-панель Django.'},
            status=status.HTTP_403_FORBIDDEN
        )
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
            return Response({'detail': 'Псевдоним обязателен'}, status=status.HTTP_400_BAD_REQUEST)
        if User.objects.filter(username=username).exists():
            return Response({'detail': 'Псевдоним уже занят'}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'detail': 'Псевдоним доступен'}, status=status.HTTP_200_OK)

# View для проверки пароля
class CheckPasswordView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        password = request.data.get('password')
        if not password:
            return Response({'detail': 'Пароль обязателен'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            user = request.user if request.user and request.user.is_authenticated else None
            validate_password(password, user)
        except DjangoValidationError as exc:
            translated = translate_password_errors(list(exc.messages))
            return Response({'detail': '; '.join(translated)}, status=status.HTTP_400_BAD_REQUEST)
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
    permission_classes = [IsAuthenticatedReadOnly]
    pagination_class = StandardResultsSetPagination

    def perform_create(self, serializer):
        try:
            instance = serializer.save()
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, 'message_dict') else exc.messages)
        AdminLog.objects.create(
            admin=self.request.user,
            action='создание книги',
            target=safe_admin_log_target(f"Книга '{instance.title}' (id={instance.id})"),
            target_id=instance.id
        )

    def perform_update(self, serializer):
        try:
            instance = serializer.save()
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, 'message_dict') else exc.messages)
        AdminLog.objects.create(
            admin=self.request.user,
            action='редактирование книги',
            target=safe_admin_log_target(f"Книга '{instance.title}' (id={instance.id})"),
            target_id=instance.id
        )

    def perform_destroy(self, instance):
        if instance.is_voting_candidate:
            raise ValidationError(
                {'detail': 'Нельзя архивировать книгу, пока она участвует в голосовании. Сначала снимите её с голосования.'}
            )

        old_archived = instance.is_archived
        instance.is_archived = True
        instance.is_voting_candidate = False
        try:
            instance.save(update_fields=['is_archived', 'is_voting_candidate'])
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, 'message_dict') else exc.messages)
        AdminLog.objects.create(
            admin=self.request.user,
            action='архивация книги',
            target=safe_admin_log_target(f"Книга '{instance.title}' (id={instance.id})"),
            target_id=instance.id,
            details=f"is_archived: {old_archived} -> True"
        )

    def get_queryset(self):
        # Архивные книги скрыты в API: работа с архивом только через Django admin.
        base_queryset = self.queryset.filter(is_archived=False)

        queryset = base_queryset.annotate(
            average_rating=Avg(Case(
                When(book_ratings__rating='GREEN', then=5),
                When(book_ratings__rating='YELLOW', then=3),
                When(book_ratings__rating='RED', then=1),
                output_field=FloatField()
            ))
        )
        params = {
            'genre': self.request.query_params.get('genre'),
            'rating_color': self.request.query_params.get('rating_color'),
            'min_rating': self.request.query_params.get('min_rating'),
            'for_voting': self.request.query_params.get('for_voting'),
            'is_discussed': self.request.query_params.get('is_discussed'),
            'search': self.request.query_params.get('search'),
        }
        logger.debug(f"Applying filters: {params}")

        search = (params['search'] or '').strip()
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search)
                | Q(author__icontains=search)
                | Q(genre__icontains=search)
            )

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
            queryset = queryset.filter(is_discussed=False, is_voting_candidate=True).order_by('id').distinct()[:8]
            logger.debug(f"Books for voting: {list(queryset.values('id', 'title'))}")

        else:
            queryset = queryset.order_by('id').distinct()

        logger.debug(f"Filtered queryset count: {queryset.count()}")
        return queryset

# ViewSet для работы со встречами (список, создание, обновление, удаление).
class MeetingViewSet(viewsets.ModelViewSet):
    queryset = Meeting.objects.all()
    serializer_class = MeetingSerializer
    permission_classes = [IsAdminOrReadOnly]

    def perform_create(self, serializer):#вызывается после валидации данных, но перед сохранением в базу дабы избежать попадание мусора
        instance = serializer.save()#сохраняем новый объект в базу и возвращаем его
        AdminLog.objects.create(#создаём новую запись в модели админ лог
            admin=self.request.user,#какой из админов
            action='создание встречи',#тип действия
            target=safe_admin_log_target(f"Встреча '{instance.title}' (id={instance.id})"),#описание объекта
            target_id=instance.id#числовой айди встречи (для дальнейшей кликабельности)
        )

    def perform_update(self, serializer):
        instance = serializer.instance
        changed_fields = []
        changed_values = {}
        previous_status = instance.status

        # Prevent admins from linking an archived book to a meeting via either
        # `discussed_book` or `discussed_book_id` in request data.
        try:
            req_data = self.request.data or {}
            book_id_candidate = None
            if 'discussed_book' in req_data:
                try:
                    book_id_candidate = int(req_data.get('discussed_book'))
                except Exception:
                    book_id_candidate = None
            if 'discussed_book_id' in req_data and not book_id_candidate:
                try:
                    book_id_candidate = int(req_data.get('discussed_book_id'))
                except Exception:
                    book_id_candidate = None
            if book_id_candidate:
                from .models import Book
                if Book.objects.filter(id=book_id_candidate, is_archived=True).exists():
                    raise ValidationError({'discussed_book': 'Нельзя привязать архивную книгу к встрече.'})
        except ValidationError:
            raise
        except Exception:
            # ignore parse errors and fall through to normal validation
            pass

        for field, new_value in serializer.validated_data.items():
            old_value = getattr(instance, field)

            if hasattr(old_value, 'pk'):
                old_value = old_value.pk
            if hasattr(new_value, 'pk'):
                new_value = new_value.pk

            if old_value != new_value:
                changed_fields.append(field)
                changed_values[field] = (getattr(instance, field), serializer.validated_data[field])

        instance = serializer.save()

        if changed_fields:
            AdminLog.objects.create(
                admin=self.request.user,
                action='редактирование встречи',
                target=safe_admin_log_target(f"Встреча '{instance.title}' (id={instance.id})"),
                target_id=instance.id,
                details=f"Изменены поля: {', '.join(changed_fields)}"
            )

            registered_user_ids = list(
                instance.registrations.values_list('user_id', flat=True)
            )

            if registered_user_ids:
                status_changed_to_cancelled = (
                    'status' in changed_values
                    and previous_status != MeetingStatus.CANCELLED
                    and instance.status == MeetingStatus.CANCELLED
                )
                status_changed_to_restored = (
                    'status' in changed_values
                    and previous_status == MeetingStatus.CANCELLED
                    and instance.status == MeetingStatus.UPCOMING
                )

                if status_changed_to_cancelled:
                    meeting_dt = instance.date
                    if timezone.is_aware(meeting_dt):
                        meeting_date = timezone.localtime(meeting_dt).strftime('%d.%m.%Y %H:%M')
                    else:
                        meeting_date = meeting_dt.strftime('%d.%m.%Y %H:%M')

                    Notification.objects.bulk_create([
                        Notification(
                            user_id=user_id,
                            meeting=instance,
                            title='Встреча отменена',
                            message=(
                                f"Встреча '{instance.title}', на которую вы зарегистрированы, "
                                f"была отменена. Плановая дата: {meeting_date}."
                            ),
                        )
                        for user_id in registered_user_ids
                    ])
                    # Инвалидация кэша рекомендаций для пользователей, записанных на встречу.
                    try:
                        for uid in registered_user_ids:
                            RecommendationForMeView().bump_user_cache_bust_version(uid)
                    except Exception:
                        logger.exception("Не удалось инвалиировать кэш рекомендаций при отмене встречи id=%s", instance.id)
                elif status_changed_to_restored:
                    meeting_dt = instance.date
                    if timezone.is_aware(meeting_dt):
                        meeting_date = timezone.localtime(meeting_dt).strftime('%d.%m.%Y %H:%M')
                    else:
                        meeting_date = meeting_dt.strftime('%d.%m.%Y %H:%M')

                    Notification.objects.bulk_create([
                        Notification(
                            user_id=user_id,
                            meeting=instance,
                            title='Встреча восстановлена',
                            message=(
                                f"Встреча '{instance.title}' снова активна. "
                                f"Актуальная дата: {meeting_date}."
                            ),
                        )
                        for user_id in registered_user_ids
                    ])
                else:
                    notify_fields = {'date', 'location'}
                    # Notify about reschedule only for active upcoming meetings.
                    # This prevents false "перенесена" messages when status changes
                    # to COMPLETED or any non-upcoming state.
                    should_notify_reschedule = (
                        notify_fields.intersection(changed_values.keys())
                        and instance.status == MeetingStatus.UPCOMING
                        and previous_status == MeetingStatus.UPCOMING
                    )

                    if should_notify_reschedule:
                        title = 'Встреча перенесена'
                        message_parts = [
                            f"Вы были записаны на встречу '{instance.title}'."
                        ]

                        if 'date' in changed_values:
                            old_date, new_date = changed_values['date']
                            old_date_str = timezone.localtime(old_date).strftime('%d.%m.%Y %H:%M')
                            new_date_str = timezone.localtime(new_date).strftime('%d.%m.%Y %H:%M')
                            message_parts.append(
                                f"Встреча перенесена: {old_date_str} -> {new_date_str}."
                            )

                        if 'location' in changed_values:
                            old_location, new_location = changed_values['location']
                            message_parts.append(
                                f"Место встречи изменено: {old_location} -> {new_location}."
                            )

                        Notification.objects.bulk_create([
                            Notification(
                                user_id=user_id,
                                meeting=instance,
                                title=title,
                                message=' '.join(message_parts),
                            )
                            for user_id in registered_user_ids
                        ])

    def perform_destroy(self, instance):
        # Вместо физического удаления переводим встречу в CANCELLED.
        old_status = instance.status
        if is_effectively_completed(instance):
            raise ValidationError({'detail': 'Нельзя отменить встречу, которая уже прошла или помечена как завершённая.'})

        registered_users = list(
            instance.registrations.values_list('user_id', flat=True)
        )
        instance.status = 'CANCELLED'
        instance.save(update_fields=['status'])

        if old_status != MeetingStatus.CANCELLED and registered_users:
            if timezone.is_aware(instance.date):
                meeting_date = timezone.localtime(instance.date).strftime('%d.%m.%Y %H:%M')
            else:
                meeting_date = instance.date.strftime('%d.%m.%Y %H:%M')

            try:
                Notification.objects.bulk_create([
                    Notification(
                        user_id=user_id,
                        meeting=instance,
                        title='Встреча отменена',
                        message=(
                            f"Встреча '{instance.title}', на которую вы зарегистрированы, "
                            f"была отменена. Плановая дата: {meeting_date}."
                        )
                    )
                    for user_id in registered_users
                ])
            except Exception:
                logger.exception(
                    "Не удалось создать уведомления об отмене встречи id=%s",
                    instance.id,
                )

            # Инвалидация кэша рекомендаций для пользователей, записанных на встречу.
            try:
                for uid in registered_users:
                    RecommendationForMeView().bump_user_cache_bust_version(uid)
            except Exception:
                logger.exception("Не удалось инвалиировать кэш рекомендаций при отмене встречи id=%s", instance.id)

        AdminLog.objects.create(
            admin=self.request.user,
            action='отмена встречи',
            target=safe_admin_log_target(f"Встреча '{instance.title}' (id={instance.id})"),
            target_id=instance.id,
            details=f"Статус: {old_status} -> CANCELLED"
        )

    def get_queryset(self):
        # Админ видит все встречи (включая COMPLETED/CANCELLED) и на GET, и на write-методах.
        if is_admin_user(self.request.user):
            status = self.request.query_params.get('status')
            if status:
                return self.queryset.filter(status=status)
            return self.queryset

        return self.queryset.filter(status='UPCOMING', date__gte=timezone.now()).order_by('date')

    def retrieve(self, request, *args, **kwargs):
        if is_admin_user(request.user):
            instance = get_object_or_404(Meeting, pk=kwargs.get('pk'))
        else:
            allowed_queryset = Meeting.objects.filter(
                status='UPCOMING',
                date__gte=timezone.now(),
            )
            instance = get_object_or_404(allowed_queryset, pk=kwargs.get('pk'))

        serializer = self.get_serializer(instance)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'], permission_classes=[IsAdmin])
    def add_participant(self, request, pk=None):
        """
        Админ добавляет существующего пользователя на встречу.
        Ожидает одно из полей: email или username, плюс phone_number.
        """
        meeting = self.get_object()
        meeting_id = meeting.id

        identifier_email = request.data.get('email')
        identifier_username = request.data.get('username')
        phone_number = request.data.get('phone_number')

        if not phone_number:
            return Response(
                {'detail': 'Поле phone_number обязательно.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not (identifier_email or identifier_username):
            return Response(
                {'detail': 'Нужно указать email или username пользователя.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Ищем пользователя по email или username
        try:
            if identifier_email:
                user = User.objects.get(email=identifier_email)
            else:
                user = User.objects.get(username=identifier_username)
        except ObjectDoesNotExist:
            return Response(
                {'detail': 'Пользователь с такими данными не найден.'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Нельзя добавлять админов, забаненных, удалённых и неактивных
        if is_admin_user(user):
            return Response(
                {'detail': 'Нельзя добавлять на встречи администраторский аккаунт.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if user.is_banned or not user.is_active:
            return Response(
                {'detail': 'Нельзя добавить этого пользователя: аккаунт заблокирован или удалён.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            locked_meeting = Meeting.objects.select_for_update().get(pk=meeting_id)

            # Встреча должна быть предстоящей
            if locked_meeting.status != 'UPCOMING':
                return Response(
                    {'detail': 'Добавлять участников можно только на предстоящие встречи.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            # Также требуем, чтобы дата была в будущем — сначалали нерегистрации обычных пользователей
            if locked_meeting.date <= timezone.now():
                return Response(
                    {'detail': 'Добавлять участников можно только на предстоящие встречи с будущей датой.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Проверка, не записан ли уже
            if MeetingRegistration.objects.filter(user=user, meeting=locked_meeting).exists():
                return Response(
                    {'detail': 'Пользователь уже зарегистрирован на эту встречу.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Проверка лимита мест как в сериализаторе
            if locked_meeting.max_attendees:
                current_count = locked_meeting.registrations.count()
                if current_count >= locked_meeting.max_attendees:
                    return Response(
                        {'detail': 'Максимальное количество участников достигнуто.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            # Можно дополнительно валидировать формат телефона, но можно и доверить фронту
            registration = MeetingRegistration.objects.create(
                user=user,
                meeting=locked_meeting,
                phone_number=phone_number
            )

        # Опционально залогировать действие админа
        AdminLog.objects.create(
            admin=request.user,
            action='add_participant',
            target=f"Встреча '{locked_meeting.title}' (id={locked_meeting.id})",
            target_id=locked_meeting.id,
            details=f"Добавлен участник {user.username} (id={user.id})"
        )

        return Response(
            MeetingRegistrationSerializer(registration).data,
            status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=['get'], permission_classes=[IsAdmin])
    def participants(self, request, pk=None):
        """Список всех участников конкретной встречи (только для админа)."""
        meeting = self.get_object()
        registrations = meeting.registrations.select_related('user').order_by('-registered_at')
        serializer = MeetingRegistrationSerializer(registrations, many=True)
        return Response(
            {
                'meeting_id': meeting.id,
                'count': registrations.count(),
                'participants': serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'], permission_classes=[IsAdmin])
    def remove_participant(self, request, pk=None):
        """
        Админ удаляет пользователя со встречи.
        Ожидает registration_id в теле запроса.
        """
        meeting = self.get_object()
        registration_id = request.data.get('registration_id')

        if registration_id is None or str(registration_id).strip().lower() in {'', 'null', 'none'}:
            return Response(
                {'detail': 'Поле registration_id обязательно.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            registration_id = int(registration_id)
        except (TypeError, ValueError):
            return Response(
                {'detail': 'registration_id должен быть числом.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            registration = MeetingRegistration.objects.select_related('user').get(
                id=registration_id,
                meeting=meeting,
            )
        except MeetingRegistration.DoesNotExist:
            return Response(
                {'detail': 'Регистрация не найдена для указанной встречи.'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Если по регистрации уже есть отзыв — отказ (конкретная причина важнее общего запрета по статусу).
        if MeetingReview.objects.filter(user=registration.user, meeting=registration.meeting).exists():
            return Response(
                {'detail': 'Нельзя удалить участника: пользователь уже оставил отзыв по этой встрече.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Запретить удаление, если по регистрации отмечено посещение.
        if getattr(registration, 'is_attended', False):
            return Response(
                {'detail': 'Нельзя удалить участника: по регистрации уже отмечено посещение.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Нельзя изменять список участников у отменённой встречи.
        if meeting.status == MeetingStatus.CANCELLED:
            return Response(
                {'detail': 'Нельзя изменять список участников у отменённой встречи.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Если встреча имеет статус UPCOMING, но дата уже в прошлом — нельзя удалять участников.
        if meeting.status == MeetingStatus.UPCOMING and meeting.date <= timezone.now():
            return Response(
                {'detail': 'Нельзя удалять участника: дата встречи уже прошла.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Запретить удаление участников для фактически завершённой встречи — сохраняем историю.
        if is_effectively_completed(meeting):
            return Response(
                {'detail': 'Нельзя удалять участника: встреча уже завершена.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        removed_user = registration.user
        registration.delete()

        current_attendees = meeting.registrations.count()

        AdminLog.objects.create(
            admin=request.user,
            action='remove_participant',
            target=f"Встреча '{meeting.title}' (id={meeting.id})",
            target_id=meeting.id,
            details=f"Удалён участник {removed_user.username} (id={removed_user.id})"
        )

        return Response(
            {
                'detail': 'Участник удалён со встречи.',
                'meeting_id': meeting.id,
                'current_attendees': current_attendees,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'], permission_classes=[IsAdmin])
    def restore(self, request, pk=None):
        """Восстановление отменённой встречи обратно в UPCOMING."""
        meeting = get_object_or_404(Meeting, pk=pk)

        if meeting.status != MeetingStatus.CANCELLED:
            return Response(
                {'detail': 'Восстановить можно только встречу в статусе CANCELLED.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = {'status': MeetingStatus.UPCOMING}
        if 'date' in request.data:
            payload['date'] = request.data.get('date')

        serializer = self.get_serializer(meeting, data=payload, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        meeting.refresh_from_db()
        AdminLog.objects.create(
            admin=request.user,
            action='восстановление встречи',
            target=safe_admin_log_target(f"Встреча '{meeting.title}' (id={meeting.id})"),
            target_id=meeting.id,
            details='Статус: CANCELLED -> UPCOMING',
        )

        return Response(self.get_serializer(meeting).data, status=status.HTTP_200_OK)

# ViewSet для регистрации на встречу
# C:\Users\gajda\PycharmProjects\bookclub\core\views.py (только MeetingRegistrationViewSet)
class MeetingRegistrationViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def list(self, request):
        meeting_id = request.query_params.get('meeting_id')
        if meeting_id is None:
            meeting_id = request.data.get('meeting_id')
        if not meeting_id:
            return Response(
                {'detail': 'Параметр meeting_id обязателен (передайте как query: /api/meetings/register/?meeting_id=<id>).'},
                status=status.HTTP_400_BAD_REQUEST,
            )
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
        #Админ не может регистрировать себя как участника
        if is_admin_user(request.user):
            return Response(
            {'detail': 'Администратор не может регистрироваться на встречи своим администраторским аккаунтом.'},
            status=status.HTTP_403_FORBIDDEN
            )
        serializer = MeetingRegistrationSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def destroy(self, request, pk=None):
        """Отмена регистрации на встречу (только своей)"""
        try:
            registration = MeetingRegistration.objects.get(pk=pk)
        except MeetingRegistration.DoesNotExist:
            return Response({'detail': 'Регистрация не найдена'}, status=status.HTTP_404_NOT_FOUND)
        
        # Пользователь может удалять только свои регистрации
        if registration.user != request.user:
            return Response(
                {'detail': 'Вы можете отменить только свою регистрацию'},
                status=status.HTTP_403_FORBIDDEN
            )

        if registration.meeting.status != 'UPCOMING':
            return Response(
                {'detail': 'Отменить регистрацию можно только для предстоящих встреч.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if registration.meeting.date <= timezone.now():
            return Response(
                {'detail': 'Отменить регистрацию можно только для предстоящих встреч с будущей датой.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        registration.delete()
        return Response({'detail': 'Регистрация отменена'}, status=status.HTTP_204_NO_CONTENT)

    def mark_attendance(self, request, pk=None):
        if not is_admin_user(request.user):
            return Response({'detail': 'Только администратор может отмечать посещение.'}, status=status.HTTP_403_FORBIDDEN)

        try:
            registration = MeetingRegistration.objects.select_related('meeting', 'user').get(pk=pk)
        except MeetingRegistration.DoesNotExist:
            return Response({'detail': 'Регистрация не найдена'}, status=status.HTTP_404_NOT_FOUND)

        meeting = registration.meeting
        meeting_status = (meeting.status or '').strip().upper()
        meeting_date = meeting.date
        now = timezone.now()

        # Разрешаем отмечать посещение если встреча завершена по статусу или по дате (и не отменена).
        can_mark = (
            is_effectively_completed(meeting) and meeting.status != MeetingStatus.CANCELLED
        )

        if not can_mark:
            return Response(
                {
                    'detail': 'Отмечать посещение можно только для завершённых или уже прошедших по дате встреч (и не отменённых).',
                    'meeting_status': registration.meeting.status,
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        is_attended_param = request.data.get('is_attended', True)
        if isinstance(is_attended_param, bool):
            new_is_attended = is_attended_param
        else:
            new_is_attended = str(is_attended_param).lower() in ['true', '1', 'yes']

        # Idempotent behavior: repeated same-state request should not create extra logs.
        if registration.is_attended == new_is_attended:
            return Response(
                {
                    'registration_id': registration.id,
                    'meeting_id': registration.meeting.id,
                    'user_id': registration.user.id,
                    'is_attended': registration.is_attended,
                    'attended_at': registration.attended_at,
                    'detail': 'Статус посещения не изменился.',
                },
                status=status.HTTP_200_OK,
            )

        if not new_is_attended and MeetingReview.objects.filter(
            user=registration.user,
            meeting=registration.meeting,
        ).exists():
            return Response(
                {
                    'detail': 'Нельзя снять отметку посещения: пользователь уже оставил отзыв по этой встрече.'
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        registration.is_attended = new_is_attended
        if new_is_attended:
            registration.attended_at = timezone.now()
            registration.attendance_marked_by = request.user
        else:
            registration.attended_at = None
            registration.attendance_marked_by = None
        registration.save(update_fields=['is_attended', 'attended_at', 'attendance_marked_by'])

        AdminLog.objects.create(
            admin=request.user,
            action='mark_attendance',
            target=f"Регистрация id={registration.id}",
            target_id=registration.id,
            registration=registration,
            details=(
                f"Пользователь {registration.user.username} (id={registration.user.id}) на встрече "
                f"{registration.meeting.title} (id={registration.meeting.id}), is_attended={registration.is_attended}"
            ),
        )

        return Response(
            {
                'registration_id': registration.id,
                'meeting_id': registration.meeting.id,
                'user_id': registration.user.id,
                'is_attended': registration.is_attended,
                'attended_at': registration.attended_at,
            },
            status=status.HTTP_200_OK
        )

# ViewSet для голосования за книги
class BookVoteViewSet(viewsets.ModelViewSet):
    queryset = BookVote.objects.all()
    serializer_class = BookVoteSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_permissions(self):
        # Для пользователя доступны создание и просмотр своих голосов.
        if self.action in ['create', 'list', 'destroy', 'all_for_counts']:
            return [permissions.IsAuthenticated()]
        #для всего остального (list, retrieve, by_period и т.д.) — только админ
        return [IsAdmin()]

    def get_queryset(self):
        if is_admin_user(self.request.user):
            logger.debug("Admin user is excluded from voting queryset.")
            return self.queryset.none()

        # Обычный пользователь видит только свои голоса за текущий активный период.
        today = timezone.now()
        active_period = VotingPeriod.objects.filter(
            start_date__lte=today,
            end_date__gte=today,
            is_active=True,
        ).first()

        # Обычный пользователь видит только свои голоса.

        queryset = self.queryset.filter(user=self.request.user)
        if active_period is None:
            return queryset.none()

        queryset = queryset.filter(voting_period=active_period)

        # Для обычного пользователя период фиксирован текущим активным;
        # query-параметр voting_period_id не расширяет доступ.
        voting_period_id = self.request.query_params.get('voting_period_id')
        if voting_period_id and str(voting_period_id) != str(active_period.id):
            try:
                int(voting_period_id)
                logger.debug(
                    "Ignored voting_period_id=%s for non-admin user=%s; forced active_period=%s",
                    voting_period_id,
                    self.request.user.id,
                    active_period.id,
                )
            except ValueError:
                logger.error(f"Invalid voting_period_id: {voting_period_id}")

        logger.debug(f"Filtered queryset count: {queryset.count()}")
        return queryset.order_by('-voted_at')  #новые сверху

    def perform_create(self, serializer):
        #админ не может голосовать админским аккаунтом
        if is_admin_user(self.request.user):
            AdminLog.objects.create(
                admin=self.request.user,
                action='admin_vote_attempt',
                target='BookVote',
                details='Администратор попытался проголосовать'
            )
            raise PermissionDenied("Администратор не может голосовать за книги своим администраторским аккаунтом.")

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
        if book.is_archived:
            raise ValidationError("Нельзя голосовать за архивную книгу.")

        serializer.save(user=user, voting_period=active_period)

    # Запрещаем изменение голосов; позволяем удаление (unvote)
    def update(self, request, *args, **kwargs):
        return Response({"detail": "Изменение голосов запрещено"}, status=status.HTTP_403_FORBIDDEN)

    def partial_update(self, request, *args, **kwargs):
        return Response({"detail": "Изменение голосов запрещено"}, status=status.HTTP_403_FORBIDDEN)

    def destroy(self, request, *args, **kwargs):
        if is_admin_user(self.request.user):
            raise PermissionDenied("Администратор не может удалять голоса.")
        # Allow users to unvote (delete their own votes)
        vote = self.get_object()
        if vote.user != self.request.user and not is_admin_user(self.request.user):
            raise PermissionDenied("Вы можете удалить только свои голоса")
        return super().destroy(request, *args, **kwargs)

    @action(detail=False, methods=['get'], url_path='all-for-counts')
    def all_for_counts(self, request):
        today = timezone.now()
        period_id = request.query_params.get('voting_period_id')
        active_period = None

        if period_id:
            try:
                active_period = VotingPeriod.objects.get(id=int(period_id))
            except (VotingPeriod.DoesNotExist, ValueError):
                return Response([], status=status.HTTP_200_OK)
        else:
            active_period = VotingPeriod.objects.filter(
                start_date__lte=today,
                end_date__gte=today,
                is_active=True,
            ).first()

        if not active_period:
            return Response([], status=status.HTTP_200_OK)

        if not is_admin_user(request.user) and active_period.voting_mode != VotingMode.OPEN:
            return Response([], status=status.HTTP_200_OK)

        votes = self.queryset.filter(
            voting_period=active_period,
        ).order_by('-voted_at')

        serializer = self.get_serializer(votes, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], permission_classes=[IsAdmin], url_path='period-stats')
    def period_stats(self, request):
        """Возвращает статистику голосов по книгам за указанный период или за текущий активный."""
        period_id = request.query_params.get('voting_period_id')
        payload = build_voting_period_stats(period_id)
        if payload is None:
            return Response({'message': 'Нет указанного или активного периода'}, status=status.HTTP_200_OK)
        return Response(payload, status=status.HTTP_200_OK)


class VotingPeriodStatsView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        period_id = request.query_params.get('voting_period_id')
        payload = build_voting_period_stats(period_id)
        if payload is None:
            return Response({'message': 'Нет указанного или активного периода'}, status=status.HTTP_200_OK)
        return Response(payload, status=status.HTTP_200_OK)

# ViewSet для оценки книг (зелёная, жёлтая, красная)
class BookRatingViewSet(viewsets.ModelViewSet):
    queryset = BookRating.objects.all()
    serializer_class = BookRatingSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        if is_admin_user(self.request.user):
            AdminLog.objects.create(
                admin=self.request.user,
                action='admin_rating_attempt',
                target='BookRating',
            details='Администратор попытался поставить оценку'
        )   
            raise PermissionDenied("Администратор не может ставить оценки книгам")

        book = serializer.validated_data.get('book')
        new_rating = serializer.validated_data.get('rating')
        existing_rating = BookRating.objects.filter(user=self.request.user, book=book).first()
        if existing_rating:
            # No-op update: same rating should not trigger an extra DB write.
            if existing_rating.rating == new_rating:
                serializer.instance = existing_rating
                return
            existing_rating.rating = new_rating
            existing_rating.save(update_fields=['rating'])
            serializer.instance = existing_rating
            RecommendationForMeView().bump_user_cache_bust_version(self.request.user.id)
            return

        serializer.save(user=self.request.user, book=book)
        RecommendationForMeView().bump_user_cache_bust_version(self.request.user.id)

    def update(self, request, *args, **kwargs):
        return Response(
            {'detail': 'Изменение оценки по id запрещено. Используйте POST /api/ratings/ с book_id и rating.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    def partial_update(self, request, *args, **kwargs):
        return Response(
            {'detail': 'Изменение оценки по id запрещено. Используйте POST /api/ratings/ с book_id и rating.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    def get_queryset(self):
        # Любой пользователь, включая админа, видит только свои оценки.
        return self.queryset.filter(user=self.request.user)

    def list(self, request, *args, **kwargs):
        if is_admin_user(request.user):
            return Response(
                {'detail': 'Просмотр оценок пользователей запрещен политикой приватности.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        rating = get_object_or_404(BookRating, pk=kwargs.get('pk'))
        if rating.user_id != request.user.id:
            return Response(
                {'detail': 'Просмотр чужих оценок запрещен политикой приватности.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = self.get_serializer(rating)
        return Response(serializer.data)

# ViewSet для вопросов отзывов
class ReviewQuestionViewSet(viewsets.ModelViewSet):
    queryset = ReviewQuestion.objects.all()
    serializer_class = ReviewQuestionSerializer
    permission_classes = [IsAdminReadOnly]
    ordering = ['-created_at']

    def get_queryset(self):
        # Обычный пользователь вообще ничего не видит
        if is_admin_user(self.request.user):
            return ReviewQuestion.objects.all()
        return ReviewQuestion.objects.none()

    def _is_used_in_completed_meeting(self, question):
        return (
            MeetingReviewQuestion.objects.filter(
                meeting__status=MeetingStatus.COMPLETED,
                question1=question,
            ).exists()
            or MeetingReviewQuestion.objects.filter(
                meeting__date__lte=timezone.now(),
                question1=question,
            ).exists()
            or MeetingReviewQuestion.objects.filter(
                meeting__status=MeetingStatus.COMPLETED,
                question2=question,
            ).exists()
            or MeetingReviewQuestion.objects.filter(
                meeting__date__lte=timezone.now(),
                question2=question,
            ).exists()
            or MeetingReviewQuestion.objects.filter(
                meeting__status=MeetingStatus.COMPLETED,
                question3=question,
            ).exists()
            or MeetingReviewQuestion.objects.filter(
                meeting__date__lte=timezone.now(),
                question3=question,
            ).exists()
        )

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if self._is_used_in_completed_meeting(instance):
            raise ValidationError(
                "Нельзя изменять вопрос, который уже используется в завершенной встрече."
            )
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        if self._is_used_in_completed_meeting(instance):
            raise ValidationError(
                "Нельзя изменять вопрос, который уже используется в завершенной встрече."
            )
        return super().partial_update(request, *args, **kwargs)


# ViewSet для вопросов встреч
class MeetingReviewQuestionViewSet(viewsets.ModelViewSet):
    queryset = MeetingReviewQuestion.objects.all()
    serializer_class = MeetingReviewQuestionSerializer

    def _question_summary(self, instance):
        return (
            f"1: {instance.question1.question_text} (id={instance.question1_id}); "
            f"2: {instance.question2.question_text} (id={instance.question2_id}); "
            f"3: {instance.question3.question_text} (id={instance.question3_id})"
        )

    def get_permissions(self):
        # Write операции только для админов
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            return [IsAdmin()]
        # Read операции с ограничениями для обычных пользователей
        return [IsAdminOrReadOnly()]

    def get_queryset(self):
        queryset = self.queryset
        meeting_id = self.request.query_params.get('meeting_id')

        #если meeting_id не передан — никто ничего не видит (кроме админа)
        if not meeting_id:
            if is_admin_user(self.request.user):
                return self.queryset
            return MeetingReviewQuestion.objects.none()

        #проверяем существование встречи
        try:
            meeting = Meeting.objects.get(id=meeting_id)
        except Meeting.DoesNotExist:
            return MeetingReviewQuestion.objects.none()

        # Обычный пользователь видит вопросы ТОЛЬКО если:
        # 1. Зарегистрирован на эту встречу и отмечен как присутствовавший (`is_attended=True`),
        # 2. Встреча либо имеет статус COMPLETED, либо по времени уже прошла (meeting.date <= now()).
        # Это позволяет пользователям видеть вопросы для встреч, дата которых уже в прошлом,
        # даже если админ ещё не обновил статус встречи на COMPLETED.
        if not is_admin_user(self.request.user):
            if not MeetingRegistration.objects.filter(
                    user=self.request.user,
                meeting=meeting,
                is_attended=True,
            ).exists():
                return MeetingReviewQuestion.objects.none()

            # Разрешаем доступ, если встреча завершена по статусу или её дата уже прошла.
            # Отдельно запрещаем доступ к вопросам для отменённых встреч.
            if meeting.status == MeetingStatus.CANCELLED:
                return MeetingReviewQuestion.objects.none()

            if not is_effectively_completed(meeting):
                return MeetingReviewQuestion.objects.none()

        #если все проверки пройдены — возвращаем вопросы именно этой встречи
        return queryset.filter(meeting_id=meeting_id)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        meeting = instance.meeting
        # Запрет для отменённых встреч: вопросы нельзя удалять у CANCELLED.
        if meeting.status == MeetingStatus.CANCELLED:
            raise ValidationError(
                "Нельзя удалять вопросы встречи: встреча отменена."
            )

        if is_effectively_completed(meeting):
            raise ValidationError(
                "Нельзя удалить вопросы встречи: встреча уже прошла или помечена как завершённая."
            )

        if MeetingReview.objects.filter(meeting=instance.meeting).exists():
            raise ValidationError(
                "Нельзя удалить вопросы встречи, по которой уже оставлены отзывы."
            )
        AdminLog.objects.create(
            admin=request.user,
            action='удаление набора вопросов встречи',
            target=safe_admin_log_target(f"Встреча '{instance.meeting.title}' (id={instance.meeting_id})"),
            target_id=instance.meeting_id,
            details=f"Удалён набор вопросов: {self._question_summary(instance)}",
        )
        return super().destroy(request, *args, **kwargs)

    def perform_create(self, serializer):
        meeting = serializer.validated_data.get('meeting')
        if meeting and meeting.status == MeetingStatus.CANCELLED:
            raise ValidationError("Нельзя назначать вопросы для отменённой встречи.")
        instance = serializer.save()
        AdminLog.objects.create(
            admin=self.request.user,
            action='создание набора вопросов встречи',
            target=safe_admin_log_target(f"Встреча '{instance.meeting.title}' (id={instance.meeting_id})"),
            target_id=instance.meeting_id,
            details=f"Назначены вопросы: {self._question_summary(instance)}",
        )

    def perform_update(self, serializer):
        old_instance = self.get_object()
        old_summary = self._question_summary(old_instance)
        # Запрет на изменение вопросов для отменённой встречи.
        if old_instance.meeting.status == MeetingStatus.CANCELLED:
            raise ValidationError("Нельзя изменять вопросы отменённой встречи.")

        instance = serializer.save()
        new_summary = self._question_summary(instance)

        if old_summary != new_summary:
            AdminLog.objects.create(
                admin=self.request.user,
                action='изменение набора вопросов встречи',
                target=safe_admin_log_target(f"Встреча '{instance.meeting.title}' (id={instance.meeting_id})"),
                target_id=instance.meeting_id,
                details=f"Было: {old_summary}; Стало: {new_summary}",
            )

# ViewSet для отзывов на встречи (тестировать)
class MeetingReviewViewSet(viewsets.ModelViewSet):
    queryset = MeetingReview.objects.all()
    serializer_class = MeetingReviewSerializer
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        #для создания отзыва — любой авторизованный
        if self.action in ['create', 'by_user']:
            return [IsAuthenticated()]
        #для всего остального — только админ
        return [IsAdmin()]

    def get_queryset(self):
        if self.action in ['by_meeting', 'list']:
            if not is_admin_user(self.request.user):
                return MeetingReview.objects.none()
        #обычный пользователь видит ТОЛЬКО свои отзывы
        if is_admin_user(self.request.user):
            queryset = self.queryset
        else:
            queryset = self.queryset.filter(user=self.request.user)

        meeting_id = self.request.query_params.get('meeting_id')
        if meeting_id:
            queryset = queryset.filter(meeting_id=meeting_id)

        return queryset.order_by('-reviewed_at')

    def perform_create(self, serializer):
        user = self.request.user
        # Админ не может оставлять отзывы своим админским аккаунтом
        if is_admin_user(user):
            AdminLog.objects.create(
                admin=user,
                action='admin_review_attempt',
                target='MeetingReview',
                details='Администратор попытался оставить отзыв'
            )
            raise PermissionDenied("Администратор не может оставлять отзывы своим администраторским аккаунтом.")
        meeting = serializer.validated_data['meeting']
        # Нельзя оставлять отзывы для отменённых встреч.
        if meeting.status == MeetingStatus.CANCELLED:
            raise ValidationError("Нельзя оставлять отзыв для отменённой встречи.")
        registration = MeetingRegistration.objects.filter(user=user, meeting=meeting).first()
        if not registration:
            raise ValidationError("Вы не зарегистрированы на эту встречу.")
        if not registration.is_attended:
            raise ValidationError("Отзывы могут оставлять только участники, присутствовавшие на встрече.")
        # Разрешаем оставлять отзывы, если встреча помечена завершённой,
        # либо если её дата уже прошла — чтобы пользователи могли оставлять отзывы
        # даже когда админ ещё не сменил статус на COMPLETED.
        if not is_effectively_completed(meeting):
            raise ValidationError("Отзывы можно оставлять только для завершённых встреч.")
        if MeetingReview.objects.filter(user=user, meeting=meeting).exists():
            raise ValidationError("Вы уже оставили отзыв на эту встречу.")
        serializer.save(user=user)

    #убираем или запрещаем update/destroy
    def update(self, request, *args, **kwargs):
        return Response({"detail": "Изменение отзывов запрещено"}, status=status.HTTP_403_FORBIDDEN)

    def partial_update(self, request, *args, **kwargs):
        return Response({"detail": "Изменение отзывов запрещено"}, status=status.HTTP_403_FORBIDDEN)

    def destroy(self, request, *args, **kwargs):
        return Response({"detail": "Удаление отзывов запрещено"}, status=status.HTTP_403_FORBIDDEN)
    @action(detail=False, methods=['get'], permission_classes=[IsAdmin])
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

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated], url_path='stats')
    def stats(self, request):
        meeting_id = request.query_params.get('meeting_id')
        if not meeting_id:
            raise ValidationError("Параметр meeting_id обязателен.")
        try:
            meeting = Meeting.objects.get(id=meeting_id)
        except Meeting.DoesNotExist:
            raise ValidationError("Встреча не найдена.")

        if not is_admin_user(request.user):
            has_registration = MeetingRegistration.objects.filter(
                user=request.user,
                meeting=meeting,
            ).exists()
            if not has_registration:
                raise PermissionDenied("Вы можете смотреть статистику только по своим встречам.")

        reviews = self.queryset.filter(meeting=meeting)
        total = reviews.count()
        if total == 0:
            return Response({
                'meeting_id': meeting.id,
                'meeting_title': meeting.title,
                'total_reviews': 0,
                'averages': {'question1': None, 'question2': None, 'question3': None},
                'distributions': {'question1': {}, 'question2': {}, 'question3': {}},
            })

        from django.db.models import Avg, Count

        avgs = reviews.aggregate(
            q1_avg=Avg('question1_answer'),
            q2_avg=Avg('question2_answer'),
            q3_avg=Avg('question3_answer')
        )

        def make_distribution(field_name):
            qs = reviews.exclude(**{f'{field_name}__isnull': True}).values(field_name).annotate(count=Count('id')).order_by(field_name)
            return {str(item[field_name]): item['count'] for item in qs}

        data = {
            'meeting_id': meeting.id,
            'meeting_title': meeting.title,
            'total_reviews': total,
            'averages': {
                'question1': round(avgs['q1_avg'], 2) if avgs.get('q1_avg') is not None else None,
                'question2': round(avgs['q2_avg'], 2) if avgs.get('q2_avg') is not None else None,
                'question3': round(avgs['q3_avg'], 2) if avgs.get('q3_avg') is not None else None,
            },
            'distributions': {
                'question1': make_distribution('question1_answer'),
                'question2': make_distribution('question2_answer'),
                'question3': make_distribution('question3_answer'),
            }
        }
        return Response(data)

# View для проверки текущего периода голосования
class CurrentVotingPeriodView(APIView):
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
                'end_date': serializer.data['end_date'],
                'voting_mode': serializer.data.get('voting_mode', 'closed')
            })
        return Response({
            'is_active': False,
            'message': 'Голосование ещё не открыто или уже закрыто'
        })

class VotingPeriodViewSet(viewsets.ModelViewSet):
    queryset = VotingPeriod.objects.all()
    serializer_class = VotingPeriodSerializer
    permission_classes = [IsAuthenticatedReadOnly]

    def get_queryset(self):
        #админ видит все периоды, остальные — ничего
        if is_admin_user(self.request.user):
            return super().get_queryset().order_by('-start_date')
        return VotingPeriod.objects.none()

    def perform_create(self, serializer):
        data = serializer.validated_data
        overlap = VotingPeriod.objects.filter(
            start_date__lt=data['end_date'],
            end_date__gt=data['start_date']
        ).exists()
        if overlap:
            raise ValidationError("Период голосования пересекается с существующим.")
        instance = serializer.save()
        AdminLog.objects.create(
            admin=self.request.user,
            action='создание периода голосования',
            target=f"Период {instance.start_date} — {instance.end_date}",
            target_id=instance.id
        )

    def perform_update(self, serializer):
        instance = serializer.instance
        data = serializer.validated_data
        start_date = data.get('start_date', instance.start_date)
        end_date = data.get('end_date', instance.end_date)
        overlap = VotingPeriod.objects.filter(
            start_date__lt=end_date,
            end_date__gt=start_date
        ).exclude(id=instance.id).exists()
        if overlap:
            raise ValidationError("Период голосования пересекается с существующим.")

        instance = serializer.save()
        AdminLog.objects.create(
            admin=self.request.user,
            action='изменение периода голосования',
            target=f"Период {instance.start_date} — {instance.end_date}",
            target_id=instance.id
        )

    def perform_destroy(self, instance):
        AdminLog.objects.create(
            admin=self.request.user,
            action='удаление периода голосования',
            target=f"Период {instance.start_date} — {instance.end_date}",
            target_id=instance.id
        )
        instance.delete()


# View для информации о клубе (главная страница, только для авторизованных)
@api_view(['GET'])
@permission_classes([permissions.AllowAny])
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

class AdminLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AdminLog.objects.all()
    serializer_class = AdminLogSerializer
    permission_classes = [IsAdmin]  #только админ видит логи

    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['action', 'target_id', 'admin__username']
    ordering_fields = ['timestamp']

    def get_queryset(self):
        queryset = super().get_queryset()
        queryset = queryset.filter(admin__is_superuser=False)
        return queryset.order_by('-timestamp')  # новые сверху

    @action(detail=False, methods=['get'], permission_classes=[IsAdmin], url_path='meta')
    def meta(self, request):
        """Возвращает уникальные значения для фильтров (actions и admins)."""
        actions = list(AdminLog.objects.values_list('action', flat=True).distinct().order_by('action'))
        admins_qs = AdminLog.objects.exclude(admin__isnull=True).values_list('admin__username', flat=True).distinct()
        admins = list(admins_qs.order_by('admin__username')) if admins_qs is not None else []
        return Response({'actions': actions, 'admins': admins})

    def destroy(self, request, *args, **kwargs):
        return Response(
            {'detail': 'Удаление логов запрещено.'},
            status=status.HTTP_403_FORBIDDEN
        )



class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user).order_by('-created_at')

    @action(detail=True, methods=['patch'], permission_classes=[IsAuthenticated])
    def mark_read(self, request, pk=None):
        notification = self.get_object()

        if not notification.is_read:
            notification.is_read = True
            notification.read_at = timezone.now()
            notification.save(update_fields=['is_read', 'read_at'])

        serializer = self.get_serializer(notification)
        return Response(serializer.data, status=status.HTTP_200_OK)

# View для сбора статистики по пользователям, встречам и книгам (только для админов)
class AdminStatisticsView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        now = timezone.now()

        # USERS
        users_total = User.objects.count()
        users_active = User.objects.filter(is_active=True).count()
        users_banned = User.objects.filter(is_banned=True).count()
        users_new_7d = User.objects.filter(created_at__gte=now - timedelta(days=7)).count()
        users_new_30d = User.objects.filter(created_at__gte=now - timedelta(days=30)).count()

        # MEETINGS
        meetings_total = Meeting.objects.count()
        meetings_upcoming = Meeting.objects.filter(status='UPCOMING').count()
        meetings_completed = Meeting.objects.filter(Q(status='COMPLETED') | Q(date__lte=now)).count()
        meetings_cancelled = Meeting.objects.filter(status='CANCELLED').count()

        # посещаемость по датам (по регистрациям)
        registrations_by_date = (
            MeetingRegistration.objects
            .annotate(date=TruncDate('registered_at'))
            .values('date')
            .annotate(registrations=Count('id'))
            .order_by('date')
        )

        # список всех встреч + кол-во регистраций
        meeting_items = (
            Meeting.objects
            .annotate(registrations_count=Count('registrations'))
            .values('id', 'title', 'date', 'status', 'registrations_count')
            .order_by('-date')
        )

        # BOOKS
        # средний рейтинг по шкале GREEN=5 YELLOW=3 RED=1
        avg_rating = (
            BookRating.objects.aggregate(
                avg=Avg(
                    Case(
                        When(rating='GREEN', then=5),
                        When(rating='YELLOW', then=3),
                        When(rating='RED', then=1),
                        output_field=FloatField()
                    )
                )
            )['avg'] or 0.0
        )
        books_total = Book.objects.count()
        
        # VOTING STATISTICS
        voting_periods_total = VotingPeriod.objects.count()
        voting_periods_active = VotingPeriod.objects.filter(
            start_date__lte=now,
            end_date__gte=now,
            is_active=True
        ).count()
        total_votes = BookVote.objects.count()
        
        # топ книги по голосам (за последний активный период)
        active_period = VotingPeriod.objects.filter(
            start_date__lte=now,
            end_date__gte=now,
            is_active=True
        ).first()
        
        top_books_by_votes = []
        if active_period:
            top_books_by_votes = list(
                BookVote.objects
                .filter(voting_period=active_period)
                .values('book__id', 'book__title')
                .annotate(votes_count=Count('id'))
                .order_by('-votes_count')[:5]
            )
        
        # REVIEWS STATISTICS
        reviews_total = BookReview.objects.count()
        reviews_hidden = BookReview.objects.filter(is_hidden=True).count()
        reviews_visible = BookReview.objects.filter(is_hidden=False).count()
        
        # среднее количество отзывов на книгу
        avg_reviews_per_book = 0.0
        if books_total > 0:
            avg_reviews_per_book = round(reviews_total / books_total, 1)
        
        # ADMIN LOGS
        logs_total = AdminLog.objects.count()
        logs_7d = AdminLog.objects.filter(timestamp__gte=now - timedelta(days=7)).count()
        
        # типы действий админа (top 10)
        action_types = list(
            AdminLog.objects
            .values('action')
            .annotate(count=Count('id'))
            .order_by('-count')[:10]
        )
        
        # TOP MEETINGS
        top_meetings = list(
            Meeting.objects
            .annotate(registrations_count=Count('registrations'))
            .values('id', 'title', 'status', 'date', 'registrations_count')
            .order_by('-registrations_count')[:5]
        )
        
        # CONVERSION METRICS
        total_registered = MeetingRegistration.objects.values('user_id').distinct().count()
        total_users_active = User.objects.filter(is_active=True).exclude(role='admin', is_staff=True).count()
        conversion_rate = 0.0
        if total_users_active > 0:
            conversion_rate = round((total_registered / total_users_active) * 100, 1)
        
        # завершённые встречи - посещаемость (кол-во регистраций на фактически завершённые встречи)
        completed_meetings_attended = MeetingRegistration.objects.filter(
            is_attended=True,
        ).filter(
            Q(meeting__status='COMPLETED') | Q(meeting__date__lte=now)
        ).count()
        completed_meetings_total = Meeting.objects.filter(
            Q(status='COMPLETED') | Q(date__lte=now)
        ).count()
        avg_attendance = 0.0
        if completed_meetings_total > 0:
            avg_attendance = round(completed_meetings_attended / completed_meetings_total, 1)
        
        data = {
            "users": {
                "total": users_total,
                "active": users_active,
                "banned": users_banned,
                "new_7d": users_new_7d,
                "new_30d": users_new_30d
            },
            "meetings": {
                "total": meetings_total,
                "upcoming": meetings_upcoming,
                "completed": meetings_completed,
                "cancelled": meetings_cancelled,
                "by_date": list(registrations_by_date),
                "items": list(meeting_items)
            },
            "books": {
                "total": books_total,
                "avg_rating": round(float(avg_rating), 1)
            },
            "voting": {
                "voting_periods_total": voting_periods_total,
                "voting_periods_active": voting_periods_active,
                "total_votes": total_votes,
                "top_books": top_books_by_votes
            },
            "reviews": {
                "total": reviews_total,
                "visible": reviews_visible,
                "hidden": reviews_hidden,
                "avg_per_book": avg_reviews_per_book
            },
            "admin_logs": {
                "total": logs_total,
                "last_7d": logs_7d,
                "action_types": action_types
            },
            "top_meetings": top_meetings,
            "conversion": {
                "registered_users": total_registered,
                "active_users": total_users_active,
                "conversion_rate_percent": conversion_rate,
                "avg_attendance_per_completed_meeting": avg_attendance
            }
        }
        return Response(data)
    
class BookReviewViewSet(viewsets.ModelViewSet):
    #ViewSet для отзывов о книгаx
    queryset = BookReview.objects.all()
    serializer_class = BookReviewSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        base_queryset = self.queryset.filter(user__is_superuser=False)

        # Обычные пользователи видят только не скрытые отзывы
        if is_admin_user(self.request.user):
            queryset = base_queryset
        else:
            # Показываем обычным пользователям либо все не скрытые отзывы,
            # либо их собственные отзывы (даже если они ещё скрыты модератором).
            queryset = base_queryset.filter(Q(is_hidden=False) | Q(user=self.request.user))
        
        # Фильтр по книге если передан параметр
        book_id = self.request.query_params.get('book_id')
        if book_id:
            queryset = queryset.filter(book_id=book_id)
        
        return queryset.order_by('-created_at')

    def perform_create(self, serializer):
        # Админ не может писать отзывы (только модерировать)
        if is_admin_user(self.request.user):
            AdminLog.objects.create(
                admin=self.request.user,
                action='admin_review_create_attempt',
                target='BookReview',
                details='Администратор попытался создать отзыв'
            )
            raise PermissionDenied("Администратор не может писать отзывы.")
        
        # Отзыв независим от оценки: оценка отправляется отдельным endpoint /api/ratings/.
        try:
            serializer.save(user=self.request.user, rating=None)
        except IntegrityError:
            raise ValidationError({'detail': 'Вы уже написали отзыв на эту книгу.'})

    def update(self, request, *args, **kwargs):
        # Запрещаем редактирование отзывов (даже админу)
        return Response(
            {'detail': 'Редактирование отзывов запрещено'},
            status=status.HTTP_405_METHOD_NOT_ALLOWED
        )

    def partial_update(self, request, *args, **kwargs):
        # Запрещаем редактирование отзывов
        return Response(
            {'detail': 'Редактирование отзывов запрещено'},
            status=status.HTTP_405_METHOD_NOT_ALLOWED
        )

    def destroy(self, request, *args, **kwargs):
        # Удаление запрещено для всех - только скрывание через toggle_visibility
        return Response(
            {'detail': 'Удаление отзывов запрещено. Используйте toggle_visibility для модерации.'},
            status=status.HTTP_405_METHOD_NOT_ALLOWED
        )

    @action(detail=True, methods=['patch'], permission_classes=[IsAdmin])
    def toggle_visibility(self, request, pk=None):
        """Скрыть/показать отзыв (только админ)"""
        review = self.get_object()
        is_hidden_param = request.data.get('is_hidden')
        
        if is_hidden_param is None:
            return Response(
                {'detail': 'Нужно передать is_hidden: true/false'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if isinstance(is_hidden_param, bool):
            is_hidden = is_hidden_param
        else:
            normalized = str(is_hidden_param).strip().lower()
            if normalized in ['true', '1', 'yes']:
                is_hidden = True
            elif normalized in ['false', '0', 'no']:
                is_hidden = False
            else:
                return Response(
                    {'detail': 'is_hidden должен быть true/false.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

        if review.is_hidden == is_hidden:
            serializer = BookReviewSerializer(review, context={'request': request})
            payload = dict(serializer.data)
            payload['detail'] = 'Статус видимости не изменился.'
            return Response(payload, status=status.HTTP_200_OK)
        
        review.is_hidden = is_hidden
        review.save()
        
        action = 'скрыт' if is_hidden else 'показан'
        AdminLog.objects.create(
            admin=request.user,
            action=f'отзыв_{action}',
            target=f"Отзыв на книгу '{review.book.title}' от пользователя {review.user.username}",
            target_id=review.id
        )
        
        return Response(BookReviewSerializer(review).data)