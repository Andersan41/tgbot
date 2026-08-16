"""Tests for backtest/cache_ohlcv.py — DatetimeIndex preservation and time overlap."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtest.cache_ohlcv as ohlcv_cache
from backtest.cache_ohlcv import (
    load_cached,
    save_cached,
    validate_time_overlap,
    _cache_path,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ohlcv(start: str, periods: int, freq: str = "1h") -> pd.DataFrame:
    """Create a synthetic OHLCV DataFrame with DatetimeIndex."""
    idx = pd.date_range(start, periods=periods, freq=freq, tz=timezone.utc)
    import random
    random.seed(42)
    data = {
        "open": [100.0 + random.uniform(-5, 5) for _ in range(periods)],
        "high": [105.0 + random.uniform(0, 5) for _ in range(periods)],
        "low": [95.0 + random.uniform(-5, 0) for _ in range(periods)],
        "close": [100.0 + random.uniform(-5, 5) for _ in range(periods)],
        "volume": [1000.0 + random.uniform(-100, 100) for _ in range(periods)],
    }
    return pd.DataFrame(data, index=idx)


def _make_bad_ohlcv(periods: int) -> pd.DataFrame:
    """Create OHLCV DataFrame with integer RangeIndex (no DatetimeIndex)."""
    data = {
        "open": [100.0] * periods,
        "high": [105.0] * periods,
        "low": [95.0] * periods,
        "close": [100.0] * periods,
        "volume": [1000.0] * periods,
    }
    return pd.DataFrame(data, index=range(periods))


def _df_from_ms(start_ms: int, count: int, freq: str = "15min") -> pd.DataFrame:
    """Synthetic OHLCV DataFrame starting at a millisecond timestamp."""
    idx = pd.date_range(pd.to_datetime(start_ms, unit="ms", utc=True),
                        periods=count, freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "open": 100.0, "high": 101.0, "low": 99.0,
            "close": 100.5, "volume": 1000.0,
        },
        index=idx,
    )


# ---------------------------------------------------------------------------
# save_cached / load_cached round-trip
# ---------------------------------------------------------------------------

class TestSaveLoadRoundTrip:
    def test_preserves_datetime_index(self, tmp_path, monkeypatch):
        """save_cached must preserve DatetimeIndex through parquet round-trip."""
        monkeypatch.setattr("backtest.cache_ohlcv.CACHE_DIR", tmp_path)
        df = _make_ohlcv("2025-01-01", 100)
        save_cached("TEST/USDT", "1h", 100, df)

        loaded = load_cached("TEST/USDT", "1h", 100)
        assert loaded is not None
        assert isinstance(loaded.index, pd.DatetimeIndex)
        assert loaded.index[0] == df.index[0]
        assert loaded.index[-1] == df.index[-1]
        assert len(loaded) == 100

    def test_load_rejects_non_datetime_index(self, tmp_path, monkeypatch):
        """load_cached raises ValueError when index is not DatetimeIndex."""
        monkeypatch.setattr("backtest.cache_ohlcv.CACHE_DIR", tmp_path)
        df_bad = _make_bad_ohlcv(100)
        path = _cache_path("TEST/USDT", "1h", 100)
        tmp_path.mkdir(parents=True, exist_ok=True)
        df_bad.to_parquet(path, index=True)  # saves with RangeIndex

        with pytest.raises(ValueError, match="DatetimeIndex"):
            load_cached("TEST/USDT", "1h", 100)

    def test_load_returns_none_when_not_cached(self, tmp_path, monkeypatch):
        """load_cached returns None when file does not exist."""
        monkeypatch.setattr("backtest.cache_ohlcv.CACHE_DIR", tmp_path)
        assert load_cached("NONEXISTENT/USDT", "1h", 100) is None


# ---------------------------------------------------------------------------
# validate_time_overlap
# ---------------------------------------------------------------------------

class TestValidateTimeOverlap:
    def test_passes_when_aligned(self):
        """No error when 1h and 15m data fully overlap."""
        df_1h = _make_ohlcv("2025-01-01", 100, "1h")
        # 15m data covers the same range
        start = df_1h.index[0]
        end = df_1h.index[-1]
        df_15m = _make_ohlcv("2025-01-01", 400, "15min")
        # Trim 15m to same range
        df_15m = df_15m[(df_15m.index >= start) & (df_15m.index <= end)]
        validate_time_overlap(df_1h, df_15m)

    def test_fails_when_no_overlap(self):
        """Raises ValueError when 15m data starts after 1h data ends."""
        df_1h = _make_ohlcv("2025-01-01", 100, "1h")
        df_15m = _make_ohlcv("2026-06-01", 400, "15min")
        with pytest.raises(ValueError, match="No temporal overlap"):
            validate_time_overlap(df_1h, df_15m)

    def test_fails_when_overlap_below_threshold(self):
        """Raises ValueError when overlap < 90%."""
        df_1h = _make_ohlcv("2025-01-01", 100, "1h")
        # 15m data starts halfway through 1h range
        mid = df_1h.index[50]
        df_15m = _make_ohlcv(str(mid), 200, "15min")
        with pytest.raises(ValueError, match="below threshold"):
            validate_time_overlap(df_1h, df_15m)

    def test_rejects_non_datetime_index(self):
        """Raises ValueError when either DataFrame lacks DatetimeIndex."""
        df_bad = _make_bad_ohlcv(100)
        df_good = _make_ohlcv("2025-01-01", 100, "1h")
        with pytest.raises(ValueError, match="DatetimeIndex"):
            validate_time_overlap(df_bad, df_good)


# ---------------------------------------------------------------------------
# Time alignment test (simulates real 1h/15m cache scenario)
# ---------------------------------------------------------------------------

class TestTimeAlignment:
    def test_15m_first_last_within_1h_range(self, tmp_path, monkeypatch):
        """Load 1h and 15m data, verify 15m timestamps fall within 1h range.

        This test should FAIL on old cache files (index=False) and PASS after fix.
        """
        monkeypatch.setattr("backtest.cache_ohlcv.CACHE_DIR", tmp_path)

        # Simulate realistic data: same time range, different granularities
        df_1h = _make_ohlcv("2025-01-01", 3900, "1h")
        df_15m = _make_ohlcv("2025-01-01", 15552, "15min")

        save_cached("TEST/USDT", "1h", 3900, df_1h)
        save_cached("TEST/USDT", "15m", 15552, df_15m)

        loaded_1h = load_cached("TEST/USDT", "1h", 3900)
        loaded_15m = load_cached("TEST/USDT", "15m", 15552)

        assert loaded_1h is not None and loaded_15m is not None
        assert isinstance(loaded_1h.index, pd.DatetimeIndex)
        assert isinstance(loaded_15m.index, pd.DatetimeIndex)

        h_first, h_last = loaded_1h.index.min(), loaded_1h.index.max()
        m_first, m_last = loaded_15m.index.min(), loaded_15m.index.max()

        # 15m first timestamp must be >= 1h first timestamp
        assert m_first >= h_first, (
            f"15m starts at {m_first} which is before 1h start {h_first}"
        )
        # 15m last timestamp must be <= 1h last timestamp
        assert m_last <= h_last, (
            f"15m ends at {m_last} which is after 1h end {h_last}"
        )

        # Overlap validation must pass
        validate_time_overlap(loaded_1h, loaded_15m)


# ---------------------------------------------------------------------------
# Unified history cache (ohlcv_cache/) — fetch_full_history / update_history
# ---------------------------------------------------------------------------

class TestUnifiedRoundTrip:
    def test_save_load_columns_and_format(self, tmp_path, monkeypatch):
        """Unified files carry timestamp + OHLCV columns, sorted, deduped."""
        monkeypatch.setattr(ohlcv_cache, "OHLCV_CACHE_DIR", tmp_path)
        idx = pd.date_range("2025-01-01", periods=4, freq="15min", tz="UTC")
        df = pd.DataFrame(
            {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0},
            index=idx,
        )
        # Duplicate rows on purpose → must be deduped
        dup = pd.concat([df, df.iloc[[0]]])
        ohlcv_cache.save_unified(dup, "TEST/USDT", "swap", "15m")

        path = ohlcv_cache.unified_cache_path("TEST/USDT", "swap", "15m")
        assert path.exists()
        raw = pd.read_parquet(path)
        assert list(raw.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
        assert len(raw) == 4
        assert raw["timestamp"].is_monotonic_increasing
        assert raw["timestamp"].is_unique

        loaded = ohlcv_cache.load_unified("TEST/USDT", "swap", "15m")
        assert isinstance(loaded.index, pd.DatetimeIndex)
        assert loaded.index.tz is not None
        assert loaded.index[0] == idx[0]

    def test_load_unified_missing_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ohlcv_cache, "OHLCV_CACHE_DIR", tmp_path)
        assert ohlcv_cache.load_unified("NOPE/USDT", "swap", "15m") is None

    def test_to_unified_dedups_and_sorts(self):
        idx = pd.date_range("2025-01-01", periods=3, freq="15min", tz="UTC")
        df = pd.DataFrame(
            {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0},
            index=idx[::-1],  # reversed order
        )
        df = pd.concat([df, df.iloc[[0]]])
        out = ohlcv_cache._to_unified_df(df)
        assert list(out.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
        assert out["timestamp"].is_monotonic_increasing
        assert out["timestamp"].is_unique
        assert len(out) == 3


class TestFetchFullHistory:
    @pytest.mark.asyncio
    async def test_returns_none_and_saves_parquet(self, tmp_path, monkeypatch):
        """fetch_full_history does NOT return a DataFrame; writes parquet + marker."""
        monkeypatch.setattr(ohlcv_cache, "OHLCV_CACHE_DIR", tmp_path)
        monkeypatch.setattr(ohlcv_cache.exchange_client, "connect", AsyncMock())

        calls = []

        async def fake_fetch(symbol, timeframe, limit=998, drop_last=True, end_time=None):
            calls.append(end_time)
            return _df_from_ms(end_time - 3 * 15 * 60_000, 3)  # partial → history ends

        monkeypatch.setattr(ohlcv_cache.exchange_client, "fetch_ohlcv", fake_fetch)

        result = await ohlcv_cache.fetch_full_history(
            "TEST/USDT", years=1, market_type="swap", page_size=5,
        )
        assert result is None
        assert len(calls) == 1
        path = ohlcv_cache.unified_cache_path("TEST/USDT", "swap", "15m")
        assert path.exists()
        assert ohlcv_cache._marker_exists(path)

    @pytest.mark.asyncio
    async def test_backward_pagination_end_time_per_batch(self, tmp_path, monkeypatch):
        """Backward pagination: each next batch uses end_time < previous oldest."""
        monkeypatch.setattr(ohlcv_cache, "OHLCV_CACHE_DIR", tmp_path)
        monkeypatch.setattr(ohlcv_cache.exchange_client, "connect", AsyncMock())
        monkeypatch.setattr(ohlcv_cache.asyncio, "sleep", AsyncMock())

        page_size = 5
        tf_ms = 15 * 60_000
        calls: list[dict] = []

        async def fake_fetch(symbol, timeframe, limit=998, drop_last=True, end_time=None):
            calls.append({
                "symbol": symbol, "timeframe": timeframe,
                "end_time": end_time, "drop_last": drop_last, "limit": limit,
            })
            if len(calls) < 3:
                # батч — окно [end_time - (page_size-1)*tf, end_time] по возрастанию
                return _df_from_ms(end_time - (page_size - 1) * tf_ms, page_size)
            return _df_from_ms(end_time - tf_ms, 2)  # partial → история закончилась

        monkeypatch.setattr(ohlcv_cache.exchange_client, "fetch_ohlcv", fake_fetch)

        await ohlcv_cache.fetch_full_history(
            "TEST/USDT", years=1, market_type="swap", page_size=page_size,
        )

        assert len(calls) == 3
        b1, b2, b3 = calls
        assert b1["symbol"] == "TEST/USDT"
        assert b1["timeframe"] == "15m"
        assert b1["end_time"] is not None
        assert b1["drop_last"] is False
        assert b1["limit"] == page_size
        # следующий батч идёт строго до самой старой свечи предыдущего (минус 1ms)
        assert b2["end_time"] == b1["end_time"] - (page_size - 1) * tf_ms - 1
        assert b3["end_time"] == b2["end_time"] - (page_size - 1) * tf_ms - 1

        # файл сохранён с правильным числом свечей (5+5+2) и помечен как полный
        path = ohlcv_cache.unified_cache_path("TEST/USDT", "swap", "15m")
        assert path.exists()
        assert ohlcv_cache._marker_exists(path)
        df = pd.read_parquet(path)
        assert len(df) == page_size + page_size + 2
        assert df.index.is_monotonic_increasing

    @pytest.mark.asyncio
    async def test_stops_at_start_ms_cutoff(self, tmp_path, monkeypatch):
        """Backward pagination stops when a batch's oldest <= start_ms (target depth)."""
        monkeypatch.setattr(ohlcv_cache, "OHLCV_CACHE_DIR", tmp_path)
        monkeypatch.setattr(ohlcv_cache.exchange_client, "connect", AsyncMock())
        monkeypatch.setattr(ohlcv_cache.asyncio, "sleep", AsyncMock())

        # Контролируем через _fetch_batch_with_retry: возвращаем батч, который
        # перекрывает и цель, и современные свечи — проверим, что fetch_full_history
        # не ходит дальше первого батча (oldest <= start_ms).
        start_ms = int((datetime.now(timezone.utc) - timedelta(days=365)).timestamp() * 1000)

        async def fake_fetch(symbol, timeframe, limit=998, drop_last=True, end_time=None):
            # батч перекрывает start_ms → после обрезки остаётся кусок,
            # а oldest <= start_ms означает, что цель достигнута
            return _df_from_ms(start_ms, 20)

        monkeypatch.setattr(ohlcv_cache.exchange_client, "fetch_ohlcv", fake_fetch)

        await ohlcv_cache.fetch_full_history(
            "TEST/USDT", years=1, market_type="swap", page_size=20,
        )
        path = ohlcv_cache.unified_cache_path("TEST/USDT", "swap", "15m")
        assert path.exists()
        assert ohlcv_cache._marker_exists(path)
        df = pd.read_parquet(path)
        assert len(df) == 20


