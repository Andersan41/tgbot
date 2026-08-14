"""Compare v1 (no filters) vs v2 (SELL blocked, WIF BUY blocked, risk soft gates)."""
import pandas as pd
import os

def calc_stats(df):
    hits = df['result'].value_counts()
    sl = hits.get('HIT_SL', 0)
    tp = hits.get('HIT_TP', 0)
    exp = hits.get('EXPIRED', 0)
    n = len(df)
    wr = tp / (sl + tp) * 100 if (sl + tp) > 0 else 0
    avg_pnl = df['net_pnl_pct'].mean()
    return n, tp, sl, exp, wr, avg_pnl

print("=" * 70)
print("  BEFORE vs AFTER comparison")
print("=" * 70)

# Load v1 (old) and v2 (new) datasets
v1_path = 'reports/dataset/ict_360d_all.csv'
v2_files = ['ict_360d_v2_btc_eth.csv', 'ict_360d_v2_sol.csv']

frames_v2 = []
for f in v2_files:
    path = f'reports/dataset/{f}'
    if os.path.exists(path):
        frames_v2.append(pd.read_csv(path))

if os.path.exists(v1_path):
    v1 = pd.read_csv(v1_path)
    v2 = pd.concat(frames_v2, ignore_index=True) if frames_v2 else pd.DataFrame()

    print(f"\n{'Metric':<25s} {'BEFORE (v1)':>15s} {'AFTER (v2)':>15s} {'Delta':>10s}")
    print("-" * 65)

    n1, tp1, sl1, exp1, wr1, pnl1 = calc_stats(v1)
    n2, tp2, sl2, exp2, wr2, pnl2 = calc_stats(v2)

    print(f"{'Total setups':<25s} {n1:>15d} {n2:>15d} {n2-n1:>+10d}")
    print(f"{'Win rate':<25s} {wr1:>14.1f}% {wr2:>14.1f}% {wr2-wr1:>+9.1f}%")
    print(f"{'Avg PnL/setup':<25s} {pnl1:>14.3f}% {pnl2:>14.3f}% {pnl2-pnl1:>+9.3f}%")
    print(f"{'HIT_TP':<25s} {tp1:>15d} {tp2:>15d} {tp2-tp1:>+10d}")
    print(f"{'HIT_SL':<25s} {sl1:>15d} {sl2:>15d} {sl2-sl1:>+10d}")

    # Per-symbol comparison
    print(f"\n{'Per-symbol':<25s} {'WR (v1)':>10s} {'WR (v2)':>10s} {'PnL (v1)':>10s} {'PnL (v2)':>10s}")
    print("-" * 65)
    for sym in ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']:
        s1 = v1[v1['symbol'] == sym]
        s2 = v2[v2['symbol'] == sym]
        _, _, _, _, wr1s, pnl1s = calc_stats(s1) if len(s1) > 0 else (0,0,0,0,0,0)
        _, _, _, _, wr2s, pnl2s = calc_stats(s2) if len(s2) > 0 else (0,0,0,0,0,0)
        n1s = len(s1)
        n2s = len(s2)
        print(f"  {sym:<23s} {wr1s:>9.1f}% {wr2s:>9.1f}% {pnl1s:>+9.3f}% {pnl2s:>+9.3f}%  (N:{n1s}->{n2s})")

    # Setup type
    print(f"\n{'Setup type':<25s} {'WR (v1)':>10s} {'WR (v2)':>10s} {'N (v1)':>8s} {'N (v2)':>8s}")
    print("-" * 65)
    for st in ['continuation', 'reversal']:
        s1 = v1[v1['setup_type'] == st]
        s2 = v2[v2['setup_type'] == st]
        _, _, _, _, wr1s, _ = calc_stats(s1) if len(s1) > 0 else (0,0,0,0,0,0)
        _, _, _, _, wr2s, _ = calc_stats(s2) if len(s2) > 0 else (0,0,0,0,0,0)
        print(f"  {st:<23s} {wr1s:>9.1f}% {wr2s:>9.1f}% {len(s1):>8d} {len(s2):>8d}")

    # Direction (v2 is BUY-only)
    print(f"\n  v2 is BUY-only (SELL blocked)")
    s1_buy = v1[v1['direction'] == 'BUY']
    _, _, _, _, wr1b, pnl1b = calc_stats(s1_buy)
    print(f"  v1 BUY only:  WR={wr1b:.1f}%  PnL={pnl1b:+.3f}%  N={len(s1_buy)}")
    _, _, _, _, wr2, pnl2 = calc_stats(v2)
    print(f"  v2 BUY only:  WR={wr2:.1f}%  PnL={pnl2:+.3f}%  N={len(v2)}")
else:
    print("v1 dataset not found")
