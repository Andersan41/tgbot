"""Mark open positions as closed in database."""
import sqlite3
from datetime import datetime, timezone

DB_PATH = "data/signals.db"

def main():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Find open positions
    cursor.execute("""
        SELECT so.id, so.signal_id, s.symbol, s.signal_type, s.close_price
        FROM signal_outcomes so
        JOIN signals s ON s.id = so.signal_id
        WHERE so.status = 'OPEN'
    """)
    open_positions = cursor.fetchall()
    
    print("=== OPEN POSITIONS ===")
    for pos in open_positions:
        print(f"  #{pos[0]} | Signal #{pos[1]} | {pos[2]} {pos[3]} | Entry={pos[4]}")
    
    # Mark all as closed with pnl_pct=0 (manual close, no TP/SL hit)
    now = datetime.now(timezone.utc).isoformat()
    for pos in open_positions:
        outcome_id = pos[0]
        signal_id = pos[1]
        symbol = pos[2]
        direction = pos[3]
        entry = pos[4]
        
        # We don't know current price, so set pnl_pct=0 and status=MANUAL_CLOSE
        cursor.execute("""
            UPDATE signal_outcomes
            SET status = 'MANUAL_CLOSE',
                closed_at = ?,
                pnl_pct = 0.0,
                checked_at = ?
            WHERE id = ?
        """, (now, now, outcome_id))
        
        print(f"  OK Closed #{outcome_id} (Signal #{signal_id} {symbol} {direction})")
    
    conn.commit()
    conn.close()
    print(f"\nClosed {len(open_positions)} positions")

if __name__ == "__main__":
    main()
