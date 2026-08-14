# AGENTS.md — Trading Signal Bot

## Quick start

```bash
cp .env.example .env # fill in required vars
python -m venv .venv && source .venv/bin/activate # .venv\Scripts\activate on Windows
pip install -r requirements.txt
python main.py
```

Docker: `docker-compose build && docker-compose up -d`

## Tests

```bash
pytest -v # all
pytest tests/test_signal.py -v # one file
```

No `pip install -e .` — every test file does `sys.path.insert(0, root)`. `asyncio_mode = auto`
in `pytest.ini`; use `@pytest.mark.asyncio` on async tests.

## Plan & architecture

`plan/00index.md` is the entry point — full module tree, singletons, and pipeline live in
`plan/01-architecture.md`, `plan/07-scheduler.md`, `plan/11-pipeline.md`. Environment reference
is `plan/14-env-config.md`. Update those files when behavior changes, not this one.

## Signal logic (new pipeline — scan_symbol_v2)

**Architecture:** Pattern Engine → Feature Builder → Probability Engine → Risk Engine

- **Layer 1 — Pattern Engine** (`strategy/pattern_engine.py`): Pure ICT pattern detection.
  Setup = trigger (BOS or sweep) + confirmation (OB or FVG). No indicators, no scoring.
- **Layer 2 — Feature Builder** (`strategy/feature_builder.py`): Collects ~35 raw features
  into a flat vector (ICT pattern, market structure, volume, indicators, MTF, context, risk).
  No scoring, no blocking — just data.
- **Layer 3 — Probability Engine** (`strategy/probability_engine.py`): Estimates P(TP),
  expected RR, profit factor. Rules-based fallback; ML (XGBoost/RandomForest) replaces
  rules once 100+ historical outcomes are collected.
- **Layer 4 — Risk Engine** (`risk/engine.py`): Capital protection only. Hard gates:
  R:R minimum, SL absolute limits, portfolio risk, max active signals. Position sizing
  via Kelly criterion with volatility adjustment.

**Old pipeline** (`scan_symbol`) still exists for backward compatibility.
`run_scan_cycle()` calls `scan_symbol_v2()`.

- Cooldown per `symbol_timeframe` is `SIGNAL_COOLDOWN_MINUTES` (default 45), effective cooldown =
  `max(base, tf_minutes × multiplier)` (`SIGNAL_COOLDOWN_TF_MULTIPLIER`, default 2.0). **Persisted in
  SQLite** (`db.get_cooldown` / `db.set_cooldown`) — survives restarts.
- Context never blocks: `ContextScore` provides a score [-1, 1] for the Probability Engine.
- BTC/ETH correlation removed as gates — become secondary features.
- 15m confirmation TF removed entirely.
- **HTF Bias V2** is ON by default (`config.htf_bias_v2 = True`). A/B validated:
  PF 1.10→1.28, WR 29.6%→31.1%, PnL x2.1 on 90d/1h BTC+ETH.
  Blocks buy continuations against bearish HTF bias (W1→D1→H4→H1 EMA).
- **Premium/Discount zones** are OFF (`config.premium_discount = False`). A/B showed
  they hurt performance (PF 1.28→0.91). Revisit after 500+ live trades with v2.

## Scheduler

- `scan_all_tfs` (cron `SCAN_MINUTES`, default `:02, :17, :32, :47` via `config.scheduler`) →
  `run_scan_cycle()` over all `primary_timeframes`.
- Каждые 15 минут сканируются все таймфреймы (1h, 4h).
- Cooldown 45 мин защищает от дублей (persisted in SQLite).
- `cmd_scan` (admin `/scan`) → `run_scan_cycle()` over all `primary_timeframes`.
- `SHADOW_ENABLED=true` запускает shadow-сравнение после каждого цикла (`scheduler/shadow.py`).

## Project-specific gotchas

- **Telegram HTML**: `parse_mode=ParseMode.HTML` requires `html.escape()` on every dynamic
  substring — bare `<` (e.g. `ADX < 20`) crashes Telegram's parser. `_do_full_analysis` re-escapes
  on `BadRequest`.
- **pandas-ta columns**: Supertrend → `SUPERT_…` / `SUPERTd_…`; ADX → `ADX_…`, `DMP_…`, `DMN_…`.
  `indicators/engine.py` resolves them dynamically; verify prefixes on pandas-ta upgrades.
- **`exchange_client.fetch_ohlcv` drops the last candle** (`df.iloc[:-1]`) to avoid signalling
  off an open bar.
- **Context fetcher singleton state**: `_fng_cache`, `_trending_cache`, `_rss_cache`,
  `_last_oi[symbol]`. Tests should instantiate a fresh `ContextFetcher` / `ContextEngine`
  rather than reuse the module-level singletons.
- **`main.py` adds project root to `sys.path`** — running submodules without it will fail
  sibling imports.

## Required `.env`

See `plan/14-env-config.md` for the full list. Minimum:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHANNEL_ID=
SYMBOLS=BTC/USDT,ETH/USDT
PRIMARY_TIMEFRAMES=1h,4h
EXCHANGE_API_KEY=
EXCHANGE_API_SECRET=
MARKET_TYPE=swap
```
