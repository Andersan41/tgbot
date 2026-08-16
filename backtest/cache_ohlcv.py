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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.exchange_client import exchange_client

# ── Legacy fixed-count cache (reports/abn/ohlcv_cache/) ──────────────────────
CACHE_DIR = Path(__file__).parent.parent / "reports" / "abn" / "ohlcv_cache"

# ── Unified OHLCV cache (ohlcv_cache/) ───────────────────────────────────────
# Базовый таймфрейм 15m хранится здесь. Остальные TF строятся ресемплингом.
OHLCV_CACHE_DIR = Path(__file__).parent.parent / "ohlcv_cache"
BASE_TIMEFRAME = "15m"
PARQUET_COMPRESSION = "zstd"

# Колонки, хранимые в unified-кэше (порядок фиксирован).
UNIFIED_OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

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


# ── Unified history cache (ohlcv_cache/{SYMBOL}_{market}_15m.parquet) ───────
# Только базовый таймфрейм 15m хранится на диске. 1h/2h/4h/1d/1w строятся
# ресемплингом на лету (backtest/resampler.py).

HISTORY_PAGE_SIZE = 998
HISTORY_MAX_BATCHES = 5000
HISTORY_MAX_EMPTY_BATCHES = 5
HISTORY_RETRY_ATTEMPTS = 5
HISTORY_RETRY_BACKOFF = [1, 2, 4, 8]  # секунды между попытками


def unified_cache_path(
    symbol: str,
    market_type: str,
    timeframe: str = BASE_TIMEFRAME,
) -> Path:
    """Return ohlcv_cache/{safe_symbol}_{market_type}_{timeframe}.parquet."""
    safe_symbol = symbol.replace("/", "_")
    return OHLCV_CACHE_DIR / f"{safe_symbol}_{market_type}_{timeframe}.parquet"


