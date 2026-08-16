"""
test_resampler.py — Tests for backtest/resampler.py (15m → higher TF resampling).

Рекомендуется работать на синтетических данных, не на live-бирже.
"""
from __future__ import annotations

import os
import sys
from datetime import timezone

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.resampler import (
    _TF_MAP,
    is_monday_start,
    resample_ohlcv,
    resample_to_all_tfs,
)


def _make_15m(start: str = "2025-01-01 00:00", periods: int = 96) -> pd.DataFrame:
    """Synthetic 15m OHLCV (24 hours × 4 candles)."""
    idx = pd.date_range(start, periods=periods, freq="15min", tz=timezone.utc)
    np.random.seed(42)
    close = np.random.randn(periods).cumsum() + 100.0
    df = pd.DataFrame(
        {
            "open": close + np.random.randn(periods) * 0.1,
            "high": close + np.abs(np.random.randn(periods)) * 0.5,
            "low": close - np.abs(np.random.randn(periods)) * 0.5,
            "close": close,
            "volume": np.random.rand(periods) * 1000 + 500,
        },
        index=idx,
    )
    df.index.name = "timestamp"
    return df


class TestResampleOhlcv:
    def test_unsupported_timeframe_raises(self):
        df = _make_15m(periods=16)
        with pytest.raises(ValueError, match="Unsupported target timeframe"):
            resample_ohlcv(df, "3h")

    def test_empty_input_returns_unchanged(self):
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        assert resample_ohlcv(df, "1h") is df

    def test_15m_to_1h_aggregation(self):
        """open=first, high=max, low=min, close=last, volume=sum."""
        df = _make_15m(periods=8)  # 2 hours of 15m candles
        r = resample_ohlcv(df, "1h")
        assert len(r) == 2

        # First hour bar: first 4 x 15m candles
        first_hour = df.iloc[:4]
        assert r.iloc[0]["open"] == first_hour.iloc[0]["open"]
        assert r.iloc[0]["high"] == first_hour["high"].max()
        assert r.iloc[0]["low"] == first_hour["low"].min()
        assert r.iloc[0]["close"] == first_hour.iloc[-1]["close"]
        assert r.iloc[0]["volume"] == pytest.approx(first_hour["volume"].sum())

        # Second hour bar
        second_hour = df.iloc[4:]
        assert r.iloc[1]["open"] == second_hour.iloc[0]["open"]
        assert r.iloc[1]["close"] == second_hour.iloc[-1]["close"]
        assert r.iloc[1]["volume"] == pytest.approx(second_hour["volume"].sum())

    def test_index_closed_left_label_left(self):
        """Bar labelled 00:00 contains data [00:00, 01:00)."""
        df = _make_15m(start="2025-01-01 00:00", periods=8)
        r = resample_ohlcv(df, "1h")
        assert r.index[0] == pd.Timestamp("2025-01-01 00:00", tz=timezone.utc)
        assert r.index[1] == pd.Timestamp("2025-01-01 01:00", tz=timezone.utc)

    def test_resample_2h_4h_counts(self):
        df = _make_15m(periods=32)  # 8 hours
        assert len(resample_ohlcv(df, "2h")) == 4
        assert len(resample_ohlcv(df, "4h")) == 2

    def test_resample_1d_counts(self):
        df = _make_15m(periods=4 * 24)  # exactly 24h
        r = resample_ohlcv(df, "1d")
        assert len(r) == 1
        assert r.index[0] == pd.Timestamp("2025-01-01 00:00", tz=timezone.utc)

    def test_dropna_removes_incomplete_bars(self):
        """Bars without any underlying 15m data (NaN) are dropped."""
        idx = pd.date_range("2025-01-01 00:00", periods=4, freq="15min", tz=timezone.utc)
        times = list(idx) + list(
            pd.date_range("2025-01-01 03:00", periods=4, freq="15min", tz=timezone.utc)
        )
        df = pd.DataFrame(
            {
                "open": [1.0] * len(times),
                "high": [2.0] * len(times),
                "low": [0.5] * len(times),
                "close": [1.5] * len(times),
                "volume": [10.0] * len(times),
            },
            index=times,
        )
        r = resample_ohlcv(df, "1h")
        # The 01:00 and 02:00 bars contain no data → removed by dropna()
        assert len(r) == 2
        assert not r.isna().any().any()


class TestWeeklyMonday:
    def test_week_starts_on_monday(self):
        """1w resample must start with a Monday (W-MON)."""
        # 3 weeks of 15m data starting on a Wednesday
        df = _make_15m(start="2025-01-01 00:00", periods=4 * 7 * 3)
        r = resample_ohlcv(df, "1w")
        assert is_monday_start(r)
        assert r.index[0].weekday() == 0  # Monday
        # First Monday at or before 2025-01-01 is 2024-12-30
        assert r.index[0] == pd.Timestamp("2024-12-30", tz=timezone.utc)

    def test_all_weekly_bars_are_monday(self):
        df = _make_15m(start="2025-01-01 00:00", periods=4 * 7 * 4)
        r = resample_ohlcv(df, "1w")
        for ts in r.index:
            assert ts.weekday() == 0, f"{ts} is not a Monday"


class TestHelpers:
    def test_resample_to_all_tfs(self):
        df = _make_15m(periods=4 * 24 * 3)
        out = resample_to_all_tfs(df)
        assert set(out.keys()) == set(_TF_MAP.keys())
        for tf, r in out.items():
            assert isinstance(r.index, pd.DatetimeIndex)
            assert set(r.columns) >= {"open", "high", "low", "close", "volume"}

    def test_tf_map_covers_valid_targets(self):
        assert _TF_MAP.keys() == {"1h", "2h", "4h", "1d", "1w"}