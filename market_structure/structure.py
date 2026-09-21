"""
market_structure/structure.py — Market Structure Engine V2.

Detects BOS (Break of Structure), CHoCH (Change of Character),
MSS (Market Structure Shift = strong CHoCH),
swing points (HH/HL/LH/LL), and multi-timeframe alignment.

MSS is a subset of CHoCH — classified as "mss" when:
1. Sweep within causal window (5 bars, exponential decay)
2. Displacement >= 1 ATR
3. Reclaim <= 2 bars
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

import pandas as pd

from config.settings import config


@dataclass
class SwingPoint:
    price: float
    timestamp: datetime
    type: Literal["high", "low"]
    candle_index: int = 0


@dataclass
class BOS:
    """Break of Structure."""
    type: Literal["bullish", "bearish"]
    level: float
    timestamp: datetime
    candle_index: int


@dataclass
class CHoCH:
    """Change of Character — first break of opposite structure.

    May be classified as MSS (Market Structure Shift) when confirmed
    by sweep + displacement + fast reclaim.
    """
    type: Literal["bullish", "bearish"]
    level: float
    timestamp: datetime
    candle_index: int
    # ── MSS classification ──
    strength: Literal["weak", "normal", "mss"] = "normal"
    has_sweep_reference: bool = False
    displacement_score: float = 0.0  # displacement / ATR
    reclaim_bars: int = 0
    causality_score: float = 0.0  # exponential decay from sweep (0-1)
    mss_score: float = 0.0  # 0-100 quality score (equal weights v1)


@dataclass
class StructureState:
    trend: Literal["bullish", "bearish", "ranging"]
    last_bos: Optional[BOS] = None
    last_choch: Optional[CHoCH] = None
    last_mss: Optional[CHoCH] = None  # last CHoCH with strength="mss"
    swing_points: list[SwingPoint] = field(default_factory=list)
    structure_breaks: int = 0
    recent_highs: list[float] = field(default_factory=list)
    recent_lows: list[float] = field(default_factory=list)


def calc_causality(bars_since_sweep: int, half_life: float = 3.0) -> float:
    """Exponential decay: causal link between sweep and CHoCH.

    bars=0 → 1.0, bars=3 → 0.5, bars=5 → 0.33, bars=10 → 0.1
    """
    if bars_since_sweep < 0:
        return 0.0
    return math.exp(-0.693 * bars_since_sweep / half_life)


def calc_mss_score(
    sweep_strength: float,
    displacement_atr: float,
    reclaim_bars: int,
    volume_ratio: float,
    htf_aligned: bool,
) -> float:
    """MSS quality score 0-100. Equal weights v1 (20% each).

    ML replaces these weights after 200-300 trades.
    """
    # Sweep quality: strength 0-1 → 0-20
    sweep_score = min(sweep_strength, 1.0) * 20.0

    # Displacement strength: ATR ratio → 0-20 (cap at 3 ATR)
    disp_score = min(displacement_atr / 3.0, 1.0) * 20.0

    # Reclaim speed: <=1 bars = 20, <=2 = 15, <=3 = 10, <=5 = 5, >5 = 0
    if reclaim_bars <= 1:
        reclaim_score = 20.0
    elif reclaim_bars <= 2:
        reclaim_score = 15.0
    elif reclaim_bars <= 3:
        reclaim_score = 10.0
    elif reclaim_bars <= 5:
        reclaim_score = 5.0
    else:
        reclaim_score = 0.0

    # Volume expansion: ratio < 1.0 → 0, otherwise → 0-20
    vol_score = max(0.0, min((volume_ratio - 1.0) / 2.0, 1.0)) * 20.0

    # HTF alignment: boolean → 0 or 20
    htf_score = 20.0 if htf_aligned else 0.0

    return round(sweep_score + disp_score + reclaim_score + vol_score + htf_score, 1)


def classify_choch(
    choch: CHoCH,
    sweeps: list,
    displacement_atr: float = 0.0,
    reclaim_bars: int = 0,
    volume_ratio: float = 1.0,
    htf_aligned: bool = False,
    max_causal_bars: int = 5,
    df: Optional[pd.DataFrame] = None,
    atr_value: float = 0.0,
) -> CHoCH:
    """Classify CHoCH strength as weak/normal/mss.

    MSS criteria (all must pass):
    1. Sweep within causal window (max_causal_bars, default 5)
    2. Displacement >= 1 ATR (measured as max body between sweep and CHoCH)
    3. Reclaim <= 2 bars
    """
    choch.displacement_score = displacement_atr
    choch.reclaim_bars = reclaim_bars

    # Find matching sweep (OPPOSITE direction, within causal window)
    matching_sweep = None
    bars_since = 999

    for s in sweeps:
        if not s.is_valid:
            continue
        # Sweep direction must OPPOSE CHoCH direction
        # Bullish CHoCH = structure shifts up AFTER bearish sweep (sell-side grab)
        # Bearish CHoCH = structure shifts down AFTER bullish sweep (buy-side grab)
        sweep_dir = "buy" if s.type == "bullish" else "sell"
        choch_dir = "buy" if choch.type == "bullish" else "sell"
        if sweep_dir == choch_dir:
            continue

        # Check causal window
        if choch.candle_index >= 0 and s.candle_index >= 0:
            delta = choch.candle_index - s.candle_index
        else:
            delta = 0  # unknown index, assume close
        if 0 <= delta <= max_causal_bars:
            if matching_sweep is None or delta < bars_since:
                matching_sweep = s
                bars_since = delta

    if matching_sweep is not None:
        choch.has_sweep_reference = True
        choch.causality_score = calc_causality(bars_since)

        # Measure displacement as max body/ATR between sweep and CHoCH
        # (not just the CHoCH candle — ICT: displacement leg causes the structure break)
        if df is not None and atr_value > 0 and matching_sweep.candle_index >= 0 and choch.candle_index >= 0:
            start = max(0, matching_sweep.candle_index)
            end = min(choch.candle_index + 1, len(df))
            max_disp = 0.0
            for idx in range(start, end):
                candle = df.iloc[idx]
                body = abs(float(candle["close"]) - float(candle["open"]))
                disp = body / atr_value
                if disp > max_disp:
                    max_disp = disp
            if max_disp > displacement_atr:
                displacement_atr = max_disp
                choch.displacement_score = displacement_atr
    else:
        choch.has_sweep_reference = False
        choch.causality_score = 0.0

    # Classify
    is_mss = (
        choch.has_sweep_reference
        and displacement_atr >= 1.0
        and reclaim_bars <= 2
    )

    if is_mss:
        choch.strength = "mss"
        choch.mss_score = calc_mss_score(
            sweep_strength=matching_sweep.strength if matching_sweep else 0.0,
            displacement_atr=displacement_atr,
            reclaim_bars=reclaim_bars,
            volume_ratio=volume_ratio,
            htf_aligned=htf_aligned,
        )
    elif choch.has_sweep_reference and displacement_atr >= 0.5:
        choch.strength = "normal"
        choch.mss_score = calc_mss_score(
            sweep_strength=matching_sweep.strength if matching_sweep else 0.0,
            displacement_atr=displacement_atr,
            reclaim_bars=reclaim_bars,
            volume_ratio=volume_ratio,
            htf_aligned=htf_aligned,
        ) * 0.6  # partial credit
    else:
        choch.strength = "weak"
        choch.mss_score = 0.0

    return choch


def _find_swing_points(
    df: pd.DataFrame,
    lookback: int = 50,
    swing_window: int = 5,
) -> list[SwingPoint]:
    """Find swing highs and lows in OHLCV data."""
    swings: list[SwingPoint] = []
    data = df.tail(lookback)
    offset = len(df) - len(data)  # offset from original df index

    highs = data["high"].to_numpy(dtype=float)
    lows = data["low"].to_numpy(dtype=float)
    index = data.index

    for i in range(swing_window, len(data) - swing_window):
        ts = index[i]
        if hasattr(ts, "to_pydatetime"):
            ts = ts.to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc)

        candle_idx = offset + i  # absolute index in original df

        if highs[i] == highs[i - swing_window: i + swing_window + 1].max():
            swings.append(
                SwingPoint(
                    price=float(highs[i]),
                    timestamp=ts,
                    type="high",
                    candle_index=candle_idx,
                )
            )
        if lows[i] == lows[i - swing_window: i + swing_window + 1].min():
            swings.append(
                SwingPoint(
                    price=float(lows[i]),
                    timestamp=ts,
                    type="low",
                    candle_index=candle_idx,
                )
            )

    return swings


def _detect_bos_choch(
    swings: list[SwingPoint],
) -> tuple[Optional[BOS], Optional[CHoCH], int]:
    """
    Detect BOS and CHoCH from swing points by iterating through history.

    BOS = structure continuation (trend confirmed)
    CHoCH = structure reversal (trend broken)

    Algorithm:
      1. Pair swing highs and lows chronologically.
      2. Determine trend from first pair (HH+HL=bullish, LH+LL=bearish).
      3. For each subsequent pair, check if latest swing breaks or confirms trend.
    """
    if len(swings) < 4:
        return None, None, 0

    highs = sorted(
        [s for s in swings if s.type == "high"], key=lambda s: s.candle_index
    )
    lows = sorted(
        [s for s in swings if s.type == "low"], key=lambda s: s.candle_index
    )

    if len(highs) < 2 or len(lows) < 2:
        return None, None, 0

    last_bos: Optional[BOS] = None
    last_choch: Optional[CHoCH] = None
    structure_breaks = 0

    # Build chronological list of (high, low) pairs
    # Interleave by candle_index and pair consecutive H-L swings
    all_swings = sorted(highs + lows, key=lambda s: s.candle_index)

    # Extract trend sequence from highs and lows separately
    # We track trend by looking at consecutive swing highs and swing lows
    trend = None  # None=unknown, True=bullish, False=bearish

    for i in range(1, len(highs)):
        prev_h = highs[i - 1]
        curr_h = highs[i]

        # Find the closest low before this high to pair with
        matching_lows = [l for l in lows if l.candle_index <= curr_h.candle_index]
        if len(matching_lows) < 2:
            continue

        prev_l = matching_lows[-2] if len(matching_lows) >= 2 else None
        curr_l = matching_lows[-1]

        if prev_l is None:
            continue

        highs_rising = curr_h.price > prev_h.price
        lows_rising = curr_l.price > prev_l.price

        # Determine pair trend
        if highs_rising and lows_rising:
            pair_trend = True
        elif not highs_rising and not lows_rising:
            pair_trend = False
        else:
            pair_trend = None  # mixed

        # --- Check for structure breaks ---
        if curr_h.price > prev_h.price:
            # Higher high
            if trend is False or (trend is None and pair_trend is not True):
                # CHoCH bullish: higher high after bearish/mixed trend
                last_choch = CHoCH(
                    type="bullish",
                    level=curr_h.price,
                    timestamp=curr_h.timestamp,
                    candle_index=curr_h.candle_index,
                )
                structure_breaks += 1
            else:
                # BOS bullish: higher high confirming bullish trend
                last_bos = BOS(
                    type="bullish",
                    level=curr_h.price,
                    timestamp=curr_h.timestamp,
                    candle_index=curr_h.candle_index,
                )
                structure_breaks += 1

        if curr_l.price < prev_l.price:
            # Lower low
            if trend is True or (trend is None and pair_trend is not False):
                # CHoCH bearish: lower low after bullish/mixed trend
                last_choch = CHoCH(
                    type="bearish",
                    level=curr_l.price,
                    timestamp=curr_l.timestamp,
                    candle_index=curr_l.candle_index,
                )
                structure_breaks += 1
            else:
                # BOS bearish: lower low confirming bearish trend
                last_bos = BOS(
                    type="bearish",
                    level=curr_l.price,
                    timestamp=curr_l.timestamp,
                    candle_index=curr_l.candle_index,
                )
                structure_breaks += 1

        # Update trend
        if pair_trend is not None:
            trend = pair_trend

    return last_bos, last_choch, structure_breaks


def _classify_trend(
    swings: list[SwingPoint],
    last_bos: Optional[BOS],
    last_choch: Optional[CHoCH],
) -> Literal["bullish", "bearish", "ranging"]:
    """Classify overall trend based on structure.

    Use the most recent structure event (by candle_index) to determine trend.
    BOS after CHoCH overrides the CHoCH direction.
    """
    # Pick whichever structure event is more recent
    if last_bos is not None and last_choch is not None:
        if last_bos.candle_index >= last_choch.candle_index:
            return "bullish" if last_bos.type == "bullish" else "bearish"
        else:
            return "bullish" if last_choch.type == "bullish" else "bearish"
    if last_choch is not None:
        return "bullish" if last_choch.type == "bullish" else "bearish"
    if last_bos is not None:
        return "bullish" if last_bos.type == "bullish" else "bearish"

    highs = [s.price for s in swings if s.type == "high"]
    lows = [s.price for s in swings if s.type == "low"]

    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
            return "bullish"
        if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
            return "bearish"

    return "ranging"


def analyze_structure(
    df: pd.DataFrame,
    lookback: int = 50,
    swing_window: int = 5,
    sweeps: Optional[list] = None,
    displacement_atr: float = 0.0,
    reclaim_bars: int = 0,
    volume_ratio: float = 1.0,
    htf_aligned: bool = False,
    atr_value: float = 0.0,
) -> StructureState:
    """
    Analyze market structure from OHLCV data.

    Args:
        df: DataFrame with OHLCV data (index should be datetime-like).
        lookback: number of recent candles to analyze.
        swing_window: window size for swing point detection.
        sweeps: optional list of SweepEvent for MSS classification.
        displacement_atr: displacement / ATR ratio for MSS classification.
        reclaim_bars: bars to reclaim for MSS classification.
        volume_ratio: volume / average volume for MSS scoring.
        htf_aligned: HTF alignment for MSS scoring.
        atr_value: actual ATR value for max displacement calculation.

    Returns:
        StructureState with trend, BOS, CHoCH (classified), MSS, swing points.
    """
    swings = _find_swing_points(df, lookback=lookback, swing_window=swing_window)
    last_bos, last_choch, breaks = _detect_bos_choch(swings)
    trend = _classify_trend(swings, last_bos, last_choch)

    # Classify CHoCH as MSS if criteria met
    last_mss = None
    if last_choch is not None:
        classify_choch(
            last_choch,
            sweeps=sweeps or [],
            displacement_atr=displacement_atr,
            reclaim_bars=reclaim_bars,
            volume_ratio=volume_ratio,
            htf_aligned=htf_aligned,
            df=df,
            atr_value=atr_value,
        )
        if last_choch.strength == "mss":
            last_mss = last_choch

    highs = [s.price for s in swings if s.type == "high"]
    lows = [s.price for s in swings if s.type == "low"]

    return StructureState(
        trend=trend,
        last_bos=last_bos,
        last_choch=last_choch,
        last_mss=last_mss,
        swing_points=swings,
        structure_breaks=breaks,
        recent_highs=highs[-15:] if len(highs) >= 15 else highs,
        recent_lows=lows[-15:] if len(lows) >= 15 else lows,
    )


def _parse_timeframe_to_seconds(tf: str) -> int:
    """Convert timeframe string to seconds."""
    tf = tf.lower().strip()
    if tf.endswith("m"):
        return int(tf[:-1]) * 60
    if tf.endswith("h"):
        return int(tf[:-1]) * 3600
    if tf.endswith("d"):
        return int(tf[:-1]) * 86400
    if tf.endswith("w"):
        return int(tf[:-1]) * 604800
    return 3600


@dataclass
class MTFAlignmentResult:
    alignment_state: Literal["bullish_aligned", "bearish_aligned", "mixed", "ranging"]
    aligned: bool
    states: dict[str, StructureState]


async def check_mtf_alignment(
    symbol: str,
    direction: Literal["bullish", "bearish"],
    primary_tf: str,
    exchange_client,
    required_alignment: int | None = None,
    timeframes: list[str] | None = None,
) -> MTFAlignmentResult:
    """
    Check multi-timeframe structure alignment.

    Signal direction must align with at least `required_alignment` higher timeframes.
    Range is NOT counted as aligned — it produces "mixed" or "ranging" state.

    Args:
        symbol: Trading pair symbol.
        direction: Signal direction ("bullish" for LONG, "bearish" for SHORT).
        primary_tf: Primary signal timeframe.
        exchange_client: Exchange client for fetching OHLCV data.
        required_alignment: Minimum number of aligned HTFs (from config if None).
        timeframes: List of HTFs to check (from config if None).

    Returns:
        MTFAlignmentResult with alignment_state, aligned flag, and states dict.
    """
    if required_alignment is None:
        required_alignment = getattr(config, "mtf_required_alignment", 2)
    if timeframes is None:
        raw = getattr(config, "mtf_timeframes", "1d,4h,1h")
        timeframes = [tf.strip() for tf in raw.split(",")]

    primary_seconds = _parse_timeframe_to_seconds(primary_tf)
    higher_tfs = [
        tf for tf in timeframes
        if _parse_timeframe_to_seconds(tf) > primary_seconds
    ]

    # Clamp required to available HTFs: primary=4h → ["1d"] → required=min(1,2)=1
    required_alignment = min(len(higher_tfs), required_alignment)

    if not higher_tfs:
        return MTFAlignmentResult(alignment_state="bullish_aligned", aligned=True, states={})

    states: dict[str, StructureState] = {}
    aligned_count = 0
    bullish_count = 0
    bearish_count = 0
    ranging_count = 0

    for tf in higher_tfs:
        try:
            df = await exchange_client.fetch_ohlcv(symbol, tf, limit=100)
            if df is None or len(df) < 20:
                continue
            state = analyze_structure(df, lookback=50)
            states[tf] = state

            if state.trend == "bullish":
                bullish_count += 1
                if direction == "bullish":
                    aligned_count += 1
            elif state.trend == "bearish":
                bearish_count += 1
                if direction == "bearish":
                    aligned_count += 1
            else:
                if state.last_bos and state.last_bos.type == direction:
                    aligned_count += 1
                ranging_count += 1
        except Exception:
            continue

    total = bullish_count + bearish_count + ranging_count
    if total == 0:
        alignment_state = "bullish_aligned"
    elif ranging_count == total:
        alignment_state = "ranging"
    elif bullish_count == total:
        alignment_state = "bullish_aligned"
    elif bearish_count == total:
        alignment_state = "bearish_aligned"
    else:
        alignment_state = "mixed"

    return MTFAlignmentResult(
        alignment_state=alignment_state,
        aligned=aligned_count >= required_alignment,
        states=states,
    )


# ─────────────────────────────────────────────────────────────────────────
# HTF Alignment Score (soft multiplier)
# ─────────────────────────────────────────────────────────────────────────


def _detect_trend_from_df(df: pd.DataFrame) -> str:
    """Quick trend detection from a small OHLCV window using EMAs."""
    if df is None or len(df) < 20:
        return "ranging"
    ema_fast = df["close"].ewm(span=8, adjust=False).mean()
    ema_slow = df["close"].ewm(span=21, adjust=False).mean()
    last_fast = float(ema_fast.iloc[-1])
    last_slow = float(ema_slow.iloc[-1])
    if last_fast > last_slow * 1.005:
        return "bullish"
    elif last_fast < last_slow * 0.995:
        return "bearish"
    return "ranging"


async def calc_htf_alignment_score(
    symbol: str,
    direction: str,
    exchange_client,
    timeframes: list[str] | None = None,
) -> float:
    """Score based on W1/D1/H4 trend agreement with signal direction.

    Scoring table (3 HTFs):
        W1 same + D1 same + H4 same  → 1.0
        W1 same + D1 same + H4 opp   → 0.8
        W1 same + D1 opp  + H4 same  → 0.5
        W1 opp  + D1 opp  + H4 same  → 0.2
        ranging counted as neutral (no penalty)

    If fewer HTFs available, score is proportionally scaled.

    Args:
        symbol: Trading pair.
        direction: "buy" or "sell" (mapped to "bullish"/"bearish").
        exchange_client: for fetching OHLCV data.
        timeframes: override list (default: ["1w", "1d", "4h"]).

    Returns:
        Score 0.0–1.0.
    """
    if timeframes is None:
        timeframes = ["1w", "1d", "4h"]

    trend_map = {}
    for tf in timeframes:
        try:
            df = await exchange_client.fetch_ohlcv(symbol, tf, limit=50)
            if df is not None and len(df) >= 20:
                trend_map[tf] = _detect_trend_from_df(df)
        except Exception:
            continue

    if not trend_map:
        return 0.5  # unknown — neutral

    bull = "bullish" if direction in ("buy", "bullish") else "bearish"
    opp = "bearish" if bull == "bullish" else "bullish"

    same_count = sum(1 for t in trend_map.values() if t == bull)
    opp_count = sum(1 for t in trend_map.values() if t == opp)
    ranging_count = sum(1 for t in trend_map.values() if t == "ranging")

    total = len(trend_map)

    if same_count == total:
        return 1.0
    if same_count == total - 1 and opp_count == 1:
        return 0.8
    if same_count >= 1 and opp_count <= 1:
        return 0.5
    if opp_count >= same_count and same_count >= 1:
        return 0.2
    if ranging_count == total:
        return 0.5

    return 0.3


# ─────────────────────────────────────────────────────────────────────────
# Premium / Discount Score (soft multiplier)
# ─────────────────────────────────────────────────────────────────────────


def get_htf_directional_bias(df_1d: pd.DataFrame, df_4h: pd.DataFrame) -> str:
    """
    Определяет HTF bias на основе 1D и 4H.

    Returns: 'bullish', 'bearish', 'neutral'
    """
    # 1D bias
    ema55_1d = df_1d['close'].ewm(span=55).mean().iloc[-1]
    price_1d = df_1d['close'].iloc[-1]
    slope_1d = (ema55_1d - df_1d['close'].ewm(span=55).mean().iloc[-5]) / 5

    # 4H bias
    ema55_4h = df_4h['close'].ewm(span=55).mean().iloc[-1]
    price_4h = df_4h['close'].iloc[-1]
    slope_4h = (ema55_4h - df_4h['close'].ewm(span=55).mean().iloc[-5]) / 5

    slope_threshold = 0.001

    if abs(slope_1d) < slope_threshold and abs(slope_4h) < slope_threshold:
        return 'neutral'

    if price_1d > ema55_1d and slope_1d > 0:
        return 'bullish'
    elif price_1d < ema55_1d and slope_1d < 0:
        return 'bearish'

    if price_4h > ema55_4h and slope_4h > 0:
        return 'bullish'
    elif price_4h < ema55_4h and slope_4h < 0:
        return 'bearish'

    return 'neutral'


def calc_premium_discount_score(
    df: pd.DataFrame,
    direction: str,
    lookback: int = 200,
) -> float:
    """Score based on price location relative to ICT premium/discount zones.

    Uses fib levels from swing range:
        Discount:  fib 0.0–0.3 (cheap, near swing low) → good for BUY
        Equilibrium: fib 0.3–0.7 → neutral
        Premium:   fib 0.7–1.0 (expensive, near swing high) → good for SELL

    Args:
        df: OHLCV DataFrame (at least `lookback` rows).
        direction: "buy" or "sell".
        lookback: number of candles for range calculation.

    Returns:
        Score 0.0–1.0.
    """
    if df is None or len(df) < 10:
        return 0.5

    data = df.tail(lookback)
    range_high = float(data["high"].max())
    range_low = float(data["low"].min())
    rng = range_high - range_low

    if rng <= 0:
        return 0.5

    current = float(df["close"].iloc[-1])
    fib = (current - range_low) / rng  # 0.0 = range low, 1.0 = range high

    if direction in ("buy", "bullish"):
        if fib <= 0.3:
            return 1.0   # deep discount — optimal for BUY
        elif fib <= 0.4:
            return 0.8   # discount zone
        elif fib <= 0.6:
            return 0.5   # equilibrium — neutral
        elif fib <= 0.7:
            return 0.3   # entering premium — unfavorable
        else:
            return 0.1   # deep premium — bad for BUY
    else:
        if fib >= 0.7:
            return 1.0   # deep premium — optimal for SELL
        elif fib >= 0.6:
            return 0.8   # premium zone
        elif fib >= 0.4:
            return 0.5   # equilibrium — neutral
        elif fib >= 0.3:
            return 0.3   # entering discount — unfavorable
        else:
            return 0.1   # deep discount — bad for SELL
