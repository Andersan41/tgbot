"""
scripts/train_prob_model.py — Train & validate a candidate P(TP) model on the ICT dataset.

Consumes the leakage-free dataset produced by `backtest/replay_dataset.py` and measures
whether a learned model beats the rules-based baseline out-of-sample — the core question
of the self-learning feasibility spike. It:

  1. Selects the feature columns (exactly the `SetupFeatures.to_vector()` keys),
  2. Compares LogisticRegression / RandomForest / XGBoost under a purged+embargoed
     TimeSeriesSplit (walk-forward), reporting AUC / Brier / precision / recall,
  3. Runs Leave-One-Feature-Out importance (which of the ICT features carry signal),
  4. Measures edge: out-of-sample win-rate / profit-factor / expectancy of model-ranked
     selection vs the rules `p_tp_rules` baseline vs the current `passed_risk` subset,
  5. Serializes the validated best model to a SCRATCH path in the engine-compatible
     `{classifier, regressor, feature_names}` pickle format — NOT the live models/ path.

This is a dedicated trainer because the existing `analysis/probability_model.py` is coupled
to the OLD signal_engine schema (its exclude-list drops rsi/adx, which are legitimate ICT
features here). The sklearn/xgboost machinery is the same; only the column glue differs.

IMPORTANT — inference parity: the live `ProbabilityEngine._predict_ml` feeds the model a RAW
`to_vector()` frame with no scaler. So LogisticRegression is serialized inside a
Pipeline(StandardScaler, LR) — scaling happens INSIDE predict_proba. Tree models are stored
raw (they need no scaling and expose feature_importances_).

NOTE: written offline and NOT runtime-validated in the authoring environment (no deps there).
Run in the project venv (Python 3.11).

Usage:
    python scripts/train_prob_model.py --dataset reports/dataset/ict_dataset.parquet
    python scripts/train_prob_model.py --dataset reports/dataset/ict_dataset.parquet --embargo 200
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sklearn.calibration import calibration_curve
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, precision_score, recall_score, roc_auc_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

try:
    import xgboost as xgb
    _HAS_XGB = True
except Exception:  # noqa: BLE001 — xgboost optional at spike time
    _HAS_XGB = False

# Columns that are metadata or labels — everything else is a to_vector() feature.
_NON_FEATURE_COLS = {
    "symbol", "timeframe", "created_at", "direction", "setup_type", "entry_index",
    "entry_price", "sl_price", "tp_price", "p_tp_rules", "passed_risk", "risk_reason",
    "strategy_version",
    "result", "tp_hit", "win", "pnl_pct", "net_pnl_pct", "bars_held", "label_complete",
}

TARGET = "tp_hit"  # P(TP) = probability of hitting TP before SL — the engine's output
THRESHOLDS = [0.50, 0.55, 0.60, 0.65, 0.70]

OUT_DIR = Path(__file__).parent.parent / "reports" / "feasibility"
SCRATCH_MODEL = OUT_DIR / "candidate_probability_model.pkl"  # NOT models/probability_model.pkl
METRICS_JSON = OUT_DIR / "train_metrics.json"


def load_dataset(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
    if "label_complete" in df.columns:
        df = df[df["label_complete"].astype(bool)].copy()
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
    df = df.sort_values("created_at").reset_index(drop=True)
    return df


def feature_columns(df: pd.DataFrame) -> list[str]:
    cols = []
    for c in df.columns:
        if c in _NON_FEATURE_COLS:
            continue
        if df[c].dtype.kind in "fiub" and df[c].nunique(dropna=True) > 1:
            cols.append(c)
    return cols


def _build_models() -> dict:
    models = {
        "LogisticRegression": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(max_iter=1000, random_state=42)),
        ]),
        "RandomForest": RandomForestClassifier(
            n_estimators=200, max_depth=5, min_samples_leaf=20, random_state=42, n_jobs=-1,
        ),
    }
    if _HAS_XGB:
        models["XGBoost"] = xgb.XGBClassifier(
            n_estimators=200, max_depth=4, learning_rate=0.1,
            random_state=42, eval_metric="logloss",
        )
    return models


def walk_forward_compare(df: pd.DataFrame, feats: list[str], embargo: int, n_splits: int) -> dict:
    """Purged+embargoed TimeSeriesSplit comparison. Returns per-model OOS metrics and
    pooled OOS predictions (for calibration + edge analysis)."""
    X = df[feats].fillna(df[feats].median())
    y = df[TARGET].astype(int).values
    tscv = TimeSeriesSplit(n_splits=n_splits, gap=embargo)

    results = {}
    for name, proto in _build_models().items():
        aucs, briers, precs, recs = [], [], [], []
        oos_idx, oos_prob = [], []
        for tr, te in tscv.split(X):
            model = _clone(proto)
            model.fit(X.iloc[tr], y[tr])
            prob = model.predict_proba(X.iloc[te])[:, 1]
            aucs.append(roc_auc_score(y[te], prob))
            briers.append(brier_score_loss(y[te], prob))
            precs.append(precision_score(y[te], (prob >= 0.5).astype(int), zero_division=0))
            recs.append(recall_score(y[te], (prob >= 0.5).astype(int), zero_division=0))
            oos_idx.extend(te.tolist())
            oos_prob.extend(prob.tolist())
        results[name] = {
            "mean_auc": float(np.mean(aucs)),
            "mean_brier": float(np.mean(briers)),
            "mean_precision": float(np.mean(precs)),
            "mean_recall": float(np.mean(recs)),
            "oos_index": oos_idx,
            "oos_prob": oos_prob,
        }
    return results


def _clone(model):
    from sklearn.base import clone
    return clone(model)


def run_lofo(df: pd.DataFrame, feats: list[str], n_splits: int, embargo: int) -> tuple[list[dict], float]:
    """Leave-One-Feature-Out on LogisticRegression — which features carry signal."""
    X = df[feats].fillna(df[feats].median())
    y = df[TARGET].astype(int).values
    tscv = TimeSeriesSplit(n_splits=n_splits, gap=embargo)

    def _auc(cols: list[str]) -> float:
        scores = []
        for tr, te in tscv.split(X):
            m = Pipeline([("s", StandardScaler()),
                          ("c", LogisticRegression(max_iter=1000, random_state=42))])
            m.fit(X[cols].iloc[tr], y[tr])
            scores.append(roc_auc_score(y[te], m.predict_proba(X[cols].iloc[te])[:, 1]))
        return float(np.mean(scores))

    baseline = _auc(feats)
    out = []
    for f in feats:
        remaining = [c for c in feats if c != f]
        delta = baseline - _auc(remaining)
        out.append({"feature": f, "delta_auc": delta, "important": delta > 0.005})
    out.sort(key=lambda r: -r["delta_auc"])
    return out, baseline


def _pf_expectancy(sub: pd.DataFrame) -> dict:
    """Win-rate / profit-factor / expectancy for a selection of rows (net PnL basis)."""
    if len(sub) == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "expectancy": 0.0}
    wins = sub.loc[sub["net_pnl_pct"] > 0, "net_pnl_pct"]
    losses = sub.loc[sub["net_pnl_pct"] <= 0, "net_pnl_pct"]
    gross_loss = abs(losses.sum()) or 1e-9
    return {
        "n": int(len(sub)),
        "wr": round(float((sub["net_pnl_pct"] > 0).mean()) * 100, 1),
        "pf": round(float(wins.sum() / gross_loss), 3),
        "expectancy": round(float(sub["net_pnl_pct"].mean()), 4),
    }


def edge_analysis(df: pd.DataFrame, best_name: str, results: dict) -> dict:
    """Compare model-ranked selection vs rules baseline vs current bot selection, OOS."""
    oos = df.iloc[results[best_name]["oos_index"]].copy()
    oos["model_prob"] = results[best_name]["oos_prob"]

    report = {"oos_rows": int(len(oos)), "baseline_all": _pf_expectancy(oos)}

    # Current bot behavior: setups the RiskEngine would have accepted.
    report["bot_passed_risk"] = _pf_expectancy(oos[oos["passed_risk"].astype(bool)])

    # Model-ranked vs rules-ranked at each threshold.
    per_threshold = []
    for thr in THRESHOLDS:
        model_sel = _pf_expectancy(oos[oos["model_prob"] >= thr])
        rules_sel = (
            _pf_expectancy(oos[oos["p_tp_rules"] >= thr])
            if "p_tp_rules" in oos.columns else {"n": 0}
        )
        per_threshold.append({"threshold": thr, "model": model_sel, "rules": rules_sel})
    report["per_threshold"] = per_threshold
    return report


def calibration(df: pd.DataFrame, best_name: str, results: dict) -> dict:
    oos = df.iloc[results[best_name]["oos_index"]]
    y_true = oos[TARGET].astype(int).values
    y_prob = np.array(results[best_name]["oos_prob"])
    try:
        frac_pos, mean_pred = calibration_curve(y_true, y_prob, n_bins=10, strategy="uniform")
        ece = float(np.mean(np.abs(frac_pos - mean_pred)))
    except Exception:  # noqa: BLE001
        ece = float("nan")
    return {"ece": round(ece, 4), "brier": round(float(brier_score_loss(y_true, y_prob)), 4)}


def serialize_candidate(df: pd.DataFrame, feats: list[str], best_name: str) -> None:
    """Fit the best model on all data and save in the engine-compatible pickle format."""
    X = df[feats].fillna(df[feats].median())
    y = df[TARGET].astype(int).values
    model = _clone(_build_models()[best_name])
    model.fit(X, y)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(SCRATCH_MODEL, "wb") as f:
        pickle.dump({"classifier": model, "regressor": None, "feature_names": feats}, f)
    print(f"\n  Serialized candidate -> {SCRATCH_MODEL}")
    print("  (scratch artifact — NOT wired into models/probability_model.pkl)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Train & validate a candidate P(TP) model")
    parser.add_argument("--dataset", type=str, default="reports/dataset/ict_dataset.parquet")
    parser.add_argument("--embargo", type=int, default=200,
                        help="row gap between train/test folds to purge overlapping-trade leakage")
    parser.add_argument("--splits", type=int, default=5, help="TimeSeriesSplit folds")
    args = parser.parse_args()

    path = Path(args.dataset)
    if not path.exists():
        raise SystemExit(f"Dataset not found: {path} — run backtest.replay_dataset first.")

    df = load_dataset(path)
    feats = feature_columns(df)
    print(f"  Dataset: {len(df)} label-complete rows, {len(feats)} features")
    print(f"  Target: {TARGET}  |  positives: {df[TARGET].mean() * 100:.1f}%")
    if len(df) < 200:
        print("  WARNING: <200 rows — metrics will be noisy; treat results as directional only.")

    results = walk_forward_compare(df, feats, args.embargo, args.splits)

    print("\n  Walk-forward model comparison (OOS):")
    print(f"    {'Model':22s} {'AUC':>7s} {'Brier':>7s} {'Prec':>6s} {'Rec':>6s}")
    for name, r in sorted(results.items(), key=lambda x: -x[1]["mean_auc"]):
        print(f"    {name:22s} {r['mean_auc']:7.4f} {r['mean_brier']:7.4f} "
              f"{r['mean_precision']:6.3f} {r['mean_recall']:6.3f}")

    best_name = max(results, key=lambda k: results[k]["mean_auc"])
    print(f"\n  Best model (by OOS AUC): {best_name}")

    lofo, lofo_baseline = run_lofo(df, feats, args.splits, args.embargo)
    print(f"\n  LOFO importance (LR baseline AUC={lofo_baseline:.4f}) — top 10:")
    for r in lofo[:10]:
        flag = "  <-- important" if r["important"] else ""
        print(f"    {r['feature']:26s} delta_auc={r['delta_auc']:+.4f}{flag}")

    edge = edge_analysis(df, best_name, results)
    cal = calibration(df, best_name, results)

    print("\n  Edge analysis (out-of-sample):")
    print(f"    All setups:        {edge['baseline_all']}")
    print(f"    Bot (passed_risk): {edge['bot_passed_risk']}")
    print(f"    Calibration:       ECE={cal['ece']}  Brier={cal['brier']}")
    print(f"    {'Thr':>5s} {'model_n':>8s} {'model_pf':>9s} {'model_exp':>10s} "
          f"{'rules_n':>8s} {'rules_pf':>9s}")
    for row in edge["per_threshold"]:
        m, rl = row["model"], row["rules"]
        print(f"    {row['threshold']:5.2f} {m['n']:>8d} {m['pf']:>9.3f} {m['expectancy']:>10.4f} "
              f"{rl.get('n', 0):>8d} {rl.get('pf', 0):>9.3f}")

    serialize_candidate(df, feats, best_name)

    metrics = {
        "dataset": str(path),
        "n_rows": int(len(df)),
        "n_features": len(feats),
        "target_positive_rate": round(float(df[TARGET].mean()), 4),
        "model_comparison": {
            n: {k: v for k, v in r.items() if k not in ("oos_index", "oos_prob")}
            for n, r in results.items()
        },
        "best_model": best_name,
        "lofo_top": lofo[:15],
        "lofo_baseline_auc": lofo_baseline,
        "edge": edge,
        "calibration": cal,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(METRICS_JSON, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    print(f"\n  Saved metrics -> {METRICS_JSON}")


if __name__ == "__main__":
    main()
