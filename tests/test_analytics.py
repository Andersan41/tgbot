import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.engine import BacktestTrade
from analytics.factor_stats import (
    FactorContribution,
    FactorCombination,
    FalsePositiveReport,
    compute_factor_stats,
    compute_combination_stats,
    compute_false_positives,
)


def _make_trade(
    pnl: float = 1.0,
    verdict: str = "STRONG",
    exit_reason: str = "tp",
    factors: dict | None = None,
    regime: str = "trend",
    rr: float = 2.0,
) -> BacktestTrade:
    """Helper to create a BacktestTrade with factor data."""
    if factors is None:
        factors = {"Supertrend": 1.0, "EMA": 0.5, "MACD": 0.3}
    return BacktestTrade(
        symbol="BTC/USDT",
        timeframe="1h",
        direction="BUY",
        entry_price=50000.0,
        entry_index=0,
        entry_timestamp="2024-01-01",
        sl=49000.0,
        tp=52000.0,
        exit_price=50000.0 + pnl * 500,
        exit_index=1,
        exit_timestamp="2024-01-02",
        exit_reason=exit_reason,
        pnl_pct=pnl,
        rr=rr,
        regime=regime,
        verdict=verdict,
        factor_strengths=factors,
        factor_present={k: v > 0 for k, v in factors.items()},
    )


class TestFactorContribution:
    def test_single_factor_winrate(self):
        """Factor present in 4 wins, 1 loss → 80% WR."""
        trades = [
            _make_trade(pnl=2.0, factors={"Supertrend": 1.0}),
            _make_trade(pnl=1.5, factors={"Supertrend": 0.8}),
            _make_trade(pnl=3.0, factors={"Supertrend": 0.5}),
            _make_trade(pnl=1.0, factors={"Supertrend": 1.0}),
            _make_trade(pnl=-1.0, factors={"Supertrend": 0.3}),
        ]
        stats = compute_factor_stats(trades)
        assert "Supertrend" in stats
        st = stats["Supertrend"]
        assert st.total_trades == 5
        assert st.wins == 4
        assert st.losses == 1
        assert st.winrate == 80.0

    def test_profit_factor_calculation(self):
        """PF = gross_profit / gross_loss."""
        trades = [
            _make_trade(pnl=4.0, factors={"MACD": 1.0}),
            _make_trade(pnl=2.0, factors={"MACD": 0.5}),
            _make_trade(pnl=-2.0, factors={"MACD": 0.3}),
        ]
        stats = compute_factor_stats(trades)
        macd = stats["MACD"]
        # gross_profit=6.0, gross_loss=2.0, PF=3.0
        assert macd.profit_factor == 3.0

    def test_expectancy_calculation(self):
        """Expectancy = winrate * avg_win - (1-winrate) * avg_loss."""
        trades = [
            _make_trade(pnl=3.0, factors={"EMA": 1.0}),
            _make_trade(pnl=-1.0, factors={"EMA": 0.5}),
        ]
        stats = compute_factor_stats(trades)
        ema = stats["EMA"]
        # winrate=0.5, avg_win=3.0, avg_loss=1.0
        # expectancy = 0.5*3.0 - 0.5*1.0 = 1.0
        assert ema.expectancy == pytest.approx(1.0, abs=0.01)

    def test_factor_not_present_excluded(self):
        """Trade where factor is negative should not count for that factor."""
        trades = [
            _make_trade(pnl=2.0, factors={"Supertrend": 1.0, "RSI": -0.5}),
            _make_trade(pnl=1.0, factors={"Supertrend": 0.8, "RSI": 0.3}),
        ]
        stats = compute_factor_stats(trades)
        # RSI only present in 1 trade (second one)
        assert "RSI" in stats
        assert stats["RSI"].total_trades == 1
        assert stats["Supertrend"].total_trades == 2

    def test_false_positive_count(self):
        """STRONG verdict + SL exit should count as false positive."""
        trades = [
            _make_trade(pnl=-1.0, verdict="STRONG", exit_reason="sl",
                        factors={"Supertrend": 1.0}),
            _make_trade(pnl=2.0, verdict="STRONG", exit_reason="tp",
                        factors={"Supertrend": 1.0}),
            _make_trade(pnl=-0.5, verdict="WEAK", exit_reason="sl",
                        factors={"Supertrend": 1.0}),
        ]
        stats = compute_factor_stats(trades)
        assert stats["Supertrend"].false_positive_count == 1

    def test_avg_strength(self):
        """Average factor strength should be computed correctly."""
        trades = [
            _make_trade(pnl=2.0, factors={"MACD": 1.0}),
            _make_trade(pnl=1.0, factors={"MACD": 0.5}),
            _make_trade(pnl=0.5, factors={"MACD": 0.3}),
        ]
        stats = compute_factor_stats(trades)
        # avg = (1.0 + 0.5 + 0.3) / 3 = 0.6
        assert stats["MACD"].avg_strength == pytest.approx(0.6, abs=0.01)

    def test_empty_trades(self):
        """Empty trade list should return empty dict."""
        stats = compute_factor_stats([])
        assert stats == {}

    def test_multiple_factors(self):
        """Multiple factors should each get their own stats."""
        trades = [
            _make_trade(pnl=2.0, factors={
                "Supertrend": 1.0, "EMA": 0.5, "MACD": 0.3, "Volume": 0.8
            }),
            _make_trade(pnl=-1.0, factors={
                "Supertrend": 0.5, "EMA": -0.3, "RSI": 0.6
            }),
        ]
        stats = compute_factor_stats(trades)
        assert "Supertrend" in stats
        assert "EMA" in stats
        assert "MACD" in stats
        assert "Volume" in stats
        assert "RSI" in stats
        # Supertrend present in both
        assert stats["Supertrend"].total_trades == 2
        # EMA only in first (second has negative strength)
        assert stats["EMA"].total_trades == 1


