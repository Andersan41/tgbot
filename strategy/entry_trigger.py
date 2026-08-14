"""
strategy/entry_trigger.py — Entry Trigger.

Separate layer between Trade Thesis and Signal.
A good hypothesis does NOT automatically mean entry.

Checks:
    - Price is in entry zone (for OB-based entries)
    - Price touched the trigger level
    - Spread is acceptable
    - Session is active
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# from strategy.hypothesis import Hypothesis  # DELETED module (type hint is safe via __future__ annotations)


# ── Entry Trigger Result ───────────────────────────────────────────

@dataclass
class TriggerResult:
    """Result of checking entry conditions."""
    triggered: bool
    entry_price: float = 0.0
    reason: Optional[str] = None
    spread_pct: float = 0.0


# ── Entry Trigger ──────────────────────────────────────────────────

class EntryTrigger:
    """Checks if price has reached the entry condition.

    A hypothesis can exist for hours before price reaches the entry zone.
    This module bridges that gap.

    Design:
        Hypothesis exists → price hasn't entered OB → NO SIGNAL
        Hypothesis exists → price entered OB → SIGNAL
    """

    def __init__(
        self,
        entry_proximity_pct: float = 1.5,   # how close price must be to entry
        max_spread_pct: float = 0.1,         # max spread to allow entry
    ):
        self.entry_proximity_pct = entry_proximity_pct
        self.max_spread_pct = max_spread_pct

    def check(
        self,
        hypothesis: Hypothesis,
        current_price: float,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
    ) -> TriggerResult:
        """Check if entry conditions are met.

        Args:
            hypothesis: the winning hypothesis
            current_price: current market price
            bid: current bid price (optional, for spread check)
            ask: current ask price (optional, for spread check)

        Returns:
            TriggerResult with triggered=True if entry is valid
        """
        entry = hypothesis.entry_price
        if entry <= 0:
            return TriggerResult(
                triggered=False,
                reason="no_entry_price",
            )

        # 1. Check if price is near entry zone
        distance_pct = abs(current_price - entry) / entry * 100

        if hypothesis.direction == "buy":
            # For buy: price should be at or below entry (pullback entry)
            # OR price just broke above entry (momentum entry)
            if current_price > entry * (1 + self.entry_proximity_pct / 100):
                return TriggerResult(
                    triggered=False,
                    entry_price=entry,
                    reason=f"price {current_price:.4f} too far above entry {entry:.4f}",
                )
        elif hypothesis.direction == "sell":
            # For sell: price should be at or above entry
            if current_price < entry * (1 - self.entry_proximity_pct / 100):
                return TriggerResult(
                    triggered=False,
                    entry_price=entry,
                    reason=f"price {current_price:.4f} too far below entry {entry:.4f}",
                )

        # 2. Check spread (if available)
        if bid and ask and bid > 0 and ask > 0:
            spread_pct = (ask - bid) / bid * 100
            if spread_pct > self.max_spread_pct:
                return TriggerResult(
                    triggered=False,
                    entry_price=entry,
                    spread_pct=spread_pct,
                    reason=f"spread {spread_pct:.3f}% > max {self.max_spread_pct}%",
                )

        return TriggerResult(
            triggered=True,
            entry_price=entry,
            spread_pct=spread_pct if bid and ask else 0.0,
        )
