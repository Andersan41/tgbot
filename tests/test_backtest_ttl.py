"""
tests/test_backtest_ttl.py — Live parity: a backtest trade must not outlive
a live signal.

Live: scheduler/outcome_tracker.py closes a signal as EXPIRED after
OUTCOME_TTL_DAYS at the current close price. Backtest applies the same rule
(converted into bars), so backtest holding periods — and therefore WR/PF —
stay comparable with what the live tracker reports.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.engine import BacktestEngine
from backtest.engine import timeframe_duration
from config.settings import config
from tests.test_backtest_position_limit import _make_persistent_fvg_df


class TestBacktestOutcomeTtl:
    @pytest.mark.asyncio
    async def test_trades_never_outlive_outcome_ttl(self, monkeypatch):
        df = _make_persistent_fvg_df()
        monkeypatch.setattr(config.trading, "candles_limit", 200)
        monkeypatch.setattr(config, "htf_bias_v2", False)
        monkeypatch.setattr(config, "max_trade_duration_bars", 0)  # only TTL binds
        monkeypatch.setattr(config, "outcome_ttl_days", 1)         # 4h -> 6 bars

        engine = BacktestEngine(symbol="LINK/USDT", timeframe="4h", source="local")
        result = await engine.run(df=df)

        if not result.trades:
            pytest.skip("synthetic candles produced no trades")

        ttl_bars = int(1 * 86400 / timeframe_duration("4h").total_seconds())
        for t in result.trades:
            exit_index = t.exit_index if t.exit_index is not None else t.entry_index
            assert exit_index - t.entry_index <= ttl_bars, (
                f"trade held {exit_index - t.entry_index} bars > TTL {ttl_bars}"
            )
        assert any(t.exit_reason == "expired" for t in result.trades), (
            f"no expired exit among {[t.exit_reason for t in result.trades]}"
        )

    @pytest.mark.asyncio
    async def test_ttl_disabled_leaves_max_duration_as_the_cap(self, monkeypatch):
        df = _make_persistent_fvg_df()
        monkeypatch.setattr(config.trading, "candles_limit", 200)
        monkeypatch.setattr(config, "htf_bias_v2", False)
        monkeypatch.setattr(config, "max_trade_duration_bars", 5)
        monkeypatch.setattr(config, "outcome_ttl_days", 0)  # TTL off

        engine = BacktestEngine(symbol="LINK/USDT", timeframe="4h", source="local")
        result = await engine.run(df=df)

        for t in result.trades:
            assert t.exit_reason != "expired"
            exit_index = t.exit_index if t.exit_index is not None else t.entry_index
            assert exit_index - t.entry_index <= 5
