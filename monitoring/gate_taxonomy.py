"""Canonical gate taxonomy for the v2 signal pipeline.

Single source of truth for gate names shared by:

- ``scheduler.scanner._FUNNEL_GATES`` — runtime funnel logging (log_gate)
- ``storage.trace.GATE_ORDER`` — DecisionTrace gate order / gate_path
- ``backtest.engine.FUNNEL_STEPS`` — coarse backtest funnel buckets
- funnel comparison tooling (live vs backtest)

Names are exactly what ``scan_symbol_v2`` logs today — no renames, so
historical decision_traces rows and ``gate_path`` JSON stay readable.

Known quirks are encoded explicitly in ``LIVE_TO_BACKTEST`` instead of
being rediscovered per report:

- the backtest has no runtime-only gates (cooldown, portfolio admission,
  context, SMT fetch) → mapped to ``None``;
- ``sl_tp``: live rejects invalid SL/TP plans under ``sl_tp``, but the
  backtest counts them as ``NO_PATTERN`` (``trade_plan.is_valid`` check);
- the backtest's ``RISK_ENGINE_BLOCKED`` bucket additionally swallows the
  direction/confluence/compression/override/probability blocks that live
  logs under their own gate names.

Legacy v1 gates (``btc_global_trend``, ``confirm_tf``, ``signal_engine``,
``distance_filter``, …) live in the ``gate_*`` DecisionTrace columns and
the ``GATE_ORDER`` lists of ``analytics/counterfactual.py`` and
``analytics/gate_funnel.py`` — those belong to the old ``scan_symbol``
pipeline and are intentionally NOT part of this taxonomy.
"""
from __future__ import annotations

# ── Live pipeline (scan_symbol_v2), execution order ─────────────────────
# Every blocking gate + recorded gate, in the order the scanner reaches it.
LIVE_GATES: tuple[str, ...] = (
    "cooldown",
    "portfolio_risk",
    "indicators",
    "compression_regime",
    "pattern_engine",
    "direction_filter",
    "symbol_filter",
    "confluence_mode",
    "sweep_required",
    "displacement_gate",
    "mss_gate",
    "bos_gate",
    "sweep_continuation",
    "poi_gate",
    "sl_tp",
    "sl_tp_rebuild",
    "direction_check",
    "symbol_overrides",
    "entry_zone",
    "smt_divergence",  # recorded only, never blocks (SMT feeds confluence_mode)
    "htf_bias",
    "portfolio_risk_recheck",
    "risk_engine",
    "dedup",
    "portfolio_admission",
)

# Funnel marker events — logged, but not gates (never BLOCKED).
MARKER_GATES: tuple[str, ...] = (
    "start",      # _funnel ENTER marker
    "entry_armed",  # entry zone armed (PASS-only)
)

# Names that used to sit in _FUNNEL_GATES/GATE_ORDER but are not logged by
# v2 (leftovers from the v1 pipeline). Kept here so old reports stay
# interpretable and so tests can assert they don't creep back in.
DEPRECATED_GATES: tuple[str, ...] = (
    "structure_alignment",
    "regime_block",
)

# Back-compat alias: both scanner and storage.trace import from here.
GATE_ORDER: list[str] = list(LIVE_GATES)


# ── Backtest coarse buckets (backtest/engine.py, run_funnel.py) ─────────
BACKTEST_STEPS: tuple[str, ...] = (
    "NO_PATTERN",
    "SETUP_TYPE_GATE",
    "HTF_BIAS_BLOCKED",
    "ENTRY_ZONE",
    "RISK_ENGINE_BLOCKED",
    "DEDUP",
    "PASSED",
)

# Which backtest funnel steps are actually implemented.
BACKTEST_ACTIVE: dict[str, bool] = {step: True for step in BACKTEST_STEPS}


# ── live gate → backtest bucket ─────────────────────────────────────────
# None = runtime-only gate the backtest does not model.
LIVE_TO_BACKTEST: dict[str, str | None] = {
    "cooldown": None,                  # runtime; backtest models DEDUP instead
    "portfolio_risk": "RISK_ENGINE_BLOCKED",   # bt: open-risk cap before pipeline
    "indicators": None,                # bt skips bars without indicators
    "compression_regime": "RISK_ENGINE_BLOCKED",  # bt Phase 0.4 lumped here
    "pattern_engine": "NO_PATTERN",
    "direction_filter": "RISK_ENGINE_BLOCKED",   # bt Phase 1.35
    "symbol_filter": None,             # bt runs on a fixed symbol universe
    "confluence_mode": "RISK_ENGINE_BLOCKED",    # bt Phase 1.35
    "sweep_required": "SETUP_TYPE_GATE",         # bt: reversal requires sweep
    "displacement_gate": None,        # not modelled by bt setup-type gates
    "mss_gate": "SETUP_TYPE_GATE",
    "bos_gate": "SETUP_TYPE_GATE",
    "sweep_continuation": None,       # optional in live, absent in bt
    "poi_gate": "SETUP_TYPE_GATE",
    "sl_tp": "NO_PATTERN",            # QUIRK: bt counts invalid plan as NO_PATTERN
    "sl_tp_rebuild": None,            # live-only repair pass
    "direction_check": "RISK_ENGINE_BLOCKED",    # bt block_all_sell / per-symbol
    "symbol_overrides": "RISK_ENGINE_BLOCKED",   # bt apply_symbol_overrides
    "entry_zone": "ENTRY_ZONE",
    "smt_divergence": None,           # live-only fetch
    "htf_bias": "HTF_BIAS_BLOCKED",
    "portfolio_risk_recheck": None,   # bt checks the portfolio once (above)
    "risk_engine": "RISK_ENGINE_BLOCKED",        # + bt-only probability gate
    "dedup": "DEDUP",
    "portfolio_admission": None,      # live-only signal persistence stage
}


def is_known_gate(gate: str) -> bool:
    """True if ``gate`` is a live gate, a marker, or a deprecated name."""
    return gate in LIVE_GATES or gate in MARKER_GATES or gate in DEPRECATED_GATES


def backtest_step_for(gate: str) -> str | None:
    """Coarse backtest funnel bucket a live gate rolls up into (None = not modelled)."""
    if gate not in LIVE_TO_BACKTEST:
        raise KeyError(f"gate {gate!r} is not in the v2 taxonomy")
    return LIVE_TO_BACKTEST[gate]
