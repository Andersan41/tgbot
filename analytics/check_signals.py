import sqlite3
conn = sqlite3.connect('data/signals.db')

# Check all signals with direction
rows = conn.execute("""
    SELECT id, symbol, timeframe, direction, entry_price, sl_price, tp_price, 
           status, created_at
    FROM signals 
    ORDER BY created_at DESC
""").fetchall()
print(f"Total signals: {len(rows)}")
print("\nAll signals:")
for r in rows:
    print(f"  #{r[0]:3d} {r[1]:12s} {r[2]:3s} {r[3]:4s} entry={r[4]:.4f} SL={r[5]:.4f} TP={r[6]:.4f} status={r[7]} created={r[8]}")

# Check signal outcomes
print("\nSignal outcomes:")
rows2 = conn.execute("""
    SELECT so.signal_id, s.symbol, s.direction, so.outcome, so.exit_price, so.pnl_pct, so.closed_at
    FROM signal_outcomes so
    JOIN signals s ON s.id = so.signal_id
    ORDER BY so.closed_at DESC
""").fetchall()
for r in rows2:
    print(f"  #{r[0]:3d} {r[1]:12s} {r[2]:4s} {r[3]:8s} exit={r[4]:.4f} pnl={r[5]:+.3f}% closed={r[6]}")

conn.close()