def _to_unified_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise a DataFrame into the unified on-disk format.

    Returns columns [timestamp, open, high, low, close, volume], sorted by
    timestamp ascending, deduped on timestamp. ``timestamp`` is tz-aware UTC.
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        if "timestamp" in df.columns:
            df = df.set_index("timestamp")
        df.index = pd.to_datetime(df.index, utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    cols = [c for c in UNIFIED_OHLCV_COLUMNS if c in df.columns]
    out = df[cols].copy()
    out = out.reset_index(drop=True)  # убрать индекс (может называться 'timestamp')
    out["timestamp"] = df.index
    out = out[["timestamp"] + cols]
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    out = out.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
    return out.reset_index(drop=True)


def load_unified(
    symbol: str,
    market_type: str,
    timeframe: str = BASE_TIMEFRAME,
) -> Optional[pd.DataFrame]:
    """Load a unified-cache parquet as a DataFrame with DatetimeIndex (UTC)."""
    path = unified_cache_path(symbol, market_type, timeframe)
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    if isinstance(df.index, pd.DatetimeIndex):
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")
    elif "timestamp" in df.columns:
        df = df.set_index("timestamp")
        df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    cols = [c for c in UNIFIED_OHLCV_COLUMNS if c in df.columns]
    if cols:
        df = df[cols]
    return df


def save_unified(
    df: pd.DataFrame,
    symbol: str,
    market_type: str,
    timeframe: str = BASE_TIMEFRAME,
) -> Path:
    """Save a DataFrame to the unified parquet (zstd, deduped, sorted)."""
    path = unified_cache_path(symbol, market_type, timeframe)
    OHLCV_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _to_unified_df(df).to_parquet(path, index=False, compression=PARQUET_COMPRESSION)
    return path


def _marker_path(path: Path) -> Path:
    """Sidecar-маркер «файл скачан целиком» (рядом с parquet)."""
    return path.with_suffix(path.suffix + ".done")


def _marker_exists(path: Path) -> bool:
    """True, если для кэша был создан маркер полной загрузки."""
    return _marker_path(path).exists()


def _write_marker(path: Path) -> None:
    """Пометить файл как полностью скачанный (не обрезанный/частичный)."""
    _marker_path(path).touch()


def _timeframe_to_ms(tf: str) -> int:
    """Convert a bot timeframe ('15m', '1h', '4h', '1d', '1w') to milliseconds."""
    tf = tf.strip().lower()
    unit = tf[-1]
    mult = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
    if unit not in mult:
        raise ValueError(f"Unsupported timeframe '{tf}'")
    return int(tf[:-1]) * mult[unit]


def _fmt_ms(ms: int) -> str:
    """Format a millisecond timestamp for log messages (UTC)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


async def _fetch_batch_with_retry(
    symbol: str,
    timeframe: str,
    end_time_ms: int,
    limit: int = HISTORY_PAGE_SIZE,
) -> Optional[pd.DataFrame]:
    """Fetch one page of history (ending before *end_time_ms*) with retry.

    Использует ``endTime`` (backward-пагинацию), а не ``since``: BingX
    не отдаёт глубокую историю через ``since``, но корректно работает
    с ``endTime``; Binance поддерживает оба. Батч возвращается отсортированным
    по возрастанию, последняя (самая свежая) свеча — на границе ``end_time``.

    Retries ONLY on ccxt.NetworkError / ccxt.RateLimitExceeded with delays
    1, 2, 4, 8 s — максимум ``HISTORY_RETRY_ATTEMPTS`` попыток на батч.
    Пустой ответ (None) не ретраится — он интерпретируется как конец истории.
    """
    import ccxt as ccxt_sync

    for attempt in range(1, HISTORY_RETRY_ATTEMPTS + 1):
        try:
            return await exchange_client.fetch_ohlcv(
                symbol, timeframe, limit=limit, drop_last=False,
                end_time=end_time_ms,
            )
        except (ccxt_sync.NetworkError, ccxt_sync.RateLimitExceeded) as e:
            if attempt >= HISTORY_RETRY_ATTEMPTS:
                logger.error(
                    f"fetch_history {symbol} {timeframe}: failed after "
                    f"{HISTORY_RETRY_ATTEMPTS} attempts at end={_fmt_ms(end_time_ms)} "
                    f"({e.__class__.__name__}: {e})"
                )
                raise
            delay = HISTORY_RETRY_BACKOFF[min(attempt - 1, len(HISTORY_RETRY_BACKOFF) - 1)]
            logger.warning(
                f"fetch_history {symbol} {timeframe}: batch attempt {attempt}/"
                f"{HISTORY_RETRY_ATTEMPTS} failed at end={_fmt_ms(end_time_ms)}, "
                f"retrying in {delay}s"
            )
            await asyncio.sleep(delay)
    return None


async def _fetch_backward(
    symbol: str,
    timeframe: str,
    end_ms: int,
    start_ms: int,
    page_size: int = HISTORY_PAGE_SIZE,
) -> tuple[Optional[pd.DataFrame], int]:
    """Backward-paginate candles from *end_ms* back to *start_ms*.

    Возвращает (combined DataFrame | None, число батчей). Каждый батч
    запрашивается с ``endTime`` и отсекается снизу до ``start_ms``.
    Стопы:
      - батч пуст (история закончилась) → break
      - последняя (самая старая) свеча батча <= ``start_ms`` → цель достигнута
      - ``end_time`` не продвинулся назад (защита от зацикливания) → break
    """
    all_dfs: list[pd.DataFrame] = []
    end = end_ms
    prev_oldest: int | None = None
    batch_num = 0

    while batch_num < HISTORY_MAX_BATCHES:
        batch_num += 1
        df = await _fetch_batch_with_retry(symbol, timeframe, end, page_size)

        if df is None or df.empty:
            break  # история закончилась

        # отсекаем всё, что старше целевого начала
        df = df[df.index >= pd.to_datetime(start_ms, unit="ms", utc=True)]
        if len(df) == 0:
            break

        oldest = int(df.index[0].timestamp() * 1000)
        newest = int(df.index[-1].timestamp() * 1000)

        # защита от зацикливания: батч не ушёл в прошлое
        if prev_oldest is not None and oldest >= prev_oldest:
            logger.warning(
                f"fetch_history {symbol} {timeframe}: no backward progress "
                f"(oldest={_fmt_ms(oldest)}), stopping"
            )
            break

        all_dfs.append(df)
        prev_oldest = oldest

        if batch_num % 10 == 0:
            logger.info(
                f"fetch_history {symbol} {timeframe}: {batch_num} batches, "
                f"{df.index[0]} → {df.index[-1]}, "
                f"{sum(len(d) for d in all_dfs):,} candles"
            )

        # цель достигнута — дошли до самого старого бара
        if oldest <= start_ms:
            break

        if len(df) < page_size:
            break  # биржа выдала меньше лимита — история закончилась

        # следующий батч — строго до самой старой свечи текущего
        end = oldest - 1
        await asyncio.sleep(0.05)  # rate limit guard

    if not all_dfs:
        return None, batch_num
    combined = pd.concat(all_dfs)
    combined = combined[~combined.index.duplicated(keep="first")].sort_index()
    return combined, batch_num


async def fetch_full_history(
    symbol: str,
    years: int = 3,
    timeframe: str = BASE_TIMEFRAME,
    market_type: str = "swap",
    page_size: int = HISTORY_PAGE_SIZE,
) -> None:
    """Download full history (``years`` years) for a symbol into ohlcv_cache/.

    Сохраняет только базовый таймфрейм 15m. Ничего не возвращает — пишет
    parquet напрямую. Идемпотентно: после завершения файл дедуплицируется
    и сортируется.

    Note: BingX отдаёт 15m только ~6 мес; Binance — 3+ года. Глубина реально
    ограничена тем, что отдаёт биржа. Полная загрузка помечается sidecar-маркером
    ``.done`` (см. ``_marker_path``), чтобы повторные запуски не скачивали заново
    и не откатывали кэш до частичного результата.
    """
    if timeframe != BASE_TIMEFRAME:
        logger.warning(
            f"fetch_full_history: storing only {BASE_TIMEFRAME}; "
            f"requested timeframe '{timeframe}' ignored"
        )

    await exchange_client.connect()

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = datetime.now(timezone.utc) - timedelta(days=365 * years)
    start_ms = int(start.timestamp() * 1000)

    combined, batch_num = await _fetch_backward(symbol, BASE_TIMEFRAME, now_ms, start_ms, page_size)
    if combined is None or combined.empty:
        logger.warning(f"fetch_full_history {symbol}: no data fetched ({batch_num} batches)")
        return

    save_unified(combined, symbol, market_type, BASE_TIMEFRAME)
    path = unified_cache_path(symbol, market_type, BASE_TIMEFRAME)
    _write_marker(path)
    logger.info(
        f"fetch_full_history {symbol}: {len(combined):,} candles "
        f"({combined.index[0]} → {combined.index[-1]}) in {batch_num} batches "
        f"→ {path.name} (marked complete)"
    )


async def update_history(
    symbol: str,
    timeframe: str = BASE_TIMEFRAME,
    market_type: str = "swap",
    years: int = 3,
    page_size: int = HISTORY_PAGE_SIZE,
) -> int:
    """Incrementally update the cached history for a symbol.

    Если файла нет — вызывает ``fetch_full_history`` (глубина ``years`` лет).
    Иначе докачивает свечи с ``max(timestamp) + 1`` (backward-pagination от
    ``endTime = now``, отсекая всё, что уже есть в кэше), мерджит с dedup и
    пересохраняет.

    Returns:
        Количество добавленных свечей (для логирования).
    """
    path = unified_cache_path(symbol, market_type, timeframe)
    if not path.exists():
        await fetch_full_history(symbol, years=years, timeframe=timeframe, market_type=market_type)
        df = load_unified(symbol, market_type, timeframe)
        return len(df) if df is not None else 0

    old = load_unified(symbol, market_type, timeframe)
    if old is None or len(old) == 0 or not _marker_exists(path):
        # файл пуст или скачан частично (до появления backward-pagination) —
        # полная перезагрузка вместо долива
        await fetch_full_history(symbol, years=years, timeframe=timeframe, market_type=market_type)
        df = load_unified(symbol, market_type, timeframe)
        return len(df) if df is not None else 0

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    max_old_ts = int(old.index.max().timestamp() * 1000)
    if max_old_ts >= now_ms - _timeframe_to_ms(timeframe):
        return 0

    if not exchange_client._markets_loaded:
        await exchange_client.connect()

    # докачиваем всё новее последнего ts в кэше: endTime=now, обрезаем до max_old+1
    new_df, _ = await _fetch_backward(
        symbol, timeframe, now_ms, max_old_ts + 1, page_size,
    )
    if new_df is None or new_df.empty:
        return 0

    # отсекаем то, что уже есть (между max_old+1ms и началом нового)
    new_df = new_df[new_df.index > pd.to_datetime(max_old_ts, unit="ms", utc=True)]
    if len(new_df) == 0:
        return 0

    merged = pd.concat([_to_unified_df(old), _to_unified_df(new_df)])
    merged = merged.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
    save_unified(merged, symbol, market_type, timeframe)

    logger.info(
        f"update_history {symbol} {timeframe}: +{len(new_df)} candles "
        f"({new_df.index[0]} → {new_df.index[-1]}), total {len(merged)}"
    )
    return len(new_df)


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
