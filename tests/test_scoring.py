import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scoring.confidence_v2 import (
    ConfidenceEngineV2,
    FactorScore,
    ConfidenceResult,
    score_htf_trend,
    score_structure,
    score_liquidity,
    score_volume,
    score_btc_correlation,
    score_funding_from_state,
    score_oi_from_state,
    score_rsi,
    score_macd,
    score_adx,
)
from strategy.signal_engine import SignalResult, SignalType


@pytest.fixture
def engine():
    return ConfidenceEngineV2()


class TestFactorScore:
    def test_weighted_score_calculation(self):
        f = FactorScore.make("Test", 10, 0.5)
        assert f.weight == 10
        assert f.raw_score == 0.5
        assert f.weighted_score == 5.0  # 0.5 * 10

    def test_weighted_score_negative(self):
        f = FactorScore.make("Test", 15, -0.8)
        assert f.weighted_score == -12.0  # -0.8 * 15

    def test_weighted_score_zero(self):
        f = FactorScore.make("Test", 5, 0.0)
        assert f.weighted_score == 0.0


class TestConfidenceEngineV2:
    def test_strong_buy_all_factors_positive(self, engine):
        result = engine.compute(
            "BUY",
            htf_trend_score=0.9,
            structure_score=0.9,
            liquidity_score=0.8,
            volume_score=0.7,
            btc_corr_score=0.8,
            funding_score=0.6,
            oi_score=0.5,
            rsi_score=0.6,
            macd_score=0.5,
            adx_score=0.7,
        )
        assert result.recommendation == "BUY"
        assert result.quality == "strong"
        assert result.total_score >= 60
        assert result.confidence_pct >= 60

    def test_strong_sell_all_factors_negative(self, engine):
        result = engine.compute(
            "SELL",
            htf_trend_score=-0.9,
            structure_score=-0.9,
            liquidity_score=-0.8,
            volume_score=-0.3,
            btc_corr_score=-0.8,
            funding_score=-0.6,
            oi_score=-0.5,
            rsi_score=-0.6,
            macd_score=-0.5,
            adx_score=-0.7,
        )
        assert result.recommendation == "SELL"
        assert result.quality == "strong"
        assert result.total_score <= -60

    def test_moderate_buy(self, engine):
        result = engine.compute(
            "BUY",
            htf_trend_score=0.4,
            structure_score=0.6,
            liquidity_score=0.5,
            volume_score=0.4,
            btc_corr_score=0.4,
            funding_score=0.2,
            oi_score=0.2,
            rsi_score=0.3,
            macd_score=0.2,
            adx_score=0.3,
        )
        assert result.quality == "moderate"
        assert 30 <= abs(result.total_score) < 60

    def test_weak_mixed_factors(self, engine):
        result = engine.compute(
            "BUY",
            htf_trend_score=0.2,
            structure_score=-0.3,
            liquidity_score=0.1,
            volume_score=0.0,
            btc_corr_score=0.4,
            funding_score=-0.2,
            oi_score=0.0,
            rsi_score=0.1,
            macd_score=-0.1,
            adx_score=0.2,
        )
        assert result.quality == "weak"
        assert abs(result.total_score) < 30

    def test_total_clamped_to_100(self, engine):
        result = engine.compute(
            "BUY",
            htf_trend_score=1.0,
            structure_score=1.0,
            liquidity_score=1.0,
            volume_score=1.0,
            btc_corr_score=1.0,
            funding_score=1.0,
            oi_score=1.0,
            rsi_score=1.0,
            macd_score=1.0,
            adx_score=1.0,
        )
        assert result.total_score <= 100.0

    def test_total_clamped_to_minus_100(self, engine):
        result = engine.compute(
            "SELL",
            htf_trend_score=-1.0,
            structure_score=-1.0,
            liquidity_score=-1.0,
            volume_score=-1.0,
            btc_corr_score=-1.0,
            funding_score=-1.0,
            oi_score=-1.0,
            rsi_score=-1.0,
            macd_score=-1.0,
            adx_score=-1.0,
        )
        assert result.total_score >= -100.0

    def test_all_zero_gives_weak(self, engine):
        result = engine.compute("BUY")
        assert result.total_score == 0.0
        assert result.quality == "weak"

    def test_factors_count_is_10(self, engine):
        result = engine.compute("BUY")
        assert len(result.factors) == 10

    def test_factor_weights_sum_to_100(self, engine):
        result = engine.compute("BUY")
        total_weight = sum(f.weight for f in result.factors)
        assert total_weight == 100

    def test_confidence_pct_equals_abs_total(self, engine):
        result = engine.compute("BUY", structure_score=0.8)
        assert result.confidence_pct == abs(result.total_score)


