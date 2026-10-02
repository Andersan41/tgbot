import sys
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.pattern_engine import PatternEngine, ICTSetup, pattern_engine
from strategy.signal_evaluator import estimate_p_tp
from risk.engine import RiskEngine, RiskDecision, PortfolioState, risk_engine
from context.scorer import ContextScorer, ContextScore
from market_structure.structure import (
    CHoCH, BOS, StructureState, SwingPoint,
    classify_choch, calc_mss_score, calc_causality,
)


# === Mock objects ===

@dataclass
class MockBOS:
    type: str = "bullish"
    level: float = 50000.0
    timestamp: datetime = None
    candle_index: int = 10


@dataclass
class MockCHoCH:
    type: str = "bearish"
    level: float = 49000.0
    timestamp: datetime = None
    candle_index: int = 5
    strength: str = "mss"
    mss_score: float = 75.0
    causality_score: float = 0.8


@dataclass
class MockStructure:
    trend: str = "bullish"
    last_bos: object = None
    last_choch: object = None
    last_mss: object = None
    recent_highs: list = None
    recent_lows: list = None
    swing_points: list = None

    def __post_init__(self):
        if self.recent_highs is None:
            self.recent_highs = []
        if self.recent_lows is None:
            self.recent_lows = []
        if self.swing_points is None:
            self.swing_points = []


@dataclass
class MockSweep:
    type: str = "bullish"
    is_valid: bool = True
    strength: float = 0.75
    reclaim_candles: int = 2
    swept_level: float = 50000.0
    sweep_low: float = 49500.0
    sweep_high: float = 50200.0
    volume_ratio: float = 2.0
    timestamp: datetime = None
    candle_index: int = 0


@dataclass
class MockOB:
    type: str = "bullish"
    high: float = 50200.0
    low: float = 49800.0
    midpoint: float = 50000.0
    is_valid: bool = True
    has_bos: bool = True
    displacement_atr: float = 2.0
    volume_ratio: float = 2.0
    timestamp: datetime = None


@dataclass
class MockCandleQuality:
    is_displacement: bool = True
    body_pct: float = 0.75
    close_position: float = 0.8
    body_atr_ratio: float = 1.5


# ============================================================
# MSS Classification Tests (structure.py)
# ============================================================

class TestMSSClassification:
    def test_calc_causality_zero_bars(self):
        assert calc_causality(0) == 1.0

    def test_calc_causality_3_bars(self):
        # exp(-0.693 * 3 / 3) = exp(-0.693) ≈ 0.5
        assert abs(calc_causality(3) - 0.5) < 0.05

    def test_calc_causality_5_bars(self):
        # exp(-0.693 * 5 / 3) ≈ 0.315
        assert calc_causality(5) < 0.5
        assert calc_causality(5) > 0.2

    def test_calc_causality_10_bars(self):
        assert calc_causality(10) < 0.15

    def test_calc_causality_negative(self):
        assert calc_causality(-1) == 0.0

    def test_calc_mss_score_all_max(self):
        score = calc_mss_score(
            sweep_strength=1.0, displacement_atr=3.0,
            reclaim_bars=1, volume_ratio=3.0, htf_aligned=True,
        )
        assert score == 100.0

    def test_calc_mss_score_all_zero(self):
        score = calc_mss_score(
            sweep_strength=0.0, displacement_atr=0.0,
            reclaim_bars=10, volume_ratio=0.5, htf_aligned=False,
        )
        assert score == 0.0

    def test_calc_mss_score_partial(self):
        score = calc_mss_score(
            sweep_strength=0.5, displacement_atr=1.5,
            reclaim_bars=2, volume_ratio=2.0, htf_aligned=False,
        )
        assert 30 < score < 70

    def test_classify_choch_as_mss(self):
        choch = CHoCH(type="bearish", level=49000, timestamp=datetime.now(timezone.utc), candle_index=5)
        sweep = MockSweep(type="bearish", candle_index=2, is_valid=True)  # bearish sweep precedes bearish CHoCH (same type)
        result = classify_choch(
            choch, sweeps=[sweep],
            displacement_atr=1.5, reclaim_bars=1,
            volume_ratio=2.0, htf_aligned=True,
        )
        assert result.strength == "mss"
        assert result.mss_score > 50
        assert result.has_sweep_reference is True

    def test_classify_choch_weak_no_sweep(self):
        choch = CHoCH(type="bearish", level=49000, timestamp=datetime.now(timezone.utc), candle_index=5)
        result = classify_choch(
            choch, sweeps=[],
            displacement_atr=1.5, reclaim_bars=1,
        )
        assert result.strength == "weak"
        assert result.mss_score == 0.0

    def test_classify_choch_normal_with_sweep(self):
        choch = CHoCH(type="bearish", level=49000, timestamp=datetime.now(timezone.utc), candle_index=5)
        sweep = MockSweep(type="bearish", candle_index=2, is_valid=True)  # bearish sweep precedes bearish CHoCH (same type)
        result = classify_choch(
            choch, sweeps=[sweep],
            displacement_atr=0.6, reclaim_bars=3,
        )
        assert result.strength == "normal"
        assert result.mss_score > 0

    def test_classify_choch_too_far_from_sweep(self):
        choch = CHoCH(type="bearish", level=49000, timestamp=datetime.now(timezone.utc), candle_index=20)
        sweep = MockSweep(type="bearish", candle_index=0, is_valid=True)
        result = classify_choch(
            choch, sweeps=[sweep],
            displacement_atr=1.5, reclaim_bars=1,
            max_causal_bars=5,
        )
        assert result.strength == "weak"
        assert result.has_sweep_reference is False