class TestFetchBatchRetry:
    @pytest.mark.asyncio
    async def test_retry_exponential_backoff(self, tmp_path, monkeypatch):
        """ccxt.NetworkError → retry with backoff (1, 2, 4, 8), then success."""
        import asyncio as _asyncio
        import ccxt

        monkeypatch.setattr(ohlcv_cache, "OHLCV_CACHE_DIR", tmp_path)
        monkeypatch.setattr(ohlcv_cache.exchange_client, "connect", AsyncMock())

        state = {"n": 0, "sleeps": []}
        real_sleep = _asyncio.sleep

        async def fake_sleep(delay):
            state["sleeps"].append(delay)
            await real_sleep(0)  # не ждать по-настоящему

        monkeypatch.setattr(ohlcv_cache.asyncio, "sleep", fake_sleep)

        async def fake_fetch(symbol, timeframe, limit=998, drop_last=True, end_time=None):
            state["n"] += 1
            if state["n"] < 3:
                raise ccxt.NetworkError("boom")
            return _df_from_ms(end_time - 3 * 15 * 60_000, 3)  # partial → history ends

        monkeypatch.setattr(ohlcv_cache.exchange_client, "fetch_ohlcv", fake_fetch)

        await ohlcv_cache.fetch_full_history(
            "TEST/USDT", years=1, market_type="swap", page_size=5,
        )
        assert state["n"] == 3
        assert state["sleeps"] == [1, 2]  # backoff 1s, then 2s

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts(self, tmp_path, monkeypatch):
        """Persistent ccxt.NetworkError → raises after 5 attempts."""
        import asyncio as _asyncio
        import ccxt

        monkeypatch.setattr(ohlcv_cache, "OHLCV_CACHE_DIR", tmp_path)
        monkeypatch.setattr(ohlcv_cache.exchange_client, "connect", AsyncMock())

        state = {"n": 0}
        real_sleep = _asyncio.sleep

        async def fake_sleep(delay):
            await real_sleep(0)

        monkeypatch.setattr(ohlcv_cache.asyncio, "sleep", fake_sleep)

        async def fake_fetch(symbol, timeframe, limit=998, drop_last=True, end_time=None):
            state["n"] += 1
            raise ccxt.NetworkError("boom")

        monkeypatch.setattr(ohlcv_cache.exchange_client, "fetch_ohlcv", fake_fetch)

        with pytest.raises(ccxt.NetworkError):
            await ohlcv_cache.fetch_full_history(
                "TEST/USDT", years=1, market_type="swap", page_size=5,
            )
        assert state["n"] == ohlcv_cache.HISTORY_RETRY_ATTEMPTS


