"""
strategy/trade_thesis.py — Trade Thesis Lifecycle

The TradeThesis is the ONLY object with a lifecycle.
MarketScenario is an immutable "map". TradeThesis is the "journey".

Lifecycle:
    forming → observed → confirmed → activated → executing → closed
        ↓         ↓          ↓           ↓          ↓
      expired   expired    failed      failed     failed

Key principles:
- Only TradeThesis has a status/lifecycle
- MarketScenario is immutable (never changes)
- ScenarioEvaluation is mutable (probability updates)
- TradeThesis persists between scans, updates on each candle
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from loguru import logger

# from strategy.scenario_engine import MarketScenario, ScenarioEvaluation  # DELETED module


# ══════════════════════════════════════════════════════════════════
# Thesis states
# ══════════════════════════════════════════════════════════════════

THESIS_FORMING = "forming"
THESIS_OBSERVED = "observed"
THESIS_CONFIRMED = "confirmed"
THESIS_ACTIVATED = "activated"
THESIS_EXECUTING = "executing"
THESIS_CLOSED = "closed"
THESIS_FAILED = "failed"
THESIS_EXPIRED = "expired"

ACTIVE_THESIS_STATES = frozenset({
    THESIS_FORMING, THESIS_OBSERVED, THESIS_CONFIRMED,
    THESIS_ACTIVATED, THESIS_EXECUTING,
})

TERMINAL_THESIS_STATES = frozenset({
    THESIS_CLOSED, THESIS_FAILED, THESIS_EXPIRED,
})


# ══════════════════════════════════════════════════════════════════
# ThesisSnapshot — point-in-time record
# ══════════════════════════════════════════════════════════════════

@dataclass
class ThesisSnapshot:
    """Snapshot of thesis state at a specific bar."""
    bar: int
    price: float
    probability: float
    status: str
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def __repr__(self) -> str:
        return (
            f"Snapshot(bar={self.bar} p={self.probability:.2f} "
            f"status={self.status})"
        )


# ══════════════════════════════════════════════════════════════════
# TradeThesis — the only object with a lifecycle
# ══════════════════════════════════════════════════════════════════

@dataclass
class TradeThesis:
    """A trading thesis — lives between scans.

    Contains:
    - An immutable MarketScenario (the "map")
    - A mutable ScenarioEvaluation (the "assessment")
    - A lifecycle (forming → ... → closed/failed/expired)
    - History of snapshots
    """
    id: str
    symbol: str
    timeframe: str

    # Core
    scenario: MarketScenario
    evaluation: ScenarioEvaluation
    direction: str  # "buy" / "sell"

    # Lifecycle
    status: str = THESIS_FORMING
    created_at: datetime = field(default_factory=datetime.utcnow)
    observed_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    activated_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    expired_at: Optional[datetime] = None

    # Trade parameters (from scenario, can be updated)
    entry_zone: Optional[Tuple[float, float]] = None
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    rr_ratio: float = 0.0

    # Tracking
    current_price: float = 0.0
    update_count: int = 0
    last_update_bar: int = 0
    history: list[ThesisSnapshot] = field(default_factory=list)

    # Performance (filled after close)
    pnl_pct: Optional[float] = None
    outcome: Optional[str] = None  # "win" / "loss" / "breakeven"
    hold_bars: int = 0

    @property
    def age_bars(self) -> int:
        return self.update_count

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_THESIS_STATES

    @property
    def is_tradeable(self) -> bool:
        """Ready to be sent as a signal."""
        return (
            self.status in (THESIS_CONFIRMED, THESIS_ACTIVATED)
            and self.evaluation.probability >= 0.5
        )

    @property
    def confirmation_pct(self) -> float:
        """How many critical components are confirmed."""
        if not self.scenario.components:
            return 0.0
        critical = [c for c in self.scenario.components if c.is_critical]
        if not critical:
            return 1.0
        # Count components that are in the evaluation's confirmed list
        confirmed = sum(
            1 for c in critical
            if c.id in self.evaluation.component_scores
            and self.evaluation.component_scores[c.id] > 0.5
        )
        return confirmed / len(critical)

    def __repr__(self) -> str:
        return (
            f"Thesis({self.symbol} {self.direction.upper()} "
            f"{self.scenario.name} status={self.status} "
            f"p={self.evaluation.probability:.2f} "
            f"age={self.age_bars})"
        )


# ══════════════════════════════════════════════════════════════════
# TradeThesisManager — manages thesis lifecycle
# ══════════════════════════════════════════════════════════════════

class TradeThesisManager:
    """Manages the lifecycle of trading theses.

    One thesis per symbol_timeframe. Persists between scans.
    Updates on each candle close.
    """

    def __init__(self, max_theses: int = 50):
        self.theses: dict[str, TradeThesis] = {}  # "symbol_timeframe" -> TradeThesis
        self.max_theses = max_theses

    def update(
        self,
        symbol: str,
        timeframe: str,
        scenarios: list[MarketScenario],
        evaluations: list[ScenarioEvaluation],
        bar: int,
        price: float,
    ) -> Optional[TradeThesis]:
        """Update thesis for a symbol_timeframe based on new scenarios.

        Returns the current thesis (active or newly created).
        """
        key = f"{symbol}_{timeframe}"
        existing = self.theses.get(key)

        if existing and existing.is_active:
            return self._update_existing(existing, scenarios, evaluations, bar, price)
        else:
            return self._create_new(key, symbol, timeframe, scenarios, evaluations, bar, price)

    def _update_existing(
        self,
        thesis: TradeThesis,
        scenarios: list[MarketScenario],
        evaluations: list[ScenarioEvaluation],
        bar: int,
        price: float,
    ) -> TradeThesis:
        """Update an existing thesis with new data."""
        # Find matching scenario and evaluation
        for s, e in zip(scenarios, evaluations):
            if s.id == thesis.scenario.id:
                # Same scenario — update evaluation
                thesis.evaluation = e
                break

        # Update tracking
        thesis.current_price = price
        thesis.update_count += 1
        thesis.last_update_bar = bar

        # ── Status transitions ──

        # forming → observed (first real data)
        if thesis.status == THESIS_FORMING and thesis.update_count >= 1:
            thesis.status = THESIS_OBSERVED
            thesis.observed_at = datetime.utcnow()

        # observed → confirmed (critical components confirmed)
        if thesis.status == THESIS_OBSERVED and thesis.confirmation_pct >= 0.5:
            thesis.status = THESIS_CONFIRMED
            thesis.confirmed_at = datetime.utcnow()

        # confirmed → activated (price entered entry zone)
        if thesis.status == THESIS_CONFIRMED and thesis.entry_zone:
            low, high = thesis.entry_zone
            if low <= price <= high:
                thesis.status = THESIS_ACTIVATED
                thesis.activated_at = datetime.utcnow()
                logger.info(
                    f"Thesis {thesis.id}: ACTIVATED at {price:.4f} "
                    f"(entry zone {low:.4f}-{high:.4f})"
                )

        # activated → executing (price moved away from entry)
        if thesis.status == THESIS_ACTIVATED:
            thesis.status = THESIS_EXECUTING

        # executing → failed (SL hit)
        if thesis.status in (THESIS_ACTIVATED, THESIS_EXECUTING) and thesis.sl_price:
            if thesis.direction == "buy" and price <= thesis.sl_price:
                thesis.status = THESIS_FAILED
                thesis.failed_at = datetime.utcnow()
                thesis.pnl_pct = (price - (thesis.entry_zone[0] if thesis.entry_zone else price)) / price * 100
                thesis.outcome = "loss"
                logger.info(f"Thesis {thesis.id}: FAILED (SL hit at {price:.4f})")
            elif thesis.direction == "sell" and price >= thesis.sl_price:
                thesis.status = THESIS_FAILED
                thesis.failed_at = datetime.utcnow()
                thesis.pnl_pct = ((thesis.entry_zone[0] if thesis.entry_zone else price) - price) / price * 100
                thesis.outcome = "loss"
                logger.info(f"Thesis {thesis.id}: FAILED (SL hit at {price:.4f})")

        # executing → closed (TP hit)
        if thesis.status in (THESIS_ACTIVATED, THESIS_EXECUTING) and thesis.tp_price:
            if thesis.direction == "buy" and price >= thesis.tp_price:
                thesis.status = THESIS_CLOSED
                thesis.closed_at = datetime.utcnow()
                thesis.pnl_pct = (price - (thesis.entry_zone[0] if thesis.entry_zone else price)) / price * 100
                thesis.outcome = "win"
                logger.info(f"Thesis {thesis.id}: CLOSED (TP hit at {price:.4f})")
            elif thesis.direction == "sell" and price <= thesis.tp_price:
                thesis.status = THESIS_CLOSED
                thesis.closed_at = datetime.utcnow()
                thesis.pnl_pct = ((thesis.entry_zone[0] if thesis.entry_zone else price) - price) / price * 100
                thesis.outcome = "win"
                logger.info(f"Thesis {thesis.id}: CLOSED (TP hit at {price:.4f})")

        # forming → expired (age > 20 bars without activation)
        if thesis.status == THESIS_FORMING and thesis.update_count > 20:
            thesis.status = THESIS_EXPIRED
            thesis.expired_at = datetime.utcnow()
            logger.debug(f"Thesis {thesis.id}: EXPIRED (age={thesis.update_count})")

        # observed → expired (age > 30 bars without confirmation)
        if thesis.status == THESIS_OBSERVED and thesis.update_count > 30:
            thesis.status = THESIS_EXPIRED
            thesis.expired_at = datetime.utcnow()

        # confirmed → expired (age > 40 bars without activation)
        if thesis.status == THESIS_CONFIRMED and thesis.update_count > 40:
            thesis.status = THESIS_EXPIRED
            thesis.expired_at = datetime.utcnow()

        # Take snapshot
        thesis.history.append(ThesisSnapshot(
            bar=bar,
            price=price,
            probability=thesis.evaluation.probability,
            status=thesis.status,
        ))

        return thesis

    def _create_new(
        self,
        key: str,
        symbol: str,
        timeframe: str,
        scenarios: list[MarketScenario],
        evaluations: list[ScenarioEvaluation],
        bar: int,
        price: float,
    ) -> Optional[TradeThesis]:
        """Create a new thesis from the best scenario."""
        if not scenarios:
            return None

        best = scenarios[0]
        best_eval = evaluations[0] if evaluations else ScenarioEvaluation(scenario_id=best.id)

        # Don't create thesis for weak scenarios
        if best_eval.probability < 0.5:
            logger.debug(
                f"Thesis {key}: skipped (best p={best_eval.probability:.2f} < 0.5)"
            )
            return None

        thesis = TradeThesis(
            id=f"{key}_{bar}",
            symbol=symbol,
            timeframe=timeframe,
            scenario=best,
            evaluation=best_eval,
            direction=best.direction,
            status=THESIS_OBSERVED,
            entry_zone=best.entry_zone,
            sl_price=best.invalidation_price,
            tp_price=best.target_price,
            rr_ratio=best.rr_ratio,
            current_price=price,
            observed_at=datetime.utcnow(),
        )

        thesis.history.append(ThesisSnapshot(
            bar=bar,
            price=price,
            probability=best_eval.probability,
            status=THESIS_OBSERVED,
        ))

        # Enforce max theses (evict oldest terminal)
        if len(self.theses) >= self.max_theses:
            self._evict_oldest()

        self.theses[key] = thesis
        logger.info(
            f"Thesis CREATED: {thesis.id} "
            f"{thesis.direction.upper()} {best.name} "
            f"p={best_eval.probability:.2f} rr={best.rr_ratio:.1f}"
        )

        return thesis

    def _evict_oldest(self) -> None:
        """Remove the oldest terminal thesis."""
        terminal = [
            (k, t) for k, t in self.theses.items()
            if t.status in TERMINAL_THESIS_STATES
        ]
        if terminal:
            # Sort by created_at
            terminal.sort(key=lambda x: x[1].created_at)
            oldest_key = terminal[0][0]
            del self.theses[oldest_key]
            logger.debug(f"Evicted oldest thesis: {oldest_key}")

    def get_active(self, symbol: Optional[str] = None) -> list[TradeThesis]:
        """Get all active theses, optionally filtered by symbol."""
        result = [t for t in self.theses.values() if t.is_active]
        if symbol:
            result = [t for t in result if t.symbol == symbol]
        return result

    def get_tradeable(self, symbol: Optional[str] = None) -> list[TradeThesis]:
        """Get theses ready to be sent as signals."""
        result = [t for t in self.theses.values() if t.is_tradeable]
        if symbol:
            result = [t for t in result if t.symbol == symbol]
        return result

    def get_by_key(self, symbol: str, timeframe: str) -> Optional[TradeThesis]:
        return self.theses.get(f"{symbol}_{timeframe}")

    def close_thesis(
        self,
        key: str,
        outcome: str,
        pnl_pct: float,
    ) -> None:
        """Manually close a thesis (e.g., from exchange webhook)."""
        thesis = self.theses.get(key)
        if thesis and thesis.is_active:
            thesis.status = THESIS_CLOSED
            thesis.closed_at = datetime.utcnow()
            thesis.outcome = outcome
            thesis.pnl_pct = pnl_pct
            logger.info(f"Thesis {key}: CLOSED ({outcome}, pnl={pnl_pct:.2f}%)")


# Module-level singleton
thesis_manager = TradeThesisManager()
