"""
strategy/weight_manager.py — Weight Manager

Corrects Probability Engine outputs based on cumulative statistics.
ProbabilityEngine outputs a raw probability → WeightManager adjusts.

Rec 4b: replaced the no-op stub with a real, conservative adjustment that
activates only once a scenario type has enough closed trades (threshold from
config.scoring or env). It corrects the evaluation towards the observed
performance (winrate vs expected P(TP)) and dampens overconfident estimates.

Flow:
    ProbabilityEngine.estimate_scenario() → ScenarioEvaluation
    WeightManager.adjust(evaluation, symbol, scenario_name, direction) → ScenarioEvaluation
"""
from __future__ import annotations

import os
from typing import Optional

from loguru import logger

# from strategy.scenario_engine import ScenarioEvaluation  # DELETED module
# from strategy.scenario_memory import scenario_memory  # DELETED module


class WeightManager:
    """Adjusts scenario evaluations based on cumulative statistics.

    Does NOT modify the Probability Engine — works as a separate layer.
    Probability outputs assessment → WeightManager corrects.
    """

    # Минимальное число закрытых сделок для коррекции (по умолчанию 30 —
    # порог `is_reliable` в ScenarioStats; 300 — слишком долго ждать).
    MIN_SCENARIOS_FOR_ADJUSTMENT = int(os.getenv("WEIGHT_MANAGER_MIN_SCENARIOS", "30"))
    # Максимальное усиление P(TP) (если наблюдаемый winrate выше expected)
    MAX_BOOST_FACTOR = 1.25
    # Максимальное ослабление P(TP) (если наблюдаемый winrate ниже expected)
    MAX_PENALTY_FACTOR = 0.75

    def adjust(
        self,
        evaluation: ScenarioEvaluation,
        symbol: str,
        scenario_name: str,
        direction: str = "",
        regime: Optional[str] = None,
    ) -> ScenarioEvaluation:
        """Adjust evaluation based on historical scenario statistics.

        Correction: p_new = p_raw * clamp(winrate / expected_p_tp).
        - If observed winrate == expected P(TP) → no change (well calibrated).
        - If observed winrate > expected → small boost (capped).
        - If observed winrate < expected → penalty (floored).
        Applies only when stats are reliable (closed_count >= MIN_SCENARIOS).
        """
        stats = scenario_memory.get_stats(symbol, scenario_name, direction)

        if stats is None or stats.closed_count < self.MIN_SCENARIOS_FOR_ADJUSTMENT:
            # Not enough data — return unchanged
            return evaluation

        expected_p = stats.avg_expected_p_tp
        observed_wr = stats.winrate

        if expected_p <= 0.0:
            expected_p = 0.5  # no expected data → assume neutral calibration

        ratio = observed_wr / expected_p
        ratio = max(self.MAX_PENALTY_FACTOR, min(self.MAX_BOOST_FACTOR, ratio))

        original_p = evaluation.probability
        evaluation.probability = max(0.05, min(0.90, original_p * ratio))

        # Dampen confidence towards the amount of data available.
        # Few trades → keep engine confidence; many trades → blend with empirical.
        n = stats.closed_count
        confidence_weight = min(0.5, n / 100.0)  # 0 → 0.5 as n → 50+
        empirical_conf = 0.4 + observed_wr * 0.4  # 0.4..0.8 based on WR
        evaluation.confidence = (
            evaluation.confidence * (1 - confidence_weight)
            + empirical_conf * confidence_weight
        )

        logger.debug(
            f"WeightManager: {symbol} {scenario_name} "
            f"n={stats.closed_count} expected_p={expected_p:.2f} "
            f"wr={observed_wr:.2f} ratio={ratio:.2f} "
            f"p {original_p:.2f} → {evaluation.probability:.2f}"
        )

        return evaluation


# Module-level singleton
weight_manager = WeightManager()