class TestUpdateHistory:
    @pytest.mark.asyncio
    async def test_incremental_add_one_candle(self, tmp_path, monkeypatch):
        """Old data + 1 new candle → exactly N+1 rows, no dup at the boundary."""
        monkeypatch.setattr(ohlcv_cache, "OHLCV_CACHE_DIR", tmp_path)
        monkeypatch.setattr(ohlcv_cache.exchange_client, "connect", AsyncMock())
        monkeypatch.setattr(ohlcv_cache.exchange_client, "_markets_loaded", True)

        n = 96
        idx = pd.date_range("2025-01-01", periods=n, freq="15min", tz="UTC")
        old = pd.DataFrame(
            {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000.0},
            index=idx,
        )
        ohlcv_cache.save_unified(old, "TEST/USDT", "swap", "15m")
        ohlcv_cache._write_marker(ohlcv_cache.unified_cache_path("TEST/USDT", "swap", "15m"))

        end_time_calls = []

        async def fake_fetch(symbol, timeframe, limit=998, drop_last=True, end_time=None):
            end_time_calls.append(end_time)
            # Биржа возвращает backward-батч: свечи, оканчивающиеся на end_time.
            # Для теста игнорируем end_time и возвращаем ровно одну новую свечу
            # сразу после последнего ts кэша (как отдала бы реальная биржа).
            new_idx = pd.date_range(idx[-1] + timedelta(minutes=15), periods=1,
                                    freq="15min", tz="UTC")
            return pd.DataFrame(
                {"open": 100.0, "high": 101.0, "low": 99.0,
                 "close": 100.5, "volume": 1000.0},
                index=new_idx,
            )

        monkeypatch.setattr(ohlcv_cache.exchange_client, "fetch_ohlcv", fake_fetch)

        added = await ohlcv_cache.update_history("TEST/USDT", "15m", "swap")
        assert added == 1
        # первый backward-батч идёт с end_time = now (ms)
        assert end_time_calls and end_time_calls[0] is not None
        assert end_time_calls[0] > int(idx[-1].timestamp() * 1000)

        df = ohlcv_cache.load_unified("TEST/USDT", "swap", "15m")
        assert len(df) == n + 1
        assert df.index.is_unique
        assert df.index.max() == idx[-1] + timedelta(minutes=15)

    @pytest.mark.asyncio
    async def test_incremental_no_new_data(self, tmp_path, monkeypatch):
        """No new candles → returns 0 and the file is unchanged."""
        monkeypatch.setattr(ohlcv_cache, "OHLCV_CACHE_DIR", tmp_path)
        monkeypatch.setattr(ohlcv_cache.exchange_client, "connect", AsyncMock())
        monkeypatch.setattr(ohlcv_cache.exchange_client, "_markets_loaded", True)

        idx = pd.date_range("2025-01-01", periods=10, freq="15min", tz="UTC")
        old = pd.DataFrame(
            {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000.0},
            index=idx,
        )
        ohlcv_cache.save_unified(old, "TEST/USDT", "swap", "15m")
        ohlcv_cache._write_marker(ohlcv_cache.unified_cache_path("TEST/USDT", "swap", "15m"))

        async def fake_fetch(symbol, timeframe, limit=998, drop_last=True, end_time=None):
            return None  # nothing new

        monkeypatch.setattr(ohlcv_cache.exchange_client, "fetch_ohlcv", fake_fetch)

        added = await ohlcv_cache.update_history("TEST/USDT", "15m", "swap")
        assert added == 0
        assert len(ohlcv_cache.load_unified("TEST/USDT", "swap", "15m")) == 10

    @pytest.mark.asyncio
    async def test_dedup_on_partial_overlap(self, tmp_path, monkeypatch):
        """Partial overlap on the boundary → duplicates are removed."""
        monkeypatch.setattr(ohlcv_cache, "OHLCV_CACHE_DIR", tmp_path)
        monkeypatch.setattr(ohlcv_cache.exchange_client, "connect", AsyncMock())
        monkeypatch.setattr(ohlcv_cache.exchange_client, "_markets_loaded", True)

        n = 20
        idx = pd.date_range("2025-01-01", periods=n, freq="15min", tz="UTC")
        old = pd.DataFrame(
            {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000.0},
            index=idx,
        )
        ohlcv_cache.save_unified(old, "TEST/USDT", "swap", "15m")
        ohlcv_cache._write_marker(ohlcv_cache.unified_cache_path("TEST/USDT", "swap", "15m"))

        async def fake_fetch(symbol, timeframe, limit=998, drop_last=True, end_time=None):
            # Возвращает backward-батч, пересекающийся со старыми данными
            start = idx[-3]
            overlap_idx = pd.date_range(start, periods=5, freq="15min", tz="UTC")
            return pd.DataFrame(
                {"open": 100.0, "high": 101.0, "low": 99.0,
                 "close": 100.5, "volume": 1000.0},
                index=overlap_idx,
            )

        monkeypatch.setattr(ohlcv_cache.exchange_client, "fetch_ohlcv", fake_fetch)

        added = await ohlcv_cache.update_history("TEST/USDT", "15m", "swap")
        # пришло 5 свечей, 3 из которых пересекаются с кэшем
        assert added == 2

        df = ohlcv_cache.load_unified("TEST/USDT", "swap", "15m")
        assert len(df) == n + 2  # дубли выкинуты
        assert df.index.is_unique
        assert df.index.is_monotonic_increasing

    @pytest.mark.asyncio
    async def test_update_history_fetches_full_when_missing(self, tmp_path, monkeypatch):
        """No file → update_history delegates to fetch_full_history."""
        monkeypatch.setattr(ohlcv_cache, "OHLCV_CACHE_DIR", tmp_path)
        monkeypatch.setattr(ohlcv_cache.exchange_client, "connect", AsyncMock())

        async def fake_fetch(symbol, timeframe, limit=998, drop_last=True, end_time=None):
            return _df_from_ms(end_time - 9 * 15 * 60_000, 10)

        monkeypatch.setattr(ohlcv_cache.exchange_client, "fetch_ohlcv", fake_fetch)

        added = await ohlcv_cache.update_history("TEST/USDT", "15m", "swap")
        assert added == 10
        assert ohlcv_cache.unified_cache_path("TEST/USDT", "swap", "15m").exists()
        assert ohlcv_cache._marker_exists(
            ohlcv_cache.unified_cache_path("TEST/USDT", "swap", "15m")
        )
