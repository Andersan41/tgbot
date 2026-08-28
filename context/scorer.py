"""
context/scorer.py — Взвешенная оценка контекста и вынесение вердикта.
"""
from dataclasses import dataclass, field
from typing import Optional, List
from loguru import logger

from context.analyzer import ContextSnapshot


# === Константы весов ===
WEIGHT_FEAR_GREED = 0.15
WEIGHT_FUNDING_RATE = 0.25
WEIGHT_LONG_SHORT = 0.20
WEIGHT_OI = 0.15
WEIGHT_NEWS = 0.05
WEIGHT_PRICE_TREND = 0.10

# === Пороги вердиктов ===
VERDICT_CONFIRMED_MIN = 0.4
VERDICT_WEAK_MIN = 0.05
VERDICT_CONFLICTED_MIN = -0.1

# === Пороги OI (согласованы с derivatives/open_interest.py) ===
OI_STRONG_THRESHOLD = 2.0
OI_MODERATE_THRESHOLD = 0.5


@dataclass
class ContextVerdict:
    """Итоговый вердикт контекстного модуля.

    BACKWARD COMPATIBLE — используется в scanner.py и DB.
    Новая архитура использует ContextScore (ниже).
    """
    verdict: str           # CONFIRMED / WEAK / CONFLICTED / BLOCKED (legacy)
    confidence: float      # 0.0 до 1.0
    score: float           # итоговый взвешенный балл [-1.0, 1.0]
    supporting: List[str] = field(default_factory=list)
    opposing: List[str] = field(default_factory=list)
    snapshot: Optional[ContextSnapshot] = None


@dataclass
class ContextScore:
    """Simplified context scoring — no BLOCKED verdict.

    Context never blocks — only provides a score [-1.0, 1.0]
    that feeds into the Probability Engine as a feature.
    """
    score: float           # [-1.0, 1.0]
    confidence: float      # [0.0, 1.0]
    supporting: List[str] = field(default_factory=list)
    opposing: List[str] = field(default_factory=list)
    snapshot: Optional[ContextSnapshot] = None