# ============================================================
# PatternEngine Tests
# ============================================================

class TestPatternEngine:
    def test_singleton_exists(self):
        assert pattern_engine is not None
        assert isinstance(pattern_engine, PatternEngine)

    def test_no_valid_setup_returns_rejection(self):
        setup = pattern_engine.detect(
            sweeps=[], order_blocks=[], structure=None,
            fvgs=[], candle_quality=None, current_price=50000.0,
        )
        assert setup.detected is False
        assert setup.rejection_reason is not None

    # ── Reversal tests ──

    def test_reversal_full_setup(self):
        """Sweep + Displacement + MSS → reversal detected."""
        sweeps = [MockSweep(type="bearish", candle_index=2, is_valid=True)]
        structure = MockStructure(
            last_mss=MockCHoCH(type="bearish", candle_index=5),
            last_choch=MockCHoCH(type="bearish", candle_index=5),
        )
        candle_q = MockCandleQuality(is_displacement=True, body_pct=0.75, body_atr_ratio=1.5)

        setup = pattern_engine.detect(
            sweeps=sweeps, order_blocks=[], structure=structure,
            fvgs=[], candle_quality=candle_q, current_price=50000.0, atr=500.0,
        )
        assert setup.detected is True
        assert setup.direction == "sell"
        assert setup.setup_type == "reversal"
        assert setup.has_sweep is True
        assert setup.has_displacement is True
        assert setup.has_mss is True

    def test_reversal_no_sweep(self):
        """No sweep → reversal not detected."""
        structure = MockStructure(
            last_mss=MockCHoCH(type="bearish"),
            last_choch=MockCHoCH(type="bearish"),
        )
        candle_q = MockCandleQuality(is_displacement=True)

        setup = pattern_engine.detect(
            sweeps=[], order_blocks=[], structure=structure,
            fvgs=[], candle_quality=candle_q, current_price=50000.0, atr=500.0,
        )
        assert setup.detected is False
        assert setup.setup_type is None

    def test_reversal_no_displacement_no_mss(self):
        """Sweep but no displacement and no MSS → reversal not detected."""
        sweeps = [MockSweep(type="bearish", candle_index=2)]
        structure = MockStructure()  # no MSS
        candle_q = MockCandleQuality(is_displacement=False)

        setup = pattern_engine.detect(
            sweeps=sweeps, order_blocks=[], structure=structure,
            fvgs=[], candle_quality=candle_q, current_price=50000.0, atr=500.0,
        )
        assert setup.detected is False

    def test_reversal_no_mss(self):
        """Sweep + displacement but no MSS → reversal not detected."""
        sweeps = [MockSweep(type="bearish", candle_index=2)]
        structure = MockStructure(last_mss=None, last_choch=None)
        candle_q = MockCandleQuality(is_displacement=True, body_atr_ratio=1.5)

        setup = pattern_engine.detect(
            sweeps=sweeps, order_blocks=[], structure=structure,
            fvgs=[], candle_quality=candle_q, current_price=50000.0, atr=500.0,
        )
        assert setup.detected is False
        assert "MSS" in setup.rejection_reason

    # ── Continuation tests ──

    def test_continuation_full_setup(self):
        """Trend + BOS aligned + sweep before BOS → continuation detected."""
        structure = MockStructure(
            trend="bullish",
            last_bos=MockBOS(type="bullish", level=51000, candle_index=10),
        )
        # Bearish sweep (opposite direction) before BOS — causality satisfied
        sweep = MockSweep(type="bearish", candle_index=5)
        setup = pattern_engine.detect(
            sweeps=[sweep], order_blocks=[], structure=structure,
            fvgs=[], candle_quality=None, current_price=50000.0,
        )
        assert setup.detected is True
        assert setup.direction == "buy"
        assert setup.setup_type == "continuation"
        assert setup.has_bos is True

    def test_continuation_no_bos(self):
        """Trend but no BOS → continuation not detected."""
        structure = MockStructure(trend="bullish", last_bos=None)
        setup = pattern_engine.detect(
            sweeps=[], order_blocks=[], structure=structure,
            fvgs=[], candle_quality=None, current_price=50000.0,
        )
        assert setup.detected is False
        assert "BOS" in setup.rejection_reason

    def test_continuation_ranging_rejected(self):
        """Ranging market → continuation not detected."""
        structure = MockStructure(trend="ranging", last_bos=MockBOS(type="bullish"))
        setup = pattern_engine.detect(
            sweeps=[], order_blocks=[], structure=structure,
            fvgs=[], candle_quality=None, current_price=50000.0,
        )
        assert setup.detected is False
        assert "ranging" in setup.rejection_reason.lower()

    def test_continuation_bos_vs_trend_rejected(self):
        """BOS direction vs trend direction → continuation not detected."""
        structure = MockStructure(
            trend="bullish",
            last_bos=MockBOS(type="bearish"),  # bearish BOS in bullish trend
        )
        setup = pattern_engine.detect(
            sweeps=[], order_blocks=[], structure=structure,
            fvgs=[], candle_quality=None, current_price=50000.0,
        )
        assert setup.detected is False
        assert "trend" in setup.rejection_reason.lower()

    # ── Entry zone tests ──

    def test_ob_detected_as_entry_zone(self):
        """OB is detected but is NOT a gate — it's an entry zone."""
        sweeps = [MockSweep(type="bearish", candle_index=2)]
        structure = MockStructure(
            last_mss=MockCHoCH(type="bearish", candle_index=5),
            last_choch=MockCHoCH(type="bearish", candle_index=5),
        )
        obs = [MockOB(type="bearish", midpoint=50500.0)]
        candle_q = MockCandleQuality(is_displacement=True, body_atr_ratio=1.5)

        setup = pattern_engine.detect(
            sweeps=sweeps, order_blocks=obs, structure=structure,
            fvgs=[], candle_quality=candle_q, current_price=50000.0, atr=500.0,
        )
        assert setup.detected is True
        assert setup.has_ob is True
        assert setup.ob_midpoint == 50500.0

    def test_ob_not_required_for_reversal(self):
        """Reversal can be detected without OB (OB is optional entry zone)."""
        sweeps = [MockSweep(type="bearish", candle_index=2)]
        structure = MockStructure(
            last_mss=MockCHoCH(type="bearish", candle_index=5),
            last_choch=MockCHoCH(type="bearish", candle_index=5),
        )
        candle_q = MockCandleQuality(is_displacement=True, body_atr_ratio=1.5)

        setup = pattern_engine.detect(
            sweeps=sweeps, order_blocks=[], structure=structure,
            fvgs=[], candle_quality=candle_q, current_price=50000.0, atr=500.0,
        )
        assert setup.detected is True
        assert setup.has_ob is False

    def test_fvg_not_required_for_reversal(self):
        """Reversal can be detected without FVG."""
        sweeps = [MockSweep(type="bearish", candle_index=2)]
        structure = MockStructure(
            last_mss=MockCHoCH(type="bearish", candle_index=5),
            last_choch=MockCHoCH(type="bearish", candle_index=5),
        )
        candle_q = MockCandleQuality(is_displacement=True, body_atr_ratio=1.5)

        setup = pattern_engine.detect(
            sweeps=sweeps, order_blocks=[], structure=structure,
            fvgs=[], candle_quality=candle_q, current_price=50000.0, atr=500.0,
        )
        assert setup.detected is True
        assert setup.has_fvg is False

    def test_entry_armed_when_ob_nearby(self):
        """Price near OB midpoint → entry_armed=True."""
        sweeps = [MockSweep(type="bearish", candle_index=2)]
        structure = MockStructure(
            last_mss=MockCHoCH(type="bearish", candle_index=5),
            last_choch=MockCHoCH(type="bearish", candle_index=5),
        )
        obs = [MockOB(type="bearish", midpoint=50050.0)]  # very close to 50000
        candle_q = MockCandleQuality(is_displacement=True, body_atr_ratio=1.5)

        setup = pattern_engine.detect(
            sweeps=sweeps, order_blocks=obs, structure=structure,
            fvgs=[], candle_quality=candle_q, current_price=50000.0, atr=500.0,
        )
        assert setup.entry_armed is True

    def test_entry_not_armed_when_ob_far(self):
        """Price far from OB midpoint → entry_armed=False."""
        sweeps = [MockSweep(type="bearish", candle_index=2)]
        structure = MockStructure(
            last_mss=MockCHoCH(type="bearish", candle_index=5),
            last_choch=MockCHoCH(type="bearish", candle_index=5),
        )
        obs = [MockOB(type="bearish", midpoint=55000.0)]  # far from 50000
        candle_q = MockCandleQuality(is_displacement=True, body_atr_ratio=1.5)

        setup = pattern_engine.detect(
            sweeps=sweeps, order_blocks=obs, structure=structure,
            fvgs=[], candle_quality=candle_q, current_price=50000.0, atr=500.0,
        )
        assert setup.entry_armed is False

    # ── ICTSetup properties ──

    def test_ict_setup_components_count(self):
        setup = ICTSetup(
            detected=True, setup_type="reversal",
            has_sweep=True, has_displacement=True, has_mss=True,
            components_found=["Sweep", "Displacement", "MSS"],
        )
        assert setup.components_count == 3
        assert setup.is_reversal is True
        assert setup.is_continuation is False

    def test_ict_setup_continuation_properties(self):
        setup = ICTSetup(
            detected=True, setup_type="continuation",
            has_bos=True, components_found=["BOS"],
        )
        assert setup.is_continuation is True
        assert setup.is_reversal is False

    def test_ict_setup_has_trigger_backward_compat(self):
        setup = ICTSetup(detected=True, has_sweep=True)
        assert setup.has_trigger is True

        setup2 = ICTSetup(detected=True, has_bos=True)
        assert setup2.has_trigger is True

        setup3 = ICTSetup(detected=False)
        assert setup3.has_trigger is False


