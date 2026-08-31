"""
liquidity/pool.py — Liquidity Pool abstraction.

Groups all liquidity sources (Equal Levels, External Liquidity, Sweeps, OBs, FVGs)
into a unified "liquidity landscape" for the Trade Engine.

ICT concept: Liquidity = "where the stops are".
The Trade Engine maps liquidity first, then finds trades within that map.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

from loguru import logger

from liquidity.equal_levels import EqualLevel, detect_equal_levels
from liquidity.external import ExternalLiquidity, detect_external_liquidity
from liquidity.sweep import SweepEvent


@dataclass
class LiquidityLevel:
    """A single level in the liquidity landscape."""
    type: Literal[
        "equal_high", "equal_low",
        "old_high", "old_low",
        "swept_high", "swept_low",
        "ob_bullish", "ob_bearish",
        "fvg_bullish", "fvg_bearish",
    ]
    level: float
    strength: float = 0.0
    swept: bool = False
    source: Optional[object] = None  # original object (EqualLevel, ExternalLiquidity, etc.)

    @property
    def is_bullish_target(self) -> bool:
        """Level that price would move UP to reach (target for BUY)."""
        return self.type in ("equal_high", "old_high", "ob_bearish", "fvg_bearish")

    @property
    def is_bearish_target(self) -> bool:
        """Level that price would move DOWN to reach (target for SELL)."""
        return self.type in ("equal_low", "old_low", "ob_bullish", "fvg_bullish")

    @property
    def is_above(self) -> bool:
        return self.type in ("equal_high", "old_high", "swept_high")

    @property
    def is_below(self) -> bool:
        return self.type in ("equal_low", "old_low", "swept_low")


@dataclass
class LiquidityMap:
    """Complete liquidity landscape for a symbol/timeframe."""
    symbol: str
    timeframe: str
    current_price: float

    # All liquidity levels, sorted by strength
    levels: list[LiquidityLevel] = field(default_factory=list)

    # Source data (for reference)
    equal_levels: list[EqualLevel] = field(default_factory=list)
    external_levels: list[ExternalLiquidity] = field(default_factory=list)
    sweeps: list[SweepEvent] = field(default_factory=list)

    @property
    def targets_above(self) -> list[LiquidityLevel]:
        """All liquidity targets above current price (for BUY TP)."""
        return sorted(
            [l for l in self.levels if l.level > self.current_price and not l.swept],
            key=lambda x: x.level,
        )

    @property
    def targets_below(self) -> list[LiquidityLevel]:
        """All liquidity targets below current price (for SELL TP)."""
        return sorted(
            [l for l in self.levels if l.level < self.current_price and not l.swept],
            key=lambda x: x.level,
            reverse=True,
        )

    @property
    def supports(self) -> list[LiquidityLevel]:
        """Liquidity levels below price (potential SL for BUY)."""
        return sorted(
            [l for l in self.levels if l.level < self.current_price],
            key=lambda x: x.level,
            reverse=True,
        )

    @property
    def resistances(self) -> list[LiquidityLevel]:
        """Liquidity levels above price (potential SL for SELL)."""
        return sorted(
            [l for l in self.levels if l.level > self.current_price],
            key=lambda x: x.level,
        )


def build_liquidity_map(
    symbol: str,
    timeframe: str,
    current_price: float,
    swing_highs: list,
    swing_lows: list,
    order_blocks: list,
    fvgs: list,
    sweeps: list,
    df=None,
    lookback: int = 200,
) -> LiquidityMap:
    """Build a complete liquidity landscape.

    Combines all liquidity sources into a single ranked map
    that the Trade Engine can query for targets and SL levels.
    """
    levels = []

    # 1. Equal Highs/Lows
    equal_levels = detect_equal_levels(swing_highs, swing_lows)
    for el in equal_levels:
        levels.append(LiquidityLevel(
            type=el.type,
            level=el.level,
            strength=el.strength,
            swept=el.swept,
            source=el,
        ))

    # 2. External Liquidity (old highs/lows)
    ext_levels = detect_external_liquidity(df, lookback=lookback) if df is not None else []
    for ext in ext_levels:
        levels.append(LiquidityLevel(
            type=ext.type,
            level=ext.level,
            strength=ext.strength,
            swept=ext.swept,
            source=ext,
        ))

    # 3. Sweeps (already-swept levels — still relevant as references)
    for sweep in sweeps:
        level_type = "swept_high" if sweep.type == "bearish" else "swept_low"
        levels.append(LiquidityLevel(
            type=level_type,
            level=sweep.swept_level,
            strength=sweep.strength,
            swept=True,
            source=sweep,
        ))

    # 4. Order Blocks (as potential targets)
    for ob in order_blocks:
        level_type = "ob_bullish" if ob.type == "bullish" else "ob_bearish"
        levels.append(LiquidityLevel(
            type=level_type,
            level=ob.midpoint,
            strength=ob.displacement_atr / 3.0,  # normalize to [0,1]
            source=ob,
        ))

    # 5. FVGs (as potential targets)
    for fvg in fvgs:
        level_type = "fvg_bullish" if fvg.type == "bullish" else "fvg_bearish"
        avg_price = (fvg.top + fvg.bottom) / 2
        levels.append(LiquidityLevel(
            type=level_type,
            level=avg_price,
            strength=fvg.size_pct / 2.0,  # normalize
            swept=fvg.filled,
            source=fvg,
        ))

    # Sort by strength
    levels.sort(key=lambda x: x.strength, reverse=True)

    return LiquidityMap(
        symbol=symbol,
        timeframe=timeframe,
        current_price=current_price,
        levels=levels,
        equal_levels=equal_levels,
        external_levels=ext_levels,
        sweeps=sweeps,
    )
