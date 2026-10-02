"""
analytics/calibration.py — Probability calibration analysis.

Compares model-predicted probability vs actual win rate. Shows calibration
table: if model says 80%, does it actually win ~80%?

Two sources (v2 first):
  - p_tp: the v2 Probability Engine estimate stored in
    decision_traces.feature_snapshot (source of truth for the live pipeline);
  - confidence_v2: legacy score from the old signal_engine pipeline.

Usage:
    python -m analytics.calibration [--db data/signals.db]
    python -m analytics.calibration --source ptp
    python -m analytics.calibration --use-confidence
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "signals.db"

# Below this many samples the buckets are noise — print a warning, still show
# the table (a small sample is more useful than nothing while it accumulates).
MIN_SAMPLES = 20

BUCKETS = [
    (0, 20, "0-20%"),
    (20, 40, "20-40%"),
    (40, 60, "40-60%"),
    (60, 80, "60-80%"),
    (80, 100, "80-100%"),
]


def load_calibration_data(db_path: str | Path) -> list[dict]:
    """Load signals with confidence_v2_pct and outcomes."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    query = """
        SELECT
            s.id, s.symbol, s.signal_type, s.confidence_v2_pct,
            s.confidence_v2_factors,
            o.status, o.pnl_pct,
            dt.confidence AS trace_confidence,
            dt.regime, dt.direction
        FROM signals s
        JOIN signal_outcomes o ON s.id = o.signal_id
        LEFT JOIN decision_traces dt ON dt.signal_id = s.id
        WHERE o.status IN ('HIT_TP', 'HIT_SL')
          AND (s.confidence_v2_pct IS NOT NULL OR dt.confidence IS NOT NULL)
        ORDER BY s.created_at
    """

    rows = conn.execute(query).fetchall()
    conn.close()

    data = []
    for row in rows:
        d = dict(row)
        # Prefer confidence_v2_pct, fallback to trace confidence
        conf = d.get("confidence_v2_pct") or d.get("trace_confidence")
        if conf is not None:
            d["confidence"] = conf
            d["is_win"] = 1 if d.get("status") == "HIT_TP" else 0
            data.append(d)

    return data


def load_ptp_data(db_path: str | Path) -> list[dict]:
    """Load v2 P(TP) estimates with live outcomes.

    p_tp is written by the scanner into decision_traces.feature_snapshot
    (JSON) and the row's outcome is filled later by the outcome tracker —
    so calibration is "predicted p_tp vs realized HIT_TP" on the exact rows
    the Probability Engine produced.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    query = """
        SELECT
            dt.id, dt.symbol, dt.timeframe, dt.signal_type,
            dt.regime, dt.direction, dt.feature_snapshot,
            dt.outcome, dt.pnl_pct
        FROM decision_traces dt
        WHERE dt.outcome IN ('HIT_TP', 'HIT_SL')
          AND dt.feature_snapshot IS NOT NULL
        ORDER BY dt.id
    """

    rows = conn.execute(query).fetchall()
    conn.close()

    data = []
    for row in rows:
        d = dict(row)
        try:
            features = json.loads(d["feature_snapshot"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        p_tp = features.get("p_tp")
        if not isinstance(p_tp, (int, float)) or not (0.0 < p_tp <= 1.0):
            continue
        data.append({
            "id": d["id"],
            "symbol": d["symbol"],
            "timeframe": d["timeframe"],
            "confidence": round(float(p_tp) * 100, 4),  # compute_* expect %
            "is_win": 1 if d["outcome"] == "HIT_TP" else 0,
            "pnl_pct": d["pnl_pct"],
            "regime": d["regime"],
            "direction": d["signal_type"] or d["direction"],
            "setup_type": features.get("setup_type"),
            "source": "p_tp",
        })

    return data


def load_for_source(db_path: str | Path, source: str = "auto") -> tuple[list[dict], str]:
    """Resolve the requested probability source to ``(data, used_source)``.

    ``auto`` prefers the v2 p_tp rows and falls back to the legacy
    confidence_v2 rows while the p_tp sample is still below MIN_SAMPLES.
    """
    if source == "confidence_v2":
        return load_calibration_data(db_path), "confidence_v2"
    data = load_ptp_data(db_path)
    if source == "auto" and len(data) < MIN_SAMPLES:
        legacy = load_calibration_data(db_path)
        if len(legacy) > len(data):
            return legacy, "confidence_v2"
    return data, "p_tp"


def compute_calibration_table(data: list[dict], n_bins: int = 10) -> list[dict]:
    """Compute calibration buckets: predicted probability vs actual WR."""
    if not data:
        return []

    # Create equal-width bins from 0 to 100
    bin_edges = [i * (100 / n_bins) for i in range(n_bins + 1)]
    bins = []

    for i in range(n_bins):
        lo = bin_edges[i]
        hi = bin_edges[i + 1]
        bucket = [d for d in data if lo <= d["confidence"] < hi or (i == n_bins - 1 and d["confidence"] == hi)]

        if not bucket:
            continue

        n = len(bucket)
        wins = sum(d["is_win"] for d in bucket)
        actual_wr = wins / n * 100
        avg_confidence = sum(d["confidence"] for d in bucket) / n
        avg_pnl = sum(d.get("pnl_pct", 0) or 0 for d in bucket) / n

        bins.append({
            "range": f"{lo:.0f}-{hi:.0f}%",
            "lo": lo,
            "hi": hi,
            "count": n,
            "wins": wins,
            "losses": n - wins,
            "actual_wr": round(actual_wr, 1),
            "avg_predicted": round(avg_confidence, 1),
            "calibration_error": round(abs(avg_confidence - actual_wr), 1),
            "avg_pnl": round(avg_pnl, 3),
        })

    return bins


def compute_coarse_calibration(data: list[dict]) -> list[dict]:
    """Coarse 5-bucket calibration matching the BUCKETS constant."""
    bucket_data = {label: {"wins": 0, "total": 0, "confidences": [], "pnls": []}
                   for _, _, label in BUCKETS}

    for d in data:
        conf = min(d["confidence"], 99.99)
        for lo, hi, label in BUCKETS:
            if lo <= conf < hi:
                bucket_data[label]["total"] += 1
                bucket_data[label]["wins"] += d["is_win"]
                bucket_data[label]["confidences"].append(d["confidence"])
                bucket_data[label]["pnls"].append(d.get("pnl_pct", 0) or 0)
                break

    result = []
    for lo, hi, label in BUCKETS:
        bd = bucket_data[label]
        n = bd["total"]
        if n == 0:
            continue

        avg_conf = sum(bd["confidences"]) / n
        actual_wr = bd["wins"] / n * 100
        avg_pnl = sum(bd["pnls"]) / n

        result.append({
            "bucket": label,
            "count": n,
            "wins": bd["wins"],
            "losses": n - bd["wins"],
            "actual_wr": round(actual_wr, 1),
            "avg_predicted_confidence": round(avg_conf, 1),
            "calibration_error": round(abs(avg_conf - actual_wr), 1),
            "avg_pnl": round(avg_pnl, 3),
        })

    return result


def compute_ece(data: list[dict], n_bins: int = 10) -> float:
    """Expected Calibration Error."""
    bins = compute_calibration_table(data, n_bins)
    if not bins:
        return 0.0

    total = sum(b["count"] for b in bins)
    ece = sum(b["count"] / total * b["calibration_error"] for b in bins)
    return round(ece, 2)


def compute_brier_score(data: list[dict]) -> float:
    """Brier score (lower is better, 0 = perfect)."""
    if not data:
        return 0.0

    n = len(data)
    ss = sum((d["confidence"] / 100 - d["is_win"]) ** 2 for d in data)
    return round(ss / n, 4)


def compute_by_regime(data: list[dict]) -> dict[str, list[dict]]:
    """Calibration broken down by regime."""
    regimes: dict[str, list[dict]] = {}
    for d in data:
        r = d.get("regime") or "unknown"
        regimes.setdefault(r, []).append(d)

    result = {}
    for regime, trades in regimes.items():
        if len(trades) >= 10:
            result[regime] = compute_coarse_calibration(trades)
    return result


def compute_by_direction(data: list[dict]) -> dict[str, list[dict]]:
    """Calibration broken down by BUY/SELL."""
    directions: dict[str, list[dict]] = {}
    for d in data:
        direction = d.get("direction") or d.get("signal_type") or "unknown"
        directions.setdefault(direction, []).append(d)

    result = {}
    for direction, trades in directions.items():
        if len(trades) >= 10:
            result[direction] = compute_coarse_calibration(trades)
    return result


def print_calibration_table(bins: list[dict]) -> None:
    """Print calibration table."""
    print("=" * 100)
    print("PROBABILITY CALIBRATION")
    print("=" * 100)
    print()
    print("  Does model confidence match reality?")
    print()
    print(f"  {'Predicted':<12} {'N':>5} {'Wins':>5} {'Losses':>6} {'Actual WR':>10} "
          f"{'Avg Conf':>10} {'Cal Error':>10} {'Avg PnL':>9}")
    print(f"  {'-'*12} {'-'*5} {'-'*5} {'-'*6} {'-'*10} {'-'*10} {'-'*10} {'-'*9}")

    for b in bins:
        print(f"  {b['bucket']:<12} {b['count']:>5} {b['wins']:>5} {b['losses']:>6} "
              f"{b['actual_wr']:>9.1f}% {b['avg_predicted_confidence']:>9.1f}% "
              f"{b['calibration_error']:>9.1f}% {b['avg_pnl']:>+8.3f}%")

    # Overall
    total = sum(b["count"] for b in bins)
    total_wins = sum(b["wins"] for b in bins)
    overall_wr = total_wins / total * 100 if total > 0 else 0
    overall_conf = sum(b["avg_predicted_confidence"] * b["count"] for b in bins) / total if total > 0 else 0
    overall_ece = sum(b["count"] / total * b["calibration_error"] for b in bins) if total > 0 else 0

    print(f"  {'-'*12} {'-'*5} {'-'*5} {'-'*6} {'-'*10} {'-'*10} {'-'*10} {'-'*9}")
    print(f"  {'TOTAL':<12} {total:>5} {total_wins:>5} {total - total_wins:>6} "
          f"{overall_wr:>9.1f}% {overall_conf:>9.1f}% {overall_ece:>9.1f}%")

    # Calibration assessment
    print()
    calibration_errors = [b["calibration_error"] for b in bins]
    max_error = max(calibration_errors) if calibration_errors else 0
    avg_error = sum(calibration_errors) / len(calibration_errors) if calibration_errors else 0

    if avg_error < 3:
        verdict = "WELL CALIBRATED"
    elif avg_error < 7:
        verdict = "MODERATELY CALIBRATED"
    else:
        verdict = "POORLY CALIBRATED"

    print(f"  Calibration verdict: {verdict}")
    print(f"  Avg calibration error: {avg_error:.1f}%")
    print(f"  Max calibration error: {max_error:.1f}%")


def print_brier_score(data: list[dict]) -> None:
    """Print Brier score analysis."""
    brier = compute_brier_score(data)
    ece = compute_ece(data)

    print()
    print(f"  Brier Score:    {brier:.4f} (0=perfect, 1=worst)")
    print(f"  ECE:            {ece:.2f}% (0=perfect)")

    if brier < 0.15:
        print("  → Good probabilistic predictions")
    elif brier < 0.25:
        print("  → Moderate probabilistic predictions")
    else:
        print("  → Poor probabilistic predictions")


def print_regime_calibration(regime_cal: dict[str, list[dict]]) -> None:
    """Print calibration by regime."""
    if not regime_cal:
        return

    print()
    print("=" * 100)
    print("CALIBRATION BY REGIME")
    print("=" * 100)
    print()

    for regime, bins in regime_cal.items():
        total = sum(b["count"] for b in bins)
        if total < 5:
            continue

        total_wins = sum(b["wins"] for b in bins)
        wr = total_wins / total * 100
        avg_conf = sum(b["avg_predicted_confidence"] * b["count"] for b in bins) / total
        ece = sum(b["count"] / total * b["calibration_error"] for b in bins)

        print(f"  {regime:<15} N={total:>4}  WR={wr:>5.1f}%  AvgConf={avg_conf:>5.1f}%  ECE={ece:.1f}%")


def main():
    parser = argparse.ArgumentParser(description="Probability Calibration Analysis")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--bins", type=int, default=10)
    parser.add_argument(
        "--source", choices=("auto", "ptp", "confidence_v2"), default="auto",
        help="probability source: v2 p_tp from decision_traces, legacy "
             "confidence_v2, or auto (p_tp first, legacy fallback)",
    )
    parser.add_argument(
        "--use-confidence", action="store_true",
        help="legacy alias for --source confidence_v2",
    )
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}")
        sys.exit(1)

    source = "confidence_v2" if args.use_confidence else args.source
    data, used = load_for_source(db_path, source)

    print(f"Loaded {len(data)} rows with {used} probability data")

    if not data:
        print("No calibration data found (need outcomes + probability estimates).")
        return
    if len(data) < MIN_SAMPLES:
        print(
            f"WARNING: only {len(data)} samples (<{MIN_SAMPLES}) — "
            f"buckets are noisy; treat as a smoke test."
        )

    bins = compute_coarse_calibration(data)
    regime_cal = compute_by_regime(data)

    print_calibration_table(bins)
    print_brier_score(data)
    print_regime_calibration(regime_cal)

    if args.export:
        output = Path(__file__).resolve().parent.parent / "reports" / "calibration.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        with open(output, "w", encoding="utf-8") as f:
            json.dump({
                "source": used,
                "n_samples": len(data),
                "buckets": bins,
                "brier_score": compute_brier_score(data),
                "ece": compute_ece(data),
                "by_regime": regime_cal,
            }, f, indent=2, ensure_ascii=False)
        print(f"\nExported to {output}")


if __name__ == "__main__":
    main()