class TestIndividualScoring:
    def test_htf_trend_aligned(self):
        assert score_htf_trend(True, 3, 2) > 0.5

    def test_htf_trend_not_aligned(self):
        assert score_htf_trend(False, 1, 2) < 0

    def test_structure_bullish_for_buy(self):
        s = score_structure("bullish", "bullish", "BUY")
        assert s > 0.5

    def test_structure_bearish_for_buy(self):
        s = score_structure("bearish", "bearish", "BUY")
        assert s < 0

    def test_structure_bearish_for_sell(self):
        s = score_structure("bearish", "bearish", "SELL")
        assert s > 0.5

    def test_structure_bullish_for_sell(self):
        s = score_structure("bullish", "bullish", "SELL")
        assert s < 0

    def test_structure_none_returns_neutral(self):
        assert score_structure(None, None, "BUY") == 0.2

    def test_liquidity_bullish_sweeps_positive(self):
        s = score_liquidity(bullish_sweeps=2)
        assert s > 0

    def test_liquidity_bearish_sweeps_negative(self):
        s = score_liquidity(bearish_sweeps=2)
        assert s < 0

    def test_liquidity_mixed(self):
        s = score_liquidity(bullish_sweeps=1, bearish_sweeps=1)
        assert s == 0.0

    def test_volume_above_avg_positive(self):
        assert score_volume(True, 1.5) > 0

    def test_volume_below_avg_negative(self):
        assert score_volume(False, 0.8) < 0

    def test_btc_allows_positive(self):
        assert score_btc_correlation(True) > 0

    def test_btc_blocked_negative(self):
        assert score_btc_correlation(False) < 0

    def test_funding_bullish_for_buy(self):
        s = score_funding_from_state("bullish", "strong", "BUY")
        assert s > 0.5

    def test_funding_bearish_for_buy(self):
        s = score_funding_from_state("bearish", "strong", "BUY")
        assert s < -0.5

    def test_funding_neutral_zero(self):
        assert score_funding_from_state("neutral", "weak", "BUY") == 0.0

    def test_oi_bullish_cont_for_buy(self):
        s = score_oi_from_state("bullish_cont", "strong", "BUY")
        assert s > 0.5

    def test_oi_long_squeeze_for_buy(self):
        s = score_oi_from_state("long_squeeze", "strong", "BUY")
        assert s < -0.5

    def test_oi_ignore_zero(self):
        assert score_oi_from_state("bullish_cont", "ignore", "BUY") == 0.0

    def test_rsi_good_for_buy(self):
        s = score_rsi(55.0, "BUY")
        assert s > 0

    def test_rsi_overbought_bad_for_buy(self):
        s = score_rsi(75.0, "BUY")
        assert s < 0

    def test_rsi_good_for_sell(self):
        s = score_rsi(45.0, "SELL")
        assert s > 0

    def test_rsi_oversold_bad_for_sell(self):
        s = score_rsi(25.0, "SELL")
        assert s < 0

    def test_macd_positive_hist_for_buy(self):
        s = score_macd(100.0, 50000.0, "BUY")
        assert s > 0

    def test_macd_negative_hist_for_buy(self):
        s = score_macd(-100.0, 50000.0, "BUY")
        assert s < 0

    def test_macd_negative_hist_for_sell(self):
        s = score_macd(-100.0, 50000.0, "SELL")
        assert s > 0

    def test_macd_noise_below_threshold_returns_zero(self):
        """MACD hist = 0.01 at price 50000 → norm = 0.00002% < 0.03% → neutral"""
        s = score_macd(0.01, 50000.0, "BUY")
        assert s == 0.0

    def test_macd_noise_negative_below_threshold_returns_zero(self):
        """MACD hist = -0.01 at price 50000 → noise → neutral"""
        s = score_macd(-0.01, 50000.0, "SELL")
        assert s == 0.0

    def test_macd_at_threshold_boundary(self):
        """MACD hist = 15 at price 50000 → norm = 0.03% == threshold → significant"""
        s = score_macd(15.0, 50000.0, "BUY")
        assert s > 0

    def test_macd_above_threshold_strong_signal(self):
        """MACD hist = 50 at price 50000 → norm = 0.1% >> 0.03% → strong"""
        s = score_macd(50.0, 50000.0, "BUY")
        assert s > 0.5

    def test_adx_strong_trend_buy(self):
        s = score_adx(35.0, 30.0, 10.0, "BUY")
        assert s > 0

    def test_adx_strong_trend_sell(self):
        s = score_adx(35.0, 10.0, 30.0, "SELL")
        assert s > 0

    def test_adx_flat_negative(self):
        s = score_adx(15.0, 15.0, 15.0, "BUY")
        assert s < 0


