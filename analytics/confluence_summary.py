"""Confluence Mode: summary + out-of-sample test."""
import pandas as pd
import numpy as np
import os

frames = []
for f in ['ict_360d_conf_btc_eth.csv', 'ict_360d_conf_sol.csv']:
    path = f'reports/dataset/{f}'
    if os.path.exists(path):
        frames.append(pd.read_csv(path))
df = pd.concat(frames, ignore_index=True)

print("=" * 70)
print("  CONFLUENCE MODE — 360d BACKTEST RESULTS")
print("=" * 70)

hits = df['result'].value_counts()
sl = hits.get('HIT_SL', 0)
tp = hits.get('HIT_TP', 0)
exp = hits.get('EXPIRED', 0)
wr = tp / (sl + tp) * 100
print(f"\n  Total: N={len(df)}  WR={wr:.1f}%  AvgPnL={df['net_pnl_pct'].mean():+.3f}%")
print(f"  TP={tp}  SL={sl}  EXP={exp}")

print("\n  Per-symbol:")
for sym in sorted(df['symbol'].unique()):
    s = df[df['symbol'] == sym]
    h = s['result'].value_counts()
    sl_s = h.get('HIT_SL', 0)
    tp_s = h.get('HIT_TP', 0)
    wr_s = tp_s / (sl_s + tp_s) * 100 if (sl_s + tp_s) > 0 else 0
    print(f"    {sym:12s}  N={len(s):5d}  WR={wr_s:.1f}%  AvgPnL={s['net_pnl_pct'].mean():+.3f}%")

# Out-of-sample test: split by time (first 60% train, last 40% test)
print("\n  === OUT-OF-SAMPLE TEST ===")
df = df.sort_values('created_at').reset_index(drop=True)
split_idx = int(len(df) * 0.6)
train = df.iloc[:split_idx]
test = df.iloc[split_idx:]

print(f"  Train: N={len(train)}  Created: {train['created_at'].min()} to {train['created_at'].max()}")
print(f"  Test:  N={len(test)}  Created: {test['created_at'].min()} to {test['created_at'].max()}")

for name, sub in [("Train", train), ("Test", test)]:
    if len(sub) == 0:
        print(f"  {name}: no data")
        continue
    h = sub['result'].value_counts()
    sl_s = h.get('HIT_SL', 0)
    tp_s = h.get('HIT_TP', 0)
    wr_s = tp_s / (sl_s + tp_s) * 100 if (sl_s + tp_s) > 0 else 0
    avg_pnl = sub['net_pnl_pct'].mean()
    print(f"  {name:5s}  N={len(sub):5d}  WR={wr_s:.1f}%  AvgPnL={avg_pnl:+.3f}%")

# Compare with v2
print("\n  === COMPARISON: v2.5 vs Confluence Mode ===")
v2_path = 'reports/dataset/ict_360d_v2_all.csv'
if os.path.exists(v2_path):
    v2 = pd.read_csv(v2_path)
    h2 = v2['result'].value_counts()
    sl2 = h2.get('HIT_SL', 0)
    tp2 = h2.get('HIT_TP', 0)
    wr2 = tp2 / (sl2 + tp2) * 100
    pnl2 = v2['net_pnl_pct'].mean()

    print(f"  {'Metric':<25s} {'v2.5':>15s} {'Confluence':>15s} {'Delta':>12s}")
    print("  " + "-" * 67)
    print(f"  {'Setups':<25s} {len(v2):>15d} {len(df):>15d} {len(df)-len(v2):>+12d}")
    print(f"  {'Win Rate':<25s} {wr2:>14.1f}% {wr:>14.1f}% {wr-wr2:>+11.1f}%")
    print(f"  {'Avg PnL/setup':<25s} {pnl2:>14.3f}% {df['net_pnl_pct'].mean():>14.3f}% {df['net_pnl_pct'].mean()-pnl2:>+11.3f}%")

    # Estimated annual PnL (assuming 1h TF, 360 days)
    trades_per_year = len(df)  # in 360d
    annual_v2 = pnl2 * len(v2) / 360 * 365
    annual_conf = df['net_pnl_pct'].mean() * len(df) / 360 * 365
    print(f"\n  Estimated annual PnL (360d sample):")
    print(f"    v2.5:       {annual_v2:+.2f}%")
    print(f"    Confluence: {annual_conf:+.2f}%")
    print(f"    Delta:      {annual_conf-annual_v2:+.2f}%")