class TestCombinationStats:
    def test_basic_combination(self):
        """Same factor combination should be grouped together."""
        factors = {"Supertrend": 1.0, "EMA": 0.5, "MACD": 0.3}
        trades = [
            _make_trade(pnl=2.0, factors=factors),
            _make_trade(pnl=1.0, factors=factors),
            _make_trade(pnl=-1.0, factors=factors),
        ]
        combos = compute_combination_stats(trades, min_occurrences=1)
        assert len(combos) == 1
        combo = combos[0]
        assert combo.factors == ("EMA", "MACD", "Supertrend")  # sorted
        assert combo.total_trades == 3
        assert combo.wins == 2
        assert combo.losses == 1

    def test_min_occurrences_filter(self):
        """Combinations below min_occurrences should be excluded."""
        factors_a = {"Supertrend": 1.0, "EMA": 0.5}
        factors_b = {"MACD": 0.3, "Volume": 0.8}
        trades = [
            _make_trade(pnl=2.0, factors=factors_a),
            _make_trade(pnl=1.0, factors=factors_a),
            _make_trade(pnl=-1.0, factors=factors_b),  # only 1 occurrence
        ]
        combos = compute_combination_stats(trades, min_occurrences=2)
        assert len(combos) == 1
        assert combos[0].factors == ("EMA", "Supertrend")

    def test_sorted_by_expectancy(self):
        """Results should be sorted by expectancy descending."""
        factors_good = {"Supertrend": 1.0}
        factors_bad = {"RSI": 0.5}
        trades = [
            _make_trade(pnl=5.0, factors=factors_good),
            _make_trade(pnl=3.0, factors=factors_good),
            _make_trade(pnl=-1.0, factors=factors_bad),
            _make_trade(pnl=-2.0, factors=factors_bad),
        ]
        combos = compute_combination_stats(trades, min_occurrences=1)
        assert len(combos) == 2
        assert combos[0].expectancy > combos[1].expectancy

    def test_empty_trades(self):
        """Empty trade list should return empty list."""
        combos = compute_combination_stats([])
        assert combos == []

    def test_different_combinations(self):
        """Different factor sets should produce separate combinations."""
        trades = [
            _make_trade(pnl=2.0, factors={"Supertrend": 1.0, "EMA": 0.5}),
            _make_trade(pnl=1.0, factors={"Supertrend": 1.0, "MACD": 0.3}),
            _make_trade(pnl=-1.0, factors={"Supertrend": 1.0, "EMA": 0.5}),
            _make_trade(pnl=0.5, factors={"Supertrend": 1.0, "MACD": 0.3}),
        ]
        combos = compute_combination_stats(trades, min_occurrences=1)
        assert len(combos) == 2
        combo_factors = {c.factors for c in combos}
        assert ("EMA", "Supertrend") in combo_factors
        assert ("MACD", "Supertrend") in combo_factors


