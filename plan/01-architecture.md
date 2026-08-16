# Architecture — Trading Signal Bot

> Модульное дерево, синглтоны, слои пайплайна. Актуально для `VERSION = "2.6.0"`
> (`config/settings.py:15`). Обновлять при изменении поведения.

## Слои пайплайна (production: `scan_symbol_v2`)

```
Pattern Engine → Feature Builder → Probability Engine → Risk Engine → Telegram
```

| Layer | Модуль | Назначение |
|-------|--------|------------|
| L1 Pattern Engine | `strategy/pattern_engine.py` | Чистое ICT-детектирование: trigger (BOS/sweep) + confirmation (OB/FVG). Без индикаторов и скоринга |
| L2 Feature Builder | `strategy/feature_builder.py` | ~35 сырых фич в плоский вектор. Без скоринга/блокировок |
| L3 Probability Engine | `strategy/probability_engine.py` | P(TP), expected RR, PF. Rules fallback; ML после 100+ исходов |
| L4 Risk Engine | `risk/engine.py` | Хард-гейты: R:R ≥ 1.2, SL 0.25–5%, портфель ≤3%, ≤3 активных; сайзинг по Келли |

## Модульное дерево

```
main.py                     # точка входа: lock, метрики, bot/scheduler/web
config/
  settings.py               # центральная конфигурация (VERSION, StrategyMode, классы)
  logger.py                 # loguru (console + bot.log + logs/logs.txt + error sink)
data/
  exchange_client.py        # ccxt (sync в run_in_executor), _symbol_map (swap), drop последней свечи; fetch_ohlcv(drop_last, end_time) — endTime для истории
storage/
  database.py               # SQLite (Signal, DecisionTrace, cooldown, outcomes, candidates)
  trace.py                  # DecisionTraceBuilder; GATE_ORDER (:31-35), FEATURE_KEYS
scheduler/
  scanner.py                # scan_symbol_v2 (production), run_scan_cycle, воронка гейтов
  tasks.py                  # APScheduler (scan_all_tfs каждые 15 мин, daily_report 00:05 UTC, update_history_cache */15)
  shadow.py                 # shadow-режим (SHADOW_ENABLED=true) — A/B сравнение
  outcome_tracker.py        # закрытие сигналов (SL/TP/EXPIRED), PnL после издержек
  circuit_breaker.py        # 3 убытка → пауза 30 мин
strategy/
  pattern_engine.py         # ICTSetup: reversal (sweep/displacement/MSS/OB/FVG), continuation (BOS)
  feature_builder.py        # сбор фич
  probability_engine.py     # P(TP), estimate_scenario
  decision_engine.py        # MarketState, narrative_weights, выбор лучшей гипотезы
  hypothesis.py             # 12 NarrativeType; HypothesisSet
  market_phase_engine.py    # 8 фаз
  scenario_engine.py        # MarketScenario (immutable), ScenarioEvaluation (mutable)
  scenario_memory.py        # Expected vs Observed; record_expected/record_observation/record_outcome
  scenario_invalidator.py   # инвалидация сценариев (BOS broken, OB mitigated, FVG filled)
  trade_thesis.py           # lifecycle forming→…→closed / failed/expired
  trade_plan.py             # TradePlan + TargetScore
  entry_trigger.py          # TriggerResult, proximity/spread
  weight_manager.py         # корректировка P(TP) по empirical stats (Rec 4b)
  market_thesis_engine.py   # DAG-движок, LiquidityGraph, HeuristicTransitionModel
  invalidation.py           # SL: sweep extreme > OB boundary > swing low (+buffer ATR*15%)
  levels.py                 # find_swing_levels / cluster_levels
  weights.py                # константы весов (OB_FROM_BOS=1.0 и др.)
risk/
  engine.py                 # Risk Engine (хард-гейты, Kelly)
  dynamic_risk.py           # risk_strong 1% / moderate 0.5% / weak → блок; structural SL/TP
  market_regime.py          # RegimeDetector (trend/range/compression/expansion/reversal)
  volatility_regime.py      # low <1% (breakout forbidden), high >4% → ×0.5
  no_trade_zones.py         # ATR низкий, range, BTC unclear, TP blocked, OI weak
  news_filter.py            # новостной фильтр (Rec 4a; события из config/macro_events.json)
indicators/engine.py        # EMA, RSI, MACD, ADX/DMI, ATR, Supertrend, volume
market_structure/
  structure.py              # BOS/CHoCH, MTF-alignment; MSS-критерии
  htf_bias.py               # HTFBias V1 (hard-reject против HTF-тренда)
  htf_bias_v2.py            # HTFBias V2 (W1→D1→H4→H1, ≥55 баров, EMA21/55)
  premium_discount.py       # fib-зоны: discount 0–0.3 / eq 0.3–0.7 / premium 0.7–1.0
  tp_path.py                # оценка пути к TP (+15 clear, −20 strong obstacle, TP в S/R → REJECT)
  distance_filter.py        # дистанция до S/R (default 1.2%)
  diagnostic.py             # MSS-funnel диагностика
liquidity/
  sweep.py, order_blocks.py, fvg.py, pool.py, external.py,
  ob_state.py, candle_quality.py, equal_levels.py
derivatives/
  btc_correlation.py, eth_correlation.py, funding.py, open_interest.py, smt_divergence.py
context/
  fetcher.py                # aiohttp, retry, кэши; _last_oi per symbol
  analyzer.py               # ContextSnapshot (F&G, CoinGecko, Binance Futures funding/OI/LS, news)
  scorer.py                 # ContextScorer → ContextScore [-1,1] (не блокирует)
scoring/confidence_v2.py    # 10-факторный скоринг −100..100 (display + features)
bot/
  notifier.py, handlers.py, admin.py, menu.py, rate_limit.py
web/server.py               # aiohttp WebSocket-дашборд
monitoring/metrics.py       # Prometheus: signals_total, scan_duration, context_errors
backtest/
  engine.py                # CLI backtest: --source live|local (читает 15m-кэш + ресемплинг)
  resampler.py             # resample_ohlcv 15m→1h/2h/4h/1d/1w (pandas 3: '1h','1D','W-MON')
  cache_ohlcv.py           # unified 15m parquet-кэш (ohlcv_cache/), fetch_full_history / update_history
  download_history.py      # CLI: python -m backtest.download_history (3 года 15m, semaphore)
  __main__.py              # точка входа для download_history
analytics/                  # daily_report, gate_funnel, performance, calibration, и др.
```

