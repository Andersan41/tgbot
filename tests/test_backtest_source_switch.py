"""
test_backtest_source_switch.py — engine --source=local reads parquet cache,
--source=live calls exchange_client, missing cache falls back to live.
"""
from __future__ import annotations

import os
import sys
from datetime import timezone
from unittest.mock import AsyncMock

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtest.cache_ohlcv as ohlcv_cache
from backtest.engine import BacktestEngine, BacktestResult
from config.settings import config
from data.exchange_client import exchange_client


def _make_15m_df(n: int = 600) -> pd.DataFrame:
    """Synthetic 15m OHLCV with a DatetimeIndex (UTC)."""
    idx = pd.date_range("2025-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame(
        {
            "open": 100.0 + idx.asi8 / 1e9 % 1.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000.0,
        },
        index=idx,
    )


def _write_cache(tmp_path, symbol: str, market_type: str, n: int = 600):
    """Write a unified 15m parquet into *tmp_path* as the OHLCV cache dir."""
    monkeypatch_cwd = tmp_path
    ohlcv_cache.save_unified(_make_15m_df(n), symbol, market_type, "15m")
    return monkeypatch_cwd


class TestSourceSwitch:
    @pytest.mark.asyncio
    async def test_local_reads_parquet(self, tmp_path, monkeypatch):
        """--source=local reads the parquet cache, never hits the exchange."""
        market = config.exchange.market_type
        monkeypatch.setattr(ohlcv_cache, "OHLCV_CACHE_DIR", tmp_path)
        _write_cache(tmp_path, "BTC/USDT", market)

        monkeypatch.setattr(config.trading, "candles_limit", 100)
        monkeypatch.setattr(config, "htf_bias_v2", False)

        called = {"load": 0, "fetch": 0, "connect": 0}
        orig_load = ohlcv_cache.load_unified

        def fake_load(symbol, market_type, timeframe="15m"):
            called["load"] += 1
            return orig_load(symbol, market_type, timeframe)

        monkeypatch.setattr(ohlcv_cache, "load_unified", fake_load)

        async def no_connect():
            called["connect"] += 1

        async def fail_fetch(*args, **kwargs):
            called["fetch"] += 1
            raise AssertionError("fetch_ohlcv must not be called with --source=local")

        monkeypatch.setattr(exchange_client, "connect", no_connect)
        monkeypatch.setattr(exchange_client, "fetch_ohlcv", fail_fetch)

        engine = BacktestEngine(symbol="BTC/USDT", timeframe="15m", source="local")
        result = await engine.run()

        assert isinstance(result, BacktestResult)
        assert result.symbol == "BTC/USDT"
        assert called["load"] >= 1
        assert called["fetch"] == 0
        assert called["connect"] == 0

    @pytest.mark.asyncio
    async def test_local_resamples_non_base_tf(self, tmp_path, monkeypatch):
        """--source=local with tf=1h resamples the stored 15m data."""
        market = config.exchange.market_type
        monkeypatch.setattr(ohlcv_cache, "OHLCV_CACHE_DIR", tmp_path)
        _write_cache(tmp_path, "ETH/USDT", market, n=4 * 24 * 5)  # 5 days of 15m

        monkeypatch.setattr(config.trading, "candles_limit", 100)
        monkeypatch.setattr(config, "htf_bias_v2", False)

        from backtest import resampler as resampler_mod

        resample_calls = []
        orig_resample = resampler_mod.resample_ohlcv

        def fake_resample(df, target_tf):
            resample_calls.append(target_tf)
            return orig_resample(df, target_tf)

        monkeypatch.setattr(resampler_mod, "resample_ohlcv", fake_resample)

        async def fail_fetch(*args, **kwargs):
            raise AssertionError("fetch_ohlcv must not be called with --source=local")

        monkeypatch.setattr(exchange_client, "connect", AsyncMock())
        monkeypatch.setattr(exchange_client, "fetch_ohlcv", fail_fetch)

        engine = BacktestEngine(symbol="ETH/USDT", timeframe="1h", source="local")
        result = await engine.run()

        assert isinstance(result, BacktestResult)
        assert "1h" in resample_calls

    @pytest.mark.asyncio
    async def test_live_calls_exchange(self, monkeypatch):
        """--source=live calls exchange_client.fetch_ohlcv."""
        monkeypatch.setattr(config.trading, "candles_limit", 100)
        monkeypatch.setattr(config, "htf_bias_v2", False)

        called = {"fetch": 0, "connect": 0}

        async def no_connect():
            called["connect"] += 1

        async def fake_fetch(symbol, timeframe, limit=200, since=None, drop_last=True):
            called["fetch"] += 1
            return None  # no data → engine returns an empty result quickly

        monkeypatch.setattr(exchange_client, "connect", no_connect)
        monkeypatch.setattr(exchange_client, "fetch_ohlcv", fake_fetch)

        engine = BacktestEngine(symbol="BTC/USDT", timeframe="1h", source="live")
        result = await engine.run()

        assert isinstance(result, BacktestResult)
        assert called["fetch"] >= 1
        assert called["connect"] >= 1

    @pytest.mark.asyncio
    async def test_local_missing_falls_back_to_live(self, tmp_path, monkeypatch):
        """--source=local with no cache file → WARNING + live fallback."""
        monkeypatch.setattr(ohlcv_cache, "OHLCV_CACHE_DIR", tmp_path)  # empty dir
        monkeypatch.setattr(config.trading, "candles_limit", 100)
        monkeypatch.setattr(config, "htf_bias_v2", False)

        called = {"fetch": 0, "connect": 0}

        async def no_connect():
            called["connect"] += 1

        async def fake_fetch(symbol, timeframe, limit=200, since=None, drop_last=True):
            called["fetch"] += 1
            return None

        monkeypatch.setattr(exchange_client, "connect", no_connect)
        monkeypatch.setattr(exchange_client, "fetch_ohlcv", fake_fetch)

        engine = BacktestEngine(symbol="BTC/USDT", timeframe="1h", source="local")
        result = await engine.run()

        assert isinstance(result, BacktestResult)
        assert called["fetch"] >= 1
        assert called["connect"] >= 1