class TestFalsePositives:
    def test_strong_fp_rate(self):
        """Strong FP rate = strong_hit_sl / strong_signals."""
        trades = [
            _make_trade(pnl=-1.0, verdict="STRONG", exit_reason="sl"),
            _make_trade(pnl=-0.5, verdict="STRONG", exit_reason="sl"),
            _make_trade(pnl=2.0, verdict="STRONG", exit_reason="tp"),
            _make_trade(pnl=1.0, verdict="MODERATE", exit_reason="sl"),
        ]
        report = compute_false_positives(trades)
        assert report.total_signals == 4
        assert report.strong_signals == 3
        assert report.strong_hit_sl == 2
        assert report.strong_fp_rate == pytest.approx(66.7, abs=0.1)

    def test_lowercase_prod_verdict_counted(self):
        """Prod stores score_verdict lowercase ('strong') — must bucket as STRONG.

        Regression: uppercase-only comparisons silently skipped all real data.
        """
        trades = [
            _make_trade(pnl=-1.0, verdict="strong", exit_reason="sl"),
            _make_trade(pnl=2.0, verdict="strong", exit_reason="tp"),
            _make_trade(pnl=1.0, verdict="weak", exit_reason="tp"),
        ]
        report = compute_false_positives(trades)
        assert report.strong_signals == 2
        assert report.strong_hit_sl == 1
        assert report.fp_by_verdict["STRONG"]["sl"] == 1
        assert "WEAK" in report.fp_by_verdict

        stats = compute_factor_stats(trades)
        for fs in stats.values():
            assert fs.false_positive_count == 1

    def test_moderate_fp_rate(self):
        """Moderate FP rate computed separately."""
        trades = [
            _make_trade(pnl=-1.0, verdict="MODERATE", exit_reason="sl"),
            _make_trade(pnl=2.0, verdict="MODERATE", exit_reason="tp"),
            _make_trade(pnl=1.0, verdict="MODERATE", exit_reason="tp"),
        ]
        report = compute_false_positives(trades)
        assert report.moderate_hit_sl == 1
        assert report.moderate_fp_rate == pytest.approx(33.3, abs=0.1)

    def test_worst_factors(self):
        """Worst factors should be sorted by false positive count."""
        trades = [
            _make_trade(pnl=-1.0, verdict="STRONG", exit_reason="sl",
                        factors={"Supertrend": 1.0, "EMA": 0.5}),
            _make_trade(pnl=-2.0, verdict="STRONG", exit_reason="sl",
                        factors={"Supertrend": 1.0, "MACD": 0.3}),
            _make_trade(pnl=-0.5, verdict="STRONG", exit_reason="sl",
                        factors={"Supertrend": 1.0, "RSI": 0.6}),
            _make_trade(pnl=2.0, verdict="STRONG", exit_reason="tp",
                        factors={"Volume": 0.8}),
        ]
        report = compute_false_positives(trades)
        assert len(report.worst_factors) >= 1
        # Supertrend should be the worst (3 FPs)
        assert report.worst_factors[0].factor == "Supertrend"
        assert report.worst_factors[0].false_positive_count == 3

    def test_best_worst_combinations(self):
        """Best combos have WR >= 50%, worst have WR < 50%."""
        factors_good = {"Supertrend": 1.0, "EMA": 0.5}
        factors_bad = {"RSI": 0.3}
        trades = [
            _make_trade(pnl=2.0, factors=factors_good),
            _make_trade(pnl=3.0, factors=factors_good),
            _make_trade(pnl=-1.0, factors=factors_bad),
            _make_trade(pnl=-2.0, factors=factors_bad),
            _make_trade(pnl=-0.5, factors=factors_bad),
        ]
        report = compute_false_positives(trades, min_occurrences=1)
        assert len(report.best_combinations) >= 1
        assert len(report.worst_combinations) >= 1
        assert report.best_combinations[0].winrate == 100.0
        assert report.worst_combinations[0].winrate == 0.0

    def test_fp_by_regime(self):
        """FP breakdown by regime should be populated."""
        trades = [
            _make_trade(pnl=-1.0, verdict="STRONG", exit_reason="sl", regime="trend"),
            _make_trade(pnl=-0.5, verdict="STRONG", exit_reason="sl", regime="range"),
            _make_trade(pnl=2.0, verdict="STRONG", exit_reason="tp", regime="trend"),
        ]
        report = compute_false_positives(trades)
        assert "trend" in report.fp_by_regime
        assert "range" in report.fp_by_regime
        assert report.fp_by_regime["trend"]["strong_sl"] == 1
        assert report.fp_by_regime["range"]["strong_sl"] == 1

    def test_fp_by_verdict(self):
        """FP breakdown by verdict should be populated."""
        trades = [
            _make_trade(pnl=-1.0, verdict="STRONG", exit_reason="sl"),
            _make_trade(pnl=2.0, verdict="STRONG", exit_reason="tp"),
            _make_trade(pnl=-0.5, verdict="WEAK", exit_reason="sl"),
            _make_trade(pnl=1.0, verdict="MODERATE", exit_reason="tp"),
            _make_trade(pnl=0.0, verdict="MODERATE", exit_reason="eob"),
        ]
        report = compute_false_positives(trades)
        assert "STRONG" in report.fp_by_verdict
        assert "WEAK" in report.fp_by_verdict
        assert "MODERATE" in report.fp_by_verdict
        assert report.fp_by_verdict["STRONG"]["sl"] == 1
        assert report.fp_by_verdict["STRONG"]["tp"] == 1
        assert report.fp_by_verdict["MODERATE"]["eob"] == 1

    def test_empty_trades(self):
        """Empty trade list should produce zero report."""
        report = compute_false_positives([])
        assert report.total_signals == 0
        assert report.strong_signals == 0
        assert report.strong_fp_rate == 0.0

    def test_no_strong_signals(self):
        """No strong signals → strong_fp_rate = 0."""
        trades = [
            _make_trade(pnl=1.0, verdict="WEAK"),
            _make_trade(pnl=-0.5, verdict="MODERATE", exit_reason="sl"),
        ]
        report = compute_false_positives(trades)
        assert report.strong_signals == 0
        assert report.strong_fp_rate == 0.0