class TestSignalResultV2Confidence:
    def test_verdict_from_v2_strong(self):
        from scoring.confidence_v2 import ConfidenceResult, FactorScore
        v2 = ConfidenceResult(
            factors=[],
            total_score=72.0,
            quality="strong",
            recommendation="BUY",
        )
        sig = SignalResult(
            signal=SignalType.BUY, symbol="BTC/USDT", timeframe="1h",
            close=50000.0, score=5, reasons=[], _confidence_v2=v2,
        )
        assert sig.verdict == "СИЛЬНЫЙ"  # localized display label
        assert sig.confidence == 72.0

    def test_verdict_from_v2_moderate(self):
        from scoring.confidence_v2 import ConfidenceResult
        v2 = ConfidenceResult(
            factors=[],
            total_score=45.0,
            quality="moderate",
            recommendation="BUY",
        )
        sig = SignalResult(
            signal=SignalType.BUY, symbol="BTC/USDT", timeframe="1h",
            close=50000.0, score=5, reasons=[], _confidence_v2=v2,
        )
        assert sig.verdict == "УМЕРЕННЫЙ"  # localized display label
        assert sig.confidence == 45.0

    def test_verdict_from_v2_weak(self):
        from scoring.confidence_v2 import ConfidenceResult
        v2 = ConfidenceResult(
            factors=[],
            total_score=20.0,
            quality="weak",
            recommendation="BUY",
        )
        sig = SignalResult(
            signal=SignalType.BUY, symbol="BTC/USDT", timeframe="1h",
            close=50000.0, score=5, reasons=[], _confidence_v2=v2,
        )
        assert sig.verdict == "СЛАБЫЙ"  # localized display label
        assert sig.confidence == 20.0

    def test_verdict_from_v2_sell_negative_score(self):
        from scoring.confidence_v2 import ConfidenceResult
        v2 = ConfidenceResult(
            factors=[],
            total_score=-65.0,
            quality="strong",
            recommendation="SELL",
        )
        sig = SignalResult(
            signal=SignalType.SELL, symbol="BTC/USDT", timeframe="1h",
            close=50000.0, score=5, reasons=[], _confidence_v2=v2,
        )
        assert sig.verdict == "СИЛЬНЫЙ"  # localized display label
        assert sig.confidence == 65.0

    def test_fallback_to_legacy_when_no_v2(self):
        sig = SignalResult(
            signal=SignalType.BUY, symbol="BTC/USDT", timeframe="1h",
            close=50000.0, score=5, reasons=[],
        )
        assert sig.verdict == "УМЕРЕННЫЙ"  # localized display label
        expected = round(5 / 7 * 100, 1)
        assert sig.confidence == expected

    def test_format_message_shows_quality_when_v2(self):
        from scoring.confidence_v2 import ConfidenceResult
        v2 = ConfidenceResult(
            factors=[],
            total_score=72.0,
            quality="strong",
            recommendation="BUY",
        )
        sig = SignalResult(
            signal=SignalType.BUY, symbol="BTC/USDT", timeframe="1h",
            close=50000.0, score=5, reasons=[], _confidence_v2=v2,
        )
        msg = sig.format_message()
        assert "Confidence: 72/100" in msg  # Quality line removed in e6e311a


