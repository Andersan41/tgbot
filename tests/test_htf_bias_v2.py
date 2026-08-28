"""
tests/test_htf_bias_v2.py — Tests for HTF Bias V2 multi-timeframe alignment.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market_structure.htf_bias_v2 import (
    get_htf_bias_v2,
    get_tf_bias,
    BiasStrength,
)


def _make_bullish_df(close_start: float = 50000, step: float = 100, n: int = 60) -> pd.DataFrame:
    """Steady uptrend: price rises each period."""
    closes = [close_start + i * step for i in range(n)]
    return pd.DataFrame({
        "open": closes,
        "high": [c + 200 for c in closes],
        "low": [c - 200 for c in closes],
        "close": closes,
        "volume": [1000] * n,
    })


def _make_bearish_df(close_start: float = 50000, step: float = 100, n: int = 60) -> pd.DataFrame:
    """Steady downtrend."""
    closes = [close_start - i * step for i in range(n)]
    return pd.DataFrame({
        "open": closes,
        "high": [c + 200 for c in closes],
        "low": [c - 200 for c in closes],
        "close": closes,
        "volume": [1000] * n,
    })


def _make_neutral_df(price: float = 50000, n: int = 60) -> pd.DataFrame:
    """Flat price."""
    closes = [price] * n
    return pd.DataFrame({
        "open": closes,
        "high": [c + 100 for c in closes],
        "low": [c - 100 for c in closes],
        "close": closes,
        "volume": [1000] * n,
    })


class TestGetTfBias:
    def test_bullish_ema_alignment(self):
        df = _make_bullish_df()
        direction, conf = get_tf_bias(df, use_structure=False)
        assert direction == 'bullish'
        assert conf > 0

    def test_bearish_ema_alignment(self):
        df = _make_bearish_df()
        direction, conf = get_tf_bias(df, use_structure=False)
        assert direction == 'bearish'
        assert conf > 0

    def test_neutral_short_dataframe(self):
        df = _make_neutral_df(n=10)
        direction, conf = get_tf_bias(df, use_structure=False)
        assert direction == 'unknown'
        assert conf == 0.0

    def test_neutral_flat(self):
        df = _make_neutral_df()
        direction, conf = get_tf_bias(df, use_structure=False)
        assert direction == 'neutral'


class TestGetHtfBiasV2:
    def test_strong_bullish(self):
        """W1=D1=H4=bullish → strong bullish."""
        df_1w = _make_bullish_df(close_start=48000, step=80, n=60)
        df_1d = _make_bullish_df(close_start=49000, step=50, n=60)
        df_4h = _make_bullish_df(close_start=49500, step=30, n=60)

        result = get_htf_bias_v2(df_1w, df_1d, df_4h, None)
        assert result.direction == 'bullish'
        assert result.strength == BiasStrength.STRONG

    def test_strong_bearish(self):
        """W1=D1=H4=bearish → strong bearish."""
        df_1w = _make_bearish_df(close_start=52000, step=80, n=60)
        df_1d = _make_bearish_df(close_start=51000, step=50, n=60)
        df_4h = _make_bearish_df(close_start=50500, step=30, n=60)

        result = get_htf_bias_v2(df_1w, df_1d, df_4h, None)
        assert result.direction == 'bearish'
        assert result.strength == BiasStrength.STRONG

    def test_majority_bullish(self):
        """W1=neutral, D1=H4=bullish → moderate bullish."""
        df_1w = _make_neutral_df()
        df_1d = _make_bullish_df(close_start=49000, step=50, n=60)
        df_4h = _make_bullish_df(close_start=49500, step=30, n=60)

        result = get_htf_bias_v2(df_1w, df_1d, df_4h, None)
        assert result.direction == 'bullish'
        assert result.strength == BiasStrength.MODERATE

    def test_w1_conflict_override(self):
        """W1=bearish, D1=H4=bullish → bullish with override."""
        df_1w = _make_bearish_df(close_start=52000, step=80, n=60)
        df_1d = _make_bullish_df(close_start=49000, step=50, n=60)
        df_4h = _make_bullish_df(close_start=49500, step=30, n=60)

        result = get_htf_bias_v2(df_1w, df_1d, df_4h, None)
        assert result.direction == 'bullish'
        assert result.strength == BiasStrength.MODERATE
        assert result.override_reason is not None
        assert 'override' in result.override_reason

    def test_pullback_detected(self):
        """W1=D1=bullish, H4=bearish → pullback."""
        df_1w = _make_bullish_df(close_start=48000, step=80, n=60)
        df_1d = _make_bullish_df(close_start=49000, step=50, n=60)
        df_4h = _make_bearish_df(close_start=50200, step=20, n=60)

        result = get_htf_bias_v2(df_1w, df_1d, df_4h, None)
        assert result.direction == 'bullish'
        assert result.override_reason is not None
        assert 'pullback' in result.override_reason

    def test_all_neutral(self):
        """All neutral → neutral."""
        df_1w = _make_neutral_df(n=55)
        df_1d = _make_neutral_df(n=55)
        df_4h = _make_neutral_df(n=55)

        result = get_htf_bias_v2(df_1w, df_1d, df_4h, None)
        assert result.direction == 'neutral'
        assert result.strength == BiasStrength.NEUTRAL

    def test_h1_bias_recorded(self):
        """H1 bias is recorded even when not used for voting."""
        df_1w = _make_bullish_df(close_start=48000, step=80, n=60)
        df_1d = _make_bullish_df(close_start=49000, step=50, n=60)
        df_4h = _make_bullish_df(close_start=49500, step=30, n=60)
        df_1h = _make_bullish_df(close_start=49700, step=10, n=60)

        result = get_htf_bias_v2(df_1w, df_1d, df_4h, df_1h)
        assert result.h1_bias == 'bullish'

    def test_weekly_bias_recorded(self):
        df_1w = _make_bullish_df(close_start=48000, step=80, n=60)
        df_1d = _make_bullish_df(close_start=49000, step=50, n=60)
        df_4h = _make_bullish_df(close_start=49500, step=30, n=60)

        result = get_htf_bias_v2(df_1w, df_1d, df_4h, None)
        assert result.weekly_bias == 'bullish'
        assert result.daily_bias == 'bullish'
        assert result.h4_bias == 'bullish'

    def test_none_dataframes(self):
        """None W1/H1 should not crash."""
        df_1d = _make_bullish_df()
        df_4h = _make_bullish_df()

        result = get_htf_bias_v2(None, df_1d, df_4h, None)
        assert result.direction in ('bullish', 'neutral')
