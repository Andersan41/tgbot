"""
analytics/live_performance.py — Live WR monitor for auto-switching.

Tracks recent trade outcomes and recommends strategy mode:
- WR > 45%: keep v2.5
- WR < 40%: switch to Confluence
- WR < 30%: crisis mode (cash or Confluence only)

Usage:
    python analytics/live_performance.py [--lookback 50]
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "signals.db"


def get_recent_trades(lookback: int = 50):
    """Get recent closed trades from data/signals.db."""
    if not DB.exists():
        print(f"ERROR: {DB} not found")
        return []

    conn = sqlite3.connect(str(DB))
    try:
        rows = conn.execute("""
            SELECT s.symbol, s.signal_type, s.close_price, 
                   so.close_price as exit_price, so.pnl_pct, so.closed_at
            FROM signal_outcomes so
            JOIN signals s ON s.id = so.signal_id
            WHERE so.status != 'OPEN' AND so.pnl_pct IS NOT NULL
            ORDER BY so.closed_at DESC
            LIMIT ?
        """, (lookback,)).fetchall()
        return rows
    finally:
        conn.close()


def analyze_performance(trades):
    """Analyze win rate and PnL of recent trades."""
    if not trades:
        return None

    total = len(trades)
    wins = sum(1 for t in trades if t[4] and t[4] > 0)
    losses = sum(1 for t in trades if t[4] and t[4] <= 0)
    wr = wins / total * 100 if total > 0 else 0

    total_pnl = sum(t[4] for t in trades if t[4])
    avg_pnl = total_pnl / total if total > 0 else 0

    # Per-symbol breakdown
    symbols = {}
    for t in trades:
        sym = t[0]
        if sym not in symbols:
            symbols[sym] = {"wins": 0, "losses": 0, "pnl": 0}
        if t[4] and t[4] > 0:
            symbols[sym]["wins"] += 1
        else:
            symbols[sym]["losses"] += 1
        symbols[sym]["pnl"] += t[4] or 0

    # Consecutive losses
    max_consec_loss = 0
    curr_consec = 0
    for t in reversed(trades):
        if t[4] and t[4] <= 0:
            curr_consec += 1
            max_consec_loss = max(max_consec_loss, curr_consec)
        else:
            curr_consec = 0

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "wr": wr,
        "total_pnl": total_pnl,
        "avg_pnl": avg_pnl,
        "symbols": symbols,
        "max_consec_loss": max_consec_loss,
    }


def recommend_mode(perf):
    """Recommend strategy mode based on performance."""
    if perf is None:
        return "CONFLUENCE", "No data -- default to safe mode"

    wr = perf["wr"]
    consec = perf["max_consec_loss"]

    if wr >= 55:
        return "HYBRID", f"WR {wr:.1f}% >= 55% -- full hybrid mode"
    elif wr >= 45:
        return "V25", f"WR {wr:.1f}% >= 45% -- keep v2.5"
    elif wr >= 40:
        return "CONFLUENCE", f"WR {wr:.1f}% < 45% -- switch to Confluence"
    elif consec >= 5:
        return "CONFLUENCE", f"WR {wr:.1f}% + {consec} consecutive losses -- Confluence mode"
    else:
        return "CONFLUENCE", f"WR {wr:.1f}% < 40% -- Confluence mode"


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Live performance monitor")
    parser.add_argument("--lookback", type=int, default=50, help="Number of recent trades to analyze")
    args = parser.parse_args()

    trades = get_recent_trades(args.lookback)
    perf = analyze_performance(trades)
    mode, reason = recommend_mode(perf)

    print("=" * 60)
    print("  LIVE PERFORMANCE MONITOR")
    print("=" * 60)

    if perf is None:
        print("\n  No closed trades found.")
    else:
        print(f"\n  Trades analyzed: {perf['total']}")
        print(f"  Win rate:        {perf['wr']:.1f}% ({perf['wins']}W / {perf['losses']}L)")
        print(f"  Avg PnL/trade:   {perf['avg_pnl']:+.3f}%")
        print(f"  Total PnL:       {perf['total_pnl']:+.3f}%")
        print(f"  Max consec loss: {perf['max_consec_loss']}")

        if perf["symbols"]:
            print(f"\n  Per-symbol:")
            for sym, data in sorted(perf["symbols"].items()):
                sym_wr = data["wins"] / (data["wins"] + data["losses"]) * 100 if (data["wins"] + data["losses"]) > 0 else 0
                print(f"    {sym:12s}  W={data['wins']:2d}  L={data['losses']:2d}  WR={sym_wr:.0f}%  PnL={data['pnl']:+.3f}%")

    print(f"\n  === RECOMMENDATION ===")
    print(f"  Mode: {mode}")
    print(f"  Reason: {reason}")
    print("=" * 60)


if __name__ == "__main__":
    main()
