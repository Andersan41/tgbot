"""
Comprehensive analysis script:
PART 1: Regime block analysis from log files
PART 2: Open trade status from signals.db
PART 3: SL calibration with ATR analysis
"""
import os
import re
import sys
import glob
import sqlite3
import asyncio
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
import numpy as np

ROOT = Path(r"E:\Projects\tgbot")
LOGS_DIR = ROOT / "logs"
DB_PATH = ROOT / "data" / "signals.db"

# ─── PART 1: REGIME BLOCK ANALYSIS ───────────────────────────────────────────

def collect_log_files():
    """Collect all log files: bot.log, logs.txt, logs.*.txt, and extracted zips."""
    files = []
    # Main logs
    for name in ["bot.log", "logs.txt"]:
        p = LOGS_DIR / name
        if p.exists():
            files.append(p)
    # Daily logs
    files.extend(sorted(LOGS_DIR.glob("logs.*.txt")))
    # Extracted zip logs
    for d in LOGS_DIR.glob("__extracted_*"):
        files.extend(sorted(d.glob("*.log")))
    return files


def parse_regime_blocks(log_files):
    """Parse all regime_block BLOCKED lines from log files."""
    # Pattern: [FUNNEL] SYMBOL TF → regime_block: BLOCKED (reason)
    pattern = re.compile(
        r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*\|?\s*\[FUNNEL\]\s+'
        r'(\S+/\S+)\s+(\S+)\s*→\s*regime_block:\s*BLOCKED\s*\(([^)]+)\)'
    )
    blocks = []
    for f in log_files:
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = pattern.search(line)
                    if m:
                        ts_str, symbol, tf, reason = m.groups()
                        blocks.append({
                            "timestamp": ts_str,
                            "symbol": symbol,
                            "timeframe": tf.strip(),
                            "reason": reason.strip(),
                            "file": f.name,
                        })
        except Exception as e:
            print(f"  Error reading {f}: {e}")
    return blocks


def analyze_apt_before_signal(log_files):
    """Check APT regime blocks around July 12 specifically."""
    apt_pattern = re.compile(
        r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*\|?\s*\[FUNNEL\]\s+'
        r'APT/USDT\s+(\S+)\s*→\s*regime_block:\s*BLOCKED'
    )
    apt_signal_pattern = re.compile(
        r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*\|?\s*.*(?:Signal sent|Signal:|SIGNAL).*APT/USDT'
    )

    apt_blocks_before_july12 = []
    apt_signals = []

    for f in log_files:
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = apt_pattern.search(line)
                    if m:
                        ts_str, tf = m.groups()
                        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                        apt_blocks_before_july12.append({
                            "timestamp": ts,
                            "timeframe": tf.strip(),
                            "file": f.name,
                        })
                    ms = apt_signal_pattern.search(line)
                    if ms:
                        ts_str = ms.group(1)
                        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                        apt_signals.append({
                            "timestamp": ts,
                            "line": line.strip()[:200],
                            "file": f.name,
                        })
        except Exception as e:
            pass

    return apt_blocks_before_july12, apt_signals


