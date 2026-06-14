from django.core.management.base import BaseCommand

from core.views import RecommendationForMeView


class Command(BaseCommand):
    help = 'Precompute and cache Apriori rules for recommendations.'

    def add_arguments(self, parser):
        parser.add_argument('--min-support', type=float, default=RecommendationForMeView.DEFAULT_MIN_SUPPORT)
        parser.add_argument('--min-confidence', type=float, default=RecommendationForMeView.DEFAULT_MIN_CONFIDENCE)
        parser.add_argument('--min-lift', type=float, default=RecommendationForMeView.DEFAULT_MIN_LIFT)

    def handle(self, *args, **options):
        view = RecommendationForMeView()
        transactions, _, _ = view._build_user_signals()
        rules, cache_status = view._get_cached_rules(
            transactions=transactions,
            min_support=float(options['min_support']),
            min_confidence=float(options['min_confidence']),
            min_lift=float(options['min_lift']),
        )

        total_rules = sum(len(value) for value in rules.values())
        cache_key = view._get_rules_cache_key(
            float(options['min_support']),
            float(options['min_confidence']),
            float(options['min_lift']),
        )

        self.stdout.write(self.style.SUCCESS(
            f'Rules cached: {cache_status} | key={cache_key} | consequents={total_rules}'
        ))