class ContextScorer:
    """Вычисляет взвешенный score и вердикт на основе ContextSnapshot."""

    def score(self, signal_direction: str, snapshot: ContextSnapshot) -> ContextVerdict:
        """
        Оценивает контекст относительно направления сигнала.
        signal_direction: "BUY" или "SELL"
        """
        total_weight = 0.0
        weighted_sum = 0.0
        supporting = []
        opposing = []

        # --- Fear & Greed (вес 0.15) ---
        if snapshot.fear_greed_value is not None:
            w = WEIGHT_FEAR_GREED
            total_weight += w
            try:
                fg_value = int(snapshot.fear_greed_value)
            except (ValueError, TypeError):
                fg_value = None
            if fg_value is not None:
                score = self._score_fear_greed(fg_value, signal_direction)
                weighted_sum += score * w
                if score > 0:
                    supporting.append(f"F&G={fg_value} ({snapshot.fear_greed_label})")
                elif score < 0:
                    opposing.append(f"F&G={fg_value} ({snapshot.fear_greed_label})")

        # --- Funding Rate (вес 0.25) ---
        if snapshot.funding_rate is not None:
            w = WEIGHT_FUNDING_RATE
            total_weight += w
            try:
                rate = float(snapshot.funding_rate)
            except (ValueError, TypeError):
                rate = None
            if rate is not None:
                score = self._score_funding_rate(rate, signal_direction)
                weighted_sum += score * w
                pct = rate * 100
                if score > 0:
                    supporting.append(f"Funding={pct:.3f}%")
                elif score < 0:
                    opposing.append(f"Funding={pct:.3f}%")

        # --- Long/Short Ratio (вес 0.20) ---
        if snapshot.long_short_ratio is not None:
            w = WEIGHT_LONG_SHORT
            total_weight += w
            try:
                ratio = float(snapshot.long_short_ratio)
            except (ValueError, TypeError):
                ratio = None
            if ratio is not None:
                score = self._score_long_short(ratio, signal_direction)
                weighted_sum += score * w
                if score > 0:
                    supporting.append(f"L/S={ratio:.2f}")
                elif score < 0:
                    opposing.append(f"L/S={ratio:.2f}")

        # --- Open Interest (вес 0.15) ---
        if snapshot.open_interest_delta is not None and not getattr(snapshot, "oi_is_warmup", False):
            w = WEIGHT_OI
            total_weight += w
            try:
                delta = float(snapshot.open_interest_delta)
            except (ValueError, TypeError):
                delta = None
            if delta is not None:
                score = self._score_oi(delta, signal_direction)
                weighted_sum += score * w
                if score > 0:
                    supporting.append(f"OI {delta:+.1f}%")
                elif score < 0:
                    opposing.append(f"OI {delta:+.1f}%")

        # --- News Sentiment (вес 0.15) ---
        if snapshot.news_sentiment_score is not None:
            w = WEIGHT_NEWS
            total_weight += w
            try:
                news_score = float(snapshot.news_sentiment_score)
            except (ValueError, TypeError):
                news_score = None
            if news_score is not None:
                score = self._score_news(news_score, signal_direction)
                weighted_sum += score * w
                if score > 0:
                    supporting.append(f"Новости: позитивные ({news_score:.2f})")
                elif score < 0:
                    opposing.append(f"Новости: негативные ({news_score:.2f})")

        # --- Price Trend (вес 0.10) ---
        if snapshot.price_change_7d is not None:
            w = WEIGHT_PRICE_TREND
            total_weight += w
            try:
                change_7d = float(snapshot.price_change_7d)
            except (ValueError, TypeError):
                change_7d = None
            if change_7d is not None:
                score = self._score_price_trend(change_7d, signal_direction)
                weighted_sum += score * w
                if score > 0:
                    supporting.append(f"7d+{change_7d:.1f}%")
                elif score < 0:
                    opposing.append(f"7d{change_7d:.1f}%")

        # Нормализация: перераспределение весов если некоторые источники недоступны
        if total_weight > 0:
            final_score = weighted_sum / total_weight
        else:
            final_score = 0.0

        # Ограничение [-1.0, 1.0]
        final_score = max(-1.0, min(1.0, final_score))

        # Вердикт
        if final_score >= VERDICT_CONFIRMED_MIN:
            verdict = "CONFIRMED"
        elif final_score >= VERDICT_WEAK_MIN:
            verdict = "WEAK"
        elif final_score >= VERDICT_CONFLICTED_MIN:
            verdict = "CONFLICTED"
        else:
            verdict = "BLOCKED"

        confidence = abs(final_score)

        logger.info(
            f"Context verdict: {verdict} (score={final_score:.2f}, "
            f"conf={confidence:.0%}) for {signal_direction} {snapshot.symbol}"
        )

        return ContextVerdict(
            verdict=verdict,
            confidence=confidence,
            score=final_score,
            supporting=supporting,
            opposing=opposing,
            snapshot=snapshot,
        )

    # --- Методы оценки отдельных метрик ---

    def _score_fear_greed(self, value: int, direction: str) -> float:
        if direction == "BUY":
            if value < 20:
                return 0.8
            elif value < 40:
                return 0.1
            elif value < 60:
                return 0.0
            elif value < 80:
                return -0.3
            else:
                return -0.8
        else:  # SELL
            if value < 20:
                return -0.8
            elif value < 40:
                return 0.1
            elif value < 60:
                return 0.0
            elif value < 80:
                return 0.3
            else:
                return 0.8

    def _score_funding_rate(self, rate: float, direction: str) -> float:
        """Score funding rate using configurable thresholds from config.derivatives."""
        from config.settings import config
        strong = config.derivatives.funding_strong_threshold
        neutral = config.derivatives.funding_neutral_zone

        abs_rate = abs(rate)

        if abs_rate <= neutral:
            # Neutral zone — no signal
            return 0.1

        if abs_rate >= strong:
            # Strong funding
            if direction == "BUY":
                # Negative funding (shorts paying longs) is bullish
                return 0.9 if rate < 0 else -0.9
            else:
                # Positive funding (longs paying shorts) is bearish
                return -0.9 if rate < 0 else 0.9
        else:
            # Moderate funding
            if direction == "BUY":
                return 0.4 if rate < 0 else -0.4
            else:
                return -0.4 if rate < 0 else 0.4

    def _score_long_short(self, ratio: float, direction: str) -> float:
        if direction == "BUY":
            if ratio < 0.7:
                return 0.7
            elif ratio <= 1.2:
                return 0.0
            else:
                return -0.6
        else:  # SELL
            if ratio < 0.7:
                return -0.6
            elif ratio <= 1.2:
                return 0.0
            else:
                return 0.7

    def _score_oi(self, delta: float, direction: str) -> float:
        """Score open interest delta using configurable thresholds."""
        from config.settings import config
        strong = config.derivatives.oi_strong_threshold
        moderate = config.derivatives.oi_moderate_threshold

        if direction == "BUY":
            if delta > strong:
                return 0.5
            elif delta > moderate:
                return 0.3
            elif delta > 0.0:
                return 0.2
            elif delta > -moderate:
                return -0.1
            elif delta > -strong:
                return -0.2
            else:
                return -0.3
        else:  # SELL
            if delta < -strong:
                return 0.5
            elif delta < -moderate:
                return 0.3
            elif delta < 0.0:
                return 0.2
            elif delta < moderate:
                return -0.1
            elif delta < strong:
                return -0.2
            else:
                return -0.3

    def _score_news(self, score: float, direction: str) -> float:
        return score

    def _score_price_trend(self, change_7d: float, direction: str) -> float:
        if direction == "BUY":
            if change_7d > 5:
                return 0.5
            elif change_7d > 0:
                return 0.3
            elif change_7d > -5:
                return 0.0
            else:
                return -0.5
        else:  # SELL
            if change_7d > 5:
                return -0.5
            elif change_7d > 0:
                return 0.0
            elif change_7d > -5:
                return 0.3
            else:
                return 0.5


    def score_simple(self, signal_direction: str, snapshot: ContextSnapshot) -> ContextScore:
        """Simplified context scoring — returns ContextScore (no BLOCKED).

        Same logic as score() but returns ContextScore instead of ContextVerdict.
        Context never blocks — only provides a score for the Probability Engine.
        """
        verdict = self.score(signal_direction, snapshot)
        return ContextScore(
            score=verdict.score,
            confidence=verdict.confidence,
            supporting=verdict.supporting,
            opposing=verdict.opposing,
            snapshot=verdict.snapshot,
        )


# Singleton
context_scorer = ContextScorer()
