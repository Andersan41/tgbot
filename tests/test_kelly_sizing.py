"""Kelly sizing tests (Э4): hard base cap + confidence range guard.

Covers risk/engine.py evaluate():
  1. base_risk_pct is applied AFTER the quality/volatility multipliers, so
     MAX_ACTIVE_SIGNALS × base always fits the portfolio budget;
  2. confidence outside [0, 1] (e.g. a 0-100 percentage) is rejected before
     sizing, so the Kelly term can't be blown up by a scale mistake;
  3. kelly_fraction is raw Kelly × confidence (documented f·p haircut —
     estimate_p_tp returns confidence ≡ p_tp by construction).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import config
from risk.engine import PortfolioState, RiskEngine


def make_engine() -> RiskEngine:
    r = config.risk_engine
    return RiskEngine(
        min_rr_ratio=config.trading.min_rr_threshold,
        sl_absolute_min_pct=r.sl_absolute_min_pct,
        sl_absolute_max_pct=r.sl_absolute_max_pct,
        base_risk_pct=r.base_risk_pct,
        min_risk_pct=r.min_risk_pct,
        max_risk_pct=r.max_risk_pct,
        max_active_signals=config.max_active_signals,
        max_portfolio_risk_pct=config.max_portfolio_risk_pct,
    )


@pytest.fixture()
def engine() -> RiskEngine:
    return make_engine()


class TestBaseRiskCap:
    def test_bonus_multipliers_cannot_exceed_base(self, engine):
        """Tight-SL + high-MSS bonuses used to push risk past base_risk_pct."""
        dec = engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=50000.0, sl=49950.0, tp=50500.0,   # SL 0.1% → tight bonus
            atr_pct=1.0, p_tp=0.75, confidence=0.75,
            mss_quality=100, atr=500.0, sl_source="ob",
        )
        assert dec.should_trade
        # Without the final cap this computes to base × 1.1 (mss) × 1.1 (tight SL).
        assert dec.risk_pct <= config.risk_engine.base_risk_pct

    def test_budget_invariant_holds(self, engine):
        """MAX_ACTIVE_SIGNALS × risk_pct ≤ max_portfolio_risk_pct for any setup."""
        dec = engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=50000.0, sl=49950.0, tp=50500.0,
            atr_pct=1.0, p_tp=0.85, confidence=0.85,
            mss_quality=100, atr=500.0, sl_source="ob",
        )
        assert dec.should_trade
        total = dec.risk_pct * config.max_active_signals
        assert total <= config.max_portfolio_risk_pct + 1e-9

    def test_downward_adjustments_still_apply(self, engine):
        """Volatility penalty must still shrink a capped position."""
        dec = engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=50000.0, sl=49950.0, tp=50500.0,
            atr_pct=5.0, p_tp=0.75, confidence=0.75,       # atr_pct > 4 → 0.5x
            mss_quality=0, atr=500.0, sl_source="ob",
        )
        assert dec.should_trade
        assert dec.risk_pct < config.risk_engine.base_risk_pct
        assert dec.volatility_adjustment == 0.5


class TestConfidenceRange:
    # Confidence out of [0, 1] is rejected up-front (validate block), so a
    # 0-100 scale caller can never blow up the Kelly term.
    # RR = 2.0 (passes MIN_RR_THRESHOLD for both the .env value 2.0 and the
    # code default 1.5). p_tp=0.3367 @ RR=2 → raw Kelly 0.00505 → 0.505% risk
    # with confidence=1.0 — strictly below base_risk_pct, so the sizing is
    # observable instead of being swallowed by the base cap.
    ARGS = dict(entry_price=50000.0, sl=49500.0, tp=51000.0, atr_pct=1.5,
                p_tp=0.3367, mss_quality=0, atr=500.0, sl_source="atr")

    def test_percentage_scale_confidence_rejected(self, engine):
        dec = engine.evaluate(portfolio=PortfolioState(), confidence=33.67, **self.ARGS)
        assert dec.should_trade is False
        assert "outside [0, 1]" in (dec.rejection_reason or "")

    def test_unit_confidence_sizes_below_base(self, engine):
        dec = engine.evaluate(portfolio=PortfolioState(), confidence=1.0, **self.ARGS)
        assert dec.should_trade
        assert dec.risk_pct <= config.risk_engine.base_risk_pct
        # Raw Kelly 0.00505 → 0.505% risk, unclamped by base (0.6).
        assert dec.risk_pct == pytest.approx(0.505, abs=1e-6)

    def test_zero_confidence_rejected(self, engine):
        dec = engine.evaluate(portfolio=PortfolioState(), confidence=0.0, **self.ARGS)
        assert dec.should_trade is False
        assert "Kelly" in (dec.rejection_reason or "")


class TestKellyHaircut:
    def test_kelly_fraction_is_haircut_not_raw(self, engine):
        """kelly_fraction returned to analytics is raw_kelly × confidence."""
        dec = engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=50000.0, sl=49500.0, tp=51500.0,   # RR = 3
            atr_pct=1.5, p_tp=0.6, confidence=0.6,
            mss_quality=0, atr=500.0, sl_source="atr",
        )
        assert dec.should_trade
        raw = (0.6 * 3 - 0.4) / 3          # 0.3333 → capped to 0.20
        assert dec.kelly_fraction == pytest.approx(min(raw, 0.20) * 0.6, abs=1e-4)

    def test_nonpositive_kelly_rejected(self, engine):
        # RR = 2.0 clears the RR gate; p_tp=0.30 is below breakeven (1/3).
        dec = engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=50000.0, sl=49500.0, tp=51000.0,
            atr_pct=1.5, p_tp=0.30, confidence=0.30,
            mss_quality=0, atr=500.0, sl_source="atr",
        )
        assert dec.should_trade is False
        assert "Kelly" in (dec.rejection_reason or "")
