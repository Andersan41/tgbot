"""Merge and summarize 360d backtest datasets for BTC/ETH/SOL/WIF."""
import pandas as pd
import os

frames = []
for f in ['ict_360d_btc_eth.csv', 'ict_360d_sol.csv', 'ict_360d_wif.csv']:
    path = f'reports/dataset/{f}'
    if os.path.exists(path):
        sub = pd.read_csv(path)
        frames.append(sub)
        print(f'{f}: {len(sub)} rows')

df = pd.concat(frames, ignore_index=True)
df.to_csv('reports/dataset/ict_360d_all.csv', index=False)
df.to_parquet('reports/dataset/ict_360d_all.parquet', index=False)
print(f'\nTotal: {len(df)} rows saved to ict_360d_all.csv')

COL = 'result'

def calc_stats(s):
    hits = s[COL].value_counts()
    sl = hits.get('HIT_SL', 0)
    tp = hits.get('HIT_TP', 0)
    exp = hits.get('EXPIRED', 0)
    n = len(s)
    wr = tp / (sl + tp) * 100 if (sl + tp) > 0 else 0
    avg_pnl = s['net_pnl_pct'].mean()
    return n, tp, sl, exp, wr, avg_pnl

print('\n=== Per-symbol ===')
for sym in sorted(df['symbol'].unique()):
    s = df[df['symbol'] == sym]
    n, tp, sl, exp, wr, avg_pnl = calc_stats(s)
    print(f'  {sym:12s}  N={n:5d}  TP={tp:4d}  SL={sl:4d}  EXP={exp:3d}  WR={wr:.1f}%  AvgPnL={avg_pnl:+.3f}%')

n, tp, sl, exp, wr, avg_pnl = calc_stats(df)
print(f'  {"OVERALL":12s}  N={n:5d}  TP={tp:4d}  SL={sl:4d}  EXP={exp:3d}  WR={wr:.1f}%  AvgPnL={avg_pnl:+.3f}%')

print('\n=== Setup type ===')
for st in sorted(df['setup_type'].dropna().unique()):
    s = df[df['setup_type'] == st]
    n, tp, sl, exp, wr, avg_pnl = calc_stats(s)
    print(f'  {str(st):14s}  N={n:5d}  WR={wr:.1f}%  AvgPnL={avg_pnl:+.3f}%')

print('\n=== Direction ===')
for d in ['BUY', 'SELL']:
    s = df[df['direction'] == d]
    n, tp, sl, exp, wr, avg_pnl = calc_stats(s)
    print(f'  {d:6s}  N={n:5d}  WR={wr:.1f}%  AvgPnL={avg_pnl:+.3f}%')

print('\n=== Risk engine pass ===')
for v in [True, False]:
    s = df[df['passed_risk'] == v]
    if len(s) == 0:
        continue
    n, tp, sl, exp, wr, avg_pnl = calc_stats(s)
    print(f'  passed={str(v):5s}  N={n:5d}  WR={wr:.1f}%  AvgPnL={avg_pnl:+.3f}%')

print('\n=== PD bucket ===')
if 'premium_discount_score' in df.columns:
    for pd_val in sorted(df['premium_discount_score'].dropna().unique()):
        s = df[df['premium_discount_score'] == pd_val]
        n, tp, sl, exp, wr, avg_pnl = calc_stats(s)
        print(f'  PD={pd_val:.1f}  N={n:5d}  WR={wr:.1f}%  AvgPnL={avg_pnl:+.3f}%')

print('\n=== Per-symbol x Direction ===')
for sym in sorted(df['symbol'].unique()):
    for d in ['BUY', 'SELL']:
        s = df[(df['symbol'] == sym) & (df['direction'] == d)]
        if len(s) == 0:
            continue
        n, tp, sl, exp, wr, avg_pnl = calc_stats(s)
        print(f'  {sym:12s} {d:4s}  N={n:5d}  WR={wr:.1f}%  AvgPnL={avg_pnl:+.3f}%')

print('\n=== Feature x Outcome crosstabs ===')
for feat in ['has_ob', 'has_fvg', 'has_bos', 'has_sweep', 'mtf_aligned', 'volume_above_avg']:
    if feat not in df.columns:
        continue
    print(f'\n  --- {feat} ---')
    for val in [True, False]:
        s = df[df[feat] == val]
        if len(s) == 0:
            continue
        n, tp, sl, exp, wr, avg_pnl = calc_stats(s)
        print(f'    {feat}={val!s:5s}  N={n:5d}  WR={wr:.1f}%  AvgPnL={avg_pnl:+.3f}%')

print('\n=== p_tp calibration (binned) ===')
if 'p_tp_rules' in df.columns:
    df['p_tp_bin'] = pd.cut(df['p_tp_rules'], bins=[0, 0.2, 0.3, 0.4, 0.5, 0.6, 1.0])
    for b in sorted(df['p_tp_bin'].dropna().unique()):
        s = df[df['p_tp_bin'] == b]
        n, tp, sl, exp, wr, avg_pnl = calc_stats(s)
        avg_pred = s['p_tp_rules'].mean()
        print(f'    p_tp {str(b):12s}  N={n:5d}  WR={wr:.1f}%  AvgPnL={avg_pnl:+.3f}%  AvgPred={avg_pred:.3f}  Gap={wr/100-avg_pred:+.3f}')
    df.drop(columns=['p_tp_bin'], inplace=True)
