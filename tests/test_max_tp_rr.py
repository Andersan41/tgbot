"""
tests/test_max_tp_rr.py — MAX_TP_RR: upper bound for the planned TP.

A target farther than MAX_TP_RR x SL cannot be reached inside
OUTCOME_TTL_DAYS, so the trade would expire while still holding a
portfolio slot. The cap filters candidates in _find_targets and clamps
the final TP in build_trade_plan.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import config
from indicators.engine import IndicatorValues
from liquidity.order_blocks import OrderBlock
from liquidity.pool import LiquidityMap, LiquidityLevel
from liquidity.sweep import SweepEvent
from strategy.signal_engine import SignalType
from strategy.trade_engine import TradeEngine


def _liq(current_price: float = 100.0, levels: list | None = None) -> LiquidityMap:
    return LiquidityMap(
        symbol="BTC/USDT",
        timeframe="4h",
        current_price=current_price,
        levels=list(levels or []),
    )


def _find(entry=100.0, atr=1.0, invalidation=99.0, atr_tp=3.0,
          max_tp_rr=6.0, levels=None):
    return TradeEngine()._find_targets(
        signal=SignalType.BUY,
        entry=entry,
        atr=atr,
        liq_map=_liq(levels=levels),
        invalidation_level=invalidation,
        atr_tp=atr_tp,
        max_tp_rr=max_tp_rr,
    )


class TestFindTargetsCap:
    def test_far_target_dropped_near_kept(self):
        targets = _find(levels=[
            LiquidityLevel(type="equal_high", level=101.0, strength=0.8),
            LiquidityLevel(type="old_high", level=130.0, strength=0.9),
        ])
        levels = [t.level for t in targets]
        assert 101.0 in levels          # RR 1.0 — inside the cap
        assert 130.0 not in levels      # RR 30.0 — beyond MAX_TP_RR=6

    def test_cap_disabled_keeps_far_target(self):
        targets = _find(max_tp_rr=0.0, levels=[
            LiquidityLevel(type="equal_high", level=101.0, strength=0.8),
            LiquidityLevel(type="old_high", level=130.0, strength=0.9),
        ])
        levels = [t.level for t in targets]
        assert 101.0 in levels and 130.0 in levels

    def test_atr_fallback_clamped(self):
        # No liquidity levels -> ATR fallback (10x ATR = RR 10) must be clamped
        targets = _find(atr=1.0, invalidation=99.0, atr_tp=10.0, max_tp_rr=2.0)
        assert len(targets) == 1
        assert targets[0].type == "atr"
        assert targets[0].level == pytest.approx(102.0)
        assert targets[0].rr_ratio == pytest.approx(2.0)

    def test_atr_fallback_unclamped_when_cap_off(self):
        targets = _find(atr=1.0, invalidation=99.0, atr_tp=10.0, max_tp_rr=0.0)
        assert targets[0].level == pytest.approx(110.0)
        assert targets[0].rr_ratio == pytest.approx(10.0)


def _indicator_values(**overrides) -> IndicatorValues:
    fields = dict(
        symbol="BTC/USDT",
        timeframe="4h",
        close=100.0,
        high=101.0,
        low=99.0,
        volume=1000.0,
        ema_fast=100.1,
        ema_slow=100.0,
        ema_trend=99.5,
        ema_fast_prev=100.05,
        ema_slow_prev=99.98,
        rsi=55.0,
        macd=0.1,
        macd_signal=0.05,
        macd_hist=0.05,
        macd_hist_prev=0.04,
        adx=25.0,
        dmi_plus=20.0,
        dmi_minus=15.0,
        atr=3.0,
        supertrend=99.0,
        supertrend_direction=1,
        volume_sma=900.0,
    )
    fields.update(overrides)
    return IndicatorValues(**fields)


class TestBuildTradePlanCap:
    def _plan(self):
        # BUY: sweep low 99.4 -> invalidation, SL 98.95 (dist 1.05)
        # Bearish OB midpoint 109 -> RR 9/1.05 = 8.57 > MAX_TP_RR
        sweep = SweepEvent(
            type="bullish",
            swept_level=99.5,
            sweep_low=99.4,
            sweep_high=100.2,
            reclaim_candles=1,
            volume_ratio=3.0,
            timestamp=datetime.now(timezone.utc),
        )
        ob = OrderBlock(
            type="bearish",
            high=110.0,
            low=108.0,
            timestamp=datetime.now(timezone.utc),
            displacement_atr=3.0,
        )
        return TradeEngine().build_trade_plan(
            _indicator_values(),
            "buy",
            sweeps=[sweep],
            order_blocks=[ob],
            fvgs=[],
            df=None,
            timeframe="4h",
        )

    def test_final_rr_clamped(self):
        plan = self._plan()
        assert plan.is_valid, plan.rejection_reason
        assert plan.rr_ratio == pytest.approx(config.trading.max_tp_rr)
        assert plan.tp == pytest.approx(
            100.0 + abs(plan.entry_price - plan.sl) * config.trading.max_tp_rr
        )
        assert "capped" in plan.tp_source

    def test_uncapped_when_disabled(self, monkeypatch):
        monkeypatch.setattr(config.trading, "max_tp_rr", 0.0)
        plan = self._plan()
        assert plan.is_valid, plan.rejection_reason
        assert plan.tp == pytest.approx(109.0)   # OB midpoint, untouched
        assert plan.rr_ratio > 6.0
        assert "capped" not in plan.tp_source
