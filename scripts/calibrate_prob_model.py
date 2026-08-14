"""
Platt scaling / Isotonic calibration for the P(TP) model.

Loads the candidate model, applies CalibratedClassifierCV with isotonic regression,
compares calibrated vs uncalibrated performance, and saves the calibrated model.
"""
import json
import os
import pickle
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.model_selection import TimeSeriesSplit

warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NON_FEATURE_COLS = {
    "symbol", "timeframe", "created_at", "direction", "setup_type", "entry_index",
    "entry_price", "sl_price", "tp_price", "p_tp_rules", "passed_risk", "risk_reason",
    "strategy_version", "result", "tp_hit", "win", "pnl_pct", "net_pnl_pct",
    "bars_held", "label_complete",
}

TARGET = "tp_hit"
DATASET = Path("reports/dataset/ict_360d_v2_all.parquet")
CANDIDATE_MODEL = Path("reports/feasibility/candidate_probability_model.pkl")
CALIBRATED_MODEL = Path("models/probability_model.pkl")
METRICS_PATH = Path("reports/feasibility/calibration_metrics.json")


def load_dataset(path):
    df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
    if "label_complete" in df.columns:
        df = df[df["label_complete"].astype(bool)].copy()
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
    df = df.sort_values("created_at").reset_index(drop=True)
    return df


def feature_columns(df):
    return [c for c in df.columns if c not in NON_FEATURE_COLS
            and df[c].dtype.kind in "fiub" and df[c].nunique(dropna=True) > 1]


def walk_forward_calibrate(df, feats, n_splits=5, embargo=100):
    """Walk-forward: train RF, calibrate with isotonic, compare metrics."""
    X = df[feats].fillna(df[feats].median())
    y = df[TARGET].astype(int).values
    tscv = TimeSeriesSplit(n_splits=n_splits, gap=embargo)

    uncal_probs, cal_probs, y_true_all, oos_indices = [], [], [], []

    for tr, te in tscv.split(X):
        from sklearn.ensemble import RandomForestClassifier
        base_rf = RandomForestClassifier(
            n_estimators=200, max_depth=5, min_samples_leaf=20,
            random_state=42, n_jobs=-1,
        )
        base_rf.fit(X.iloc[tr], y[tr])

        # Uncalibrated
        uncal_prob = base_rf.predict_proba(X.iloc[te])[:, 1]

        # Platt scaling (sigmoid) — more stable than isotonic on small datasets
        cal_rf = CalibratedClassifierCV(base_rf, method="sigmoid", cv=3)
        cal_rf.fit(X.iloc[tr], y[tr])
        cal_prob = cal_rf.predict_proba(X.iloc[te])[:, 1]

        uncal_probs.extend(uncal_prob.tolist())
        cal_probs.extend(cal_prob.tolist())
        y_true_all.extend(y[te].tolist())
        oos_indices.extend(te.tolist())

    y_true = np.array(y_true_all)
    uncal = np.array(uncal_probs)
    cal = np.array(cal_probs)

    results = {}
    for name, probs in [("uncalibrated", uncal), ("calibrated", cal)]:
        try:
            auc = roc_auc_score(y_true, probs)
        except Exception:
            auc = float("nan")
        brier = brier_score_loss(y_true, probs)
        try:
            frac_pos, mean_pred = calibration_curve(y_true, probs, n_bins=10, strategy="uniform")
            ece = float(np.mean(np.abs(frac_pos - mean_pred)))
        except Exception:
            ece = float("nan")
        results[name] = {"auc": round(auc, 4), "brier": round(brier, 4), "ece": round(ece, 4)}

    return results, oos_indices, uncal, cal, y_true


def edge_at_thresholds(y_true, cal_probs, thresholds):
    """WR / PF / expectancy at each probability threshold."""
    results = []
    for thr in thresholds:
        mask = cal_probs >= thr
        n = int(mask.sum())
        if n == 0:
            results.append({"threshold": thr, "n": 0, "wr": 0, "pf": 0, "expectancy": 0})
            continue
        sub_y = y_true[mask]
        wr = float(sub_y.mean()) * 100
        tp = sub_y.sum()
        fp = n - tp
        pf = round(tp / max(fp, 1), 3)
        results.append({
            "threshold": thr, "n": n,
            "wr": round(wr, 1), "pf": pf,
            "expectancy": round(float((cal_probs[mask].mean() - 0.5) * 2), 4),
        })
    return results


def serialize_calibrated(df, feats):
    """Train RF on all data, calibrate with isotonic, save."""
    from sklearn.ensemble import RandomForestClassifier
    X = df[feats].fillna(df[feats].median())
    y = df[TARGET].astype(int).values

    base_rf = RandomForestClassifier(
        n_estimators=200, max_depth=5, min_samples_leaf=20,
        random_state=42, n_jobs=-1,
    )
    base_rf.fit(X, y)

    cal_rf = CalibratedClassifierCV(base_rf, method="sigmoid", cv=5)
    cal_rf.fit(X, y)

    CALIBRATED_MODEL.parent.mkdir(parents=True, exist_ok=True)
    with open(CALIBRATED_MODEL, "wb") as f:
        pickle.dump({
            "classifier": cal_rf,
            "regressor": None,
            "feature_names": feats,
            "calibration": "platt",
        }, f)
    print(f"  Serialized calibrated model -> {CALIBRATED_MODEL}")


def main():
    print("=" * 60)
    print("  Platt Scaling / Isotonic Calibration")
    print("=" * 60)

    df = load_dataset(DATASET)
    feats = feature_columns(df)
    print(f"  Dataset: {len(df)} rows, {len(feats)} features")

    results, oos_idx, uncal, cal, y_true = walk_forward_calibrate(df, feats)

    print("\n  Walk-forward calibration comparison:")
    print(f"    {'Method':15s} {'AUC':>7s} {'Brier':>7s} {'ECE':>7s}")
    for name in ["uncalibrated", "calibrated"]:
        r = results[name]
        print(f"    {name:15s} {r['auc']:7.4f} {r['brier']:7.4f} {r['ece']:7.4f}")

    delta_ece = results["uncalibrated"]["ece"] - results["calibrated"]["ece"]
    print(f"\n  ECE improvement: {delta_ece:+.4f} (lower is better)")

    thresholds = [0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
    edge = edge_at_thresholds(y_true, cal, thresholds)
    print("\n  Edge analysis (calibrated, OOS):")
    print(f"    {'Thr':>5s} {'N':>6s} {'WR':>7s} {'PF':>7s}")
    for row in edge:
        print(f"    {row['threshold']:5.2f} {row['n']:>6d} {row['wr']:>6.1f}% {row['pf']:>7.3f}")

    serialize_calibrated(df, feats)

    metrics = {
        "dataset": str(DATASET),
        "n_rows": int(len(df)),
        "n_features": len(feats),
        "calibration_comparison": results,
        "edge_calibrated": edge,
    }
    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(METRICS_PATH, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"\n  Saved metrics -> {METRICS_PATH}")


if __name__ == "__main__":
    main()
