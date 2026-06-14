from collections import defaultdict
from random import Random
import csv
from pathlib import Path

from django.core.management.base import BaseCommand
from efficient_apriori import apriori

from django.utils import timezone
from core.models import BookRating, BookReview, BookVote, MeetingRegistration


class Command(BaseCommand):
    help = "Offline recommendation evaluation with model comparison (leave-one-out)."

    def add_arguments(self, parser):
        parser.add_argument('--k', type=int, default=5, help='Top-K recommendations for evaluation')
        parser.add_argument('--seed', type=int, default=42, help='Random seed for reproducibility')
        parser.add_argument(
            '--mode',
            choices=['all', 'collaborative', 'apriori', 'hybrid'],
            default='all',
            help='Model to evaluate',
        )
        parser.add_argument('--min-support', type=float, default=0.05, help='Apriori min support')
        parser.add_argument('--min-confidence', type=float, default=0.2, help='Apriori min confidence')
        parser.add_argument('--min-lift', type=float, default=1.05, help='Apriori min lift')
        parser.add_argument('--top-k-neighbors', type=int, default=10, help='Collaborative neighborhood size')
        parser.add_argument('--apriori-weight', type=float, default=0.6, help='Hybrid apriori weight')
        parser.add_argument('--collab-weight', type=float, default=0.4, help='Hybrid collaborative weight')
        parser.add_argument('--csv', type=str, default='', help='Optional path to save comparison metrics as CSV')

    def handle(self, *args, **options):
        k = max(1, int(options['k']))
        rng = Random(int(options['seed']))
        mode = options['mode']

        min_support = float(options['min_support'])
        min_confidence = float(options['min_confidence'])
        min_lift = float(options['min_lift'])
        top_k_neighbors = max(1, int(options['top_k_neighbors']))
        apriori_weight = float(options['apriori_weight'])
        collab_weight = float(options['collab_weight'])
        csv_path = (options.get('csv') or '').strip()

        apriori_weight, collab_weight = self._normalize_weights(apriori_weight, collab_weight)

        user_items = self._build_user_items()

        eligible_users = [uid for uid, items in user_items.items() if len(items) >= 2]
        if not eligible_users:
            self.stdout.write(self.style.WARNING('Not enough data: need users with at least 2 interactions.'))
            return

        if mode == 'all':
            modes = ['collaborative', 'apriori', 'hybrid']
        else:
            modes = [mode]

        metrics = {
            m: {
                'hits': 0,
                'evaluated_users': 0,
                'empty_recommendations': 0,
            }
            for m in modes
        }

        for user_id in eligible_users:
            full_items = set(user_items[user_id])
            holdout = rng.choice(sorted(full_items))
            train_items = full_items - {holdout}

            # Транзакции для current user строим на train_items (без holdout).
            effective_user_items = {uid: set(items) for uid, items in user_items.items()}
            effective_user_items[user_id] = set(train_items)

            for current_mode in modes:
                recommendations = self._recommend(
                    mode=current_mode,
                    user_id=user_id,
                    user_items=effective_user_items,
                    train_items=train_items,
                    k=k,
                    min_support=min_support,
                    min_confidence=min_confidence,
                    min_lift=min_lift,
                    top_k_neighbors=top_k_neighbors,
                    apriori_weight=apriori_weight,
                    collab_weight=collab_weight,
                )

                if not recommendations:
                    metrics[current_mode]['empty_recommendations'] += 1

                if holdout in recommendations:
                    metrics[current_mode]['hits'] += 1
                metrics[current_mode]['evaluated_users'] += 1

        self.stdout.write(self.style.SUCCESS('Offline recommendation evaluation (leave-one-out):'))
        self.stdout.write(f'  users_evaluated: {len(eligible_users)}')
        self.stdout.write(f'  k: {k}')
        self.stdout.write(f'  modes: {", ".join(modes)}')

        self.stdout.write('')
        self.stdout.write('Model comparison:')
        self.stdout.write('  mode           hits   precision@k   recall@k   hit_rate@k   empty_reco')

        for current_mode in modes:
            hits = metrics[current_mode]['hits']
            evaluated_users = metrics[current_mode]['evaluated_users']
            empty_reco = metrics[current_mode]['empty_recommendations']

            precision_at_k = hits / (evaluated_users * k) if evaluated_users else 0.0
            recall_at_k = hits / evaluated_users if evaluated_users else 0.0
            hit_rate_at_k = hits / evaluated_users if evaluated_users else 0.0

            self.stdout.write(
                f'  {current_mode:<13}{hits:<7}{precision_at_k:<14.4f}{recall_at_k:<11.4f}{hit_rate_at_k:<13.4f}{empty_reco}'
            )

        if csv_path:
            self._write_csv(
                csv_path=csv_path,
                k=k,
                modes=modes,
                metrics=metrics,
            )
            self.stdout.write(self.style.SUCCESS(f'CSV saved: {csv_path}'))

    def _write_csv(self, csv_path, k, modes, metrics):
        path = Path(csv_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open('w', newline='', encoding='utf-8') as fh:
            writer = csv.writer(fh)
            writer.writerow([
                'mode',
                'k',
                'users_evaluated',
                'hits',
                'precision_at_k',
                'recall_at_k',
                'hit_rate_at_k',
                'empty_recommendations',
            ])

            for current_mode in modes:
                hits = metrics[current_mode]['hits']
                evaluated_users = metrics[current_mode]['evaluated_users']
                empty_reco = metrics[current_mode]['empty_recommendations']
                precision_at_k = hits / (evaluated_users * k) if evaluated_users else 0.0
                recall_at_k = hits / evaluated_users if evaluated_users else 0.0
                hit_rate_at_k = hits / evaluated_users if evaluated_users else 0.0

                writer.writerow([
                    current_mode,
                    k,
                    evaluated_users,
                    hits,
                    f'{precision_at_k:.6f}',
                    f'{recall_at_k:.6f}',
                    f'{hit_rate_at_k:.6f}',
                    empty_reco,
                ])

    def _recommend(
        self,
        mode,
        user_id,
        user_items,
        train_items,
        k,
        min_support,
        min_confidence,
        min_lift,
        top_k_neighbors,
        apriori_weight,
        collab_weight,
    ):
        if mode == 'collaborative':
            return self._recommend_collaborative(user_id, user_items, train_items, k, top_k_neighbors)
        if mode == 'apriori':
            return self._recommend_apriori(user_id, user_items, train_items, k, min_support, min_confidence, min_lift)
        if mode == 'hybrid':
            return self._recommend_hybrid(
                user_id=user_id,
                user_items=user_items,
                train_items=train_items,
                k=k,
                min_support=min_support,
                min_confidence=min_confidence,
                min_lift=min_lift,
                top_k_neighbors=top_k_neighbors,
                apriori_weight=apriori_weight,
                collab_weight=collab_weight,
            )
        return []

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
            return 0.6, 0.4
        return apriori_weight / total, collab_weight / total

    def _build_user_items(self):
        items = defaultdict(set)

        for row in BookVote.objects.values('user_id', 'book_id'):
            items[row['user_id']].add(row['book_id'])

        for row in BookRating.objects.filter(rating__in=['GREEN', 'YELLOW']).values('user_id', 'book_id'):
            items[row['user_id']].add(row['book_id'])

        for row in BookReview.objects.values('user_id', 'book_id'):
            items[row['user_id']].add(row['book_id'])

        for row in MeetingRegistration.objects.filter(
            meeting__discussed_book__isnull=False,
            is_attended=True,
        ).filter(
            Q(meeting__status='COMPLETED') | Q(meeting__date__lte=timezone.now())
        ).values('user_id', 'meeting__discussed_book_id'):
            items[row['user_id']].add(row['meeting__discussed_book_id'])

        return items

    def _recommend_collaborative(self, user_id, user_items, train_items, k, top_k_neighbors):
        neighbors = []
        for other_user_id, other_items in user_items.items():
            if other_user_id == user_id:
                continue
            inter = len(train_items & other_items)
            if inter == 0:
                continue
            union = len(train_items | other_items)
            sim = (inter / union) if union else 0.0
            if sim > 0:
                neighbors.append((other_user_id, sim))

        neighbors.sort(key=lambda x: x[1], reverse=True)

        score_map = defaultdict(float)
        for other_user_id, sim in neighbors[:top_k_neighbors]:
            for book_id in user_items[other_user_id]:
                if book_id in train_items:
                    continue
                score_map[book_id] += sim

        ranked = sorted(score_map.items(), key=lambda x: x[1], reverse=True)
        return [book_id for book_id, _ in ranked[:k]]

    def _mine_rules_with_apriori(self, user_items, min_support, min_confidence, min_lift):
        transactions = [tuple(sorted(items)) for items in user_items.values() if items]
        if not transactions:
            return {}

        _, rules = apriori(
            transactions,
            min_support=min_support,
            min_confidence=min_confidence,
            max_length=2,
        )

        out = {}
        for rule in rules:
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
            })
        return out

    def _recommend_apriori(self, user_id, user_items, train_items, k, min_support, min_confidence, min_lift):
        rules = self._mine_rules_with_apriori(
            user_items=user_items,
            min_support=min_support,
            min_confidence=min_confidence,
            min_lift=min_lift,
        )
        score_map = defaultdict(float)
        for seen_book_id in train_items:
            for rec in rules.get(seen_book_id, []):
                candidate = rec['book_id']
                if candidate in train_items:
                    continue
                score_map[candidate] += rec['confidence'] * rec['lift']
        ranked = sorted(score_map.items(), key=lambda x: x[1], reverse=True)
        return [book_id for book_id, _ in ranked[:k]]

    def _recommend_hybrid(
        self,
        user_id,
        user_items,
        train_items,
        k,
        min_support,
        min_confidence,
        min_lift,
        top_k_neighbors,
        apriori_weight,
        collab_weight,
    ):
        collab_recs = self._recommend_collaborative(user_id, user_items, train_items, k * 3, top_k_neighbors)
        apriori_recs = self._recommend_apriori(user_id, user_items, train_items, k * 3, min_support, min_confidence, min_lift)

        collab_score_map = {bid: float(len(collab_recs) - idx) for idx, bid in enumerate(collab_recs)}
        apriori_score_map = {bid: float(len(apriori_recs) - idx) for idx, bid in enumerate(apriori_recs)}

        norm_collab = self._normalize_score_map(collab_score_map)
        norm_apriori = self._normalize_score_map(apriori_score_map)

        combined = {}
        for bid in set(collab_score_map.keys()) | set(apriori_score_map.keys()):
            if bid in train_items:
                continue
            combined[bid] = (
                collab_weight * norm_collab.get(bid, 0.0)
                + apriori_weight * norm_apriori.get(bid, 0.0)
            )

        ranked = sorted(combined.items(), key=lambda x: x[1], reverse=True)
        return [book_id for book_id, _ in ranked[:k]]
