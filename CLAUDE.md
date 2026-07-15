# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram trading-signal bot: it scans crypto markets on a schedule, runs an ICT-based
(Inner Circle Trader) signal pipeline, and posts BUY/SELL setups to a Telegram channel. It also
serves a live web dashboard, tracks trade outcomes, and ships an extensive backtesting/analytics
suite. Comments and docs are largely in Russian; keep new repository artifacts in proper English.

`AGENTS.md` is the authoritative quick-reference (setup, gotchas, signal logic) — read it first.
`plan/` and `reports/system_map/SYSTEM_ARCHITECTURE.md` hold deep architecture docs; update those
when behavior changes rather than duplicating here.

## Commands

```bash
# Run the bot (loads .env, acquires .trading_bot.lock, starts polling + scheduler + web)
python main.py

# Docker
docker-compose build && docker-compose up -d && docker-compose logs -f

# Tests (CI runs: pytest -v --tb=short)
pytest -v                       # all
pytest tests/test_signal.py -v  # one file
pytest tests/test_signal.py::test_name -v   # one test

# Backtest (full live-parity engine)
python -m backtest.engine BTC/USDT 1h 336                  # console
python -m backtest.engine BTC/USDT 1h 336 --telegram       # post to TG
python -m backtest.engine BTC/USDT 1h 336 --market future
python -m backtest.engine BTC/USDT 1h 336 --preset <name>
```

Deps: `pip install -r requirements.txt -r requirements-dev.txt` (Python 3.11). There is **no**
`pip install -e .` — every test and entry script prepends the repo root to `sys.path`. `pytest.ini`
sets `asyncio_mode = auto`; async tests use `@pytest.mark.asyncio`.

## Architecture

### Startup (`main.py`)
Acquires a PID lock file, inits the async SQLite DB, connects the exchange (ccxt), optionally starts
the web dashboard, builds the `python-telegram-bot` `Application` (polling), starts the
`TaskScheduler`, and launches the background `outcome_tracker_loop`. Running any submodule directly
without the root on `sys.path` breaks sibling imports.

### Signal pipeline (`scan_symbol_v2`, the live path)
Four layers — see `AGENTS.md` for detail. The orchestration lives in `scheduler/scanner.py`
(`run_scan_cycle` → `scan_symbol_v2`):
1. **Pattern Engine** (`strategy/pattern_engine.py`) — pure ICT pattern detection (trigger = BOS or
   sweep; confirmation = order block or FVG). No indicators, no scoring.
2. **Feature Builder** (`strategy/feature_builder.py`) — collects ~35 raw features into a flat vector.
3. **Probability Engine** (`strategy/probability_engine.py`) — estimates P(TP)/RR/profit factor;
   rules-based fallback, ML once enough outcomes exist.
4. **Risk Engine** (`risk/engine.py`) — capital-protection hard gates + Kelly position sizing.

`scan_symbol` (old pipeline) still exists for backward compatibility. The scanner logs a
**signal funnel** (`_FUNNEL_GATES`) recording where each candidate is dropped, and writes a
`DecisionTrace` (`storage/trace.py`) per evaluation for later analytics.

### Supporting subsystems (each a top-level package)
- `indicators/` — EMA/RSI/MACD/ADX/ATR/Supertrend via pandas-ta.
- `market_structure/` — BOS/CHoCH, MTF alignment, `htf_bias.py` + `htf_bias_v2.py`, `premium_discount.py`.
- `liquidity/` — order blocks, FVG, sweeps, equal highs/lows, pools.
- `context/` — non-blocking macro/sentiment context producing a `ContextScore` in [-1, 1].
- `derivatives/` — funding, open interest, BTC/ETH correlation, SMT divergence (features, not gates).
- `risk/` — regime detection, dynamic risk, volatility regime, news filter, no-trade zones.
- `scheduler/` — `tasks.py` (APScheduler), `scanner.py`, `outcome_tracker.py`, `circuit_breaker.py`.
- `bot/` — Telegram handlers, admin commands, notifier, rate limiting, menu.
- `storage/` — async SQLAlchemy over SQLite (`database.py`) + decision traces (`trace.py`).
- `web/` — aiohttp dashboard (`server.py`, static `web/public/`), WebSocket signal broadcast.
- `strategy/` also holds an experimental **shadow-mode** stack (market-phase / scenario / thesis
  engines) run alongside the live path for evaluation, not for gating live signals.

### Config (`config/settings.py`)
Single source of truth: nested `@dataclass` configs read from `.env`, exposed as the module-level
`config = AppConfig()` singleton. `VERSION` bumps on every logic change (traceability, tagged into
traces/backtests). Notable flags validated by A/B tests: `htf_bias_v2` **ON** by default,
`premium_discount` **OFF** (hurt PF in backtest). Runtime symbols and filter toggles are
hot-reloadable (`refresh_runtime_symbols`, `reload_filter_toggles`) — some state lives in the DB, not
just `.env`. Default exchange is `bingx`, `market_type=swap` (perpetual futures).

### Scheduling (`scheduler/tasks.py`)
`scan_all_tfs` runs on a cron derived from `config.scheduler.scan_minutes` (every 15 min over all
`primary_timeframes`), plus a daily report at 00:05 UTC. Per-`symbol_timeframe` cooldown
(`get_cooldown_minutes`) prevents duplicate signals and is **in-memory only** — it resets on restart.

### Backtesting & analytics
`backtest/engine.py` is the single engine and targets full parity with the live scanner (same regime
detection, structural SL/TP, buffers, RR filter, fees/slippage). `analytics/` generates daily/full
reports; `reports/` is a large tree of prior experiment outputs, cached OHLCV parquet, and system-map
docs — treat generated report files there as prior artifacts, not code to edit.

## Gotchas (see `AGENTS.md` for the full list)
- **Telegram HTML**: with `parse_mode=HTML`, `html.escape()` every dynamic substring — a bare `<`
  (e.g. `ADX < 20`) crashes Telegram's parser.
- **`exchange_client.fetch_ohlcv` drops the last (open) candle** (`df.iloc[:-1]`) to avoid signalling
  off an unclosed bar.
- **pandas-ta column names** are resolved dynamically in `indicators/engine.py`; verify prefixes
  (`SUPERT_…`, `ADX_…`/`DMP_…`/`DMN_…`) after any pandas-ta upgrade.
- **Context fetcher module-level singletons** hold caches (`_fng_cache`, `_last_oi`, …); tests should
  construct fresh `ContextFetcher`/`ContextEngine` instances rather than reuse them.
