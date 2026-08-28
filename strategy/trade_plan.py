"""
strategy/trade_plan.py — TradePlan dataclass + TargetScore.

The output of the Trade Engine: a complete trade plan with
entry zone, invalidation, ranked targets, and RR analysis.

ICT flow: Thesis → Invalidation → Target → Entry → RR → Execute?
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from strategy.invalidation import Invalidation
from liquidity.pool import LiquidityLevel


@dataclass
class TargetScore:
    """A ranked TP target with quality metrics."""
    level: float
    type: Literal["equal_high", "equal_low", "old_high", "old_low", "ob", "fvg", "atr"]
    strength: float           # liquidity quality [0, 1]
    distance_pct: float       # distance from entry
    rr_ratio: float           # risk:reward if this is the target
    path_clear: bool = True   # is the path from entry to target unobstructed?
    source_label: str = ""    # human-readable label

    @property
    def score(self) -> float:
        """Composite score: strength × RR × path clarity.

        RR scaling uses log formula: ln(1+rr)/ln(6), capped at 1.0.
        RR=1→0.39, RR=2→0.61, RR=3→0.77, RR=5→1.0, RR=10→capped.
        Old linear: RR>=3 all scored 1.0 — no incentive for higher targets.
        """
        import math
        if self.rr_ratio > 0:
            rr_factor = min(1.0, math.log(1.0 + self.rr_ratio) / math.log(6.0))
        else:
            rr_factor = 0.0
        path_factor = 1.0 if self.path_clear else 0.5
        return round(self.strength * rr_factor * path_factor, 3)


@dataclass
class TradePlan:
    """Complete trade plan — the output of the Trade Engine.

    Flow: Thesis → Invalidation → Target → Entry → RR → Execute?
    """
    direction: Literal["buy", "sell"]
    symbol: str
    timeframe: str

    # Entry
    entry_price: float
    entry_zone: tuple[float, float] = (0.0, 0.0)  # (low, high) of optimal entry

    # Invalidation (where the idea breaks = SL)
    invalidation: Optional[Invalidation] = None

    # Targets (ranked by quality)
    targets: list[TargetScore] = field(default_factory=list)

    # Final SL/TP (chosen from invalidation + best target)
    sl: float = 0.0
    tp: float = 0.0
    sl_source: str = ""
    tp_source: str = ""

    # RR
    rr_ratio: float = 0.0
    sl_distance_pct: float = 0.0
    tp_distance_pct: float = 0.0

    # Liquidity context
    liquidity_summary: str = ""

    # Validity
    is_valid: bool = False
    rejection_reason: str = ""

    # === Market Thesis Engine fields (new) ===
    market_thesis: Optional[object] = None       # MarketThesis from market_thesis_engine
    liquidity_path: Optional[object] = None      # LiquidityPath from market_thesis_engine
    scenario_score: float = 0.0                  # scenario score [0, 100]
    scenario_stability: float = 0.0              # scenario stability [0, 1] from DynamicTradeThesis
    thesis_source: str = ""                      # "market_thesis" or "legacy"

    @property
    def best_target(self) -> Optional[TargetScore]:
        """The highest-scoring target."""
        if not self.targets:
            return None
        return max(self.targets, key=lambda t: t.score)

    @property
    def target_count(self) -> int:
        return len(self.targets)

    def format_summary(self) -> str:
        """Human-readable trade plan summary."""
        lines = [
            f"{'BUY' if self.direction == 'buy' else 'SELL'} {self.symbol} {self.timeframe}",
            f"Entry: {self.entry_price:.4f}",
        ]

        # Market Thesis summary (if available)
        if self.market_thesis is not None:
            thesis = self.market_thesis
            lines.append(f"Thesis: {getattr(thesis, 'scenario', '')}")
            lines.append(f"Scenario Score: {self.scenario_score:.0f}/100")
            if self.scenario_stability > 0:
                lines.append(f"Stability: {self.scenario_stability:.0%}")

        if self.invalidation:
            lines.append(f"Invalidate: {self.invalidation.label}")
            lines.append(f"SL: {self.sl:.4f} ({self.sl_distance_pct:+.2f}%) [{self.sl_source}]")

        if self.targets:
            lines.append(f"Targets ({len(self.targets)}):")
            for i, t in enumerate(self.targets, 1):
                marker = " <- chosen" if t.level == self.tp else ""
                lines.append(f"  TP{i}: {t.level:.4f} ({t.distance_pct:+.2f}%) "
                           f"[{t.type}] RR=1:{t.rr_ratio:.1f}{marker}")

        lines.append(f"Final RR: 1:{self.rr_ratio:.1f}")

        if self.liquidity_summary:
            lines.append(f"Liquidity: {self.liquidity_summary}")

        # Liquidity Path (if available)
        if self.liquidity_path is not None:
            path = self.liquidity_path
            if hasattr(path, "format"):
                lines.append(path.format())

        if not self.is_valid:
            lines.append(f"BLOCKED: {self.rejection_reason}")

        return "\n".join(lines)
