"""
liquidity/order_blocks.py — Order Block Detection.

Bullish OB: last bearish candle before impulsive move up, validated by BOS.
Bearish OB: last bullish candle before strong selloff, validated by BOS.

Validation requirements:
1. BOS after OB candle (break of previous swing high/low)
2. ATR-normalized displacement (move_size / atr >= MIN_OB_DISPLACEMENT_ATR)
3. Volume confirmation (candle_volume / avg_volume > MIN_OB_VOLUME_RATIO)
4. Retest validation (price revisits zone with bounce/rejection)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

import pandas as pd

from config.settings import config


@dataclass
class OrderBlock:
    type: Literal["bullish", "bearish"]
    high: float
    low: float
    timestamp: datetime
    mitigated: bool = False
    mitigation_price: Optional[float] = None
    candle_index: int = 0
    displacement_atr: float = 0.0
    volume_ratio: float = 1.0
    has_bos: bool = False
    retested: bool = False
    retest_reaction: Optional[float] = None

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2

    @property
    def is_valid(self) -> bool:
        min_disp = getattr(config, "liquidity_ob_min_displacement_atr", 1.5)
        min_vol = getattr(config, "liquidity_ob_min_volume_ratio", 1.5)
        retest_req = getattr(config, "liquidity_ob_retest_required", False)
        if not self.has_bos:
            return False
        if self.displacement_atr < min_disp:
            return False
        if self.volume_ratio < min_vol:
            return False
        if retest_req and not self.retested:
            return False
        return True


def detect_order_blocks(
    df: pd.DataFrame,
    lookback: int = 100,
    displacement_pct: Optional[float] = None,
    max_age_candles: Optional[int] = None,
    min_displacement_atr: Optional[float] = None,
    min_volume_ratio: Optional[float] = None,
    require_bos: bool = True,
    retest_required: Optional[bool] = None,
) -> list[OrderBlock]:
    """
    Detect order blocks in OHLCV data with full validation.

    Args:
        df: DataFrame with OHLCV data.
        lookback: number of recent candles to analyze.
        displacement_pct: minimum % move after OB to confirm (from config if None).
        max_age_candles: max age of OB in candles (from config if None).
        min_displacement_atr: min displacement in ATR units (from config if None).
        min_volume_ratio: min volume ratio vs average (from config if None).
        require_bos: require BOS validation (default True).
        retest_required: require price retest of zone (from config if None).

    Returns:
        List of OrderBlock objects.
    """
    if displacement_pct is None:
        displacement_pct = getattr(config, "liquidity_ob_min_displacement_pct", 2.0)
    if max_age_candles is None:
        max_age_candles = getattr(config, "liquidity_ob_max_age_candles", 50)
    if min_displacement_atr is None:
        min_displacement_atr = getattr(config, "liquidity_ob_min_displacement_atr", 1.5)
    if min_volume_ratio is None:
        min_volume_ratio = getattr(config, "liquidity_ob_min_volume_ratio", 1.5)
    if retest_required is None:
        retest_required = getattr(config, "liquidity_ob_retest_required", False)

    if len(df) < lookback:
        lookback = len(df)
    if lookback < 5:
        return []

    # Preserve original datetime index before reset
    data = df.tail(lookback).copy()
    _orig_index = data.index.tolist()
    data = data.reset_index(drop=True)

    offset = len(df) - len(data)
    _open = data["open"].to_numpy(dtype=float)
    _high = data["high"].to_numpy(dtype=float)
    _low = data["low"].to_numpy(dtype=float)
    _close = data["close"].to_numpy(dtype=float)
    _vol = data["volume"].to_numpy(dtype=float)
    n = len(data)

    atr = _calc_atr(data)
    avg_vol = float(_vol.mean())
    swing_highs = _find_swing_highs_np(_high)
    swing_lows = _find_swing_lows_np(_low)

    blocks: list[OrderBlock] = []

    for i in range(1, n - 2):
        body = abs(_close[i] - _open[i])
        range_val = _high[i] - _low[i]
        if range_val == 0:
            continue

        is_bearish_candle = _close[i] < _open[i]
        is_bullish_candle = _close[i] > _open[i]

        if is_bearish_candle:
            next_move_pct = (_close[i + 1] - _low[i]) / _low[i] * 100
            if next_move_pct >= displacement_pct:
                move_size = _close[i + 1] - _low[i]
                disp_atr = move_size / atr if atr > 0 else 0.0
                vol_ratio = _vol[i + 1] / avg_vol if avg_vol > 0 else 1.0
                has_bos = _check_bos_bullish_np(_high, i + 1, swing_highs)

                if require_bos and not has_bos:
                    continue

                ts = _to_datetime(_orig_index[i])
                retested, reaction = _check_retest_bullish_np(_low, _close, i, _high[i], _low[i])

                blocks.append(OrderBlock(
                    type="bullish",
                    high=_high[i],
                    low=_low[i],
                    timestamp=ts,
                    candle_index=offset + i,
                    displacement_atr=round(disp_atr, 3),
                    volume_ratio=round(vol_ratio, 3),
                    has_bos=has_bos,
                    retested=retested,
                    retest_reaction=reaction,
                ))

        if is_bullish_candle:
            next_move_pct = (_high[i] - _close[i + 1]) / _high[i] * 100
            if next_move_pct >= displacement_pct:
                move_size = _high[i] - _close[i + 1]
                disp_atr = move_size / atr if atr > 0 else 0.0
                vol_ratio = _vol[i + 1] / avg_vol if avg_vol > 0 else 1.0
                has_bos = _check_bos_bearish_np(_low, i + 1, swing_lows)

                if require_bos and not has_bos:
                    continue

                ts = _to_datetime(_orig_index[i])
                retested, reaction = _check_retest_bearish_np(_high, _close, i, _high[i], _low[i])

                blocks.append(OrderBlock(
                    type="bearish",
                    high=_high[i],
                    low=_low[i],
                    timestamp=ts,
                    candle_index=offset + i,
                    displacement_atr=round(disp_atr, 3),
                    volume_ratio=round(vol_ratio, 3),
                    has_bos=has_bos,
                    retested=retested,
                    retest_reaction=reaction,
                ))

    blocks = _filter_by_age(blocks, n, max_age_candles)
    return blocks


def _calc_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Calculate average ATR over the DataFrame."""
    if len(df) < period + 1:
        return 0.0
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return float(tr.iloc[-period:].mean())


