"""
OHLCV cache layer for deterministic A/B/n backtests.

Fetches data once from BingX, saves to disk as parquet files.
Subsequent runs load from cache instead of fetching live.

Usage:
    # Cache all symbols (3900 candles each)
    python -m backtest.cache_ohlcv

    # Cache specific symbols
    python -m backtest.cache_ohlcv --symbols BTC/USDT,ETH/USDT

    # Use cached data in run_abn (monkey-patches exchange_client)
    # Just import and call patch_exchange() before running presets
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.exchange_client import exchange_client

CACHE_DIR = Path(__file__).parent.parent / "reports" / "abn" / "ohlcv_cache"

DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "XRP/USDT", "SOL/USDT", "DOGE/USDT",
    "AVAX/USDT", "LINK/USDT", "ADA/USDT", "DOT/USDT", "UNI/USDT",
    "NEAR/USDT", "APT/USDT", "ARB/USDT", "OP/USDT", "SUI/USDT",
    "INJ/USDT", "WIF/USDT", "FLOKI/USDT", "FIL/USDT", "GRT/USDT",
]

TIMEFRAME = "1h"
CONFIRM_TIMEFRAME = "15m"
DEFAULT_CANDLES = 3900
# For 15m confirm TF, we need more candles to cover the same time period
# 3900 candles on 1h = ~162 days; 162 days on 15m = ~162 * 24 * 4 = 15,552 candles
# BingX max per request is 998, so we need ~16 pages
CONFIRM_CANDLES = 15552


def _cache_path(symbol: str, timeframe: str, candles: int) -> Path:
    """Return cache file path for a given symbol/timeframe/candles."""
    safe_symbol = symbol.replace("/", "_")
    return CACHE_DIR / f"{safe_symbol}_{timeframe}_{candles}.parquet"


def load_cached(symbol: str, timeframe: str, candles: int) -> pd.DataFrame | None:
    """Load cached OHLCV data if available.

    Raises ValueError if the cached file exists but does not contain a
    DatetimeIndex — this means the file was written with ``index=False``
    and is therefore invalid (time-alignment cannot be guaranteed).
    """
    path = _cache_path(symbol, timeframe, candles)
    if path.exists():
        df = pd.read_parquet(path)
        if not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError(
                f"Cache file {path.name} has {type(df.index).__name__} index "
                f"instead of DatetimeIndex. Delete the file and re-cache."
            )
        if len(df) >= candles * 0.9:  # Allow 10% tolerance
            return df
    return None


def save_cached(symbol: str, timeframe: str, candles: int, df: pd.DataFrame) -> None:
    """Save OHLCV data to cache with DatetimeIndex preserved."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol, timeframe, candles)
    df.to_parquet(path, index=True)


# ── Max-history cache ──────────────────────────────────────────────────────
# The fixed-count cache above keys files by requested candle count and requires
# >=90% of that count on load. For multi-year training the usable depth varies
# per symbol (some altcoins are recent listings — see scripts/probe_history_depth.py),
# so a fixed request would perpetually cache-miss on shallow symbols. The max cache
# stores "as deep as the exchange served" under a stable `<sym>_<tf>_max.parquet`
# name and accepts whatever was stored, validated by DatetimeIndex rather than count.


def _max_cache_path(symbol: str, timeframe: str) -> Path:
    """Return the max-history cache file path for a symbol/timeframe."""
    safe_symbol = symbol.replace("/", "_")
    return CACHE_DIR / f"{safe_symbol}_{timeframe}_max.parquet"


