# Environment Config — Trading Signal Bot

> Справочник переменных окружения (`.env`). Актуально для `VERSION = "2.6.0"`.
> Все ключи считываются в `config/settings.py` при старте / `reload_config()`.

## Минимальный набор

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHANNEL_ID=
SYMBOLS=BTC/USDT,ETH/USDT
PRIMARY_TIMEFRAMES=1h,4h
EXCHANGE_API_KEY=
EXCHANGE_API_SECRET=
MARKET_TYPE=swap
```

## Биржа (ExchangeConfig, settings.py:46-58)

| Ключ | Default | Описание |
|------|---------|----------|
| `EXCHANGE` | `bingx` | ccxt-ид биржи. Для `--years 3` истории 15m ставьте `binance` (BingX хранит 15m только ~6 мес; Binance отдаёт 3+ года). Свечи с обеих бирж консистентны |
| `EXCHANGE_API_KEY` / `BINANCE_API_KEY` | — | ключ (читается универсальный или бирже-специфичный) |
| `EXCHANGE_API_SECRET` / `BINANCE_API_SECRET` | — | секрет |
| `USE_TESTNET` | `false` | тестнет |
| `MARKET_TYPE` | `swap` | `spot` / `swap` / `future` |
| `EXCHANGE_FEE_PCT` | `0.05` | комиссия за сторону |
| `SLIPPAGE_PCT` | `0.05` | проскальзывание за сторону |

## Стратегия и режимы

| Ключ | Default | Описание |
|------|---------|----------|
| `STRATEGY_MODE` | `v2.5` | `v2.5` / `confluence` / `hybrid` |
| `HTF_HARD_GATE` | `true` | continuation против HTF-bias → block |
| `HTF_BIAS_CONTINUATION_PENALTY` | `0.85` | множитель P(TP) при `HTF_HARD_GATE=false` |
| `REVERSAL_REQUIRE_DISPLACEMENT` | `false` | требовать displacement на reversal |
| `REQUIRE_ENTRY_ZONE` | `false` | сигнал только при входе в OB/FVG-зону |
| `MIN_P_TP` | `0.45` | Probability-селектор (P(TP) флор; 0.0 = off) |
| `EXECUTION_MODEL` | `median_immediate` | цена входа в бэктесте: `median_immediate` (фантомная медиана FVG) / `close` (close бара, зеркалит live-P&L) / `limit_pending` (лимитка на медиане, филл при касании) |
| `EXECUTION_PENDING_MAX_BARS` | `50` | макс. свечей жизни pending-ордера для `EXECUTION_MODEL=limit_pending` |

> **Важно (2026-08-18, см. `docs/decisions/execution_model.md`):** `median_immediate`
> даёт фантомные филлы и завышенные метрики (risk-гейт считает RR от нереалистичной
> цены). Любая публикуемая метрика бэктеста обязана сопровождаться пометкой, какой
> `EXECUTION_MODEL` использован. Продуктовое решение (исполнять ли ордера) ещё не
> принято.

## Фильтры направления/символа (Rec 3 — config/direction_filter)

| Ключ | Default | Описание |
|------|---------|----------|
| `BLOCK_ALL_SELL` | `true` | блок всех SELL-сигналов |
| `BLOCK_ALL_SELL_REASON` | `SELL blocked: WR 33.7%...` | причина для лога |
| `BLOCKED_SYMBOL_DIRECTIONS` | `{"WIF/USDT": "buy"}` | JSON `{SYMBOL: "buy"/"sell"}` |
| `DIRECTION_FILTER_STATS` | `{"SELL": {...}, "WIF/USDT:buy": {...}}` | справочная статистика |

## News Filter (Rec 4a — opt-in)

| Ключ | Default | Описание |
|------|---------|----------|
| `NEWS_FILTER_ENABLED` | `false` | включить фильтр новостей |
| `NEWS_EVENTS_FILE` | `config/macro_events.json` | локальный JSON событий |
| `NEWS_BLOCK_BEFORE_MINUTES` | `60` | окно до события |
| `NEWS_BLOCK_AFTER_MINUTES` | `30` | окно после события |

## Торговля (TradingConfig)

| Ключ | Default |
|------|---------|
| `SYMBOLS` | `BTC/USDT,ETH/USDT,SOL/USDT` |
| `PRIMARY_TIMEFRAMES` | `1h,4h` |
| `CONFIRM_TIMEFRAME` | `15m` |
| `CONFIRM_TF_ENABLED` | `true` |
| `EMA_FAST/SLOW/TREND` | `8/21/55` |
| `RSI_PERIOD` | `10` |
| `MACD_FAST/SLOW/SIGNAL` | `8/21/5` |
| `ADX_PERIOD` | `14` |
| `ATR_MULTIPLIER_SL/TP` | `1.5/3.0` |
| `MIN_RR_THRESHOLD` | `1.5` |
| `CANDLES_LIMIT` | `200` |
| `SYMBOL_OVERRIDES` | `{}` (JSON) |
| `SIGNAL_COOLDOWN_MINUTES` | `45` |
| `SIGNAL_COOLDOWN_TF_MULTIPLIER` | `2.0` |

## Риск (RiskConfig / RiskEngineConfig)

| Ключ | Default | Описание |
|------|---------|----------|
| `RISK_STRONG_PCT` | `1.0` | риск на strong |
| `RISK_MODERATE_PCT` | `0.5` | риск на moderate |
| `RISK_WEAK_TRADE` | `false` | разрешить weak |
| `RISK_WEAK_PCT` | `0.25` | риск на weak |
| `NO_TRADE_MIN_ATR_PCT` | `0.6` | no-trade при низком ATR |
| `VOLATILITY_LOW_THRESHOLD` | `0.8` | low-vol (% ATR) |
| `VOLATILITY_HIGH_THRESHOLD` | `6.0` | high-vol |
| `RISK_ENGINE_MIN_RR` | `1.2` | Risk Engine R:R min |
| `RISK_ENGINE_SL_MIN_PCT` / `SL_MAX_PCT` | `0.25` / `5.0` | SL абс. лимиты |
| `MAX_ACTIVE_SIGNALS` | `3` | макс активных |
| `MAX_PORTFOLIO_RISK_PCT` | `3.0` | макс портфельный риск |
| `DYNAMIC_RISK_ENABLED` | `true` | dynamic risk |
| `VOLATILITY_FILTER_ENABLED` | `true` | volatility gate |

## Структура / MTF (MarketStructureConfig)

| Ключ | Default |
|------|---------|
| `DISTANCE_FILTER_MIN_PCT` | `1.2` |
| `MTF_ENABLED` | `true` |
| `MTF_REQUIRED_ALIGNMENT` | `2` |
| `TP_PATH_ENABLED` | `false` |
| `STRUCTURE_LOOKBACK` | `50` |

## Деривативы (DerivativesConfig)

| Ключ | Default |
|------|---------|
| `FUNDING_STRONG_THRESHOLD` | `0.0003` |
| `OI_STRONG_THRESHOLD` | `2.0` |
| `BTC_CORRELATION_ENABLED` | `true` |
| `ETH_CORRELATION_ENABLED` | `true` |
| `SMT_ENABLED` | `true` |

## Контекст

| Ключ | Default |
|------|---------|
| `CONTEXT_ENABLED` | `true` |
| `CONTEXT_MIN_VERDICT` | `WEAK` |
| `CONTEXT_FETCH_TIMEOUT` | `10` |
| `CONTEXT_CACHE_TTL_SECONDS` | `1800` |
| `CRYPTOPANIC_API_KEY` | — |
| `COINGECKO_SYMBOL_MAP` | `BTC/USDT:bitcoin,ETH/USDT:ethereum` |

## Скоринг / Probability / Weight Manager

| Ключ | Default |
|------|---------|
| `QUALITY_STRONG_THRESHOLD` | `65` |
| `QUALITY_MODERATE_THRESHOLD` | `30` |
| `PROBABILITY_MODEL_PATH` | `models/probability_model.pkl` |
| `PROBABILITY_MIN_SAMPLES_FOR_ML` | `100` |
| `WEIGHT_MANAGER_MIN_SCENARIOS` | `30` (Rec 4b) |

## Планировщик / Web / Метрики / Rate-limit

| Ключ | Default |
|------|---------|
| `SCAN_MINUTES` | `2,17,32,47` |
| `OUTCOME_CHECK_INTERVAL_SECONDS` | `300` |
| `OUTCOME_TTL_DAYS` | `7` |
| `FUNDING_RATE_8H` | `0.0001` |
| `CIRCUIT_BREAKER_LOSS_THRESHOLD` | `3` |
| `WEB_ENABLED` | `true` |
| `WEB_PORT` | `3001` |
| `METRICS_ENABLED` | `false` |
| `METRICS_PORT` | `9090` |
| `RATE_LIMIT_MAX_RATE` | `5` |
| `RATE_LIMIT_TIME_PERIOD` | `10` |

## Прочее

| Ключ | Default |
|------|---------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/signals.db` |
| `LOG_LEVEL` | `INFO` |
| `LOG_FILE` | `logs/bot.log` |
| `SHADOW_ENABLED` | `false` |
| `SHADOW_PRESET` | `simplified` |
| `HTF_BIAS_V2` | `true` |
| `PREMIUM_DISCOUNT` | `false` |
| `SIGNAL_BLOCK_NOTIFY` | `true` |

> Полный список — в `config/settings.py`. Ключи, не описанные здесь,
> см. по именам классов: `ExchangeConfig`, `TradingConfig`, `MarketStructureConfig`,
> `LiquidityConfig`, `DerivativesConfig`, `RiskConfig`, `ScoringConfig`, `SchedulerConfig`,
> `RateLimitConfig`, `NotifierConfig`, `SupportResistanceConfig`, `WebConfig`,
> `PatternEngineConfig`, `ProbabilityConfig`, `RiskEngineConfig`, `DirectionFilterConfig`.

## Исторический кэш OHLCV (backtest --source=local)

Новых env-ключей нет. Параметры — константы `backtest/cache_ohlcv.py`:
`OHLCV_CACHE_DIR=ohlcv_cache/`, `BASE_TIMEFRAME=15m`, `PARQUET_COMPRESSION=zstd`,
`HISTORY_PAGE_SIZE=998`, `HISTORY_RETRY_ATTEMPTS=5`, `HISTORY_RETRY_BACKOFF=[1,2,4,8]`.
Глубина по умолчанию — 3 года (флаг `--years` CLI).