def _find_swing_highs(df: pd.DataFrame, window: int = 5) -> list[dict]:
    """Find swing highs (local maxima)."""
    highs = []
    for i in range(window, len(df) - window):
        high_window = df["high"].iloc[i - window: i + window + 1]
        if df["high"].iloc[i] == high_window.max():
            highs.append({"index": i, "price": float(df["high"].iloc[i])})
    return highs


def _find_swing_lows(df: pd.DataFrame, window: int = 5) -> list[dict]:
    """Find swing lows (local minima)."""
    lows = []
    for i in range(window, len(df) - window):
        low_window = df["low"].iloc[i - window: i + window + 1]
        if df["low"].iloc[i] == low_window.min():
            lows.append({"index": i, "price": float(df["low"].iloc[i])})
    return lows


def _check_bos_bullish(df: pd.DataFrame, start_idx: int, swing_highs: list[dict], look_ahead: int = 20) -> bool:
    """Check if price breaks above a previous swing high after OB formation."""
    relevant_highs = [s for s in swing_highs if s["index"] < start_idx]
    if not relevant_highs:
        return False
    prev_swing_high = max(relevant_highs, key=lambda s: s["index"])["price"]
    end = min(start_idx + look_ahead, len(df))
    for j in range(start_idx, end):
        if float(df["high"].iloc[j]) > prev_swing_high:
            return True
    return False


def _check_bos_bearish(df: pd.DataFrame, start_idx: int, swing_lows: list[dict], look_ahead: int = 20) -> bool:
    """Check if price breaks below a previous swing low after OB formation."""
    relevant_lows = [s for s in swing_lows if s["index"] < start_idx]
    if not relevant_lows:
        return False
    prev_swing_low = min(relevant_lows, key=lambda s: s["index"])["price"]
    end = min(start_idx + look_ahead, len(df))
    for j in range(start_idx, end):
        if float(df["low"].iloc[j]) < prev_swing_low:
            return True
    return False