def load_cached_max(symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Load the deepest-available cached OHLCV series if present.

    Unlike ``load_cached`` there is no candle-count threshold — the file holds
    whatever depth the exchange served at cache time. Raises ValueError if the
    file exists but lacks a DatetimeIndex (written with ``index=False``).
    """
    path = _max_cache_path(symbol, timeframe)
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError(
            f"Cache file {path.name} has {type(df.index).__name__} index "
            f"instead of DatetimeIndex. Delete the file and re-cache."
        )
    return df


def save_cached_max(symbol: str, timeframe: str, df: pd.DataFrame) -> None:
    """Save the deepest-available OHLCV series with DatetimeIndex preserved."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(_max_cache_path(symbol, timeframe), index=True)


async def fetch_and_cache_max(symbol: str, timeframe: str, cap_candles: int) -> pd.DataFrame | None:
    """Fetch as much history as the exchange serves (up to cap) and cache it.

    Reuses the existing cached file when present (no re-fetch). ``cap_candles``
    is only a safety ceiling; the paginator stops earlier when the exchange runs
    out of history.
    """
    cached = load_cached_max(symbol, timeframe)
    if cached is not None:
        print(f"  [CACHE HIT] {symbol} {timeframe} max → {len(cached)} rows "
              f"({cached.index[0]} → {cached.index[-1]})")
        return cached

    print(f"  [FETCH] {symbol} {timeframe} max (cap={cap_candles})...", end=" ", flush=True)
    t0 = time.time()
    df = await exchange_client.fetch_ohlcv_paginated(
        symbol, timeframe, total_limit=cap_candles, page_size=998,
    )
    elapsed = time.time() - t0
    if df is None or df.empty:
        print(f"NO DATA ({elapsed:.1f}s)")
        return None
    print(f"{len(df)} rows, {df.index[0]} → {df.index[-1]} ({elapsed:.1f}s)")

    save_cached_max(symbol, timeframe, df)
    return df


def validate_time_overlap(
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    min_overlap_pct: float = 0.90,
) -> None:
    """Validate that 1h and 15m DataFrames have sufficient temporal overlap.

    Raises ValueError if the overlap is below *min_overlap_pct* of the
    1h time range.

    The overlap is computed as:
        overlap_hours = length of intersection([1h_start, 1h_end], [15m_start, 15m_end])
        overlap_pct  = overlap_hours / (1h_end - 1h_start in hours)
    """
    if not isinstance(df_1h.index, pd.DatetimeIndex) or not isinstance(df_15m.index, pd.DatetimeIndex):
        raise ValueError("Both DataFrames must have DatetimeIndex for overlap validation")

    h_start, h_end = df_1h.index.min(), df_1h.index.max()
    m_start, m_end = df_15m.index.min(), df_15m.index.max()

    overlap_start = max(h_start, m_start)
    overlap_end = min(h_end, m_end)

    if overlap_start >= overlap_end:
        raise ValueError(
            f"No temporal overlap between 1h ({h_start} → {h_end}) and "
            f"15m ({m_start} → {m_end}) data"
        )

    total_hours = (h_end - h_start).total_seconds() / 3600
    overlap_hours = (overlap_end - overlap_start).total_seconds() / 3600
    overlap_pct = overlap_hours / total_hours if total_hours > 0 else 0.0

    if overlap_pct < min_overlap_pct:
        raise ValueError(
            f"Temporal overlap {overlap_pct:.1%} is below threshold {min_overlap_pct:.0%}. "
            f"1h range: {h_start} → {h_end} ({total_hours:.0f}h). "
            f"15m range: {m_start} → {m_end}. "
            f"Overlap: {overlap_start} → {overlap_end} ({overlap_hours:.0f}h = {overlap_pct:.1%})"
        )


async def fetch_and_cache(symbol: str, timeframe: str, candles: int) -> pd.DataFrame:
    """Fetch OHLCV from exchange (with pagination) and cache to disk."""
    # Check cache first
    cached = load_cached(symbol, timeframe, candles)
    if cached is not None:
        print(f"  [CACHE HIT] {symbol} {timeframe} {candles}c → {len(cached)} rows")
        return cached

    # Fetch with pagination
    print(f"  [FETCH] {symbol} {timeframe} {candles}c...", end=" ", flush=True)
    t0 = time.time()
    df = await exchange_client.fetch_ohlcv_paginated(
        symbol, timeframe, total_limit=candles, page_size=998,
    )
    elapsed = time.time() - t0
    print(f"{len(df)} rows ({elapsed:.1f}s)")

    # Save to cache
    save_cached(symbol, timeframe, candles, df)
    return df


def patch_exchange_for_cache(symbol: str, timeframe: str, candles: int):
    """
    Monkey-patch exchange_client.fetch_ohlcv to use cached data.
    Call this before creating BacktestEngine.
    """
    cached = load_cached(symbol, timeframe, candles)
    if cached is None:
        raise FileNotFoundError(f"No cache for {symbol} {timeframe} {candles}")

    _orig_fetch = exchange_client.fetch_ohlcv

    async def _fetch_cached(s, tf, limit=200):
        if s == symbol and tf == timeframe:
            return cached.iloc[-limit:].copy() if limit < len(cached) else cached.copy()
        return await _orig_fetch(s, tf, limit)

    exchange_client.fetch_ohlcv = _fetch_cached
    return _orig_fetch


async def cache_all(symbols: list[str], timeframe: str, candles: int, confirm_tf: str = None, confirm_candles: int = None):
    """Fetch and cache OHLCV for all symbols."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Connect to exchange first
    await exchange_client.connect()

    print(f"Caching {len(symbols)} symbols × {candles} candles ({timeframe})")
    if confirm_tf:
        print(f"Also caching {confirm_tf} data ({confirm_candles} candles)")
    print(f"Cache dir: {CACHE_DIR}")

    total_rows = 0
    for i, symbol in enumerate(symbols, 1):
        print(f"\n[{i}/{len(symbols)}] {symbol}")
        # Cache primary timeframe
        df = await fetch_and_cache(symbol, timeframe, candles)
        if df is not None and not df.empty:
            total_rows += len(df)
        
        # Cache confirmation timeframe if specified
        if confirm_tf and confirm_candles:
            df_confirm = await fetch_and_cache(symbol, confirm_tf, confirm_candles)
            if df_confirm is not None and not df_confirm.empty:
                total_rows += len(df_confirm)
        
        await asyncio.sleep(0.5)  # Rate limit

    print(f"\nDone: {total_rows:,} total rows cached")
    await exchange_client.close()


async def cache_all_max(symbols: list[str], timeframe: str, cap_candles: int):
    """Fetch and cache the deepest-available history for all symbols.

    Used by the self-learning feasibility spike to build a multi-year corpus.
    ``cap_candles`` is a safety ceiling (e.g. ~5y of 1h ≈ 45000); each symbol
    keeps whatever depth the exchange actually served.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    await exchange_client.connect()

    print(f"Caching MAX history for {len(symbols)} symbols ({timeframe}), cap={cap_candles} candles")
    print(f"Cache dir: {CACHE_DIR}")

    total_rows = 0
    for i, symbol in enumerate(symbols, 1):
        print(f"\n[{i}/{len(symbols)}] {symbol}")
        df = await fetch_and_cache_max(symbol, timeframe, cap_candles)
        if df is not None and not df.empty:
            total_rows += len(df)
        await asyncio.sleep(0.5)  # Rate limit

    print(f"\nDone: {total_rows:,} total rows cached")
    await exchange_client.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Cache OHLCV data for deterministic backtests")
    parser.add_argument("--symbols", type=str, default=None)
    parser.add_argument("--candles", type=int, default=DEFAULT_CANDLES)
    parser.add_argument("--timeframe", type=str, default=TIMEFRAME)
    parser.add_argument("--confirm-tf", type=str, default=CONFIRM_TIMEFRAME,
                        help="Confirmation timeframe to cache (default: 15m)")
    parser.add_argument("--confirm-candles", type=int, default=CONFIRM_CANDLES,
                        help="Number of candles for confirmation timeframe")
    parser.add_argument("--max-history", action="store_true",
                        help="Cache the deepest-available history per symbol "
                             "(stored as <sym>_<tf>_max.parquet); --candles is the safety cap")
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else DEFAULT_SYMBOLS
    if args.max_history:
        # For --max-history, --candles acts as the paging safety ceiling.
        cap = args.candles if args.candles > DEFAULT_CANDLES else 45000
        asyncio.run(cache_all_max(symbols, args.timeframe, cap))
    else:
        asyncio.run(cache_all(symbols, args.timeframe, args.candles, args.confirm_tf, args.confirm_candles))
