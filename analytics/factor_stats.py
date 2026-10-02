"""
analytics/factor_stats.py — Contribution analysis and false positive analysis.

Task 8.2 — Contribution analysis:
  Per-factor and per-combination winrate, profit factor, expectancy.

Task 8.3 — False positive analysis:
  STRONG signals that hit SL, factors most often in losing trades,
  best/worst factor combinations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from backtest.engine import BacktestTrade


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class FactorContribution:
    """Performance stats for a single factor across all trades where it was present."""
    factor: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    winrate: float = 0.0
    avg_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    avg_strength: float = 0.0  # avg factor strength when present
    false_positive_count: int = 0  # STRONG signals that hit SL


@dataclass
class FactorCombination:
    """Performance stats for a specific combination of factors."""
    factors: tuple[str, ...]  # sorted tuple of factor names
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    winrate: float = 0.0
    avg_pnl: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    avg_rr: float = 0.0


@dataclass
class FalsePositiveReport:
    """Analysis of false positive signals (STRONG/MODERATE that hit SL)."""
    total_signals: int = 0
    strong_signals: int = 0
    strong_hit_sl: int = 0
    moderate_hit_sl: int = 0
    strong_fp_rate: float = 0.0  # strong_hit_sl / strong_signals
    moderate_fp_rate: float = 0.0
    worst_factors: list[FactorContribution] = field(default_factory=list)
    best_combinations: list[FactorCombination] = field(default_factory=list)
    worst_combinations: list[FactorCombination] = field(default_factory=list)
    fp_by_regime: dict[str, dict[str, int]] = field(default_factory=dict)
    fp_by_verdict: dict[str, dict[str, int]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Core analysis functions
# ---------------------------------------------------------------------------

def _verdict(t: BacktestTrade) -> str:
    """Case-insensitive verdict bucket.

    Prod writers store lowercase score_verdict ('strong'/'moderate'/'weak'),
    legacy datasets store 'STRONG'/'MODERATE'/'WEAK'. Comparisons must
    normalize or lowercase data silently skips every STRONG filter.
    """
    return (t.verdict or "").upper()


def compute_factor_stats(trades: list[BacktestTrade]) -> dict[str, FactorContribution]:
    """Compute per-factor contribution stats.

    For each factor, looks at all trades where the factor was present
    (factor_present[name] == True) and computes winrate, PF, expectancy.
    """
    factor_trades: dict[str, list[BacktestTrade]] = {}
    factor_strengths: dict[str, list[float]] = {}

    for t in trades:
        for fname, present in t.factor_present.items():
            if present:
                factor_trades.setdefault(fname, []).append(t)
                factor_strengths.setdefault(fname, []).append(
                    t.factor_strengths.get(fname, 0.0)
                )

    result: dict[str, FactorContribution] = {}
    for fname, ftrades in factor_trades.items():
        total = len(ftrades)
        wins = [t for t in ftrades if t.pnl_pct > 0]
        losses = [t for t in ftrades if t.pnl_pct <= 0]
        win_count = len(wins)
        loss_count = len(losses)

        gross_profit = sum(t.pnl_pct for t in wins) if wins else 0.0
        gross_loss = abs(sum(t.pnl_pct for t in losses)) if losses else 1.0
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        winrate = win_count / total if total > 0 else 0.0
        avg_win = gross_profit / win_count if win_count > 0 else 0.0
        avg_loss = gross_loss / loss_count if loss_count > 0 else 0.0
        expectancy = winrate * avg_win - (1 - winrate) * avg_loss

        # False positives: STRONG verdict trades that hit SL
        fp_count = sum(
            1 for t in ftrades
            if _verdict(t) == "STRONG" and t.exit_reason == "sl"
        )

        avg_str = (
            sum(factor_strengths.get(fname, [])) / len(factor_strengths[fname])
            if fname in factor_strengths and factor_strengths[fname]
            else 0.0
        )

        result[fname] = FactorContribution(
            factor=fname,
            total_trades=total,
            wins=win_count,
            losses=loss_count,
            winrate=round(winrate * 100, 1),
            avg_pnl=round(sum(t.pnl_pct for t in ftrades) / total, 4) if total else 0.0,
            gross_profit=round(gross_profit, 4),
            gross_loss=round(gross_loss, 4),
            profit_factor=round(pf, 2),
            expectancy=round(expectancy, 4),
            avg_strength=round(avg_str, 4),
            false_positive_count=fp_count,
        )

    return result


def compute_combination_stats(
    trades: list[BacktestTrade],
    min_occurrences: int = 3,
) -> list[FactorCombination]:
    """Compute stats for factor combinations.

    Groups trades by the set of factors that were present, then computes
    winrate, PF, expectancy for each combination that appears at least
    min_occurrences times.
    """
    combo_trades: dict[tuple[str, ...], list[BacktestTrade]] = {}

    for t in trades:
        active = tuple(sorted(k for k, v in t.factor_present.items() if v))
        if active:
            combo_trades.setdefault(active, []).append(t)

    result: list[FactorCombination] = []
    for factors, ctrades in combo_trades.items():
        if len(ctrades) < min_occurrences:
            continue

        total = len(ctrades)
        wins = [t for t in ctrades if t.pnl_pct > 0]
        losses = [t for t in ctrades if t.pnl_pct <= 0]
        win_count = len(wins)

        gross_profit = sum(t.pnl_pct for t in wins) if wins else 0.0
        gross_loss = abs(sum(t.pnl_pct for t in losses)) if losses else 1.0
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        winrate = win_count / total if total > 0 else 0.0
        avg_win = gross_profit / win_count if win_count > 0 else 0.0
        avg_loss = gross_loss / len(losses) if losses else 0.0
        expectancy = winrate * avg_win - (1 - winrate) * avg_loss

        avg_rr = sum(t.rr for t in ctrades) / total if total else 0.0

        result.append(FactorCombination(
            factors=factors,
            total_trades=total,
            wins=win_count,
            losses=len(losses),
            winrate=round(winrate * 100, 1),
            avg_pnl=round(sum(t.pnl_pct for t in ctrades) / total, 4),
            profit_factor=round(pf, 2),
            expectancy=round(expectancy, 4),
            avg_rr=round(avg_rr, 2),
        ))

    # Sort by expectancy descending
    result.sort(key=lambda c: c.expectancy, reverse=True)
    return result


def compute_false_positives(
    trades: list[BacktestTrade],
    factor_stats: Optional[dict[str, FactorContribution]] = None,
    combination_stats: Optional[list[FactorCombination]] = None,
    min_occurrences: int = 3,
) -> FalsePositiveReport:
    """Analyze false positive signals.

    A false positive is a STRONG or MODERATE verdict signal that hit SL.
    """
    if factor_stats is None:
        factor_stats = compute_factor_stats(trades)
    if combination_stats is None:
        combination_stats = compute_combination_stats(trades, min_occurrences=min_occurrences)

    total = len(trades)
    strong_trades = [t for t in trades if _verdict(t) == "STRONG"]
    moderate_trades = [t for t in trades if _verdict(t) == "MODERATE"]
    strong_count = len(strong_trades)
    strong_sl = sum(1 for t in strong_trades if t.exit_reason == "sl")
    moderate_sl = sum(1 for t in moderate_trades if t.exit_reason == "sl")

    # Worst factors: highest false positive count among factors with >= 3 trades
    factors_with_fp = [
        fs for fs in factor_stats.values()
        if fs.total_trades >= 3 and fs.false_positive_count > 0
    ]
    factors_with_fp.sort(key=lambda f: f.false_positive_count, reverse=True)
    worst_factors = factors_with_fp[:5]

    # Best/worst combinations by winrate
    best_combos = [c for c in combination_stats if c.winrate >= 50][:5]
    worst_combos = [c for c in combination_stats if c.winrate < 50][:5]
    worst_combos.sort(key=lambda c: c.winrate)

    # FP by regime
    regime_fp: dict[str, dict[str, int]] = {}
    for t in trades:
        regime = t.regime or "unknown"
        regime_fp.setdefault(regime, {"total": 0, "sl": 0, "strong_sl": 0})
        regime_fp[regime]["total"] += 1
        if t.exit_reason == "sl":
            regime_fp[regime]["sl"] += 1
            if _verdict(t) == "STRONG":
                regime_fp[regime]["strong_sl"] += 1

    # FP by verdict
    verdict_fp: dict[str, dict[str, int]] = {}
    for t in trades:
        v = _verdict(t) or "UNKNOWN"
        verdict_fp.setdefault(v, {"total": 0, "sl": 0, "tp": 0, "eob": 0})
        verdict_fp[v]["total"] += 1
        if t.exit_reason == "sl":
            verdict_fp[v]["sl"] += 1
        elif t.exit_reason == "tp":
            verdict_fp[v]["tp"] += 1
        elif t.exit_reason == "eob":
            verdict_fp[v]["eob"] += 1

    return FalsePositiveReport(
        total_signals=total,
        strong_signals=strong_count,
        strong_hit_sl=strong_sl,
        moderate_hit_sl=moderate_sl,
        strong_fp_rate=round(strong_sl / strong_count * 100, 1) if strong_count else 0.0,
        moderate_fp_rate=round(moderate_sl / len(moderate_trades) * 100, 1) if moderate_trades else 0.0,
        worst_factors=worst_factors,
        best_combinations=best_combos,
        worst_combinations=worst_combos,
        fp_by_regime=regime_fp,
        fp_by_verdict=verdict_fp,
    )
