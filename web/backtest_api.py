"""
web/backtest_api.py — Offline backtest API for the web dashboard.

REST endpoints:
    POST /api/backtest/run           — start a backtest job
    GET  /api/backtest/status/{id}   — poll job status
    GET  /api/backtest/list          — last 20 jobs (metrics only)
    GET  /api/backtest/result/{id}   — full result (metrics + equity + trades)

Jobs run in a thread pool so long backtests never block the bot event loop.
Data comes exclusively from the local 15m OHLCV cache (ohlcv_cache/), resampled
to the requested timeframe via backtest.resampler.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

from aiohttp import web
from loguru import logger

from config.settings import config, get_active_symbols

ALLOWED_TIMEFRAMES = ["15m", "1h", "2h", "4h", "1d", "1w"]
ALLOWED_MARKETS = ["swap", "spot", "future"]
MAX_JOBS = 50
CONSECUTIVE_FAILURES_ALERT_THRESHOLD = 3

# Job store persisted to disk so results survive both a page refresh and a
# server restart (the web dashboard dropdown reloads past runs from here).
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "backtest_history.json")


def _load_jobs() -> dict[str, dict]:
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            stored = json.load(f)
        return {j.get("id"): j for j in stored if j.get("id")}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


class _SafeFloatEncoder(json.JSONEncoder):
    def default(self, obj):
        import math
        if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
            return 0.0
        return super().default(obj)


def _save_jobs() -> None:
    jobs = sorted(
        _backtest_jobs.values(),
        key=lambda j: j.get("created_at", ""),
        reverse=True,
    )[:MAX_JOBS]
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(jobs, f, ensure_ascii=False, cls=_SafeFloatEncoder)
    except OSError:
        logger.exception("Failed to persist backtest history")


_backtest_jobs: dict[str, dict] = _load_jobs()
_consecutive_failures = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_job_id() -> str:
    return uuid.uuid4().hex[:12]


import math


def _sanitize_floats(obj):
    """Recursively replace inf/nan floats with 0.0 so JSON serialization never fails."""
    if isinstance(obj, float):
        return 0.0 if (math.isinf(obj) or math.isnan(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_floats(v) for v in obj]
    return obj


def _result_metrics_only(result: dict) -> dict:
    """Strip equity_curve / trades_log from a result dict (for /list)."""
    if not result:
        return {}
    keys = [
        "symbol", "timeframe", "total_trades", "wins", "losses",
        "winrate", "avg_pnl", "avg_net_pnl", "avg_rr", "profit_factor",
        "expectancy", "sharpe_ratio", "max_drawdown",
        "total_pnl_pct", "total_net_pnl_pct",
        "signals_generated", "signals_rejected",
    ]
    return {k: result.get(k) for k in keys if k in result}


def _result_to_dict(result) -> dict:
    """Convert a BacktestResult into a JSON-friendly dict.

    Adds ``equity_curve`` (cumulative net PnL %) and exposes the trades as
    ``trades_log`` (max 50 most recent trades).
    """
    data = asdict(result)
    trades = data.pop("trades", []) or []

    equity_curve: list[float] = []
    cum = 0.0
    for t in trades:
        cum += float(t.get("net_pnl_pct", 0.0) or 0.0)
        equity_curve.append(round(cum, 4))

    data["equity_curve"] = equity_curve
    # Sort by entry for display (engine emits trades in exit order — interleaved
    # when multiple positions are open simultaneously).
    data["trades_log"] = sorted(trades, key=lambda t: t.get("entry_index", 0) or 0)[-50:]
    return data


# ---------------------------------------------------------------------------
# Sync backtest runner (runs in a thread)
# ---------------------------------------------------------------------------

def _run_backtest_in_thread(
    symbol: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    market_type: str,
) -> dict:
    """Synchronous wrapper executed in the thread pool.

    Loads the 15m cache, resamples to ``timeframe``, slices the date window and
    runs BacktestEngine with the pre-built DataFrame (no exchange calls).
    """
    import pandas as pd

    from backtest.cache_ohlcv import BASE_TIMEFRAME, load_unified
    from backtest.resampler import resample_ohlcv

    df_15m = load_unified(symbol, market_type, BASE_TIMEFRAME)
    if df_15m is None or len(df_15m) == 0:
        raise FileNotFoundError(
            f"No local 15m cache for {symbol} ({market_type}). "
            f"Run: python -m backtest.download_history --symbols {symbol}"
        )

    if timeframe == BASE_TIMEFRAME:
        df = df_15m
    else:
        df = resample_ohlcv(df_15m, timeframe)

    start_ts = pd.Timestamp(start_date, tz="UTC")
    end_ts = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
    df = df[(df.index >= start_ts) & (df.index <= end_ts)]

    if len(df) < 100:
        raise ValueError(
            f"Not enough candles in window {start_date}→{end_date} for "
            f"{symbol} {timeframe} ({len(df)} candles)"
        )

    from backtest.engine import BacktestEngine

    engine = BacktestEngine(
        symbol=symbol,
        timeframe=timeframe,
        market_type=market_type,
        source="local",
    )
    result = asyncio.run(engine.run(df=df, htf_base=df_15m))
    return _result_to_dict(result)


# ---------------------------------------------------------------------------
# Background job runner
# ---------------------------------------------------------------------------

async def _run_job(job_id: str) -> None:
    global _consecutive_failures

    job = _backtest_jobs[job_id]
    job["status"] = "running"
    logger.info(
        f"[backtest] job {job_id} running: "
        f"{job['symbol']} {job['timeframe']} ({job['start_date']}→{job['end_date']}, {job['market_type']})"
    )

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            _run_backtest_in_thread,
            job["symbol"],
            job["timeframe"],
            job["start_date"],
            job["end_date"],
            job["market_type"],
        )
        job["status"] = "completed"
        job["result"] = result
        job["error"] = None
        _consecutive_failures = 0
        _save_jobs()
        logger.info(
            f"[backtest] job {job_id} completed: {result.get('total_trades', 0)} trades, "
            f"WR={result.get('winrate', 0)}%, PF={result.get('profit_factor', 0)}"
        )
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        _consecutive_failures += 1
        _save_jobs()
        logger.exception(f"[backtest] job {job_id} failed: {exc}")
        if _consecutive_failures >= CONSECUTIVE_FAILURES_ALERT_THRESHOLD:
            _consecutive_failures = 0
            try:
                from bot.notifier import send_error_alert
                await send_error_alert(
                    f"Backtest API failed {CONSECUTIVE_FAILURES_ALERT_THRESHOLD}x in a row. "
                    f"Last error: {exc}"
                )
            except Exception:
                logger.exception("Failed to send backtest error alert")


# ---------------------------------------------------------------------------
# Request handlers
# ---------------------------------------------------------------------------

async def api_backtest_run(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    symbol = str(data.get("symbol", "")).strip().upper()
    timeframe = str(data.get("timeframe", "")).strip().lower()
    start_date = str(data.get("start_date", "")).strip()
    end_date = str(data.get("end_date", "")).strip()
    market_type = str(data.get("market_type", "swap")).strip().lower()

    # --- Validation ---
    allowed_symbols = set(get_active_symbols())
    if symbol not in allowed_symbols:
        return web.json_response(
            {"error": f"Unknown symbol '{symbol}'. Allowed: {sorted(allowed_symbols)}"},
            status=400,
        )
    if timeframe not in ALLOWED_TIMEFRAMES:
        return web.json_response(
            {"error": f"Invalid timeframe '{timeframe}'. Allowed: {ALLOWED_TIMEFRAMES}"},
            status=400,
        )
    if market_type not in ALLOWED_MARKETS:
        return web.json_response(
            {"error": f"Invalid market_type '{market_type}'. Allowed: {ALLOWED_MARKETS}"},
            status=400,
        )

    from datetime import datetime as _dt
    try:
        start_dt = _dt.fromisoformat(start_date)
        end_dt = _dt.fromisoformat(end_date)
    except ValueError:
        return web.json_response(
            {"error": f"Invalid dates '{start_date}' / '{end_date}'. Use ISO format YYYY-MM-DD"},
            status=400,
        )
    if start_dt >= end_dt:
        return web.json_response(
            {"error": "start_date must be earlier than end_date"},
            status=400,
        )

    job_id = _new_job_id()
    job = {
        "id": job_id,
        "status": "queued",
        "symbol": symbol,
        "timeframe": timeframe,
        "start_date": start_date,
        "end_date": end_date,
        "market_type": market_type,
        "created_at": _now_iso(),
        "result": None,
        "error": None,
    }
    _backtest_jobs[job_id] = job

    # Trim job store to avoid unbounded growth
    if len(_backtest_jobs) > MAX_JOBS:
        for old_id in list(_backtest_jobs.keys())[:-MAX_JOBS]:
            _backtest_jobs.pop(old_id, None)
    _save_jobs()

    asyncio.create_task(_run_job(job_id))

    return web.json_response({"job_id": job_id, "status": "queued"}, status=200)


async def api_backtest_status(request: web.Request) -> web.Response:
    job_id = request.match_info.get("id", "")
    job = _backtest_jobs.get(job_id)
    if job is None:
        return web.json_response({"error": "Job not found"}, status=404)
    return web.json_response(_sanitize_floats(job))


async def api_backtest_list(request: web.Request) -> web.Response:
    jobs = sorted(
        _backtest_jobs.values(),
        key=lambda j: j.get("created_at", ""),
        reverse=True,
    )[:20]
    trimmed = []
    for j in jobs:
        entry = dict(j)
        entry["result"] = _result_metrics_only(j.get("result") or {})
        trimmed.append(entry)
    return web.json_response({"jobs": trimmed})


async def api_backtest_result(request: web.Request) -> web.Response:
    job_id = request.match_info.get("id", "")
    job = _backtest_jobs.get(job_id)
    if job is None:
        return web.json_response({"error": "Job not found"}, status=404)
    if job["status"] != "completed" or not job.get("result"):
        return web.json_response(
            {"error": f"Job is not completed (status={job['status']})"},
            status=404,
        )
    return web.json_response(_sanitize_floats({"job": job}))


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def setup_backtest_routes(app: web.Application) -> None:
    """Register backtest API routes on an aiohttp Application."""
    app.router.add_post("/api/backtest/run", api_backtest_run)
    app.router.add_get("/api/backtest/status/{id}", api_backtest_status)
    app.router.add_get("/api/backtest/list", api_backtest_list)
    app.router.add_get("/api/backtest/result/{id}", api_backtest_result)
    logger.info("Backtest API routes registered")