class TestIntegration:
    """Integration tests: factor stats + combination stats + false positives together."""

    def test_full_pipeline(self):
        """Run all three analyses on the same trade set."""
        trades = [
            _make_trade(pnl=3.0, verdict="STRONG", exit_reason="tp",
                        factors={"Supertrend": 1.0, "EMA": 0.8, "MACD": 0.5, "Volume": 0.7}),
            _make_trade(pnl=2.0, verdict="STRONG", exit_reason="tp",
                        factors={"Supertrend": 0.9, "EMA": 0.6, "MACD": 0.4}),
            _make_trade(pnl=-1.0, verdict="STRONG", exit_reason="sl",
                        factors={"Supertrend": 0.5, "RSI": 0.3}),
            _make_trade(pnl=-2.0, verdict="MODERATE", exit_reason="sl",
                        factors={"RSI": 0.6, "MACD": -0.2}),
            _make_trade(pnl=1.0, verdict="WEAK", exit_reason="tp",
                        factors={"Volume": 0.4}),
        ]

        factor_stats = compute_factor_stats(trades)
        combo_stats = compute_combination_stats(trades, min_occurrences=1)
        fp_report = compute_false_positives(trades, factor_stats, combo_stats)

        # Factor stats
        assert "Supertrend" in factor_stats
        assert factor_stats["Supertrend"].total_trades == 3

        # Combo stats
        assert len(combo_stats) > 0

        # FP report
        assert fp_report.strong_signals == 3
        assert fp_report.strong_hit_sl == 1
        assert fp_report.total_signals == 5

    def test_backtest_trade_has_factor_data(self):
        """BacktestTrade should have factor_strengths and factor_present fields."""
        trade = BacktestTrade(
            symbol="BTC/USDT", timeframe="1h", direction="BUY",
            entry_price=50000, entry_index=0, entry_timestamp="",
            sl=49000, tp=52000,
            factor_strengths={"Supertrend": 1.0, "EMA": 0.5},
            factor_present={"Supertrend": True, "EMA": True},
            verdict="STRONG",
        )
        assert trade.factor_strengths["Supertrend"] == 1.0
        assert trade.factor_present["EMA"] is True
        assert trade.verdict == "STRONG"


