"""
test_backtest_parity.py — Validates that the backtest engine and the live scanner
share the SAME signal-evaluation logic.

Both callers use strategy/signal_evaluator.py:
  - estimate_p_tp()          (scanner Phase 3 / backtest Phase 3)
  - apply_symbol_overrides() (scanner Phase 1.46 / backtest)
  - htf_opposition()         (scanner Phase 1.45 / backtest)

These tests exercise the shared functions with mocked inputs (no exchange, no DB).
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import config
from indicators.engine import IndicatorValues
from risk.engine import PortfolioState, risk_engine
from strategy.pattern_engine import ICTSetup
from strategy.signal_evaluator import (
    apply_symbol_overrides,
    dedup_block_window,
    entry_zone_touched,
    estimate_p_tp,
    get_cooldown_minutes,
    htf_opposition,
)
from strategy.trade_plan import TradePlan


def make_ind(adx: float = 30.0, atr: float = 500.0, close: float = 50000.0) -> IndicatorValues:
    return IndicatorValues(
        symbol="BTC/USDT",
        timeframe="1h",
        close=close,
        high=close + 100.0,
        low=close - 100.0,
        volume=1000.0,
        ema_fast=close * 0.999,
        ema_slow=close * 0.997,
        ema_trend=close * 0.995,
        ema_fast_prev=close * 0.998,
        ema_slow_prev=close * 0.998,
        rsi=55.0,
        macd=1.0,
        macd_signal=0.5,
        macd_hist=0.5,
        macd_hist_prev=0.2,
        adx=adx,
        dmi_plus=25.0,
        dmi_minus=20.0,
        atr=atr,
        supertrend=close * 0.99,
        supertrend_direction=1,
        volume_sma=1000.0,
    )


def make_setup(
    direction: str = "buy",
    setup_type: str = "continuation",
    components: int = 2,
    mss_score: float = 60.0,
    overall_quality: float = 70.0,
    has_bos: bool = True,
    has_sweep: bool = True,
    has_mss: bool = True,
) -> ICTSetup:
    return ICTSetup(
        detected=True,
        direction=direction,
        setup_type=setup_type,
        has_sweep=has_sweep,
        has_bos=has_bos,
        has_mss=has_mss,
        mss_score=mss_score,
        overall_quality=overall_quality,
        components_found=[f"c{i}" for i in range(components)],
    )


def make_plan(sl_distance_pct: float = 2.0) -> TradePlan:
    return TradePlan(
        direction="buy",
        symbol="BTC/USDT",
        timeframe="1h",
        entry_price=50000.0,
        sl=49000.0,
        tp=53000.0,
        sl_source="bos",
        sl_distance_pct=sl_distance_pct,
        tp_distance_pct=6.0,
        is_valid=True,
    )


def htf_result(direction: str):
    return SimpleNamespace(direction=direction)


class TestEstimatePTp:
    """Scanner Phase 3 / backtest Phase 3 must use the exact same formula."""

    def test_baseline_two_components(self):
        p_tp, conf = estimate_p_tp(setup=make_setup(components=2))
        assert p_tp == pytest.approx(0.45)
        assert conf == pytest.approx(0.45)

    def test_components_boost(self):
        p3, _ = estimate_p_tp(setup=make_setup(components=3))
        p4, _ = estimate_p_tp(setup=make_setup(components=4))
        assert p3 == pytest.approx(0.45 + 0.08)
        assert p4 == pytest.approx(0.45 + 0.15)

    def test_context_boost(self):
        p_pos, _ = estimate_p_tp(setup=make_setup(), context_score=0.8)
        p_neg, _ = estimate_p_tp(setup=make_setup(), context_score=-1.0)
        assert p_pos == pytest.approx(0.45 + 0.08)
        assert p_neg == pytest.approx(0.45 - 0.10)

    def test_htf_alignment_boost(self):
        aligned, _ = estimate_p_tp(setup=make_setup(direction="buy"), htf_result=htf_result("bullish"))
        opposed, _ = estimate_p_tp(setup=make_setup(direction="buy"), htf_result=htf_result("bearish"))
        assert aligned == pytest.approx(0.45 + 0.05)
        assert opposed == pytest.approx(0.45)

    def test_htf_penalty_applied(self):
        p_tp, _ = estimate_p_tp(
            setup=make_setup(),
            htf_result=htf_result("bearish"),
            htf_penalty=config.htf_bias_continuation_penalty,
        )
        assert p_tp == pytest.approx(0.45 * config.htf_bias_continuation_penalty)

    def test_mtf_and_mss_boost(self):
        p_tp, _ = estimate_p_tp(setup=make_setup(mss_score=80), mtf_aligned=True)
        assert p_tp == pytest.approx(0.45 + 0.03 + 0.05)

    def test_clamped(self):
        lo, _ = estimate_p_tp(setup=make_setup(components=4), context_score=1.0, htf_penalty=0.01)
        hi, _ = estimate_p_tp(
            setup=make_setup(components=4, mss_score=90),
            context_score=1.0,
            htf_result=htf_result("bullish"),
            mtf_aligned=True,
        )
        assert lo == pytest.approx(0.15)
        assert hi == pytest.approx(0.83)


class TestApplySymbolOverrides:
    """Scanner Phase 1.46 / backtest overrides — real fields, no AttributeError."""

    def test_no_overrides_pass(self, monkeypatch):
        monkeypatch.setattr(config.trading, "symbol_overrides", {})
        blocked, reason = apply_symbol_overrides(
            symbol="BTC/USDT", ind=make_ind(), setup=make_setup(), trade_plan=make_plan(),
        )
        assert not blocked
        assert reason == ""

    def test_adx_min(self, monkeypatch):
        monkeypatch.setattr(
            config.trading, "symbol_overrides",
            {"BTC/USDT": {"adx_min": 35.0}},
        )
        blocked, reason = apply_symbol_overrides(
            symbol="BTC/USDT", ind=make_ind(adx=30.0), setup=make_setup(), trade_plan=make_plan(),
        )
        assert blocked
        assert "ADX" in reason

    def test_max_sl_pct_uses_trade_plan(self, monkeypatch):
        monkeypatch.setattr(
            config.trading, "symbol_overrides",
            {"BTC/USDT": {"max_sl_pct": 1.5}},
        )
        blocked, reason = apply_symbol_overrides(
            symbol="BTC/USDT", ind=make_ind(), setup=make_setup(), trade_plan=make_plan(sl_distance_pct=2.0),
        )
        assert blocked
        assert "SL" in reason

    def test_max_atr_pct(self, monkeypatch):
        ind = make_ind(atr=500.0, close=50000.0)  # atr_pct = 1.0
        monkeypatch.setattr(
            config.trading, "symbol_overrides",
            {"BTC/USDT": {"max_atr_pct": 0.5}},
        )
        blocked, reason = apply_symbol_overrides(
            symbol="BTC/USDT", ind=ind, setup=make_setup(), trade_plan=make_plan(),
        )
        assert blocked
        assert "ATR" in reason
        monkeypatch.setattr(
            config.trading, "symbol_overrides",
            {"BTC/USDT": {"max_atr_pct": 2.0}},
        )
        blocked, _ = apply_symbol_overrides(
            symbol="BTC/USDT", ind=ind, setup=make_setup(), trade_plan=make_plan(),
        )
        assert not blocked

    def test_min_quality_uses_overall_quality(self, monkeypatch):
        monkeypatch.setattr(
            config.trading, "symbol_overrides",
            {"BTC/USDT": {"min_quality": 80.0}},
        )
        blocked, reason = apply_symbol_overrides(
            symbol="BTC/USDT", ind=make_ind(), setup=make_setup(overall_quality=70.0),
            trade_plan=make_plan(),
        )
        assert blocked
        assert "quality" in reason

    def test_block_setup_types(self, monkeypatch):
        monkeypatch.setattr(
            config.trading, "symbol_overrides",
            {"BTC/USDT": {"block_setup_types": ["SELL_continuation"]}},
        )
        blocked, _ = apply_symbol_overrides(
            symbol="BTC/USDT", ind=make_ind(), setup=make_setup(direction="sell"),
            trade_plan=make_plan(),
        )
        assert blocked
        blocked, _ = apply_symbol_overrides(
            symbol="BTC/USDT", ind=make_ind(), setup=make_setup(direction="buy"),
            trade_plan=make_plan(),
        )
        assert not blocked


class TestHTFOpposition:
    """Scanner Phase 1.45 / backtest HTF gate & penalty decision."""

    def test_neutral(self):
        assert htf_opposition(make_setup(), "neutral") == (False, False)
        assert htf_opposition(make_setup(), None) == (False, False)

    def test_continuation_opposed_no_gate(self, monkeypatch):
        monkeypatch.setattr(config, "htf_hard_gate", False)
        opposed, hard = htf_opposition(make_setup(direction="buy"), "bearish")
        assert opposed and not hard

    def test_continuation_opposed_hard_gate(self, monkeypatch):
        monkeypatch.setattr(config, "htf_hard_gate", True)
        opposed, hard = htf_opposition(make_setup(direction="buy", setup_type="continuation"), "bearish")
        assert opposed and hard

    def test_reversal_opposed_never_hard_blocks(self, monkeypatch):
        monkeypatch.setattr(config, "htf_hard_gate", True)
        opposed, hard = htf_opposition(
            make_setup(direction="buy", setup_type="reversal", has_sweep=True), "bearish",
        )
        assert opposed and not hard

    def test_aligned_no_opposition(self, monkeypatch):
        monkeypatch.setattr(config, "htf_hard_gate", True)
        assert htf_opposition(make_setup(direction="buy"), "bullish") == (False, False)


class TestLiveBacktestParity:
    """End-to-end: identical inputs → identical P(TP) and risk decision."""

    def test_shared_pipeline_is_deterministic(self):
        setup = make_setup(components=3, mss_score=80)
        plan = make_plan()

        # Live scanner: estimate_p_tp + risk_engine.evaluate
        p_tp_live, conf_live = estimate_p_tp(
            setup=setup, context_score=0.0, htf_result=htf_result("bullish"),
            htf_penalty=1.0, mtf_aligned=False,
        )
        dec_live = risk_engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=plan.entry_price, sl=plan.sl, tp=plan.tp,
            atr_pct=1.0, p_tp=p_tp_live, confidence=conf_live,
            mss_quality=setup.mss_score, atr=500.0, sl_source=plan.sl_source,
        )

        # Backtest: same shared call, same parameters
        p_tp_bt, conf_bt = estimate_p_tp(
            setup=setup, context_score=0.0, htf_result=htf_result("bullish"),
            htf_penalty=1.0, mtf_aligned=False,
        )
        dec_bt = risk_engine.evaluate(
            portfolio=PortfolioState(),
            entry_price=plan.entry_price, sl=plan.sl, tp=plan.tp,
            atr_pct=1.0, p_tp=p_tp_bt, confidence=conf_bt,
            mss_quality=setup.mss_score, atr=500.0, sl_source=plan.sl_source,
        )

        assert p_tp_live == p_tp_bt
        assert conf_live == conf_bt
        assert dec_live.should_trade == dec_bt.should_trade
        assert dec_live.risk_pct == dec_bt.risk_pct
        assert dec_bt.should_trade

    def test_htf_penalty_flows_into_risk_sizing(self, monkeypatch):
        """An opposed HTF bias lowers P(TP) → smaller risk sizing in both callers."""
        monkeypatch.setattr(config, "htf_hard_gate", False)
        setup = make_setup(direction="buy", components=2)
        plan = make_plan()
        penalty = config.htf_bias_continuation_penalty

        aligned_ptp, _ = estimate_p_tp(setup=setup, htf_result=htf_result("bullish"), htf_penalty=1.0)
        opposed_ptp, _ = estimate_p_tp(setup=setup, htf_result=htf_result("bearish"), htf_penalty=penalty)

        assert opposed_ptp < aligned_ptp

        dec_aligned = risk_engine.evaluate(
            portfolio=PortfolioState(), entry_price=plan.entry_price, sl=plan.sl, tp=plan.tp,
            atr_pct=1.0, p_tp=aligned_ptp, confidence=aligned_ptp,
            mss_quality=setup.mss_score, atr=500.0, sl_source=plan.sl_source,
        )
        dec_opposed = risk_engine.evaluate(
            portfolio=PortfolioState(), entry_price=plan.entry_price, sl=plan.sl, tp=plan.tp,
            atr_pct=1.0, p_tp=opposed_ptp, confidence=opposed_ptp,
            mss_quality=setup.mss_score, atr=500.0, sl_source=plan.sl_source,
        )
        assert dec_opposed.risk_pct <= dec_aligned.risk_pct


# ---------------------------------------------------------------------------
# Cooldown / Dedup parity (live Phase-6 gate ⇄ backtest) — bit-in-bit, no copy
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone


class TestCooldownDedupParity:
    """Both callers invoke the SAME function (strategy/signal_evaluator.py).

    The live scanner and the backtest engine must never hold divergent copies —
    scanner.py imports dedup_block_window directly (verified in
    test_scanner_uses_shared_dedup). These tests pin the exact semantics,
    including the LINK 07-02 cluster case (4h-spaced same-direction entries).
    """

    def test_cooldown_formula(self):
        # max(base, tf_minutes × multiplier); 1h → 240 min (matches .env: base=240, mult=1.0)
        assert get_cooldown_minutes("1h", 240, 1.0) == 240
        # default-shaped config: 1h → max(45, 60×2)=120
        assert get_cooldown_minutes("1h", 45, 2.0) == 120
        # 15m with multiplier 2.0 → max(45, 15×2)=45
        assert get_cooldown_minutes("15m", 45, 2.0) == 45

    def test_same_direction_within_cooldown_blocked(self):
        now = datetime(2026, 7, 2, 0, 0, tzinfo=timezone.utc)
        last = now - timedelta(minutes=30)  # same-dir re-entry after 30m on 1h
        assert dedup_block_window(last, now, "BUY", "BUY", "1h", 240, 1.0) is not None

    def test_same_direction_at_cooldown_boundary_passes(self):
        # The LINK 07-02 case: entries at 00:00 / 04:00 / 08:00 — exactly 240m apart,
        # elapsed == cooldown → NOT blocked (identical in live and backtest).
        now = datetime(2026, 7, 2, 4, 0, tzinfo=timezone.utc)
        last = datetime(2026, 7, 2, 0, 0, tzinfo=timezone.utc)
        assert dedup_block_window(last, now, "BUY", "BUY", "1h", 240, 1.0) is None

    def test_same_direction_past_cooldown_passes(self):
        now = datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc)
        last = datetime(2026, 7, 2, 0, 0, tzinfo=timezone.utc)
        assert dedup_block_window(last, now, "BUY", "BUY", "1h", 240, 1.0) is None

    def test_cross_direction_half_cooldown(self):
        now = datetime(2026, 7, 2, 0, 0, tzinfo=timezone.utc)
        # cross-dir within half cooldown (120m) → blocked
        last = now - timedelta(minutes=60)
        assert dedup_block_window(last, now, "BUY", "SELL", "1h", 240, 1.0) is not None
        # cross-dir past half but within full → passes
        last = now - timedelta(minutes=180)
        assert dedup_block_window(last, now, "BUY", "SELL", "1h", 240, 1.0) is None

    def test_scanner_uses_shared_dedup(self):
        """BUG-19 guard: scanner must not carry its own dedup copy."""
        import scheduler.scanner as sc

        from strategy.signal_evaluator import dedup_block_window, get_cooldown_minutes

        assert sc.dedup_block_window is dedup_block_window
        assert sc.get_cooldown_minutes is get_cooldown_minutes


class TestEntryZoneParity:
    """require_entry_zone gate — shared function, correct direction logic.

    The scanner, backtest engine and offline funnels must all call the SAME
    entry_zone_touched() from strategy/signal_evaluator.py (mirroring the
    test_scanner_uses_shared_dedup pattern) so a parity-gap can never appear.

    The direction logic is deliberate and matches liquidity/fvg.py:_is_fvg_filled:
      BUY  (bullish FVG) → bar_low  <= fvg.top    (price retraced from above)
      SELL (bearish FVG) → bar_high >= fvg.bottom (price retraced from below)
    Swapping it would block the correct side and let phantom fills through.
    """

    @staticmethod
    def _fvg(type_, top, bottom, filled=False):
        from datetime import datetime, timezone
        return SimpleNamespace(
            type=type_, top=top, bottom=bottom, is_active=not filled,
            filled=filled, timestamp=datetime.now(timezone.utc), index=0,
        )

    def test_buy_touched_from_above(self):
        f = self._fvg("bullish", top=101.0, bottom=99.0)
        assert entry_zone_touched("buy", [f], bar_high=102.0, bar_low=100.5) is True

    def test_buy_not_reached(self):
        # BUY needs price to dip INTO the gap (bar_low <= top). bar_low=101.5
        # stays above top=101 → zone never touched → reject.
        f = self._fvg("bullish", top=101.0, bottom=99.0)
        assert entry_zone_touched("buy", [f], bar_high=102.0, bar_low=101.5) is False

    def test_sell_touched_from_below(self):
        f = self._fvg("bearish", top=101.0, bottom=99.0)
        assert entry_zone_touched("sell", [f], bar_high=99.5, bar_low=98.0) is True

    def test_sell_not_reached(self):
        f = self._fvg("bearish", top=101.0, bottom=99.0)
        assert entry_zone_touched("sell", [f], bar_high=98.5, bar_low=97.0) is False

    def test_wrong_direction_fvg_ignored(self):
        f = self._fvg("bullish", top=101.0, bottom=99.0)
        # SELL signal but only a bullish FVG present → no matching zone → pass
        assert entry_zone_touched("sell", [f], bar_high=100.0, bar_low=98.0) is True

    def test_filled_fvg_skipped(self):
        f = self._fvg("bearish", top=101.0, bottom=99.0, filled=True)
        assert entry_zone_touched("sell", [f], bar_high=98.0, bar_low=97.0) is True

    def test_no_fvgs_passes(self):
        assert entry_zone_touched("buy", [], bar_high=100.0, bar_low=99.0) is True

    def test_scanner_uses_shared_entry_zone(self):
        """Parity guard: scanner must call the same entry_zone_touched()."""
        import scheduler.scanner as sc

        from strategy.signal_evaluator import entry_zone_touched

        assert sc.entry_zone_touched is entry_zone_touched

    def test_engine_uses_shared_entry_zone(self):
        """Parity guard: backtest engine must call the same entry_zone_touched()."""
        import backtest.engine as be

        from strategy.signal_evaluator import entry_zone_touched

        assert be.entry_zone_touched is entry_zone_touched


class TestIndicatorPrecomputeParity:
    """The O(n) backtest path (precompute + values_at) must equal the legacy
    per-window calculate() bit-for-bit — otherwise results change."""

    _FIELDS = [
        "close", "high", "low", "volume",
        "ema_fast", "ema_slow", "ema_trend", "ema_fast_prev", "ema_slow_prev",
        "rsi", "macd", "macd_signal", "macd_hist", "macd_hist_prev",
        "adx", "dmi_plus", "dmi_minus", "atr",
        "supertrend", "supertrend_direction", "volume_sma",
    ]

    def _make_df(self):
        import numpy as np
        import pandas as pd

        n = max(160, config.trading.candles_limit // 2 + 20)
        rng = np.random.default_rng(7)
        idx = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
        close = 100 + np.cumsum(rng.normal(0, 1, n))
        return pd.DataFrame(
            {
                "open": close,
                "high": close + rng.uniform(0.1, 1.0, n),
                "low": close - rng.uniform(0.1, 1.0, n),
                "close": close,
                "volume": rng.uniform(100, 200, n),
            },
            index=idx,
        )

    def test_values_at_matches_calculate(self):
        from indicators.engine import IndicatorEngine

        df = self._make_df()
        eng = IndicatorEngine()
        full = df.copy()
        assert eng.precompute(full, "TEST/USDT", "1h") is not None

        start = config.trading.candles_limit // 2 + 10
        for i in [start, start + 10, len(df) - 5, len(df) - 1]:
            ref = eng.calculate(df.iloc[: i + 1].copy(), "TEST/USDT", "1h")
            got = eng.values_at(full, "TEST/USDT", "1h", i)
            assert ref is not None and got is not None
            for f in self._FIELDS:
                rv, gv = getattr(ref, f), getattr(got, f)
                assert abs(rv - gv) < 1e-9, f"row {i} {f}: {rv} vs {gv}"

    def test_values_at_early_bar_none(self):
        from indicators.engine import IndicatorEngine

        df = self._make_df()
        eng = IndicatorEngine()
        full = df.copy()
        eng.precompute(full, "TEST/USDT", "1h")
        assert eng.values_at(full, "TEST/USDT", "1h", 0) is None
        assert eng.values_at(full, "TEST/USDT", "1h", 2) is None