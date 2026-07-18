"""
scripts/feasibility_report.py — Consolidate the self-learning feasibility spike into a report.

Reads the JSON artifacts produced by the earlier spike steps and writes a single markdown
decision document answering the user's actual question: is there a learnable edge, and what
does it say we should change?

Inputs (all under reports/feasibility/, produced by the prior steps):
    history_depth.json     ← scripts/probe_history_depth.py       (Step 1)
    dataset_summary.json   ← backtest/replay_dataset.py           (Step 3)
    train_metrics.json     ← scripts/train_prob_model.py          (Step 4)

Output:
    reports/feasibility/FEASIBILITY_REPORT.md

Pure standard library — no pandas/sklearn needed, so the report can be regenerated cheaply.
Missing inputs are reported as "not yet run" rather than crashing.

Usage:
    python scripts/feasibility_report.py
"""
from __future__ import annotations

import json
from pathlib import Path

FEAS_DIR = Path(__file__).parent.parent / "reports" / "feasibility"
HISTORY = FEAS_DIR / "history_depth.json"
DATASET = FEAS_DIR / "dataset_summary.json"
METRICS = FEAS_DIR / "train_metrics.json"
OUT = FEAS_DIR / "FEASIBILITY_REPORT.md"

# ── Go/No-Go thresholds (documented in the report so the verdict is auditable) ──
MIN_AUC_GO = 0.56          # OOS AUC clearly above coin-flip
MIN_AUC_NOGO = 0.52        # at/below this → no learnable signal in current features
MIN_SELECTION_N = 20       # a threshold bucket must hold this many OOS setups to count
MIN_PF_GO = 1.10           # model-selected profit factor worth acting on


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a column-aligned GitHub markdown table (every pipe lines up)."""
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(str(cell)))
    def fmt(cells: list[str]) -> str:
        return "| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)) + " |"
    sep = "| " + " | ".join("-" * widths[i] for i in range(len(headers))) + " |"
    return "\n".join([fmt(headers), sep, *(fmt(r) for r in rows)])


def _section_history(hist: dict | None) -> tuple[str, dict]:
    if not hist:
        return "## 1. Data reality — history depth\n\n_Not yet run (scripts/probe_history_depth.py)._\n", {}
    rows = []
    depth_by_symbol: dict = {}
    for r in hist.get("results", []):
        if not r.get("available"):
            rows.append([r["symbol"], r["timeframe"], "—", "NOT AVAILABLE", "—"])
            continue
        depth = f"{r['depth_years']:.2f}y" + (" (>=cap)" if r.get("cap_reached") else "")
        rows.append([r["symbol"], r["timeframe"], str(r["candles"]),
                     (r.get("earliest") or "")[:10], depth])
        depth_by_symbol.setdefault(r["symbol"], {})[r["timeframe"]] = r["depth_years"]
    table = _md_table(["Symbol", "TF", "Candles", "Earliest", "Depth"], rows)
    body = (
        "## 1. Data reality — history depth\n\n"
        f"Probe cap: {hist.get('years_cap')} years. '>=cap' means real history may be deeper.\n\n"
        f"{table}\n"
    )
    return body, depth_by_symbol


def _section_dataset(ds: dict | None) -> str:
    if not ds:
        return "## 2. Dataset\n\n_Not yet run (backtest/replay_dataset.py)._\n"
    lines = ["## 2. Dataset (offline ICT replay)\n"]
    lines.append(f"- Total setups: **{ds.get('total_setups', 0)}**, "
                 f"label-complete: **{ds.get('label_complete', 0)}**")
    dr = ds.get("date_range")
    if dr:
        lines.append(f"- Date range: {dr[0][:10]} → {dr[1][:10]}")
    if ds.get("outcomes"):
        total = max(ds.get("label_complete", 0), 1)
        rows = [[k, str(v), f"{v / total * 100:.1f}%"] for k, v in ds["outcomes"].items()]
        lines.append("\n" + _md_table(["Outcome", "Count", "Share"], rows))
    lines.append(f"\n- Win rate (net>0): **{ds.get('win_rate_pct', 0)}%**  |  "
                 f"TP-before-SL: **{ds.get('tp_before_sl_pct', 0)}%**  |  "
                 f"Avg net PnL/setup: **{ds.get('avg_net_pnl_pct', 0):+.3f}%**")
    lines.append(f"- Setups the RiskEngine would accept today (passed_risk): "
                 f"**{ds.get('passed_risk_setups', 0)}**")
    if ds.get("per_symbol"):
        rows = [[k, str(v)] for k, v in ds["per_symbol"].items()]
        lines.append("\n" + _md_table(["Symbol", "Setups"], rows))
    return "\n".join(lines) + "\n"


def _section_model(m: dict | None) -> str:
    if not m:
        return "## 3. Model comparison & edge\n\n_Not yet run (scripts/train_prob_model.py)._\n"
    lines = ["## 3. Model comparison (walk-forward, out-of-sample)\n"]
    comp = m.get("model_comparison", {})
    rows = []
    for name, r in sorted(comp.items(), key=lambda x: -x[1].get("mean_auc", 0)):
        rows.append([name, f"{r.get('mean_auc', 0):.4f}", f"{r.get('mean_brier', 0):.4f}",
                     f"{r.get('mean_precision', 0):.3f}", f"{r.get('mean_recall', 0):.3f}"])
    lines.append(_md_table(["Model", "AUC", "Brier", "Prec", "Rec"], rows))
    lines.append(f"\nBest model: **{m.get('best_model')}**  |  "
                 f"n_rows={m.get('n_rows')}  features={m.get('n_features')}  "
                 f"target+={m.get('target_positive_rate', 0) * 100:.1f}%")

    cal = m.get("calibration", {})
    lines.append(f"\nCalibration: ECE={cal.get('ece')}  Brier={cal.get('brier')}")

    # LOFO importance
    lofo = m.get("lofo_top", [])
    if lofo:
        lines.append("\n### What carries signal — top features (leave-one-out)\n")
        rows = [[r["feature"], f"{r['delta_auc']:+.4f}", "yes" if r["important"] else ""]
                for r in lofo[:12]]
        lines.append(_md_table(["Feature", "ΔAUC when removed", "Important"], rows))

    # Edge analysis
    edge = m.get("edge", {})
    if edge:
        lines.append("\n### Edge — model-ranked vs rules baseline vs current bot (OOS)\n")
        base = edge.get("baseline_all", {})
        bot = edge.get("bot_passed_risk", {})
        lines.append(f"- All setups: n={base.get('n', 0)}, PF={base.get('pf', 0)}, "
                     f"expectancy={base.get('expectancy', 0):+.4f}%")
        lines.append(f"- Bot (passed_risk): n={bot.get('n', 0)}, PF={bot.get('pf', 0)}, "
                     f"expectancy={bot.get('expectancy', 0):+.4f}%\n")
        rows = []
        for row in edge.get("per_threshold", []):
            mo, ru = row.get("model", {}), row.get("rules", {})
            rows.append([f"{row['threshold']:.2f}",
                         str(mo.get("n", 0)), f"{mo.get('pf', 0):.3f}", f"{mo.get('expectancy', 0):+.4f}",
                         str(ru.get("n", 0)), f"{ru.get('pf', 0):.3f}", f"{ru.get('expectancy', 0):+.4f}"])
        lines.append(_md_table(
            ["Thr", "model n", "model PF", "model exp%", "rules n", "rules PF", "rules exp%"], rows))
    return "\n".join(lines) + "\n"


def _verdict(m: dict | None) -> str:
    lines = ["## 4. Verdict — go / no-go\n"]
    if not m:
        lines.append("**INCOMPLETE** — training metrics missing; run scripts/train_prob_model.py.\n")
        return "\n".join(lines)

    best = m.get("best_model")
    best_auc = m.get("model_comparison", {}).get(best, {}).get("mean_auc", 0.0)
    base_exp = m.get("edge", {}).get("baseline_all", {}).get("expectancy", 0.0)

    # Best qualifying model-selected threshold bucket.
    best_bucket = None
    for row in m.get("edge", {}).get("per_threshold", []):
        mo = row.get("model", {})
        if mo.get("n", 0) >= MIN_SELECTION_N and mo.get("pf", 0) >= MIN_PF_GO \
                and mo.get("expectancy", -9) > max(base_exp, 0.0):
            if best_bucket is None or mo["expectancy"] > best_bucket["model"]["expectancy"]:
                best_bucket = row

    if best_auc >= MIN_AUC_GO and best_bucket is not None:
        verdict = "GO"
        rationale = (
            f"OOS AUC {best_auc:.3f} ≥ {MIN_AUC_GO} and the model-ranked selection at "
            f"threshold {best_bucket['threshold']:.2f} lifts profit factor to "
            f"{best_bucket['model']['pf']:.2f} (expectancy {best_bucket['model']['expectancy']:+.3f}%) "
            f"over the {base_exp:+.3f}% baseline — a learnable edge is present."
        )
    elif best_auc <= MIN_AUC_NOGO:
        verdict = "NO-GO"
        rationale = (
            f"OOS AUC {best_auc:.3f} ≤ {MIN_AUC_NOGO}: the current ICT feature set carries "
            f"essentially no learnable signal about TP-before-SL. Do not build the training loop "
            f"on these features — revisit feature engineering or labeling first."
        )
    else:
        verdict = "CONDITIONAL"
        rationale = (
            f"OOS AUC {best_auc:.3f} is above coin-flip but no threshold bucket clears the "
            f"PF≥{MIN_PF_GO} / beats-baseline / n≥{MIN_SELECTION_N} bar. There may be a weak edge; "
            f"expand data (more symbols / deeper history) and re-measure before committing."
        )

    lines.append(f"### {verdict}\n")
    lines.append(rationale + "\n")
    lines.append("**If GO — recommended safe deployment shape (out of scope for this spike):**\n")
    lines.append("1. Offline-train the model on the full labeled dataset.")
    lines.append("2. Human-gated promotion of the artifact to `models/probability_model.pkl` "
                 "(the live `ProbabilityEngine` auto-loads it; today it is absent → rules fallback).")
    lines.append("3. Run in shadow mode alongside the live path, comparing predicted vs realized "
                 "TP over 2-4 weeks before letting it gate signals via `min_p_tp`.")
    lines.append("4. Never auto-retrain-and-deploy — re-validate walk-forward on each refresh.\n")
    lines.append("_Thresholds are configurable at the top of scripts/feasibility_report.py._\n")
    return "\n".join(lines)


def main() -> None:
    hist = _load(HISTORY)
    ds = _load(DATASET)
    m = _load(METRICS)

    history_body, _ = _section_history(hist)
    parts = [
        "# Self-Learning Feasibility Report\n",
        "Offline feasibility spike: can a model learn from multi-year history to rank ICT "
        "setups better than the current rules, and what does it say we should change?\n",
        history_body,
        _section_dataset(ds),
        _section_model(m),
        _verdict(m),
    ]
    report = "\n".join(parts)

    FEAS_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Wrote {OUT}")
    missing = [p.name for p, v in [(HISTORY, hist), (DATASET, ds), (METRICS, m)] if v is None]
    if missing:
        print(f"NOTE: sections incomplete — missing inputs: {', '.join(missing)}")


if __name__ == "__main__":
    main()
