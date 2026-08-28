"""
liquidity/external.py — External Liquidity detection.

Identifies "old highs" and "old lows" — swing points from earlier structure
that represent resting orders beyond the current trading range.

ICT concept: External Liquidity = "where the big money is".
Old highs/lows are where institutional orders sit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

import pandas as pd
from loguru import logger


@dataclass
class ExternalLiquidity:
    """A historical swing level that represents external liquidity."""
    type: Literal["old_high", "old_low"]
    level: float
    timestamp: datetime
    age_candles: int              # how many candles ago this formed
    volume_at_level: float        # volume when level was formed
    strength: float = 0.0         # [0, 1] importance score
    swept: bool = False

    @property
    def is_above_current(self) -> bool:
        return self.type == "old_high"

    @property
    def is_below_current(self) -> bool:
        return self.type == "old_low"


def detect_external_liquidity(
    df: pd.DataFrame,
    lookback: int = 200,
    min_age_candles: int = 20,
    swing_window: int = 5,
) -> list[ExternalLiquidity]:
    """Detect old highs and old lows that represent external liquidity.

    External liquidity = swing points from earlier structure that are
    beyond the current trading range. These are where institutional
    orders (resting stops) are likely concentrated.

    Args:
        df: OHLCV DataFrame
        lookback: how many candles to analyze
        min_age_candles: minimum age for a level to be "old"
        swing_window: window for swing point detection

    Returns:
        List of ExternalLiquidity objects
    """
    if len(df) < lookback:
        lookback = len(df)

    data = df.tail(lookback).reset_index(drop=True)
    if len(data) < swing_window * 3:
        return []

    highs = data["high"].to_numpy(dtype=float)
    lows = data["low"].to_numpy(dtype=float)
    vols = data["volume"].to_numpy(dtype=float)
    index = data.index

    # Find all swing points
    swing_highs = []
    swing_lows = []

    for i in range(swing_window, len(data) - swing_window):
        high = highs[i]
        low = lows[i]

        # Swing high: highest high in window
        if high == highs[i - swing_window: i + swing_window + 1].max():
            ts = index[i] if hasattr(index[i], 'hour') else datetime.now()
            swing_highs.append({
                "price": high,
                "index": i,
                "timestamp": ts,
                "volume": float(vols[i]),
            })

        # Swing low: lowest low in window
        if low == lows[i - swing_window: i + swing_window + 1].min():
            ts = index[i] if hasattr(index[i], 'hour') else datetime.now()
            swing_lows.append({
                "price": low,
                "index": i,
                "timestamp": ts,
                "volume": float(vols[i]),
            })

    # Current range (last N candles)
    recent = data.tail(min_age_candles)
    current_high = recent["high"].max()
    current_low = recent["low"].min()

    external = []

    # Old highs = swing highs above current range and old enough
    for sh in swing_highs:
        age = len(data) - 1 - sh["index"]
        if age >= min_age_candles and sh["price"] > current_high:
            strength = _calc_ext_strength(age, lookback, sh["volume"], data)
            external.append(ExternalLiquidity(
                type="old_high",
                level=sh["price"],
                timestamp=sh["timestamp"],
                age_candles=age,
                volume_at_level=sh["volume"],
                strength=strength,
            ))

    # Old lows = swing lows below current range and old enough
    for sl in swing_lows:
        age = len(data) - 1 - sl["index"]
        if age >= min_age_candles and sl["price"] < current_low:
            strength = _calc_ext_strength(age, lookback, sl["volume"], data)
            external.append(ExternalLiquidity(
                type="old_low",
                level=sl["price"],
                timestamp=sl["timestamp"],
                age_candles=age,
                volume_at_level=sl["volume"],
                strength=strength,
            ))

    external.sort(key=lambda x: x.strength, reverse=True)
    return external


def find_external_liquidity(df: pd.DataFrame, side: str, entry: float, sl_distance: float) -> Optional[float]:
    """
    Находит внешнюю ликвидность для TP.
    Для LONG: EQH (Equal Highs)
    Для SHORT: EQL (Equal Lows)

    Returns: уровень TP или None, если не найдено.
    """
    tolerance = 0.003  # 0.3%

    if len(df) < 110:
        return None

    lookback = min(100, len(df) - 10)
    highs = df['high'].iloc[-lookback:].values
    lows = df['low'].iloc[-lookback:].values

    if side == 'long':
        eqhs = []
        for i in range(len(highs) - 1):
            if abs(highs[i] - highs[i+1]) / max(highs[i], 0.0001) < tolerance:
                eqhs.append(highs[i])

        if eqhs:
            nearest_eqh = min([h for h in eqhs if h > entry], default=None)
            if nearest_eqh:
                rr = (nearest_eqh - entry) / max(sl_distance, 0.0001)
                if 1.5 <= rr <= 10.0:
                    return nearest_eqh

    elif side == 'short':
        eqls = []
        for i in range(len(lows) - 1):
            if abs(lows[i] - lows[i+1]) / max(lows[i], 0.0001) < tolerance:
                eqls.append(lows[i])

        if eqls:
            nearest_eql = max([l for l in eqls if l < entry], default=None)
            if nearest_eql:
                rr = (entry - nearest_eql) / max(sl_distance, 0.0001)
                if 1.5 <= rr <= 10.0:
                    return nearest_eql

    return None


def _calc_ext_strength(
    age: int,
    lookback: int,
    volume: float,
    data: pd.DataFrame,
) -> float:
    """Calculate strength of an external liquidity level.

    Older levels with higher volume are stronger (more resting orders).
    """
    # Age score: older = stronger (up to a point)
    age_score = min(1.0, age / (lookback * 0.5))

    # Volume score: higher volume at level = more orders
    avg_volume = data["volume"].mean()
    vol_score = min(1.0, volume / avg_volume) if avg_volume > 0 else 0.5

    return round(age_score * 0.4 + vol_score * 0.6, 3)