def part1_regime_block_analysis():
    print("=" * 80)
    print("PART 1: REGIME BLOCK ANALYSIS")
    print("=" * 80)

    log_files = collect_log_files()
    print(f"\nFound {len(log_files)} log files:")
    for f in log_files[:20]:
        print(f"  {f.name}")
    if len(log_files) > 20:
        print(f"  ... and {len(log_files)-20} more")

    blocks = parse_regime_blocks(log_files)
    print(f"\nTotal regime_block BLOCKED lines found: {len(blocks)}")

    # Count per symbol
    symbol_counts = Counter(b["symbol"] for b in blocks)
    print(f"\n{'Symbol':<20} {'Count':>8}  {'Pct':>7}")
    print("-" * 40)
    for sym, cnt in symbol_counts.most_common():
        pct = cnt / len(blocks) * 100 if blocks else 0
        print(f"{sym:<20} {cnt:>8}  {pct:>6.1f}%")

    # Count per timeframe
    tf_counts = Counter(b["timeframe"] for b in blocks)
    print(f"\nBy Timeframe:")
    for tf, cnt in tf_counts.most_common():
        print(f"  {tf}: {cnt}")

    # Count by date
    date_counts = Counter(b["timestamp"][:10] for b in blocks)
    print(f"\nBy Date:")
    for d in sorted(date_counts.keys()):
        print(f"  {d}: {date_counts[d]}")

    # Reason breakdown
    reason_counts = Counter(b["reason"] for b in blocks)
    print(f"\nBy Reason:")
    for reason, cnt in reason_counts.most_common():
        print(f"  {reason}: {cnt}")

    # APT analysis before signal
    print(f"\n--- APT/USDT BLOCK ANALYSIS (before winning signal) ---")
    apt_blocks, apt_signals = analyze_apt_before_signal(log_files)

    print(f"Total APT regime_block blocks: {len(apt_blocks)}")
    print(f"Total APT signal mentions: {len(apt_signals)}")

    if apt_signals:
        print(f"\nAPT Signals found:")
        for s in apt_signals:
            print(f"  {s['timestamp']} ({s['file']}): {s['line'][:150]}")

    # Show APT blocks on July 12
    apt_july12 = [b for b in apt_blocks if b["timestamp"].strftime("%Y-%m-%d") == "2026-07-12"]
    apt_july13 = [b for b in apt_blocks if b["timestamp"].strftime("%Y-%m-%d") == "2026-07-13"]

    print(f"\nAPT regime_block blocks on July 12: {len(apt_july12)}")
    if apt_july12:
        for b in apt_july12[:5]:
            print(f"  {b['timestamp']} {b['timeframe']}")
        if len(apt_july12) > 5:
            print(f"  ... and {len(apt_july12)-5} more")

    print(f"APT regime_block blocks on July 13: {len(apt_july13)}")
    if apt_july13:
        for b in apt_july13[:5]:
            print(f"  {b['timestamp']} {b['timeframe']}")
        if len(apt_july13) > 5:
            print(f"  ... and {len(apt_july13)-5} more")

    # APT signal was at 2026-07-13 01:03:25
    signal_time = datetime(2026, 7, 13, 1, 3, 25)
    apt_before_signal = [b for b in apt_blocks if b["timestamp"] < signal_time]
    apt_july12_before = [b for b in apt_before_signal
                         if b["timestamp"].strftime("%Y-%m-%d") == "2026-07-12"]

    print(f"\nAPT regime_block blocks BEFORE the SELL signal at {signal_time}:")
    print(f"  Total: {len(apt_before_signal)}")
    print(f"  On July 12: {len(apt_july12_before)}")
    print(f"  Last 5 blocks before signal:")
    for b in sorted(apt_before_signal, key=lambda x: x["timestamp"], reverse=True)[:5]:
        print(f"    {b['timestamp']} {b['timeframe']}")

    # KEY QUESTION: Was APT blocked by regime_block at any point before the signal?
    # The signal was at 01:03:25 on July 13. Let's check if there was a regime_block
    # on the same timeframe (1h) within 2 hours before
    apt_1h_before = [b for b in apt_before_signal
                     if b["timeframe"] == "1h"
                     and (signal_time - b["timestamp"]).total_seconds() < 7200]
    print(f"\nAPT 1h regime_block within 2h before signal: {len(apt_1h_before)}")
    if apt_1h_before:
        for b in apt_1h_before:
            print(f"  {b['timestamp']} — range regime block on 1h")

    return blocks


# ─── PART 2: OPEN TRADE STATUS ───────────────────────────────────────────────

def get_db_schema():
    """Print DB schema."""
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name")
    for name, sql in cur.fetchall():
        if sql:
            print(f"\nTABLE: {name}")
            print(sql)
    conn.close()


