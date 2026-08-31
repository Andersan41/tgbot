"""
liquidity/fvg.py — Fair Value Gap (Imbalance) Detection.

Bullish FVG: Low of candle 3 > High of candle 1 (gap between candles 1 and 3).
Bearish FVG: High of candle 3 < Low of candle 1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

import pandas as pd

from config.settings import config


@dataclass
class FairValueGap:
    type: Literal["bullish", "bearish"]
    top: float
    bottom: float
    timestamp: datetime
    filled: bool = False
    index: int = 0  # позиция свечи в DataFrame (для проверки закрытия)

    @property
    def size_pct(self) -> float:
        if self.bottom == 0:
            return 0.0
        return abs(self.top - self.bottom) / self.bottom * 100

    @property
    def is_active(self) -> bool:
        return not self.filled


def detect_fvg(
    df: pd.DataFrame,
    lookback: int = 100,
    min_size_pct: Optional[float] = None,
) -> list[FairValueGap]:
    """
    Detect Fair Value Gaps in OHLCV data.

    Args:
        df: DataFrame with OHLCV data.
        lookback: number of recent candles to analyze.
        min_size_pct: minimum gap size % to include (from config if None).

    Returns:
        List of FairValueGap objects.
    """
    if min_size_pct is None:
        min_size_pct = getattr(config, "liquidity_fvg_min_size_pct", 0.3)

    if len(df) < lookback:
        lookback = len(df)
    if lookback < 3:
        return []

    # Preserve original datetime index before reset
    data = df.tail(lookback).copy()
    _orig_index = data.index.tolist()
    data = data.reset_index(drop=True)

    offset = len(df) - len(data)
    highs = data["high"].to_numpy(dtype=float)
    lows = data["low"].to_numpy(dtype=float)

    fvgs: list[FairValueGap] = []

    for i in range(1, len(data) - 1):
        high1 = highs[i - 1]
        low1 = lows[i - 1]
        high3 = highs[i + 1]
        low3 = lows[i + 1]

        if low3 > high1:
            gap_size_pct = (low3 - high1) / high1 * 100
            if gap_size_pct >= min_size_pct:
                ts = _to_datetime(_orig_index[i])
                fvgs.append(FairValueGap(
                    type="bullish",
                    top=low3,
                    bottom=high1,
                    timestamp=ts,
                    index=offset + i + 1,  # absolute index of candle3
                ))

        if high3 < low1:
            gap_size_pct = (low1 - high3) / high3 * 100
            if gap_size_pct >= min_size_pct:
                ts = _to_datetime(_orig_index[i])
                fvgs.append(FairValueGap(
                    type="bearish",
                    top=low1,
                    bottom=high3,
                    timestamp=ts,
                    index=offset + i + 1,  # absolute index of candle3
                ))

    # Проверка: какие FVG уже закрыты ценой
    # Use local index (i+2) — highs/lows are local to the sliced data.
    for fvg, local_idx in zip(fvgs, range(1, len(data) - 1)):
        _fill_start = local_idx + 2
        if _fill_start < len(highs):
            fvg.filled = _is_fvg_filled_np(fvg, highs[_fill_start:], lows[_fill_start:])

    return fvgs


def _is_fvg_filled_np(fvg: FairValueGap, after_highs, after_lows) -> bool:
    """Numpy-backed fill check — same logic as _is_fvg_filled."""
    if fvg.type == "bullish":
        return bool((after_lows <= fvg.top).any())
    elif fvg.type == "bearish":
        return bool((after_highs >= fvg.bottom).any())
    return False


def _is_fvg_filled(fvg: FairValueGap, candles_after: pd.DataFrame) -> bool:
    """Проверяет, была ли зона FVG уже закрыта ценой."""
    for _, candle in candles_after.iterrows():
        if fvg.type == "bullish":
            if float(candle["low"]) <= fvg.top:
                return True
        elif fvg.type == "bearish":
            if float(candle["high"]) >= fvg.bottom:
                return True
    return False


def _to_datetime(idx) -> datetime:
    """Convert index value to datetime."""
    if hasattr(idx, "to_pydatetime"):
        ts = idx.to_pydatetime()
    else:
        ts = idx
    if isinstance(ts, datetime) and ts.tzinfo is None:
        ts = ts.replace(tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc)
    return ts
