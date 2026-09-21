"""
strategy/invalidation.py — Invalidation logic for ICT setups.

Determines "where does the idea become wrong?" — the invalidation level.

ICT concept: Invalidation = "if price reaches this level, my trade idea is dead."
This is where SL goes, NOT behind an arbitrary indicator.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


@dataclass
class Invalidation:
    """A level that invalidates the trade idea if reached."""
    level: float
    type: Literal["sweep_extreme", "ob_boundary", "structure_break", "swing_point", "atr"]
    reason: str              # human-readable explanation
    buffer_pct: float = 0.0  # additional buffer beyond the level
    distance_pct: float = 0.0  # distance from entry to this level

    @property
    def sl_with_buffer(self) -> float:
        """Invalidation level with buffer applied (for actual SL placement)."""
        return self.level * (1 + self.buffer_pct / 100)

    @property
    def label(self) -> str:
        return f"{self.type}: {self.level:.4f} ({self.reason})"


def find_invalidation_buy(
    entry: float,
    sweep_lows: list[float],
    ob_lows: list[float],
    swing_lows: list[float],
    bos_level: Optional[float] = None,
    atr: float = 0.0,
    buffer_atr_pct: float = 15.0,  # buffer = ATR * 15%
    min_distance_pct: float = 0.5,  # minimum SL distance from entry (%)
    max_sl_atr: float = 3.0,  # max SL distance in ATR multiples
) -> Optional[Invalidation]:
    """Find the invalidation level for a BUY setup.

    Priority (market logic, not programmer logic):
    1. Sweep extreme (if a sweep just happened, the low of the sweep is invalidation)
    2. OB boundary (if OB is the confirmation, below OB = invalid)
    3. Swing low (fractal point below entry)
    4. BOS level (break of structure level)
    5. ATR fallback
    """
    min_dist = entry * min_distance_pct / 100
    max_dist = atr * max_sl_atr if atr > 0 else float('inf')

    # 1. Sweep extreme — strongest invalidation
    valid_sweeps = [s for s in sweep_lows if s < entry and (entry - s) >= min_dist]
    if valid_sweeps:
        level = max(valid_sweeps)  # closest sweep low to entry (but still far enough)
        dist = entry - level
        if dist <= max_dist:
            return Invalidation(
                level=level,
                type="sweep_extreme",
                reason=f"sweep low — if price breaks here, liquidity was NOT swept",
                buffer_pct=0,
                distance_pct=dist / entry * 100,
            )
        # Sweep too wide — fall through to next level

    # 2. OB boundary
    valid_obs = [ob for ob in ob_lows if ob < entry]
    if valid_obs:
        level = max(valid_obs)  # closest OB low to entry
        dist = entry - level
        if dist <= max_dist:
            return Invalidation(
                level=level,
                type="ob_boundary",
                reason=f"OB low — below this, the order block is invalidated",
                buffer_pct=0,
                distance_pct=dist / entry * 100,
            )

    # 3. Swing low (fractal)
    valid_swings = [s for s in swing_lows if s < entry]
    if valid_swings:
        # Prefer closest swing low that's within ATR cap
        for level in sorted(valid_swings, reverse=True):
            dist = entry - level
            if dist <= max_dist:
                return Invalidation(
                    level=level,
                    type="swing_point",
                    reason=f"swing low — fractal point below entry",
                    buffer_pct=0,
                    distance_pct=dist / entry * 100,
                )
        # All swing lows too wide — use closest one anyway (better than ATR fallback)
        level = max(valid_swings)
        return Invalidation(
            level=level,
            type="swing_point",
            reason=f"swing low — fractal point (wide SL, {((entry - level) / atr):.1f} ATR)",
            buffer_pct=0,
            distance_pct=(entry - level) / entry * 100,
        )

    # 4. BOS level
    if bos_level and bos_level < entry:
        dist = entry - bos_level
        if dist <= max_dist:
            return Invalidation(
                level=bos_level,
                type="structure_break",
                reason=f"BOS level — break of structure",
                buffer_pct=0,
                distance_pct=dist / entry * 100,
            )

    # 5. ATR fallback
    if atr > 0:
        level = entry - atr * 1.5
        return Invalidation(
            level=level,
            type="atr",
            reason=f"ATR fallback — no structural level found",
            buffer_pct=0,
            distance_pct=(entry - level) / entry * 100,
        )

    return None


def find_invalidation_sell(
    entry: float,
    sweep_highs: list[float],
    ob_highs: list[float],
    swing_highs: list[float],
    bos_level: Optional[float] = None,
    atr: float = 0.0,
    buffer_atr_pct: float = 15.0,
    min_distance_pct: float = 0.5,  # minimum SL distance from entry (%)
    max_sl_atr: float = 3.0,  # max SL distance in ATR multiples
) -> Optional[Invalidation]:
    """Find the invalidation level for a SELL setup.

    Priority:
    1. Sweep extreme (high of the sweep)
    2. OB boundary (above OB = invalid)
    3. Swing high (fractal point above entry)
    4. BOS level
    5. ATR fallback
    """
    min_dist = entry * min_distance_pct / 100
    max_dist = atr * max_sl_atr if atr > 0 else float('inf')

    # 1. Sweep extreme
    valid_sweeps = [s for s in sweep_highs if s > entry and (s - entry) >= min_dist]
    if valid_sweeps:
        level = min(valid_sweeps)  # closest sweep high to entry (but still far enough)
        dist = level - entry
        if dist <= max_dist:
            return Invalidation(
                level=level,
                type="sweep_extreme",
                reason=f"sweep high — if price breaks here, liquidity was NOT swept",
                buffer_pct=0,
                distance_pct=dist / entry * 100,
            )

    # 2. OB boundary
    valid_obs = [ob for ob in ob_highs if ob > entry]
    if valid_obs:
        level = min(valid_obs)
        dist = level - entry
        if dist <= max_dist:
            return Invalidation(
                level=level,
                type="ob_boundary",
                reason=f"OB high — above this, the order block is invalidated",
                buffer_pct=0,
                distance_pct=dist / entry * 100,
            )

    # 3. Swing high
    valid_swings = [s for s in swing_highs if s > entry]
    if valid_swings:
        for level in sorted(valid_swings):
            dist = level - entry
            if dist <= max_dist:
                return Invalidation(
                    level=level,
                    type="swing_point",
                    reason=f"swing high — fractal point above entry",
                    buffer_pct=0,
                    distance_pct=dist / entry * 100,
                )
        # All swing highs too wide — use closest one anyway
        level = min(valid_swings)
        return Invalidation(
            level=level,
            type="swing_point",
            reason=f"swing high — fractal point (wide SL, {((level - entry) / atr):.1f} ATR)",
            buffer_pct=0,
            distance_pct=(level - entry) / entry * 100,
        )

    # 4. BOS level
    if bos_level and bos_level > entry:
        dist = bos_level - entry
        if dist <= max_dist:
            return Invalidation(
                level=bos_level,
                type="structure_break",
                reason=f"BOS level — break of structure",
                buffer_pct=0,
                distance_pct=dist / entry * 100,
            )

    # 5. ATR fallback
    if atr > 0:
        level = entry + atr * 1.5
        return Invalidation(
            level=level,
            type="atr",
            reason=f"ATR fallback — no structural level found",
            buffer_pct=0,
            distance_pct=(level - entry) / entry * 100,
        )

    return None