# ============================================================
# RiskEngine Tests
# ============================================================

class TestRiskEngine:
    """RiskEngine.evaluate — current API and gate semantics.

    Signature: evaluate(portfolio, entry_price, sl, tp, atr_pct, p_tp,
    confidence, mss_quality, atr, sl_source, *, direction).
    Hard gates: RR < min_rr_ratio, invalid geometry/price, max active
    signals, portfolio risk cap, non-positive Kelly. SL width/tightness
    are soft gates — Kelly sizing handles them.
    """

    def test_singleton_exists(self):
        assert risk_engine is not None
        assert isinstance(risk_engine, RiskEngine)

    def test_valid_trade_passes(self):
        decision = risk_engine.evaluate(
            portfolio=PortfolioState(active_count=0, total_risk_pct=0.0),
            entry_price=50000.0, sl=49000.0, tp=53000.0,
            atr_pct=2.0, p_tp=0.65, confidence=0.8,
        )
        assert decision.should_trade is True
        assert decision.rr_ratio == 3.0

    def test_rr_too_low_rejects(self):
        """RR below min_rr_ratio is a hard gate (capital protection)."""
        decision = risk_engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=50000.0, sl=49000.0, tp=50500.0,
            atr_pct=2.0, p_tp=0.55, confidence=0.7,
        )
        assert decision.should_trade is False
        assert decision.rr_ratio == 0.5
        assert "RR" in decision.rejection_reason

    def test_sl_too_tight_is_soft_gate(self):
        """Tight SL doesn't block — soft gate, Kelly sizing handles it."""
        decision = risk_engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=50000.0, sl=49980.0, tp=51000.0,
            atr_pct=2.0, p_tp=0.65, confidence=0.8,
        )
        assert decision.should_trade is True
        assert decision.risk_pct > 0

    def test_sl_too_wide_is_soft_gate(self):
        """Wide SL doesn't block — soft gate for non-structural sources."""
        decision = risk_engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=50000.0, sl=47000.0, tp=56000.0,
            atr_pct=2.0, p_tp=0.65, confidence=0.8,
        )
        assert decision.should_trade is True
        assert decision.risk_pct > 0

    def test_portfolio_risk_at_cap_rejects(self):
        """total_risk_pct + new trade above the cap is a hard gate."""
        decision = risk_engine.evaluate(
            portfolio=PortfolioState(active_count=1, total_risk_pct=3.0),
            entry_price=50000.0, sl=49500.0, tp=51500.0,
            atr_pct=2.0, p_tp=0.65, confidence=0.8,
        )
        assert decision.should_trade is False
        assert "portfolio risk" in decision.rejection_reason

    def test_max_active_signals_rejects_at_limit(self):
        """active_count >= max_active_signals is a hard gate in evaluate()."""
        decision = risk_engine.evaluate(
            portfolio=PortfolioState(active_count=3, total_risk_pct=1.0),
            entry_price=50000.0, sl=49500.0, tp=51500.0,
            atr_pct=2.0, p_tp=0.65, confidence=0.8,
        )
        assert decision.should_trade is False
        assert "max active signals" in decision.rejection_reason

    def test_invalid_price_rejects(self):
        decision = risk_engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=0.0, sl=49500.0, tp=51500.0,
            atr_pct=2.0, p_tp=0.6, confidence=0.7,
        )
        assert decision.should_trade is False
        assert "invalid price" in decision.rejection_reason

    def test_high_volatility_reduces_risk(self):
        decision_low = risk_engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=50000.0, sl=49500.0, tp=51500.0,
            atr_pct=1.0, p_tp=0.65, confidence=0.8,
        )
        decision_high = risk_engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=50000.0, sl=49500.0, tp=51500.0,
            atr_pct=5.0, p_tp=0.65, confidence=0.8,
        )
        assert decision_low.should_trade and decision_high.should_trade
        assert decision_high.risk_pct <= decision_low.risk_pct

    def test_mss_quality_soft_adjustment(self):
        """MSS quality scales risk_pct 0.8x–1.1x before the base cap."""
        decision_no_mss = risk_engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=50000.0, sl=49500.0, tp=51500.0,
            atr_pct=2.0, p_tp=0.65, confidence=0.8,
            mss_quality=1.0,
        )
        decision_high_mss = risk_engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=50000.0, sl=49500.0, tp=51500.0,
            atr_pct=2.0, p_tp=0.65, confidence=0.8,
            mss_quality=80.0,
        )
        assert decision_high_mss.risk_pct > decision_no_mss.risk_pct

    def test_kelly_fraction_used(self):
        decision = risk_engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=50000.0, sl=49500.0, tp=52500.0,
            atr_pct=2.0, p_tp=0.75, confidence=0.9,
        )
        assert decision.should_trade is True
        assert decision.kelly_fraction > 0
        assert decision.probability_confidence == 0.9


