import sqlite3
conn = sqlite3.connect('trades.db')
rows = conn.execute("""
SELECT symbol, direction, entry_price, exit_price, pnl_pct, 
       created_at, closed_at, status
FROM trades 
WHERE direction='sell' AND status='open'
ORDER BY created_at
""").fetchall()
for r in rows:
    print(f'{r[0]:12s} | {r[1]:4s} | entry={r[2]:.4f} | pnl={r[4]:+.3f}% | opened={r[6]} | status={r[7]}')
conn.close()
