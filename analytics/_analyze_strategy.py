"""Analyze current strategy weaknesses and design alternative."""
import pandas as pd
import os

# Load v2 data (filtered BUY-only, 360d)
frames = []
for f in ['ict_360d_v2_btc_eth.csv', 'ict_360d_v2_sol.csv']:
    path = f'reports/dataset/{f}'
    if os.path.exists(path):
        frames.append(pd.read_csv(path))
df = pd.concat(frames, ignore_index=True)

print("=" * 70)
print("  CURRENT STRATEGY ANALYSIS (BUY-only, 360d)")
print("=" * 70)

# 1. Overall stats
hits = df['result'].value_counts()
sl = hits.get('HIT_SL', 0)
tp = hits.get('HIT_TP', 0)
wr = tp / (sl + tp) * 100
print(f"\n  Overall: N={len(df)}  WR={wr:.1f}%  PnL={df['net_pnl_pct'].mean():+.3f}%")

# 2. Setup type breakdown
print("\n  By setup type:")
for st in ['continuation', 'reversal']:
    s = df[df['setup_type'] == st]
    if len(s) == 0:
        continue
    h = s['result'].value_counts()
    wr_s = h.get('HIT_TP', 0) / (h.get('HIT_SL', 0) + h.get('HIT_TP', 0)) * 100
    print(f"    {st:15s}  N={len(s):5d}  WR={wr_s:.1f}%  PnL={s['net_pnl_pct'].mean():+.3f}%")

# 3. By HTF alignment
print("\n  By MTF alignment:")
for v in [True, False]:
    s = df[df['mtf_aligned'] == v]
    if len(s) == 0:
        continue
    h = s['result'].value_counts()
    wr_s = h.get('HIT_TP', 0) / (h.get('HIT_SL', 0) + h.get('HIT_TP', 0)) * 100
    print(f"    mtf_aligned={str(v):5s}  N={len(s):5d}  WR={wr_s:.1f}%  PnL={s['net_pnl_pct'].mean():+.3f}%")

# 4. By structure trend
print("\n  By structure trend:")
for t in [1, -1, 0]:  # bullish, bearish, ranging
    s = df[df['structure_trend'] == t]
    if len(s) == 0:
        continue
    h = s['result'].value_counts()
    wr_s = h.get('HIT_TP', 0) / (h.get('HIT_SL', 0) + h.get('HIT_TP', 0)) * 100
    label = {1: 'bullish', -1: 'bearish', 0: 'ranging'}.get(t, str(t))
    print(f"    trend={label:10s}  N={len(s):5d}  WR={wr_s:.1f}%  PnL={s['net_pnl_pct'].mean():+.3f}%")

# 5. By ADX
print("\n  By ADX regime:")
for lo, hi, label in [(0, 20, 'weak (<20)'), (20, 30, 'moderate'), (30, 100, 'strong (>30)')]:
    s = df[(df['adx'] >= lo) & (df['adx'] < hi)]
    if len(s) == 0:
        continue
    h = s['result'].value_counts()
    wr_s = h.get('HIT_TP', 0) / (h.get('HIT_SL', 0) + h.get('HIT_TP', 0)) * 100
    print(f"    ADX {label:15s}  N={len(s):5d}  WR={wr_s:.1f}%  PnL={s['net_pnl_pct'].mean():+.3f}%")

# 6. By volume
print("\n  By volume:")
for v in [True, False]:
    s = df[df['volume_above_avg'] == v]
    if len(s) == 0:
        continue
    h = s['result'].value_counts()
    wr_s = h.get('HIT_TP', 0) / (h.get('HIT_SL', 0) + h.get('HIT_TP', 0)) * 100
    print(f"    vol_above_avg={str(v):5s}  N={len(s):5d}  WR={wr_s:.1f}%  PnL={s['net_pnl_pct'].mean():+.3f}%")

# 7. By OB
print("\n  By OB presence:")
for v in [True, False]:
    s = df[df['has_ob'] == v]
    if len(s) == 0:
        continue
    h = s['result'].value_counts()
    wr_s = h.get('HIT_TP', 0) / (h.get('HIT_SL', 0) + h.get('HIT_TP', 0)) * 100
    print(f"    has_ob={str(v):5s}  N={len(s):5d}  WR={wr_s:.1f}%  PnL={s['net_pnl_pct'].mean():+.3f}%")

# 8. By FVG
print("\n  By FVG presence:")
for v in [True, False]:
    s = df[df['has_fvg'] == v]
    if len(s) == 0:
        continue
    h = s['result'].value_counts()
    wr_s = h.get('HIT_TP', 0) / (h.get('HIT_SL', 0) + h.get('HIT_TP', 0)) * 100
    print(f"    has_fvg={str(v):5s}  N={len(s):5d}  WR={wr_s:.1f}%  PnL={s['net_pnl_pct'].mean():+.3f}%")

# 9. By session
print("\n  By session:")
for sess in ['asian', 'london', 'overlap', 'new_york', 'off_hours']:
    s = df[df['session'] == sess]
    if len(s) == 0:
        continue
    h = s['result'].value_counts()
    wr_s = h.get('HIT_TP', 0) / (h.get('HIT_SL', 0) + h.get('HIT_TP', 0)) * 100
    print(f"    {sess:12s}  N={len(s):5d}  WR={wr_s:.1f}%  PnL={s['net_pnl_pct'].mean():+.3f}%")

# 10. Combined filter: BOS + trend aligned + volume + OB
print("\n  === CONFLUENCE FILTERS ===")
# Best combo from data
filters = {
    'BOS only': (df['has_bos'] == True),
    'BOS + trend aligned': (df['has_bos'] == True) & (df['structure_bos_aligned'] == True),
    'BOS + trend + volume': (df['has_bos'] == True) & (df['structure_bos_aligned'] == True) & (df['volume_above_avg'] == True),
    'BOS + trend + OB': (df['has_bos'] == True) & (df['structure_bos_aligned'] == True) & (df['has_ob'] == True),
    'BOS + trend + volume + OB': (df['has_bos'] == True) & (df['structure_bos_aligned'] == True) & (df['volume_above_avg'] == True) & (df['has_ob'] == True),
    'BOS + ADX>25': (df['has_bos'] == True) & (df['adx'] > 25),
    'BOS + ADX>30': (df['has_bos'] == True) & (df['adx'] > 30),
    'BOS + volume': (df['has_bos'] == True) & (df['volume_above_avg'] == True),
}

for label, mask in filters.items():
    s = df[mask]
    if len(s) == 0:
        continue
    h = s['result'].value_counts()
    wr_s = h.get('HIT_TP', 0) / (h.get('HIT_SL', 0) + h.get('HIT_TP', 0)) * 100
    avg_pnl = s['net_pnl_pct'].mean()
    print(f"    {label:35s}  N={len(s):5d}  WR={wr_s:.1f}%  PnL={avg_pnl:+.3f}%")
