"""
market_structure/premium_discount.py — Discount/Premium Zone Detection (ICT).

Premium: цена выше EMA21/55, близко к swing high, "дорого"
Discount: цена ниже EMA21/55, близко к swing low, "дешево"
Equilibrium: между EMA21 и EMA55
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd


class ZoneType(Enum):
    PREMIUM = 'premium'
    DISCOUNT = 'discount'
    EQUILIBRIUM = 'equilibrium'


@dataclass
class ZoneResult:
    zone_type: ZoneType
    zone_price_low: float
    zone_price_high: float
    distance_to_premium_pct: float
    distance_to_discount_pct: float
    fib_level: float  # 0-1, где 0 = discount, 1 = premium


def classify_zone(
    df: pd.DataFrame,
    htf_bias: str,
    swing_high: float,
    swing_low: float,
) -> ZoneResult:
    """
    Классифицирует текущую ценовую зону по ICT концепции.

    fib_level = (price - swing_low) / range_size, поэтому 0.0 = у минимума, 1.0 = у максимума:
    Discount: fib 0.0-0.3 (дёшево, у минимума — хорошо для long)
    Equilibrium: fib 0.3-0.7
    Premium: fib 0.7-1.0 (дорого, у максимума — хорошо для short)
    """
    price = df['close'].iloc[-1]

    range_size = swing_high - swing_low
    if range_size <= 0:
        return ZoneResult(
            ZoneType.EQUILIBRIUM, price, price, 0, 0, 0.5,
        )

    fib_level = (price - swing_low) / range_size

    # Discount = cheap (near swing low, fib 0.0-0.3)
    # Premium = expensive (near swing high, fib 0.7-1.0)
    if fib_level <= 0.3:
        zone_type = ZoneType.DISCOUNT
    elif fib_level >= 0.7:
        zone_type = ZoneType.PREMIUM
    else:
        zone_type = ZoneType.EQUILIBRIUM

    if zone_type == ZoneType.DISCOUNT:
        zone_price_low = swing_low
        zone_price_high = swing_low + range_size * 0.3
    elif zone_type == ZoneType.PREMIUM:
        zone_price_low = swing_low + range_size * 0.7
        zone_price_high = swing_high
    else:
        zone_price_low = swing_low + range_size * 0.3
        zone_price_high = swing_low + range_size * 0.7

    return ZoneResult(
        zone_type=zone_type,
        zone_price_low=zone_price_low,
        zone_price_high=zone_price_high,
        distance_to_premium_pct=(swing_high - price) / price * 100,
        distance_to_discount_pct=(price - swing_low) / price * 100,
        fib_level=fib_level,
    )


def get_entry_zone_quality(
    zone_result: ZoneResult,
    htf_bias: str,
    setup_type: str,
) -> float:
    """
    Оценивает качество entry zone.
    Returns: multiplier 0.5-1.5
    """
    if htf_bias == 'bullish':
        if zone_result.zone_type == ZoneType.DISCOUNT:
            return 1.3 if setup_type == 'reversal' else 1.1
        elif zone_result.zone_type == ZoneType.PREMIUM:
            return 0.6
        else:
            return 1.0
    elif htf_bias == 'bearish':
        if zone_result.zone_type == ZoneType.PREMIUM:
            return 1.3 if setup_type == 'reversal' else 1.1
        elif zone_result.zone_type == ZoneType.DISCOUNT:
            return 0.6
        else:
            return 1.0

    return 1.0