class TestHistoricalWinrateBlending:
    """Task 6.1 — Confidence ≠ score: historical winrate blending tests."""

    def test_historical_winrate_blended_with_score(self, engine):
        """When historical WR=62% and score=40, blended ≈ 62*0.4 + 40*0.6 = 48.8"""
        result = engine.compute(
            "BUY",
            htf_trend_score=0.4,
            structure_score=0.4,
            liquidity_score=0.3,
            volume_score=0.3,
            btc_corr_score=0.4,
            funding_score=0.2,
            oi_score=0.2,
            rsi_score=0.3,
            macd_score=0.2,
            adx_score=0.3,
            historical_winrate=62.0,
        )
        # Score-based confidence ≈ 40 (abs of total)
        # Blended = 62 * 0.4 + 40 * 0.6 = 48.8
        assert result.total_score > 40  # blended should be higher than score alone
        assert result.total_score < 62  # blended should be lower than WR alone

    def test_historical_winrate_62_percent(self, engine):
        """Combination with 62% WR → confidence should reflect ~62% when blended."""
        # Use a moderate score so blending is visible
        result = engine.compute(
            "BUY",
            htf_trend_score=0.5,
            structure_score=0.5,
            historical_winrate=62.0,
        )
        # With historical WR, confidence should be influenced by it
        assert result.total_score >= 30  # at least moderate

    def test_new_combination_fallback_to_score(self, engine):
        """New combination without history → fallback on score (historical_winrate=None)."""
        result_no_history = engine.compute(
            "BUY",
            htf_trend_score=0.5,
            structure_score=0.5,
            liquidity_score=0.3,
        )
        result_with_history = engine.compute(
            "BUY",
            htf_trend_score=0.5,
            structure_score=0.5,
            liquidity_score=0.3,
            historical_winrate=None,
        )
        # Both should be identical when no history
        assert result_no_history.total_score == result_with_history.total_score

    def test_high_historical_winrate_boosts_confidence(self, engine):
        """High historical WR (75%) should boost confidence above score."""
        result_with_wr = engine.compute(
            "BUY",
            htf_trend_score=0.3,
            structure_score=0.3,
            historical_winrate=75.0,
        )
        result_without_wr = engine.compute(
            "BUY",
            htf_trend_score=0.3,
            structure_score=0.3,
        )
        assert result_with_wr.total_score > result_without_wr.total_score

    def test_low_historical_winrate_reduces_confidence(self, engine):
        """Low historical WR (15%) should reduce confidence below score (24%)."""
        result_with_wr = engine.compute(
            "BUY",
            htf_trend_score=0.6,
            structure_score=0.6,
            historical_winrate=15.0,
        )
        result_without_wr = engine.compute(
            "BUY",
            htf_trend_score=0.6,
            structure_score=0.6,
        )
        # Score = 24, WR = 15 → blended = 15*0.4 + 24*0.6 = 6 + 14.4 = 20.4 < 24
        assert result_with_wr.total_score < result_without_wr.total_score

    def test_blending_formula_correct(self, engine):
        """Verify blending formula: WR * 0.4 + score * 0.6."""
        # Create a scenario where we can calculate expected value
        # All factors at 0.5 → total = 0.5 * 100 = 50
        result = engine.compute(
            "BUY",
            htf_trend_score=0.5,
            structure_score=0.5,
            liquidity_score=0.5,
            volume_score=0.5,
            btc_corr_score=0.5,
            funding_score=0.5,
            oi_score=0.5,
            rsi_score=0.5,
            macd_score=0.5,
            adx_score=0.5,
            historical_winrate=60.0,
        )
        # Score = 50, WR = 60
        # Blended = 60 * 0.4 + 50 * 0.6 = 24 + 30 = 54
        expected = 60.0 * 0.4 + 50.0 * 0.6
        assert abs(result.total_score - expected) < 0.5
