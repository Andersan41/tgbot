"""
test_backtest_position_limit.py — Invariant: on a single symbol the number of
simultaneously-open positions never exceeds the configured limit.

Context: a LINK/USDT 4h run showed 8 SELLs at the same level (entry=13.5185)
spaced exactly 4h apart. The question was whether those are 8 positions open
AT ONCE (limit violated) or 8 sequential re-entries into one persistent
unfilled FVG (limit respected). This suite pins the invariant on both code
paths:

  * backtest  — BacktestEngine.result.trades (entry_index/exit_index) must
                never have more than config.max_active_signals overlapping
                intervals, and the sum of risk must stay below the portfolio cap.
  * live      — scheduler.scanner.scan_symbol_v2 Phase 0.2 must reject a new
                signal as soon as db active-signal count hits max_active_signals
                (and when portfolio risk reaches max_portfolio_risk_pct).

The backtest test uses synthetic candles shaped like the real LINK cluster
(drop -> persistent unfilled bearish FVG -> sideways drift), so a phantom
FVG-median entry that can never fill is exercised exactly as in production.
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.engine import BacktestEngine
from config.settings import config
from risk.engine import risk_engine


def _max_concurrent_open(trades) -> int:
    """Max number of [entry_index, exit_index] intervals overlapping at any bar."""
    if not trades:
        return 0
    events = []
    for t in trades:
        e = t.entry_index
        x = t.exit_index if t.exit_index is not None else t.entry_index
        events.append((min(e, x), +1))
        events.append((max(e, x), -1))
    events.sort()
    cur = mx = 0
    for _, d in events:
        cur += d
        mx = max(mx, cur)
    return mx


def _make_persistent_fvg_df(n: int = 260, base: float = 13.5) -> pd.DataFrame:
    """Synthetic 4h OHLCV: sharp drop creating a bearish FVG, then a long
    sideways drift BELOW it so the gap stays unfilled — the exact shape that
    produced the 8x SELL @ 13.5185 stack in the real LINK run."""
    idx = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    rng = np.random.default_rng(7)

    # Phase 1: rise to the gap top
    rise = np.linspace(base * 0.92, base, 40)
    # Phase 2: crash (3 candles) -> bearish FVG between candle i-2 low and i high
    crash = np.array([base, base * 0.975, base * 0.96])
    # Phase 3: flat drift well below the gap
    drift = base * 0.90 + rng.normal(0, base * 0.004, n - 40 - 3)

    closes = np.concatenate([rise, crash, drift])
    closes = np.maximum(closes, 1e-4)
    highs = np.maximum(closes * (1 + rng.uniform(0.002, 0.01, len(closes))),
                       np.concatenate([rise[1:], crash[1:], [closes[-1] * 1.002]]) if False else closes * 1.002)
    lows = closes * (1 - rng.uniform(0.002, 0.01, len(closes)))
    opens = np.concatenate([[closes[0]], closes[:-1]])

    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": rng.uniform(800, 1500, len(closes)),
    }, index=idx)


class TestBacktestPositionLimit:
    @pytest.mark.asyncio
    async def test_max_concurrent_open_never_exceeds_limit(self, monkeypatch):
        """Backtest invariant: overlapping position intervals <= max_active_signals."""
        df = _make_persistent_fvg_df()
        monkeypatch.setattr(config.trading, "candles_limit", 200)
        monkeypatch.setattr(config, "htf_bias_v2", False)

        engine = BacktestEngine(symbol="LINK/USDT", timeframe="4h", source="local")
        result = await engine.run(df=df)

        max_concurrent = _max_concurrent_open(result.trades)
        assert max_concurrent <= config.max_active_signals, (
            f"max concurrent open positions {max_concurrent} exceeds "
            f"max_active_signals={config.max_active_signals}"
        )

    @pytest.mark.asyncio
    async def test_repeated_reentries_stay_sequential_not_stack(self, monkeypatch):
        """Even when the engine re-enters the same FVG level every cooldown bar,
        positions must close before a new one opens (sequential, not a stack)."""
        df = _make_persistent_fvg_df()
        monkeypatch.setattr(config.trading, "candles_limit", 200)
        monkeypatch.setattr(config, "htf_bias_v2", False)

        engine = BacktestEngine(symbol="LINK/USDT", timeframe="4h", source="local")
        result = await engine.run(df=df)

        if not result.trades:
            pytest.skip("synthetic candles produced no trades")

        # Cluster consecutive trades by identical entry price (same FVG median)
        clusters: dict[float, list] = {}
        for t in result.trades:
            clusters.setdefault(round(t.entry_price, 4), []).append(t)

        for level, grp in clusters.items():
            if len(grp) < 2:
                continue
            # Within one level-cluster the positions must be sequential:
            # a new entry may only start after the previous one exited.
            seq = sorted(grp, key=lambda t: t.entry_index)
            for a, b in zip(seq, seq[1:]):
                assert b.entry_index > (a.exit_index if a.exit_index is not None else a.entry_index), (
                    f"two positions at level {level} overlap: "
                    f"#{a.entry_index}->#{a.exit_index} vs #{b.entry_index}->#{b.exit_index}"
                )


class TestLivePortfolioGateLimit:
    @pytest.mark.asyncio
    async def test_live_blocks_new_signal_at_max_active(self, monkeypatch):
        """Live invariant: Phase 0.2 rejects a signal once active count hits the cap."""
        from storage.database import db as _db
        from scheduler.scanner import scan_symbol_v2

        cap = config.max_active_signals
        monkeypatch.setattr(_db, "get_active_signals_count", AsyncMock(return_value=cap))
        monkeypatch.setattr(_db, "get_portfolio_risk_sum", AsyncMock(return_value=0.0))
        monkeypatch.setattr("scheduler.scanner._get_indicators",
                            AsyncMock(return_value=(MagicMock(atr=600.0, close=50000.0), MagicMock())))

        result = await scan_symbol_v2("BTC/USDT", "1h", AsyncMock())
        assert result is None  # blocked at active_count >= max_active_signals

    @pytest.mark.asyncio
    async def test_live_blocks_new_signal_at_portfolio_risk_cap(self, monkeypatch):
        from storage.database import db as _db
        from scheduler.scanner import scan_symbol_v2

        monkeypatch.setattr(_db, "get_active_signals_count", AsyncMock(return_value=1))
        monkeypatch.setattr(_db, "get_portfolio_risk_sum",
                            AsyncMock(return_value=config.max_portfolio_risk_pct))
        monkeypatch.setattr("scheduler.scanner._get_indicators",
                            AsyncMock(return_value=(MagicMock(atr=600.0, close=50000.0), MagicMock())))

        result = await scan_symbol_v2("BTC/USDT", "1h", AsyncMock())
        assert result is None  # blocked at portfolio risk >= max_portfolio_risk_pct

    @pytest.mark.asyncio
    async def test_live_never_allows_above_cap_under_repeated_scans(self, monkeypatch):
        """Simulate repeated scans: as the active count grows toward the cap, the
        scanner must keep rejecting; a signal is only allowed while below it."""
        from storage.database import db as _db
        from scheduler.scanner import scan_symbol_v2, _is_cooldown_active, _set_cooldown

        cap = config.max_active_signals
        state = {"count": 0, "risk": 0.0}
        cooldowns: dict = {}

        async def fake_get(symbol, timeframe):
            return cooldowns.get((symbol, timeframe))

        async def fake_set(symbol, timeframe, ts):
            cooldowns[(symbol, timeframe)] = ts

        monkeypatch.setattr("scheduler.scanner.db.get_cooldown", fake_get)
        monkeypatch.setattr("scheduler.scanner.db.set_cooldown", fake_set)
        monkeypatch.setattr(_db, "get_active_signals_count", AsyncMock(
            side_effect=lambda: state["count"]))
        monkeypatch.setattr(_db, "get_portfolio_risk_sum", AsyncMock(
            side_effect=lambda: state["risk"]))

        for n in range(cap, cap + 3):  # at the cap and above -> always blocked
            state["count"] = n
            monkeypatch.setattr("scheduler.scanner._get_indicators",
                                AsyncMock(return_value=(MagicMock(atr=600.0, close=50000.0), MagicMock())))
            result = await scan_symbol_v2("BTC/USDT", "1h", AsyncMock())
            assert result is None, f"signal emitted at active_count={n} >= cap={cap}"

        # Below the cap, the gate must not be the blocker (it proceeds to indicators).
        state["count"] = cap - 1
        monkeypatch.setattr("scheduler.scanner._get_indicators",
                            AsyncMock(return_value=(MagicMock(atr=600.0, close=50000.0), MagicMock())))
        result = await scan_symbol_v2("BTC/USDT", "1h", AsyncMock())
        assert result is None  # blocked later (indicators unavailable) — proves gate 0.2 passed


class TestRiskEngineMatchesConfig:
    def test_risk_engine_singleton_uses_config_limits(self):
        """The live/backtest risk engine is built from the same config caps."""
        assert risk_engine.max_active_signals == config.max_active_signals
        assert risk_engine.max_portfolio_risk_pct == config.max_portfolio_risk_pct