def part2_open_trades():
    print("\n" + "=" * 80)
    print("PART 2: OPEN TRADE STATUS")
    print("=" * 80)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    try:
        # signal_outcomes: closed_at IS NULL means OPEN
        cur.execute("""
            SELECT so.id, so.signal_id, so.closed_at, so.close_price,
                   so.pnl_pct, so.risk_pct, so.checked_at,
                   s.symbol, s.signal_type, s.close_price as entry_close,
                   s.sl, s.tp, s.created_at, s.timeframe,
                   s.entry_atr, s.entry_open, s.entry_mid, s.entry_bid, s.entry_ask,
                   s.mfe_pct, s.mae_pct
            FROM signal_outcomes so
            JOIN signals s ON so.signal_id = s.id
            WHERE so.closed_at IS NULL
        """)
        open_trades = cur.fetchall()
        print(f"\nOpen trades (closed_at IS NULL): {len(open_trades)}")
        for t in open_trades:
            d = dict(t)
            print(f"  Signal#{d['signal_id']} | {d['symbol']} {d['signal_type']} | "
                  f"Entry={d['entry_close']} SL={d['sl']} TP={d['tp']} | "
                  f"Created: {d['created_at']} | Risk: {d['risk_pct']}%")

        # All closed trades
        cur.execute("""
            SELECT so.id, so.signal_id, so.closed_at, so.close_price,
                   so.pnl_pct, so.risk_pct,
                   s.symbol, s.signal_type, s.close_price as entry_close,
                   s.sl, s.tp, s.created_at, s.timeframe,
                   s.entry_atr, s.entry_open, s.entry_mid,
                   s.mfe_pct, s.mae_pct
            FROM signal_outcomes so
            JOIN signals s ON so.signal_id = s.id
            WHERE so.closed_at IS NOT NULL
            ORDER BY so.closed_at DESC
        """)
        closed_trades = cur.fetchall()
        print(f"\nClosed trades: {len(closed_trades)}")
        for t in closed_trades:
            d = dict(t)
            print(f"  Signal#{d['signal_id']} | {d['symbol']} {d['signal_type']} | "
                  f"Entry={d['entry_close']} Close={d['close_price']} PnL={d['pnl_pct']}% | "
                  f"Created: {d['created_at']} Closed: {d['closed_at']}")

    except Exception as e:
        print(f"DB Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        conn.close()


# ─── PART 3: SL CALIBRATION ─────────────────────────────────────────────────

def part3_sl_calibration():
    print("\n" + "=" * 80)
    print("PART 3: SL CALIBRATION WITH ATR")
    print("=" * 80)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Get closed trades with their outcomes
    try:
        cur.execute("""
            SELECT s.symbol, s.signal_type as direction, s.close_price as entry_price,
                   s.sl, s.tp, s.created_at, s.timeframe,
                   so.close_price, so.pnl_pct, so.closed_at, so.risk_pct,
                   s.entry_atr, s.mfe_pct, s.mae_pct
            FROM signals s
            JOIN signal_outcomes so ON s.id = so.signal_id
            WHERE so.closed_at IS NOT NULL
            ORDER BY s.created_at DESC
        """)
        trades = cur.fetchall()
    except Exception as e:
        print(f"Query error: {e}")
        # Try alternate approach
        try:
            cur.execute("PRAGMA table_info(signals)")
            print("signals cols:", cur.fetchall())
            cur.execute("PRAGMA table_info(signal_outcomes)")
            print("signal_outcomes cols:", cur.fetchall())
        except:
            pass
        conn.close()
        return

    print(f"\nClosed trades found: {len(trades)}")
    if not trades:
        # Fallback: try all signals
        cur.execute("SELECT * FROM signals ORDER BY created_at DESC LIMIT 10")
        all_signals = cur.fetchall()
        print(f"\nAll signals (last 10):")
        for s in all_signals:
            print(f"  {dict(s)}")
        conn.close()
        return

    for t in trades:
        d = dict(t)
        print(f"\n--- {d['symbol']} {d['direction']} | Created: {d['created_at']} ---")
        print(f"  Entry (signal close_price): {d['entry_price']}")
        print(f"  SL: {d['sl']}")
        print(f"  TP: {d['tp']}")
        print(f"  Close: {d.get('close_price', 'N/A')}")
        print(f"  PnL: {d.get('pnl_pct', 'N/A')}%")
        print(f"  Risk%: {d.get('risk_pct', 'N/A')}%")
        print(f"  Closed at: {d.get('closed_at', 'N/A')}")
        print(f"  Entry ATR: {d.get('entry_atr', 'N/A')}")
        print(f"  MFE%: {d.get('mfe_pct', 'N/A')} | MAE%: {d.get('mae_pct', 'N/A')}")

        if d.get("entry_price") and d.get("sl"):
            entry = float(d["entry_price"])
            sl = float(d["sl"])
            tp = float(d["tp"]) if d.get("tp") else None

            sl_dist_pct = abs(sl - entry) / entry * 100
            print(f"  SL distance: {sl_dist_pct:.2f}%")

            if tp:
                tp_dist_pct = abs(float(tp) - entry) / entry * 100
                print(f"  TP distance: {tp_dist_pct:.2f}%")

    conn.close()

    # Now fetch historical 1h data and compute ATR
    print(f"\n--- Fetching 1h historical data for ATR calculation ---")

    # We need asyncio for exchange client
    sys.path.insert(0, str(ROOT))

    async def compute_atr_analysis(trades):
        from data.exchange_client import ExchangeClient
        client = ExchangeClient()
        await client.connect()

        symbols_needed = set()
        for t in trades:
            symbols_needed.add(dict(t)["symbol"])

        print(f"Symbols needing ATR: {symbols_needed}")

        atr_data = {}
        for sym in symbols_needed:
            df = await client.fetch_ohlcv(sym, "1h", limit=100)
            if df is not None and len(df) >= 14:
                # Compute ATR(14)
                high = df["high"]
                low = df["low"]
                close = df["close"]
                tr1 = high - low
                tr2 = abs(high - close.shift(1))
                tr3 = abs(low - close.shift(1))
                tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                atr = tr.rolling(14).mean()
                latest_atr = atr.iloc[-1]
                latest_close = close.iloc[-1]
                atr_data[sym] = {
                    "latest_1h_atr": latest_atr,
                    "latest_close": latest_close,
                    "atr_pct": latest_atr / latest_close * 100,
                    "df": df,
                }
                print(f"\n  {sym}:")
                print(f"    Latest 1h ATR(14): {latest_atr:.6f}")
                print(f"    Latest close: {latest_close:.6f}")
                print(f"    ATR as % of price: {atr_data[sym]['atr_pct']:.2f}%")
            else:
                print(f"  {sym}: insufficient data ({len(df) if df is not None else 0} candles)")

        # SL calibration analysis
        print(f"\n--- SL CALIBRATION RESULTS ---")
        print(f"{'Symbol':<12} {'Dir':<5} {'Entry':>10} {'SL':>10} {'SL_dist%':>9} {'ATR%':>7} "
              f"{'1.5xATR':>9} {'2.0xATR':>9} {'2.5xATR':>9} {'Close':>10} {'PnL%':>8}")
        print("-" * 130)

        for t in trades:
            d = dict(t)
            sym = d["symbol"]
            if sym not in atr_data:
                continue

            entry = float(d["entry_price"])
            sl = float(d["sl"])
            close_p = float(d["close_price"]) if d.get("close_price") else None
            pnl = float(d["pnl_pct"]) if d.get("pnl_pct") else None
            direction = d["direction"]
            atr = atr_data[sym]["latest_1h_atr"]

            sl_dist = abs(sl - entry) / entry * 100

            # Check each ATR multiplier
            # For a SELL: SL is above entry, so we need to check if high went above entry + mult*ATR
            # For a BUY: SL is below entry, so we need to check if low went below entry - mult*ATR
            # Since we're using the current ATR (not at time of trade), this is approximate

            atr_multiples = {}
            for mult in [1.5, 2.0, 2.5]:
                sl_atr = entry + (mult * atr * (-1 if direction == "BUY" else 1))
                atr_multiples[mult] = sl_atr

            # Check if close hit the hypothetical SL
            results = {}
            for mult in [1.5, 2.0, 2.5]:
                sl_atm = atr_multiples[mult]
                if direction == "BUY":
                    # SL is below entry
                    hit_sl = close_p <= sl_atm if close_p else "N/A"
                else:
                    # SL is above entry
                    hit_sl = close_p >= sl_atm if close_p else "N/A"
                results[mult] = (sl_atm, hit_sl)

            print(f"{sym:<12} {direction:<5} {entry:>10.4f} {sl:>10.4f} {sl_dist:>8.2f}% "
                  f"{atr_data[sym]['atr_pct']:>6.2f}% "
                  f"{results[1.5][0]:>10.4f}{'*' if results[1.5][1] else ' ':>1} "
                  f"{results[2.0][0]:>10.4f}{'*' if results[2.0][1] else ' ':>1} "
                  f"{results[2.5][0]:>10.4f}{'*' if results[2.5][1] else ' ':>1} "
                  f"{close_p if close_p else 'N/A':>10} "
                  f"{pnl if pnl else 'N/A':>8}")

        print(f"\n  * = SL would have been hit at that ATR multiple")

        # Detailed per-trade analysis with actual historical range
        print(f"\n--- DETAILED HOLD PERIOD ANALYSIS ---")
        for t in trades:
            d = dict(t)
            sym = d["symbol"]
            if sym not in atr_data:
                continue

            entry = float(d["entry_price"])
            sl = float(d["sl"])
            direction = d["direction"]
            created_at = d["created_at"]
            close_time = d.get("closed_at")
            atr = atr_data[sym]["latest_1h_atr"]

            df = atr_data[sym]["df"]
            if df is None or close_time is None:
                continue

            # Parse times — make tz-naive for comparison with tz-aware index
            if isinstance(created_at, str):
                entry_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            else:
                entry_time = created_at

            if isinstance(close_time, str):
                exit_time = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            else:
                exit_time = close_time

            # Convert to tz-naive UTC to match tz-aware index
            if hasattr(entry_time, 'tzinfo') and entry_time.tzinfo is not None:
                entry_time = entry_time.replace(tzinfo=None)
            if hasattr(exit_time, 'tzinfo') and exit_time.tzinfo is not None:
                exit_time = exit_time.replace(tzinfo=None)

            # Also strip timezone from df index for comparison
            df_compare = df.copy()
            df_compare.index = df_compare.index.tz_localize(None) if df_compare.index.tzinfo else df_compare.index

            # Filter candles in hold period
            hold_mask = (df_compare.index >= pd.Timestamp(entry_time)) & (df_compare.index <= pd.Timestamp(exit_time))
            hold_df = df_compare[hold_mask]

            if len(hold_df) == 0:
                print(f"\n  {sym} {direction} | No candles in hold period {entry_time} -> {exit_time}")
                continue

            max_high = hold_df["high"].max()
            min_low = hold_df["low"].min()
            max_drawdown_from_entry_high = abs(max_high - entry) / entry * 100
            max_drawdown_from_entry_low = abs(min_low - entry) / entry * 100

            print(f"\n  {sym} {direction} | Hold: {entry_time} -> {exit_time} ({len(hold_df)} bars)")
            print(f"    Entry: {entry:.4f} | Max High: {max_high:.4f} | Max Low: {min_low:.4f}")
            print(f"    Max excursion UP: {max_drawdown_from_entry_high:.2f}% | DOWN: {max_drawdown_from_entry_low:.2f}%")

            for mult in [1.5, 2.0, 2.5]:
                sl_distance_atr = mult * atr
                if direction == "BUY":
                    sl_atm = entry - sl_distance_atr
                    hit = min_low <= sl_atm
                    worst = min_low
                    worst_dist = abs(worst - entry) / entry * 100
                else:
                    sl_atm = entry + sl_distance_atr
                    hit = max_high >= sl_atm
                    worst = max_high
                    worst_dist = abs(worst - entry) / entry * 100

                hit_marker = "HIT" if hit else "SAFE"
                print(f"    {mult}x ATR ({sl_distance_atr:.4f}): SL={sl_atm:.4f} | "
                      f"Worst move: {worst_dist:.2f}% | {hit_marker}")

            actual_sl_dist = abs(sl - entry)
            print(f"    Actual SL: {sl:.4f} (distance={actual_sl_dist:.4f}, "
                  f"{actual_sl_dist/entry*100:.2f}%)")
            print(f"    Actual SL in ATR: {actual_sl_dist/atr:.2f}x ATR")

        pass  # cleanup

    # Run async analysis
    asyncio.run(compute_atr_analysis(trades))


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Project root: {ROOT}")
    print(f"Log dir: {LOGS_DIR}")
    print(f"DB: {DB_PATH}")
    print()

    blocks = part1_regime_block_analysis()
    part2_open_trades()
    part3_sl_calibration()

    print("\n" + "=" * 80)
    print("ANALYSIS COMPLETE")
    print("=" * 80)
