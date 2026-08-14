"""
Premium/Discount Feature Attribution Analysis.

Recalculates premium_discount_score from raw OHLCV cache and checks
correlation with win rate / profit factor across score buckets.

Separately for BUY and SELL, and by setup_type (-1=structure, 1=breakout).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.cache_ohlcv import load_cached, load_cached_max
from market_structure.structure import calc_premium_discount_score

DATASET = Path(__file__).parent.parent / "reports" / "dataset" / "ict_dataset.csv"
CACHE_DIR = Path(__file__).parent.parent / "reports" / "abn" / "ohlcv_cache"
OUTPUT_DIR = Path(__file__).parent.parent / "reports" / "analysis" / "pd_attribution"

N_BUCKETS = 5
BUCKET_EDGES = np.linspace(0.0, 1.0, N_BUCKETS + 1)
BUCKET_LABELS = [f"{BUCKET_EDGES[i]:.1f}–{BUCKET_EDGES[i+1]:.1f}" for i in range(N_BUCKETS)]

LOOKBACK = 200  # candles for PD score calculation


def recalculate_pd_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Recalculate premium_discount_score from raw OHLCV for each row."""
    symbols = df["symbol"].unique()
    ohlcv_cache: dict[str, pd.DataFrame] = {}

    for sym in symbols:
        loaded = load_cached_max(sym, "1h")
        if loaded is None:
            loaded = load_cached(sym, "1h", 3900)
        if loaded is not None:
            ohlcv_cache[sym] = loaded
            print(f"  Loaded {sym}: {len(loaded)} candles")
        else:
            print(f"  WARNING: No OHLCV cache for {sym}")

    recalculated = []
    skipped = 0
    for _, row in df.iterrows():
        sym = row["symbol"]
        idx = int(row["entry_index"])
        direction = row["direction"].lower()

        if sym not in ohlcv_cache:
            skipped += 1
            recalculated.append(np.nan)
            continue

        ohlcv = ohlcv_cache[sym]
        if idx < LOOKBACK or idx >= len(ohlcv):
            skipped += 1
            recalculated.append(np.nan)
            continue

        window = ohlcv.iloc[max(0, idx - LOOKBACK):idx + 1].copy()
        window = window.rename(columns=str.lower)

        score = calc_premium_discount_score(window, direction, lookback=LOOKBACK)
        recalculated.append(score)

    print(f"\n  Recalculated: {len(recalculated) - skipped} rows, skipped: {skipped}")
    df = df.copy()
    df["pd_score_recalc"] = recalculated
    return df


def bucket_stats(
    df: pd.DataFrame,
    score_col: str = "pd_score_recalc",
    n_buckets: int = N_BUCKETS,
) -> pd.DataFrame:
    """Compute WR, PF, avg R per bucket."""
    df = df.dropna(subset=[score_col]).copy()
    df["bucket"] = pd.cut(df[score_col], bins=n_buckets, labels=BUCKET_LABELS, include_lowest=True)

    rows = []
    for label in BUCKET_LABELS:
        subset = df[df["bucket"] == label]
        n = len(subset)
        if n == 0:
            rows.append({"Bucket": label, "Trades": 0, "WR": np.nan, "PF": np.nan, "Avg_R": np.nan})
            continue

        wins = subset["win"].sum()
        wr = wins / n * 100

        gross_profit = subset.loc[subset["pnl_pct"] > 0, "pnl_pct"].sum()
        gross_loss = abs(subset.loc[subset["pnl_pct"] < 0, "pnl_pct"].sum())
        pf = gross_profit / gross_loss if gross_loss > 0 else np.inf

        avg_r = subset["rr_ratio"].mean() if "rr_ratio" in subset.columns else np.nan

        rows.append({
            "Bucket": label,
            "Trades": n,
            "WR": round(wr, 1),
            "PF": round(pf, 2),
            "Avg_R": round(avg_r, 2) if not np.isnan(avg_r) else np.nan,
        })

    return pd.DataFrame(rows)


def main():
    print(f"Loading dataset: {DATASET}")
    df = pd.read_csv(DATASET)
    print(f"  {len(df)} rows, {df['symbol'].nunique()} symbols")

    print("\nRecalculating PD scores from OHLCV cache...")
    df = recalculate_pd_scores(df)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Overall ──
    print("\n" + "=" * 60)
    print("OVERALL — All trades")
    print("=" * 60)
    overall = bucket_stats(df)
    print(overall.to_string(index=False))
    overall.to_csv(OUTPUT_DIR / "overall.csv", index=False)

    # ── BUY only ──
    buy_df = df[df["direction"] == "BUY"]
    print(f"\n{'=' * 60}")
    print(f"BUY — {len(buy_df)} trades")
    print("=" * 60)
    buy_stats = bucket_stats(buy_df)
    print(buy_stats.to_string(index=False))
    buy_stats.to_csv(OUTPUT_DIR / "buy.csv", index=False)

    # ── SELL only ──
    sell_df = df[df["direction"] == "SELL"]
    print(f"\n{'=' * 60}")
    print(f"SELL — {len(sell_df)} trades")
    print("=" * 60)
    sell_stats = bucket_stats(sell_df)
    print(sell_stats.to_string(index=False))
    sell_stats.to_csv(OUTPUT_DIR / "sell.csv", index=False)

    # ── By setup_type: BUY + setup_type=-1 (structure) ──
    for st, st_label in [(-1, "structure"), (1, "breakout")]:
        for direction in ["BUY", "SELL"]:
            subset = df[(df["direction"] == direction) & (df["setup_type"] == st)]
            label = f"{direction}_{st_label}"
            print(f"\n{'=' * 60}")
            print(f"{label} — {len(subset)} trades")
            print("=" * 60)
            stats = bucket_stats(subset)
            print(stats.to_string(index=False))
            stats.to_csv(OUTPUT_DIR / f"{label.lower()}.csv", index=False)

    # ── Summary table ──
    print(f"\n{'=' * 60}")
    print("SUMMARY — WR by bucket × direction")
    print("=" * 60)
    summary_rows = []
    for label in BUCKET_LABELS:
        row = {"Bucket": label}
        for direction in ["BUY", "SELL"]:
            subset = df[df["direction"] == direction]
            subset_b = subset.dropna(subset=["pd_score_recalc"])
            subset_b = subset_b[pd.cut(subset_b["pd_score_recalc"], bins=N_BUCKETS, labels=BUCKET_LABELS, include_lowest=True) == label]
            n = len(subset_b)
            if n > 0:
                row[f"{direction}_WR"] = round(subset_b["win"].sum() / n * 100, 1)
                gp = subset_b.loc[subset_b["pnl_pct"] > 0, "pnl_pct"].sum()
                gl = abs(subset_b.loc[subset_b["pnl_pct"] < 0, "pnl_pct"].sum())
                row[f"{direction}_PF"] = round(gp / gl, 2) if gl > 0 else "inf"
            else:
                row[f"{direction}_WR"] = np.nan
                row[f"{direction}_PF"] = np.nan
        summary_rows.append(row)
    summary = pd.DataFrame(summary_rows)
    print(summary.to_string(index=False))
    summary.to_csv(OUTPUT_DIR / "summary.csv", index=False)

    print(f"\nAll results saved to: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