## Ключевые синглтоны

| Синглтон | Модуль | Примечание |
|----------|--------|------------|
| `config` | `config/settings.py:806` | `AppConfig`; `reload_config()` перечитывает .env |
| `db` | `storage.database.py` | async SQLAlchemy |
| `exchange_client` | `data/exchange_client.py` | ccxt обёртка |
| `probability_engine` | `strategy/probability_engine.py:431` | правила/ML |
| `pattern_engine` | `strategy/pattern_engine.py` | детектор сетапов |
| `feature_builder` | `strategy/feature_builder.py` | сбор фич |
| `risk_engine` | `risk/engine.py` | оценка риска |
| `scenario_memory` | `strategy/scenario_memory.py:285` | in-memory статистика сценариев |
| `weight_manager` | `strategy/weight_manager.py:71` | эмпирическая корректировка |

> Осторожно с синглтонами контекста (`context/fetcher.py`): `_fng_cache`,
> `_trending_cache`, `_rss_cache`, `_last_oi[symbol]` — тестам нужен свежий инстанс.

## Устаревшее / неподключённое

- `strategy/signal_engine.py` + `scan_symbol()` — легаси (обратная совместимость).
- `scheduler/core_v2.py` и `config/simplified_preset.py` — **удалены** (Rec 2): не использовались.