def _check_retest_bullish(df: pd.DataFrame, ob_idx: int, ob_high: float, ob_low: float, max_lookahead: int = 30) -> tuple[bool, Optional[float]]:
    """Check if price retests bullish OB zone and reacts (bounces)."""
    end = min(ob_idx + max_lookahead, len(df))
    for j in range(ob_idx + 1, end):
        low = float(df["low"].iloc[j])
        close = float(df["close"].iloc[j])
        if low <= ob_high and low >= ob_low:
            reaction = close - low
            return True, reaction
    return False, None


def _check_retest_bearish(df: pd.DataFrame, ob_idx: int, ob_high: float, ob_low: float, max_lookahead: int = 30) -> tuple[bool, Optional[float]]:
    """Check if price retests bearish OB zone and reacts (rejects)."""
    end = min(ob_idx + max_lookahead, len(df))
    for j in range(ob_idx + 1, end):
        high = float(df["high"].iloc[j])
        close = float(df["close"].iloc[j])
        if high >= ob_low and high <= ob_high:
            reaction = high - close
            return True, reaction
    return False, None


def _find_swing_highs_np(arr, window: int = 5) -> list[dict]:
    """Numpy-backed swing highs (local maxima) — same logic as _find_swing_highs."""
    highs = []
    for i in range(window, len(arr) - window):
        if arr[i] == arr[i - window: i + window + 1].max():
            highs.append({"index": i, "price": float(arr[i])})
    return highs


def _find_swing_lows_np(arr, window: int = 5) -> list[dict]:
    """Numpy-backed swing lows (local minima) — same logic as _find_swing_lows."""
    lows = []
    for i in range(window, len(arr) - window):
        if arr[i] == arr[i - window: i + window + 1].min():
            lows.append({"index": i, "price": float(arr[i])})
    return lows


def _check_bos_bullish_np(high, start_idx: int, swing_highs: list[dict], look_ahead: int = 20) -> bool:
    """Numpy-backed BOS check — same logic as _check_bos_bullish."""
    relevant_highs = [s for s in swing_highs if s["index"] < start_idx]
    if not relevant_highs:
        return False
    prev_swing_high = max(relevant_highs, key=lambda s: s["index"])["price"]
    end = min(start_idx + look_ahead, len(high))
    for j in range(start_idx, end):
        if float(high[j]) > prev_swing_high:
            return True
    return False


def _check_bos_bearish_np(low, start_idx: int, swing_lows: list[dict], look_ahead: int = 20) -> bool:
    """Numpy-backed BOS check — same logic as _check_bos_bearish."""
    relevant_lows = [s for s in swing_lows if s["index"] < start_idx]
    if not relevant_lows:
        return False
    prev_swing_low = min(relevant_lows, key=lambda s: s["index"])["price"]
    end = min(start_idx + look_ahead, len(low))
    for j in range(start_idx, end):
        if float(low[j]) < prev_swing_low:
            return True
    return False


def _check_retest_bullish_np(low, close, ob_idx: int, ob_high: float, ob_low: float, max_lookahead: int = 30) -> tuple[bool, Optional[float]]:
    """Numpy-backed bullish retest check — same logic as _check_retest_bullish."""
    end = min(ob_idx + max_lookahead, len(low))
    for j in range(ob_idx + 1, end):
        l = float(low[j])
        c = float(close[j])
        if l <= ob_high and l >= ob_low:
            return True, c - l
    return False, None


def _check_retest_bearish_np(high, close, ob_idx: int, ob_high: float, ob_low: float, max_lookahead: int = 30) -> tuple[bool, Optional[float]]:
    """Numpy-backed bearish retest check — same logic as _check_retest_bearish."""
    end = min(ob_idx + max_lookahead, len(high))
    for j in range(ob_idx + 1, end):
        h = float(high[j])
        c = float(close[j])
        if h >= ob_low and h <= ob_high:
            return True, h - c
    return False, None


def _filter_by_age(blocks: list[OrderBlock], total_candles: int, max_age: int) -> list[OrderBlock]:
    """Remove order blocks older than max_age candles."""
    if max_age is None:
        return blocks
    current_idx = total_candles - 1
    return [
        b for b in blocks
        if (current_idx - b.candle_index) <= max_age
    ]


def _to_datetime(idx) -> datetime:
    """Convert index value to datetime."""
    if hasattr(idx, "to_pydatetime"):
        ts = idx.to_pydatetime()
    else:
        ts = idx
    if isinstance(ts, datetime) and ts.tzinfo is None:
        ts = ts.replace(tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc)
    return ts
