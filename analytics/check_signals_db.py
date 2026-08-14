import sqlite3
conn = sqlite3.connect('signals.db')
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("Tables:", tables)
for t in tables:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]
    count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"  {t}: {count} rows, cols={cols[:10]}...")

# Check for open SELL positions
if 'positions' in tables:
    rows = conn.execute("""
        SELECT symbol, direction, entry_price, pnl_pct, created_at, status
        FROM positions 
        WHERE status='open'
        ORDER BY created_at
    """).fetchall()
    print(f"\nOpen positions: {len(rows)}")
    for r in rows:
        print(f"  {r[0]:12s} | {r[1]:4s} | entry={r[2]:.4f} | pnl={r[3]:+.3f}% | opened={r[4]} | status={r[5]}")
conn.close()
