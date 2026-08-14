import sqlite3
conn = sqlite3.connect('data/signals.db')

# Get full schema
for t in ['signals', 'signal_outcomes']:
    cols = conn.execute(f"PRAGMA table_info({t})").fetchall()
    print(f"\n{t} schema:")
    for c in cols:
        print(f"  {c[1]:20s} {c[2]:15s} {'NOT NULL' if c[3] else 'NULLABLE'} default={c[4]}")

# Get all data
print("\n--- signals ---")
rows = conn.execute("SELECT * FROM signals ORDER BY id").fetchall()
for r in rows:
    print(f"  {r}")

print("\n--- signal_outcomes ---")
rows = conn.execute("SELECT * FROM signal_outcomes ORDER BY id").fetchall()
for r in rows:
    print(f"  {r}")

conn.close()
