"""
backtest/download_history.py — CLI для загрузки полной 3-летней истории OHLCV.

Скачивает ТОЛЬКО базовый таймфрейм 15m в ohlcv_cache/{SYMBOL}_{market}_15m.parquet.
Все остальные TF (1h, 2h, 4h, 1d, 1w) строятся ресемплингом на лету
(backtest/resampler.py).

Usage:
    python -m backtest.download_history --symbols BTC/USDT,ETH/USDT,SOL/USDT,LINK/USDT --years 3
    python -m backtest.download_history --market-type swap --concurrency 2
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

from config.settings import get_active_symbols
from backtest.cache_ohlcv import (
    BASE_TIMEFRAME,
    OHLCV_CACHE_DIR,
    load_unified,
    unified_cache_path,
    update_history,
    _marker_exists,
)

DEFAULT_SYMBOLS_CLI = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT"]
COMPLETE_TOLERANCE_DAYS = 3


def _history_is_complete(
    symbol: str,
    market_type: str,
    years: int,
    timeframe: str = BASE_TIMEFRAME,
) -> bool:
    """True, если кэш помечен как полный (.done) и доходит почти до «сейчас».

    Полагаемся на sidecar-маркер ``.done`` (ставится при успешной полной
    загрузке), а не на глубину ``years``: BingX хранит 15m только ~6 мес,
    поэтому для него полный файл — это ~6 мес, а не 3 года.
    """
    path = unified_cache_path(symbol, market_type, timeframe)
    if not path.exists() or not _marker_exists(path):
        return False
    df = load_unified(symbol, market_type, timeframe)
    if df is None or df.empty:
        return False
    now = datetime.now(timezone.utc)
    tol = timedelta(days=COMPLETE_TOLERANCE_DAYS)
    return df.index.max() >= now - tol


async def download_one(
    symbol: str,
    years: int,
    market_type: str,
) -> dict:
    """Download/update history for one symbol. Returns a summary dict."""
    path = unified_cache_path(symbol, market_type, BASE_TIMEFRAME)
    if _history_is_complete(symbol, market_type, years):
        df = load_unified(symbol, market_type, BASE_TIMEFRAME)
        size_kb = path.stat().st_size / 1024
        logger.info(
            f"[SKIP] {symbol}: already complete "
            f"({len(df):,} candles, {df.index[0].date()} → {df.index[-1].date()}, "
            f"{size_kb:.1f} KB)"
        )
        return {"symbol": symbol, "skipped": True, "candles": len(df) if df is not None else 0,
                "min": str(df.index[0]) if df is not None and len(df) else "",
                "max": str(df.index[-1]) if df is not None and len(df) else "",
                "size_kb": size_kb}

    added = await update_history(symbol, timeframe=BASE_TIMEFRAME, market_type=market_type, years=years)
    df = load_unified(symbol, market_type, BASE_TIMEFRAME)
    size_kb = path.stat().st_size / 1024
    n = len(df) if df is not None else 0
    logger.info(
        f"[OK] {symbol}: {n:,} candles (+{added}), "
        f"{df.index[0]} → {df.index[-1]}, {size_kb:.1f} KB"
    )
    return {"symbol": symbol, "skipped": False, "candles": n,
            "min": str(df.index[0]) if df is not None and n else "",
            "max": str(df.index[-1]) if df is not None and n else "",
            "size_kb": size_kb}


async def download_symbols(
    symbols: list[str],
    years: int = 3,
    market_type: str = "swap",
    concurrency: int = 2,
) -> list[dict]:
    """Download history for all symbols with a concurrency semaphore."""
    OHLCV_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(concurrency)

    async def _worker(symbol: str) -> dict:
        async with sem:
            try:
                return await download_one(symbol, years, market_type)
            except Exception as e:
                logger.error(f"Failed to download {symbol}: {e}", exc_info=True)
                return {"symbol": symbol, "skipped": False, "error": str(e)}

    return list(await asyncio.gather(*(_worker(s) for s in symbols)))


def _print_summary(results: list[dict]) -> None:
    """Print a loguru INFO table: symbol, candles, date range, file size."""
    lines = [
        f"{'Symbol':<12} {'Status':<7} {'Candles':>10}  {'Range':<38} {'Size':>9}",
        "-" * 86,
    ]
    for r in results:
        if "error" in r:
            lines.append(f"{r['symbol']:<12} {'ERR':<7} {'—':>10}  {r['error']}")
            continue
        status = "SKIP" if r["skipped"] else "OK"
        rng = f"{r['min'][:16]} → {r['max'][:16]}"
        lines.append(
            f"{r['symbol']:<12} {status:<7} {r['candles']:>10,}  {rng:<38} "
            f"{r['size_kb']:>8.1f} KB"
        )
    logger.info("History download summary:\n" + "\n".join(lines))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="backtest.download_history",
        description="Download full 15m OHLCV history into ohlcv_cache/.",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="Comma-separated symbols (default: config.trading.symbols + LINK/USDT)",
    )
    parser.add_argument("--years", type=int, default=3, help="Depth of history in years")
    parser.add_argument(
        "--market-type", type=str, default="swap",
        help="Exchange market type (swap/future/spot) for the cache path",
    )
    parser.add_argument("--concurrency", type=int, default=2, help="Max concurrent symbols")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = list(dict.fromkeys([*get_active_symbols(), *DEFAULT_SYMBOLS_CLI]))
    logger.info(
        f"Downloading {len(symbols)} symbols ({args.years}y, {args.market_type}, "
        f"concurrency={args.concurrency}) into {OHLCV_CACHE_DIR}"
    )
    results = asyncio.run(
        download_symbols(symbols, years=args.years, market_type=args.market_type,
                         concurrency=args.concurrency)
    )
    _print_summary(results)


if __name__ == "__main__":
    main()