# ============================================================
# ContextScore Tests
# ============================================================

class TestContextScore:
    @pytest.fixture
    def scorer(self):
        return ContextScorer()

    def test_score_simple_returns_context_score(self, scorer):
        from context.analyzer import ContextSnapshot
        snap = ContextSnapshot(
            symbol="BTC/USDT",
            timestamp=datetime.now(timezone.utc),
            fear_greed_value=25,
        )
        result = scorer.score_simple("BUY", snap)
        assert isinstance(result, ContextScore)
        assert -1.0 <= result.score <= 1.0

    def test_score_simple_never_blocks(self, scorer):
        from context.analyzer import ContextSnapshot
        snap = ContextSnapshot(
            symbol="BTC/USDT",
            timestamp=datetime.now(timezone.utc),
            fear_greed_value=95,
            funding_rate=0.05,
        )
        result = scorer.score_simple("BUY", snap)
        assert isinstance(result, ContextScore)
        assert -1.0 <= result.score <= 1.0

    def test_context_score_properties(self):
        cs = ContextScore(score=0.5, confidence=0.5)
        assert cs.score == 0.5
        assert cs.confidence == 0.5
        assert cs.supporting == []
        assert cs.opposing == []


# ============================================================
# Integration: Full Pipeline Tests
# ============================================================

