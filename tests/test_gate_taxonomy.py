"""Tests for monitoring/gate_taxonomy.py — the canonical gate names.

The taxonomy exists to stop three independent gate lists (scanner funnel,
storage.trace, backtest buckets) from drifting apart again. These tests
enforce that:
  1. every gate name the scanner actually logs is in the taxonomy,
  2. the taxonomy's own tables stay internally consistent,
  3. consumers (scanner / trace / backtest) really use the shared lists.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from monitoring.gate_taxonomy import (
    BACKTEST_ACTIVE,
    BACKTEST_STEPS,
    DEPRECATED_GATES,
    GATE_ORDER,
    LIVE_GATES,
    LIVE_TO_BACKTEST,
    MARKER_GATES,
    backtest_step_for,
    is_known_gate,
)

ROOT = Path(__file__).resolve().parent.parent
SCANNER_SRC = (ROOT / "scheduler" / "scanner.py").read_text(encoding="utf-8")


def _logged_gates(src: str, pattern: str) -> list[str]:
    """Extract gate-name literals, tolerating multi-line calls."""
    flat = re.sub(r"\s+", " ", src)
    return re.findall(pattern, flat)


def test_scanner_log_gate_names_in_taxonomy():
    """Every _funnel.log_gate(...) name must be a known taxonomy gate."""
    names = _logged_gates(
        SCANNER_SRC, r'_funnel\.log_gate\(\s*\S+,\s*\S+,\s*"([a-z_0-9]+)"'
    )
    assert names, "no log_gate calls found — parser/pattern broken?"
    unknown = sorted({n for n in names if not is_known_gate(n)})
    assert not unknown, (
        f"scanner logs gates missing from monitoring/gate_taxonomy.py: {unknown}"
    )


def test_scanner_trace_names_in_taxonomy():
    """Every trace.blocked/passed/record(...) name must be known too."""
    names = _logged_gates(
        SCANNER_SRC, r'trace\.(?:blocked|passed|record)\(\s*"([a-z_0-9]+)"'
    )
    assert names, "no trace gate calls found — parser/pattern broken?"
    unknown = sorted({n for n in names if not is_known_gate(n)})
    assert not unknown, (
        f"scanner traces gates missing from monitoring/gate_taxonomy.py: {unknown}"
    )


def test_taxonomy_covers_all_scanner_gates():
    """Union of logged + traced names must fit inside LIVE_GATES ∪ MARKER_GATES."""
    logged = set(_logged_gates(
        SCANNER_SRC, r'_funnel\.log_gate\(\s*\S+,\s*\S+,\s*"([a-z_0-9]+)"'
    ))
    traced = set(_logged_gates(
        SCANNER_SRC, r'trace\.(?:blocked|passed|record)\(\s*"([a-z_0-9]+)"'
    ))
    known = set(LIVE_GATES) | set(MARKER_GATES)
    missing = (logged | traced) - known
    assert not missing, f"taxonomy misses live gates: {sorted(missing)}"


def test_live_gates_unique_and_deprecated_excluded():
    assert len(LIVE_GATES) == len(set(LIVE_GATES))
    assert len(MARKER_GATES) == len(set(MARKER_GATES))
    live = set(LIVE_GATES) | set(MARKER_GATES)
    leaked = [g for g in DEPRECATED_GATES if g in live]
    assert not leaked, f"deprecated gates crept back into LIVE_GATES: {leaked}"
    # Historical readers of GATE_ORDER must not see deprecated names either.
    assert not (set(GATE_ORDER) & set(DEPRECATED_GATES))


def test_live_to_backtest_mapping_complete_and_valid():
    assert set(LIVE_TO_BACKTEST) == set(LIVE_GATES), (
        f"missing: {set(LIVE_GATES) - set(LIVE_TO_BACKTEST)}; "
        f"extra: {set(LIVE_TO_BACKTEST) - set(LIVE_GATES)}"
    )
    bad = {
        g: step for g, step in LIVE_TO_BACKTEST.items()
        if step is not None and step not in BACKTEST_STEPS
    }
    assert not bad, f"mapping points outside BACKTEST_STEPS: {bad}"


def test_backtest_steps_unique_and_all_active():
    assert len(BACKTEST_STEPS) == len(set(BACKTEST_STEPS))
    assert set(BACKTEST_ACTIVE) == set(BACKTEST_STEPS)
    assert all(BACKTEST_ACTIVE.values())


def test_backtest_step_for_rejects_unknown_gate():
    assert backtest_step_for("pattern_engine") == "NO_PATTERN"
    assert backtest_step_for("cooldown") is None
    with pytest.raises(KeyError):
        backtest_step_for("btc_global_trend")  # v1 legacy gate
    assert not is_known_gate("btc_global_trend")


def test_consumers_share_taxonomy_lists():
    from scheduler.scanner import _FUNNEL_GATES
    from storage.trace import GATE_ORDER as TRACE_GATE_ORDER
    from backtest.engine import FUNNEL_STEPS, BACKTEST_ACTIVE as BT_ACTIVE

    assert _FUNNEL_GATES == list(LIVE_GATES) + list(MARKER_GATES)
    assert TRACE_GATE_ORDER == list(LIVE_GATES)
    assert GATE_ORDER == list(LIVE_GATES)
    assert FUNNEL_STEPS == list(BACKTEST_STEPS)
    assert BT_ACTIVE == BACKTEST_ACTIVE
