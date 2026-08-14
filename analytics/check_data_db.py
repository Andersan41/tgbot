import sqlite3
conn = sqlite3.connect('data/signals.db')
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("Tables:", tables)
for t in tables:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]
    count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"  {t}: {count} rows")
    if count > 0 and count < 100:
        rows = conn.execute(f"SELECT * FROM {t} LIMIT 3").fetchall()
        for r in rows:
            print(f"    {r[:8]}...")
conn.close()