class TestPipelineIntegration:
    """End-to-end: Pattern Engine → inline probability → Risk Engine.

    FeatureBuilder/ProbabilityEngine were deleted in the dead-code audit
    (commit 03cc5b0); the live scanner/backtest pipeline now inlines
    P(TP) via strategy.signal_evaluator.estimate_p_tp — used here
    directly, mirroring scanner Phase 3.
    """

    def test_full_reversal_pipeline(self):
        """Sweep → Displacement → MSS → estimate_p_tp → Risk"""
        # 1. PatternEngine — reversal
        sweeps = [MockSweep(type="bearish", candle_index=2, is_valid=True)]
        structure = MockStructure(
            last_mss=MockCHoCH(type="bearish", candle_index=5, strength="mss", mss_score=75.0),
            last_choch=MockCHoCH(type="bearish", candle_index=5, strength="mss"),
        )
        candle_q = MockCandleQuality(is_displacement=True, body_pct=0.75, body_atr_ratio=1.5)

        setup = pattern_engine.detect(
            sweeps=sweeps, order_blocks=[], structure=structure,
            fvgs=[], candle_quality=candle_q, current_price=50000.0, atr=500.0,
        )
        assert setup.detected is True
        assert setup.direction == "sell"
        assert setup.setup_type == "reversal"

        # 2. Inline probability (scanner Phase 3)
        p_tp, confidence = estimate_p_tp(setup=setup, mtf_aligned=True)
        assert 0.0 < p_tp <= 1.0
        assert 0.0 < confidence <= 1.0

        # 3. RiskEngine
        decision = risk_engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=50000.0, sl=51000.0, tp=48000.0,
            atr_pct=2.0, p_tp=p_tp, confidence=confidence,
            mss_quality=setup.mss_score, atr=500.0,
        )
        assert decision.should_trade is True
        assert decision.rr_ratio == 2.0

    def test_full_continuation_pipeline(self):
        """Trend → BOS → estimate_p_tp → Risk"""
        # 1. PatternEngine — continuation
        structure = MockStructure(
            trend="bullish",
            last_bos=MockBOS(type="bullish", level=51000),
        )
        setup = pattern_engine.detect(
            sweeps=[], order_blocks=[], structure=structure,
            fvgs=[], candle_quality=None, current_price=50000.0,
        )
        assert setup.detected is True
        assert setup.direction == "buy"
        assert setup.setup_type == "continuation"

        # 2. Inline probability (scanner Phase 3)
        p_tp, confidence = estimate_p_tp(setup=setup, mtf_aligned=False)
        assert 0.0 < p_tp <= 1.0

        # 3. RiskEngine
        decision = risk_engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=50000.0, sl=49500.0, tp=51500.0,
            atr_pct=2.0, p_tp=p_tp, confidence=confidence,
        )
        assert decision.should_trade is True
        assert decision.rr_ratio == 3.0

    def test_no_pattern_stops_early(self):
        """No pattern detected → pipeline stops."""
        setup = pattern_engine.detect(
            sweeps=[], order_blocks=[], structure=None,
            fvgs=[], candle_quality=None, current_price=50000.0,
        )
        assert setup.detected is False
        # In real pipeline, we'd stop here

    def test_risk_blocks_bad_rr(self):
        """RR below min_rr_ratio is a hard gate — Kelly cannot rescue it."""
        decision = risk_engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=50000.0, sl=49500.0, tp=50200.0,
            atr_pct=2.0, p_tp=0.80, confidence=0.9,
        )
        assert decision.should_trade is False
        assert decision.rr_ratio < 1.0
        assert "RR" in decision.rejection_reason