# === Entry Timing Analysis Tests (Task 7.2) ===

from analytics.entry_delay import (
    EntryTimingRecord,
    EntryEfficiencySummary,
    analyze_entry_timing,
    compute_entry_efficiency_summary,
)


def _make_trade_for_timing(
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    direction: str = "BUY",
    entry_price: float = 100.0,
    entry_index: int = 0,
    sl: float = 98.0,
    tp: float = 105.0,
    exit_price: float = 103.0,
    exit_reason: str = "tp",
    pnl_pct: float = 3.0,
    rr: float = 1.5,
) -> BacktestTrade:
    return BacktestTrade(
        symbol=symbol,
        timeframe=timeframe,
        direction=direction,
        entry_price=entry_price,
        entry_index=entry_index,
        entry_timestamp="2024-01-01",
        sl=sl,
        tp=tp,
        exit_price=exit_price,
        exit_index=1,
        exit_timestamp="2024-01-02",
        exit_reason=exit_reason,
        pnl_pct=pnl_pct,
        rr=rr,
        factor_strengths={},
        factor_present={},
    )


class TestEntryTiming:
    def test_buy_signal_at_30pct_of_move(self):
        """BUY: signal captures 30% of total movement → efficiency = 30%."""
        swing_data = {
            0: {"swing_start": 100.0, "swing_end": 200.0, "atr": 2.0},
        }
        trades = [_make_trade_for_timing(
            entry_price=100.0, entry_index=0, exit_price=130.0,
            sl=98.0, tp=200.0,
        )]
        records = analyze_entry_timing(trades, swing_data)
        assert len(records) == 1
        r = records[0]
        assert r.move_before == 0.0
        assert r.move_after == 30.0
        assert r.total_move == 100.0
        assert r.entry_efficiency == 30.0

    def test_buy_signal_at_70pct_of_move(self):
        """BUY: signal captures 70% of total movement → efficiency = 70%."""
        swing_data = {
            0: {"swing_start": 100.0, "swing_end": 200.0, "atr": 2.0},
        }
        trades = [_make_trade_for_timing(
            entry_price=100.0, entry_index=0, exit_price=170.0,
            sl=98.0, tp=200.0,
        )]
        records = analyze_entry_timing(trades, swing_data)
        assert len(records) == 1
        r = records[0]
        assert r.move_after == 70.0
        assert r.total_move == 100.0
        assert r.entry_efficiency == 70.0

    def test_sell_signal_efficiency(self):
        """SELL: efficiency calculated correctly for short trades."""
        swing_data = {
            0: {"swing_start": 200.0, "swing_end": 100.0, "atr": 2.0},
        }
        trades = [_make_trade_for_timing(
            direction="SELL",
            entry_price=200.0, entry_index=0, exit_price=130.0,
            sl=202.0, tp=100.0,
        )]
        records = analyze_entry_timing(trades, swing_data)
        assert len(records) == 1
        r = records[0]
        assert r.move_before == 0.0
        assert r.move_after == 70.0
        assert r.total_move == 100.0
        assert r.entry_efficiency == 70.0

    def test_late_entry_low_efficiency(self):
        """BUY: signal fires late → low efficiency."""
        swing_data = {
            0: {"swing_start": 100.0, "swing_end": 200.0, "atr": 2.0},
        }
        trades = [_make_trade_for_timing(
            entry_price=170.0, entry_index=0, exit_price=180.0,
            sl=98.0, tp=200.0,
        )]
        records = analyze_entry_timing(trades, swing_data)
        r = records[0]
        assert r.move_before == 70.0
        assert r.move_after == 10.0
        assert r.entry_efficiency == 10.0

    def test_entry_efficiency_capped_at_100(self):
        """Efficiency should not exceed 100%."""
        swing_data = {
            0: {"swing_start": 100.0, "swing_end": 150.0, "atr": 2.0},
        }
        trades = [_make_trade_for_timing(
            entry_price=100.0, entry_index=0, exit_price=160.0,
            sl=98.0, tp=200.0,
        )]
        records = analyze_entry_timing(trades, swing_data)
        r = records[0]
        assert r.entry_efficiency == 100.0

    def test_atr_normalized_metrics(self):
        """move_before_atr and move_after_atr should be normalized by ATR."""
        swing_data = {
            0: {"swing_start": 100.0, "swing_end": 200.0, "atr": 5.0},
        }
        trades = [_make_trade_for_timing(
            entry_price=110.0, entry_index=0, exit_price=130.0,
            sl=98.0, tp=200.0,
        )]
        records = analyze_entry_timing(trades, swing_data)
        r = records[0]
        assert r.move_before_atr == 2.0  # 10 / 5
        assert r.move_after_atr == 4.0   # 20 / 5

    def test_approximate_atr_from_sl(self):
        """When swing_data not provided, ATR approximated from SL distance."""
        trades = [_make_trade_for_timing(
            entry_price=100.0, entry_index=0, exit_price=103.0,
            sl=97.0,  # risk = 3.0, ATR ≈ 3.0 / 1.5 = 2.0
            tp=105.0,
        )]
        records = analyze_entry_timing(trades)
        r = records[0]
        assert r.atr == 2.0

    def test_empty_trades(self):
        """Empty trade list returns empty records."""
        records = analyze_entry_timing([])
        assert records == []

    def test_summary_avg_efficiency(self):
        """Summary computes average efficiency correctly."""
        swing_data = {
            0: {"swing_start": 100.0, "swing_end": 200.0, "atr": 2.0},
            1: {"swing_start": 100.0, "swing_end": 200.0, "atr": 2.0},
        }
        trades = [
            _make_trade_for_timing(entry_price=100.0, entry_index=0, exit_price=130.0, sl=98.0, tp=200.0),
            _make_trade_for_timing(entry_price=100.0, entry_index=1, exit_price=170.0, sl=98.0, tp=200.0),
        ]
        records = analyze_entry_timing(trades, swing_data)
        summary = compute_entry_efficiency_summary(records)
        assert summary.total_records == 2
        assert summary.avg_efficiency == 50.0  # (30 + 70) / 2

    def test_summary_median_efficiency(self):
        """Summary computes median efficiency correctly."""
        swing_data = {
            0: {"swing_start": 100.0, "swing_end": 200.0, "atr": 2.0},
            1: {"swing_start": 100.0, "swing_end": 200.0, "atr": 2.0},
            2: {"swing_start": 100.0, "swing_end": 200.0, "atr": 2.0},
        }
        trades = [
            _make_trade_for_timing(entry_price=100.0, entry_index=0, exit_price=120.0, sl=98.0, tp=200.0),
            _make_trade_for_timing(entry_price=100.0, entry_index=1, exit_price=150.0, sl=98.0, tp=200.0),
            _make_trade_for_timing(entry_price=100.0, entry_index=2, exit_price=180.0, sl=98.0, tp=200.0),
        ]
        records = analyze_entry_timing(trades, swing_data)
        summary = compute_entry_efficiency_summary(records)
        assert summary.median_efficiency == 50.0  # median of [20, 50, 80]

    def test_summary_pct_above_threshold(self):
        """Summary computes percentage above 60% and 50% thresholds."""
        swing_data = {
            0: {"swing_start": 100.0, "swing_end": 200.0, "atr": 2.0},
            1: {"swing_start": 100.0, "swing_end": 200.0, "atr": 2.0},
            2: {"swing_start": 100.0, "swing_end": 200.0, "atr": 2.0},
            3: {"swing_start": 100.0, "swing_end": 200.0, "atr": 2.0},
        }
        trades = [
            _make_trade_for_timing(entry_price=100.0, entry_index=0, exit_price=110.0, sl=98.0, tp=200.0),  # 10%
            _make_trade_for_timing(entry_price=100.0, entry_index=1, exit_price=140.0, sl=98.0, tp=200.0),  # 40%
            _make_trade_for_timing(entry_price=100.0, entry_index=2, exit_price=165.0, sl=98.0, tp=200.0),  # 65%
            _make_trade_for_timing(entry_price=100.0, entry_index=3, exit_price=180.0, sl=98.0, tp=200.0),  # 80%
        ]
        records = analyze_entry_timing(trades, swing_data)
        summary = compute_entry_efficiency_summary(records)
        assert summary.pct_above_60 == 50.0  # 2 of 4 >= 60
        assert summary.pct_above_50 == 50.0  # 2 of 4 >= 50

    def test_summary_by_direction(self):
        """Summary breaks down efficiency by BUY/SELL direction."""
        swing_data = {
            0: {"swing_start": 100.0, "swing_end": 200.0, "atr": 2.0},
            1: {"swing_start": 200.0, "swing_end": 100.0, "atr": 2.0},
        }
        trades = [
            _make_trade_for_timing(
                direction="BUY", entry_price=100.0, entry_index=0, exit_price=150.0,
                sl=98.0, tp=200.0,
            ),
            _make_trade_for_timing(
                direction="SELL", entry_price=200.0, entry_index=1, exit_price=150.0,
                sl=202.0, tp=100.0,
            ),
        ]
        records = analyze_entry_timing(trades, swing_data)
        summary = compute_entry_efficiency_summary(records)
        assert "BUY" in summary.by_direction
        assert "SELL" in summary.by_direction
        assert summary.by_direction["BUY"].avg_efficiency == 50.0
        assert summary.by_direction["SELL"].avg_efficiency == 50.0

    def test_summary_empty_records(self):
        """Empty records returns zero summary."""
        summary = compute_entry_efficiency_summary([])
        assert summary.total_records == 0
        assert summary.avg_efficiency == 0.0

    def test_entry_timing_record_dataclass(self):
        """EntryTimingRecord has all expected fields."""
        r = EntryTimingRecord(
            symbol="BTC/USDT",
            timeframe="1h",
            direction="BUY",
            entry_price=100.0,
            exit_price=110.0,
            atr=2.0,
            swing_start=95.0,
            swing_end=115.0,
            move_before=5.0,
            move_after=10.0,
            total_move=20.0,
            move_before_atr=2.5,
            move_after_atr=5.0,
            entry_efficiency=50.0,
        )
        assert r.symbol == "BTC/USDT"
        assert r.entry_efficiency == 50.0
        assert r.move_after_atr == 5.0

    def test_mixed_buy_sell_efficiency(self):
        """Mixed BUY/SELL trades analyzed correctly."""
        swing_data = {
            0: {"swing_start": 100.0, "swing_end": 200.0, "atr": 2.0},
            1: {"swing_start": 200.0, "swing_end": 100.0, "atr": 2.0},
        }
        trades = [
            _make_trade_for_timing(
                direction="BUY", entry_price=100.0, entry_index=0, exit_price=130.0,
                sl=98.0, tp=200.0,
            ),
            _make_trade_for_timing(
                direction="SELL", entry_price=200.0, entry_index=1, exit_price=170.0,
                sl=202.0, tp=100.0,
            ),
        ]
        records = analyze_entry_timing(trades, swing_data)
        assert len(records) == 2
        assert records[0].entry_efficiency == 30.0
        assert records[1].entry_efficiency == 30.0

    def test_zero_atr_fallback(self):
        """Zero ATR in swing_data falls back to approximation, no crash."""
        swing_data = {
            0: {"swing_start": 100.0, "swing_end": 200.0, "atr": 0.0},
        }
        trades = [_make_trade_for_timing(
            entry_price=100.0, entry_index=0, exit_price=130.0,
            sl=98.0, tp=200.0,
        )]
        records = analyze_entry_timing(trades, swing_data)
        r = records[0]
        # ATR should be approximated from SL: (100-98)/1.5 = 1.333...
        assert r.atr > 0  # no division by zero
        assert r.move_before_atr == 0.0  # move_before is 0
        assert r.move_after_atr > 0  # 30 / ~1.333
