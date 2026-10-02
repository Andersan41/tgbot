"""
storage/trace.py — Decision Trace Builder

Accumulates gate pass/fail results and feature snapshots during a single
pipeline pass (scan_symbol), then saves a complete DecisionTrace row.
One builder per candidate evaluation.

Usage in scanner.py:
    trace = DecisionTraceBuilder(symbol, timeframe)
    trace.record("cooldown", True)
    trace.passed("portfolio_risk")
    trace.blocked("confirm_tf", reason="direction mismatch on 15m")
    trace.set_features({
        "adx": 42.1, "rsi": 58.3, "ema_short": 105000,
        "ema_long": 104200, "volume_ratio": 2.3, ...
    })
    await trace.save(db)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Optional

from loguru import logger

from storage.database import Database


# Canonical gate order — shared with scanner._FUNNEL_GATES; single source
# of truth is monitoring/gate_taxonomy.py (live v2 pipeline).
from monitoring.gate_taxonomy import GATE_ORDER  # noqa: F401  (re-export)

# Feature keys that are captured in the snapshot
FEATURE_KEYS = {
    # Raw indicators
    "adx", "rsi", "ema_short", "ema_long", "ema_spread_pct",
    "macd_hist", "supertrend_direction", "volume_ratio",
    # Factor strengths
    "dmi_strength", "ema_strength",
    # Score / confidence
    "signal_score", "confidence",
    # Market context
    "regime", "direction",
    # SL/TP metrics
    "sl_source", "tp_distance_pct", "sl_distance_pct", "rr_ratio",
    # Structure / liquidity
    "has_bos", "has_sweep", "has_ob", "ob_distance_pct",
    # Context / multi-timeframe
    "context_score", "btc_trend_strength", "mtf_alignment_score",
    # Decision Intelligence (v2.4.0)
    "atr_pct", "ema_slope_3", "ema_slope_5",
    "nearest_support_pct", "nearest_resistance_pct",
    "regime_confidence",
    # B-011: V2 pipeline features
    "components", "overall_quality", "setup_confidence", "mtf_aligned",
    "p_tp", "risk_pct", "expected_rr", "setup_type", "git_sha",
}


@dataclass
class ExecutionSnapshot:
    """Snapshot of execution context at signal creation time.

    Captures market microstructure and timing for post-trade analysis.
    """
    entry_candle_open: Optional[str] = None  # ISO format
    entry_timestamp: Optional[str] = None   # ISO format
    entry_bar_index: Optional[int] = None   # Candle index in dataframe
    spread: Optional[float] = None          # ask - bid
    atr: Optional[float] = None             # ATR at signal creation
    tick_size: Optional[float] = None       # Minimum price increment
    buffer_total: Optional[float] = None    # Total SL buffer applied
    execution_latency_ms: Optional[float] = None  # signal detection → telegram send
    entry_source: Optional[str] = None      # CLOSE / OPEN / MID / BID / ASK
    entry_price: Optional[float] = None     # Actual entry price used
    bid: Optional[float] = None
    ask: Optional[float] = None
    open: Optional[float] = None
    close: Optional[float] = None
    mid: Optional[float] = None
    signal_detected_at: Optional[str] = None  # ISO format
    telegram_sent_at: Optional[str] = None    # ISO format

    def to_dict(self) -> dict:
        """Convert to dict, filtering None values."""
        return {k: v for k, v in asdict(self).items() if v is not None}


class DecisionTraceBuilder:
    """Accumulates gate results and feature snapshot for one scan_symbol() pass.

    Call .record(gate, passed) at each gate, or .blocked(gate, reason)
    for early exit. Call .set_features({...}) to attach indicator/market
    snapshot. Call .save(db) at the end.
    """

    __slots__ = (
        "symbol", "timeframe", "_gates", "_final_stage",
        "_blocked_reason", "_signal_generated", "_signal_type",
        "_score", "_close_price", "_sl", "_tp",
        "_features", "_strategy_version", "_config_snapshot",
        "_execution_snapshot", "_hypothesis_snapshot",
    )

    def __init__(self, symbol: str, timeframe: str) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self._gates: dict[str, Optional[bool]] = {}
        self._final_stage: Optional[str] = None
        self._blocked_reason: Optional[str] = None
        self._signal_generated: bool = False
        self._signal_type: Optional[str] = None
        self._score: Optional[int] = None
        self._close_price: Optional[float] = None
        self._sl: Optional[float] = None
        self._tp: Optional[float] = None
        self._features: dict = {}
        self._strategy_version: Optional[str] = None
        self._config_snapshot: Optional[str] = None
        self._execution_snapshot: Optional[ExecutionSnapshot] = None
        self._hypothesis_snapshot: Optional[dict] = None

    def record(self, gate: str, passed: bool) -> None:
        """Record a gate outcome. Does NOT short-circuit — caller decides."""
        self._gates[gate] = passed

    def blocked(self, gate: str, reason: str = "") -> None:
        """Record that the pipeline was blocked at this gate."""
        self._gates[gate] = False
        self._final_stage = gate
        self._blocked_reason = reason[:200] if reason else None

    def passed(self, gate: str) -> None:
        """Record that a gate passed."""
        self._gates[gate] = True

    def set_signal(
        self,
        signal_type: str,
        score: int,
        close_price: float,
        sl: Optional[float],
        tp: Optional[float],
    ) -> None:
        """Mark that a signal was generated (all gates passed)."""
        self._signal_generated = True
        self._signal_type = signal_type
        self._score = score
        self._close_price = close_price
        self._sl = sl
        self._tp = tp
        self._final_stage = "signal_generated"

    def set_execution_snapshot(self, snapshot: ExecutionSnapshot) -> None:
        """Set execution snapshot for post-trade analysis."""
        self._execution_snapshot = snapshot

    def set_candidate_id(self, candidate_id: int) -> None:
        """Store candidate_id for later DB link (not saved to trace itself)."""
        self._candidate_id = candidate_id

    def set_features(self, features: dict) -> None:
        """Keep earlier snapshots and all explicitly registered feature names."""
        self._features.update({key: value for key, value in features.items() if key in FEATURE_KEYS})

    def set_version(self, version: str, config_snapshot: Optional[str] = None) -> None:
        """Set strategy version and optional config snapshot JSON."""
        self._strategy_version = version
        self._config_snapshot = config_snapshot

    def set_hypothesis(
        self,
        hypothesis_id: str,
        narrative_type: str,
        direction: str,
        quality: float,
        confidence: float,
        decay_factor: float,
        utility: float,
        entry_price: float = 0.0,
        invalidation_price: float = 0.0,
        target_price: float = 0.0,
        rr_ratio: float = 0.0,
        phase: str = "",
    ) -> None:
        """Set hypothesis snapshot for ScenarioMemory tracking."""
        self._hypothesis_snapshot = {
            "hypothesis_id": hypothesis_id,
            "narrative_type": narrative_type,
            "direction": direction,
            "quality": quality,
            "confidence": confidence,
            "decay_factor": decay_factor,
            "utility": utility,
            "entry_price": entry_price,
            "invalidation_price": invalidation_price,
            "target_price": target_price,
            "rr_ratio": rr_ratio,
            "phase": phase,
        }

    @property
    def hypothesis_snapshot(self) -> Optional[dict]:
        return self._hypothesis_snapshot

    @property
    def gates(self) -> dict[str, Optional[bool]]:
        return self._gates

    @property
    def final_stage(self) -> Optional[str]:
        return self._final_stage

    def build_gate_path(self) -> str:
        """Preserve actual execution order, including gates absent from old schemas."""
        path = []
        for gate, passed in self._gates.items():
            if passed is None:
                continue
            value = f"{gate}:{'PASS' if passed else 'BLOCK'}"
            if not passed and gate == self._final_stage and self._blocked_reason:
                value += f":{self._blocked_reason[:80]}"
            path.append(value)
        return json.dumps(path)

    async def save(
        self,
        db: Database,
        signal_id: Optional[int] = None,
        candidate_id: Optional[int] = None,
    ) -> int:
        """Persist the decision trace. Returns the trace ID."""
        try:
            execution_snapshot_dict = None
            if self._execution_snapshot is not None:
                execution_snapshot_dict = self._execution_snapshot.to_dict()

            trace = await db.save_decision_trace(
                symbol=self.symbol,
                timeframe=self.timeframe,
                gate_results=self._gates,
                final_stage=self._final_stage,
                blocked_reason=self._blocked_reason,
                signal_generated=self._signal_generated,
                signal_type=self._signal_type,
                score=self._score,
                close_price=self._close_price,
                sl=self._sl,
                tp=self._tp,
                signal_id=signal_id,
                candidate_id=candidate_id,
                features=self._features,
                strategy_version=self._strategy_version,
                config_snapshot=self._config_snapshot,
                gate_path=self.build_gate_path(),
                execution_snapshot=execution_snapshot_dict,
                hypothesis_snapshot=self._hypothesis_snapshot,
            )
            return trace.id
        except Exception as e:
            logger.error(f"Decision trace save failed for {self.symbol} {self.timeframe}: {e}")
            return -1
