"""
market_structure/htf_bias_v2.py — HTF Bias V2 with Multi-Timeframe Alignment.

Full top-down: W1 -> D1 -> H4 -> H1 with priority and override logic.
Replicates MTT Trading Bot ICT fidelity.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd


class BiasStrength(Enum):
    STRONG = 'strong'
    MODERATE = 'moderate'
    WEAK = 'weak'
    NEUTRAL = 'neutral'


@dataclass
class HTFBiasResult:
    direction: str  # 'bullish', 'bearish', 'neutral'
    strength: BiasStrength
    weekly_bias: str
    daily_bias: str
    h4_bias: str
    h1_bias: str
    override_reason: Optional[str] = None


def detect_last_bos(df: pd.DataFrame) -> Optional[dict]:
    """Detect last BOS direction from DataFrame using structure analysis."""
    if df is None or len(df) < 20:
        return None
    try:
        from market_structure.structure import analyze_structure
        struct = analyze_structure(df, lookback=min(50, len(df)))
        if struct and struct.last_bos:
            return {
                'direction': struct.last_bos.type,
                'level': struct.last_bos.level,
                'candle_index': struct.last_bos.candle_index,
            }
    except Exception:
        pass
    return None


def get_tf_bias(df: pd.DataFrame, use_structure: bool = True) -> tuple[str, float]:
    """
    Определяет bias для одного таймфрейма.
    Returns: (direction, confidence)
    direction: 'bullish', 'bearish', 'neutral', 'unknown' (insufficient data)
    """
    if df is None or len(df) < 55:
        return 'unknown', 0.0

    ema21 = df['close'].ewm(span=21).mean().iloc[-1]
    ema55 = df['close'].ewm(span=55).mean().iloc[-1]
    price = df['close'].iloc[-1]

    # EMA alignment
    if price > ema21 > ema55:
        direction = 'bullish'
    elif price < ema21 < ema55:
        direction = 'bearish'
    else:
        direction = 'neutral'

    # Confidence based on EMA spread
    spread = abs(ema21 - ema55) / price * 100
    confidence = min(spread * 10, 100)

    # Structure boost (если доступен)
    if use_structure and direction != 'neutral':
        last_bos = detect_last_bos(df)
        if last_bos and last_bos['direction'] == direction:
            confidence = min(confidence * 1.2, 100)

    return direction, confidence


def get_htf_bias_v2(
    df_1w: Optional[pd.DataFrame],
    df_1d: pd.DataFrame,
    df_4h: pd.DataFrame,
    df_1h: Optional[pd.DataFrame],
) -> HTFBiasResult:
    """
    Мульти-таймфрейм bias с priority и override logic.

    Priority:
    1. W1: EMA21/55 alignment + structure (BOS/CHoCH)
    2. D1: EMA21/55 alignment + structure
    3. H4: EMA21/55 alignment + structure
    4. H1: EMA21/55 alignment (только для zone classification)
    """
    w1_bias, w1_conf = get_tf_bias(df_1w, use_structure=True) if df_1w is not None else ('neutral', 0)
    d1_bias, d1_conf = get_tf_bias(df_1d, use_structure=True)
    h4_bias, h4_conf = get_tf_bias(df_4h, use_structure=True)
    h1_bias, h1_conf = get_tf_bias(df_1h, use_structure=False) if df_1h is not None else ('neutral', 0)

    # Majority voting on W1, D1, H4 (exclude 'unknown' from voting)
    biases = [w1_bias, d1_bias, h4_bias]
    known_biases = [b for b in biases if b != 'unknown']
    unknown_count = biases.count('unknown')
    bullish_count = known_biases.count('bullish')
    bearish_count = known_biases.count('bearish')

    if not known_biases:
        # All unknown — insufficient data on all TFs
        direction = 'neutral'
        strength = BiasStrength.NEUTRAL
    elif bullish_count >= 3:
        direction = 'bullish'
        strength = BiasStrength.STRONG
    elif bearish_count >= 3:
        direction = 'bearish'
        strength = BiasStrength.STRONG
    elif bullish_count >= 2:
        direction = 'bullish'
        strength = BiasStrength.MODERATE
    elif bearish_count >= 2:
        direction = 'bearish'
        strength = BiasStrength.MODERATE
    else:
        # Check for weak majority (exclude 'unknown')
        if w1_bias not in ('neutral', 'unknown') and d1_bias == w1_bias:
            direction = w1_bias
            strength = BiasStrength.WEAK
        elif w1_bias not in ('neutral', 'unknown') and h4_bias == w1_bias:
            direction = w1_bias
            strength = BiasStrength.WEAK
        elif d1_bias not in ('neutral', 'unknown') and h4_bias == d1_bias:
            direction = d1_bias
            strength = BiasStrength.WEAK
        else:
            direction = 'neutral'
            strength = BiasStrength.NEUTRAL

    # Override logic
    override_reason = None

    # W1 conflict with D1/H4
    if w1_bias != direction and w1_bias != 'neutral':
        if d1_bias == h4_bias == direction:
            override_reason = f"{direction}_override_w1_{w1_bias}"
            strength = BiasStrength.MODERATE

    # H4 conflict with W1/D1 (pullback detection)
    if h4_bias != direction and h4_bias != 'neutral':
        if w1_bias == d1_bias == direction:
            override_reason = f"pullback_{h4_bias}_vs_htf_{direction}"
            strength = BiasStrength.MODERATE

    return HTFBiasResult(
        direction=direction,
        strength=strength,
        weekly_bias=w1_bias,
        daily_bias=d1_bias,
        h4_bias=h4_bias,
        h1_bias=h1_bias,
        override_reason=override_reason,
    )
