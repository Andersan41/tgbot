"""
scripts/probe_history_depth.py — Probe available OHLCV history depth per symbol.

The self-learning feasibility spike needs to know how far back the exchange
actually serves candles for each symbol: "2-5 years" is realistic for BTC/ETH
but unproven for altcoins on BingX perpetual swaps (many are recent listings).

This script pages backward through `exchange_client.fetch_ohlcv_paginated` (which
walks `endTime` until the exchange stops returning full pages) and reports, per
symbol and timeframe, the earliest candle timestamp, the number of candles
returned, and the resulting usable depth in days/years. If the requested cap is
reached, the true history may be even deeper (flagged as ">=").

No trading logic, no DB writes — a pure data-availability probe.

Usage:
    python scripts/probe_history_depth.py                       # default 5 symbols, 1h, ~5y cap
    python scripts/probe_history_depth.py --symbols BTC/USDT,ETH/USDT
    python scripts/probe_history_depth.py --timeframes 1h,15m --years 5
    python scripts/probe_history_depth.py --symbols BTC/USDT --years 6
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.exchange_client import exchange_client

# Minutes per candle for the timeframes we probe (extend as needed).
_TF_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720,
    "1d": 1440, "1w": 10080,
}

DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT", "LINK/USDT"]
OUT_PATH = Path(__file__).parent.parent / "reports" / "feasibility" / "history_depth.json"


def _candles_for_years(timeframe: str, years: float) -> int:
    """Number of candles that would cover `years` at the given timeframe."""
    minutes = _TF_MINUTES[timeframe]
    total_minutes = years * 365.25 * 24 * 60
    return int(total_minutes / minutes)


async def probe_one(symbol: str, timeframe: str, cap_candles: int) -> dict:
    """Page back as far as available (up to cap) and summarise the depth."""
    t0 = time.time()
    df = await exchange_client.fetch_ohlcv_paginated(
        symbol, timeframe, total_limit=cap_candles, page_size=998,
    )
    elapsed = time.time() - t0

    if df is None or df.empty:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "available": False,
            "candles": 0,
            "earliest": None,
            "latest": None,
            "depth_days": 0.0,
            "depth_years": 0.0,
            "cap_reached": False,
            "fetch_seconds": round(elapsed, 1),
        }

    earliest = df.index[0]
    latest = df.index[-1]
    depth_days = (latest - earliest).total_seconds() / 86400
    # A returned count within ~1% of the cap means the paginator likely stopped
    # at our request ceiling, not at the true start of history.
    cap_reached = len(df) >= cap_candles * 0.99

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "available": True,
        "candles": int(len(df)),
        "earliest": str(earliest),
        "latest": str(latest),
        "depth_days": round(depth_days, 1),
        "depth_years": round(depth_days / 365.25, 2),
        "cap_reached": bool(cap_reached),
        "fetch_seconds": round(elapsed, 1),
    }


def _print_table(results: list[dict]) -> None:
    print("\n" + "=" * 78)
    print("  HISTORY DEPTH PROBE — per symbol / timeframe (BingX)")
    print("=" * 78)
    header = f"  {'Symbol':<12} {'TF':<5} {'Candles':>9} {'Earliest':<20} {'Depth':>12}"
    print(header)
    print("  " + "-" * 74)
    for r in results:
        if not r["available"]:
            print(f"  {r['symbol']:<12} {r['timeframe']:<5} {'—':>9} {'NOT AVAILABLE':<20} {'—':>12}")
            continue
        depth = f"{r['depth_years']:.2f}y"
        if r["cap_reached"]:
            depth = ">=" + depth  # true history may be deeper than the probe cap
        earliest = r["earliest"][:19]
        print(f"  {r['symbol']:<12} {r['timeframe']:<5} {r['candles']:>9} {earliest:<20} {depth:>12}")
    print("  " + "-" * 74)
    print("  Note: '>=' means the probe cap was hit — real history may be deeper.")


async def run(symbols: list[str], timeframes: list[str], years: float) -> list[dict]:
    await exchange_client.connect()
    results: list[dict] = []
    total = len(symbols) * len(timeframes)
    n = 0
    for symbol in symbols:
        for timeframe in timeframes:
            n += 1
            cap = _candles_for_years(timeframe, years)
            print(f"  [{n}/{total}] {symbol} {timeframe} (cap={cap} candles ~{years}y)...",
                  end=" ", flush=True)
            try:
                r = await probe_one(symbol.strip(), timeframe.strip(), cap)
            except Exception as e:  # noqa: BLE001 — probe must not abort the whole sweep
                r = {
                    "symbol": symbol, "timeframe": timeframe, "available": False,
                    "candles": 0, "earliest": None, "latest": None,
                    "depth_days": 0.0, "depth_years": 0.0, "cap_reached": False,
                    "error": str(e),
                }
                print(f"ERROR: {e}")
            else:
                if r["available"]:
                    print(f"{r['candles']} candles, {r['depth_years']:.2f}y "
                          f"({r['fetch_seconds']:.0f}s)")
                else:
                    print("not available")
            results.append(r)
            await asyncio.sleep(0.5)
    await exchange_client.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe available OHLCV history depth per symbol")
    parser.add_argument("--symbols", type=str, default=None,
                        help="comma-separated, default 5 majors")
    parser.add_argument("--timeframes", type=str, default="1h,15m",
                        help="comma-separated timeframes to probe (default: 1h,15m)")
    parser.add_argument("--years", type=float, default=5.0,
                        help="probe cap expressed in years of history (default: 5)")
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else DEFAULT_SYMBOLS
    timeframes = args.timeframes.split(",")
    for tf in timeframes:
        if tf.strip() not in _TF_MINUTES:
            raise SystemExit(f"Unsupported timeframe '{tf}'. Known: {sorted(_TF_MINUTES)}")

    results = asyncio.run(run(symbols, timeframes, args.years))
    _print_table(results)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"years_cap": args.years, "results": results}, f, indent=2)
    print(f"\n  Saved: {OUT_PATH}")


if __name__ == "__main__":
    main()
