# Полный технический аудит Trading Signal Bot

**Дата аудита:** 14 августа 2026

---

# 1. Обзор проекта

## 1.1 Назначение

Telegram-бот для автоматического сканирования криптовалютных рынков и публикации торговых сигналов **BUY/SELL** в Telegram-канал. Бот **не торгует самостоятельно** — он генерирует сигналы с расчётами entry, SL, TP и направляет их пользователям/трейдерам.

- **Рынок:** Криптовалюты (Binance, BingX, Bybit)
- **Инструменты:** BTC/USDT, ETH/USDT, SOL/USDT, BNB/USDT, XRP/USDT и другие (динамический список)
- **Тип рынка:** Спот и перпетуальные фьючерсы (`market_type=swap` по умолчанию)
- **Подход:** ICT (Inner Circle Trader) — структура рынка, ликвидность, order blocks, FVG, sweep'ы

## 1.2 Стек технологий

| Компонент | Технология |
|-----------|-----------|
| Язык | Python 3.11 |
| Telegram | `python-telegram-bot==20.7` (async, polling) |
| Биржа | `ccxt==4.2.15` (универсальный клиент) |
| Индикаторы | `pandas-ta>=0.4.0` + `pandas>=2.2.0` |
| БД | SQLite через `aiosqlite==0.19.0` + `sqlalchemy==2.0.23` (async) |
| Планировщик | `apscheduler==3.10.4` |
| Логирование | `loguru==0.7.2` |
| Веб-дашборд | `aiohttp` ( aiohttp ) |
| Rate limiting | `aiolimiter>=1.1` |
| Мониторинг | `prometheus_client>=0.20` (Prometheus metrics) |
| Контейнеризация | Docker + docker-compose |

## 1.3 Точка входа и порядок запуска

**Файл:** `main.py`

```
1. sys.path.insert(0, project_root)        — добавление корня в PYTHONPATH
2. acquire_lock()                           — PID lock-файл (.trading_bot.lock)
3. config.logger инициализация              — настройка loguru
4. _start_metrics_server()                  — Prometheus HTTP-сервер (opt-in)
5. Проверка TELEGRAM_BOT_TOKEN              — критический параметр
6. db.init()                                — инициализация async SQLite
7. refresh_runtime_symbols()                — загрузка символов из DB
8. reload_filter_toggles()                  — загрузка toggle-фильтров из DB
9. exchange_client.connect()                — подключение к бирже через ccxt
10. start_web_server()                      — веб-дашборд (opt-in)
11. Telegram Application (polling)          — устойчивая конфигурация HTTPXRequest
12. register_handlers(app)                  — регистрация команд
13. setup_error_sink(app.bot)              — ERROR+ логи в Telegram
14. TaskScheduler.setup() + .start()        — APScheduler (cron)
15. outcome_tracker_loop()                  — фоновый трекинг TP/SL
16. app.updater.start_polling()             — polling с retry при Conflict
17. while True: sleep(3600)                — бесконечный event loop
```

---

# 2. Архитектура

## 2.1 Диаграмма модулей

```
main.py
├── config/               — Настройки (settings.py), логгер (logger.py)
├── data/                 — Клиент биржи (exchange_client.py), SQLite DB
├── indicators/           — EMA, RSI, MACD, ADX, ATR, Supertrend (engine.py)
├── market_structure/     — BOS, CHoCH, MSS, HTF Bias, Premium/Discount
├── liquidity/            — Order Blocks, FVG, Sweeps, Equal Levels, Pool
├── strategy/             — Pattern Engine, Signal Engine, Trade Engine, Weights
├── risk/                 — Risk Engine, Market Regime, Dynamic Risk, Volatility
├── context/              — Fear & Greed, Funding, News, Sentiment
├── derivatives/          — Funding Rate, Open Interest, BTC/ETH Correlation
├── scoring/              — Confidence scoring (confidence_v2.py)
├── scheduler/            — APScheduler tasks, Scanner, Outcome Tracker
├── bot/                  — Telegram handlers, notifier, admin, menu, rate_limit
├── storage/              — SQLAlchemy DB (database.py), Decision Traces (trace.py)
├── web/                  — aiohttp dashboard, webhook
├── monitoring/           — Prometheus metrics
├── analytics/            — 30+ аналитических скриптов (performance, reports)
├── backtest/             — Backtest engine, cache, funnel analysis
├── reports/              — Артефакты экспериментов, A/B тесты, датасеты
├── tests/                — 39 тестовых файлов
├── scripts/              — Утилиты: A/B тесты, калибровка, анализ
├── tools/                — Диагностические утилиты
└── prompts/              — Промпты для AI-агентов
```

## 2.2 Файлы/папки с описанием

| Путь | Назначение |
|------|-----------|
| `main.py` | Точка входа: PID lock, init DB, exchange, Telegram, scheduler |
| `config/settings.py` | Единый источник конфигурации: 50+ параметров из `.env`, dataclass-иерархия |
| `config/logger.py` | Настройка loguru: файловые ротации, Telegram error sink |
| `data/exchange_client.py` | Универсальный клиент биржи (ccxt): OHLCV, ticker, ордера, позиции |
| `storage/database.py` | Async SQLAlchemy: Signal, SignalOutcome, cooldowns, settings, dynamic symbols |
| `storage/trace.py` | DecisionTrace: логирование каждого этапа принятия решения |
| `indicators/engine.py` | Расчёт индикаторов через pandas-ta: EMA, RSI, MACD, ADX, ATR, Supertrend |
| `market_structure/structure.py` | BOS/CHoCH/MSS detection, swing points, MTF alignment |
| `market_structure/htf_bias_v2.py` | HTF Bias V2: W1→D1→H4→H1 multi-TF alignment |
| `market_structure/premium_discount.py` | ICT premium/discount zones (отключён A/B тестом) |
| `market_structure/distance_filter.py` | Минимальное расстояние до уровня |
| `market_structure/tp_path.py` | Анализ пути до TP |
| `liquidity/sweep.py` | Detection sweep events (stop hunts) |
| `liquidity/order_blocks.py` | Detection Order Blocks |
| `liquidity/fvg.py` | Detection Fair Value Gaps |
| `liquidity/pool.py` | LiquidityMap: агрегация уровней ликвидности |
| `liquidity/equal_levels.py` | Equal highs/lows detection |
| `liquidity/external.py` / `external_liquidity.py` | External liquidity (old highs/lows) |
| `liquidity/ob_state.py` | Order Block state tracking |
| `liquidity/candle_quality.py` | Анализ качества свечи (body, wicks) |
| `strategy/pattern_engine.py` | Layer 1: ICT pattern detection (trigger + confirmation) |
| `strategy/signal_engine.py` | Старый пайплайн: score-based signal generation |
| `strategy/trade_engine.py` | Trade planning: entry, SL (invalidation), TP (liquidity targets) |
| `strategy/invalidation.py` | Invalidation logic: sweep > OB > swing > BOS > ATR fallback |
| `strategy/entry_trigger.py` | Entry trigger: проверка цены входа, spread, session |
| `strategy/levels.py` | S/R levels: swing detection, clustering, validation |
| `strategy/trade_plan.py` | TradePlan dataclass: полный план сделки |
| `strategy/trade_thesis.py` | TradeThesis lifecycle: forming→observed→confirmed→activated→executing→closed |
| `strategy/weight_manager.py` | Корректировка P(TP) по накопленной статистике |
| `strategy/weights.py` | Конфигурируемые веса для scenario scoring |
| `strategy/transition_model.py` | Heuristic/ML transition probabilities |
| `risk/engine.py` | Layer 4: Risk Engine — hard gates (R:R, SL limits, portfolio risk, Kelly sizing) |
| `risk/market_regime.py` | RegimeDetector: trending/ranging/volatile via ADX/ATR/EMA spread |
| `risk/dynamic_risk.py` | Dynamic position sizing based on regime |
| `risk/volatility_regime.py` | Volatility regime classification |
| `risk/no_trade_zones.py` | No-trade zone detection |
| `context/fetcher.py` | ContextFetcher: Fear & Greed, trending coins, RSS news, OI |
| `context/analyzer.py` | ContextEngine: агрегация контекста |
| `context/scorer.py` | ContextScoring: вердикт (CONFIRMED/WEAK/CONFLICTED/BLOCKED) |
| `derivatives/funding.py` | Funding rate analysis |
| `derivatives/open_interest.py` | Open Interest delta analysis |
| `derivatives/btc_correlation.py` | BTC correlation feature |
| `derivatives/smt_divergence.py` | SMT divergence detection |
| `scoring/confidence_v2.py` | Confidence scoring v2 |
| `scheduler/tasks.py` | APScheduler: cron-задачи сканирования, дневной отчёт |
| `scheduler/scanner.py` | run_scan_cycle(), scan_symbol_v2() — основной пайплайн |
| `scheduler/outcome_tracker.py` | Фоновый трекинг закрытия сделок (TP/SL) |
| `scheduler/circuit_breaker.py` | Circuit breaker для биржевых ошибок |
| `bot/handlers.py` | Telegram команды: /start, /help, /status, /scan, /settings |
| `bot/admin.py` | Admin команды: /addsymbol, /setparam, /disable, /exportdb, /stats |
| `bot/notifier.py` | Отправка сигналов и уведомлений в Telegram |
| `bot/menu.py` | Inline keyboard menu: анализ, индикаторы, настройки |
| `bot/rate_limit.py` | Rate limiter для Telegram-команд |
| `web/server.py` | aiohttp веб-сервер (дашборд) |
| `web/webhook.py` | Webhook handler |
| `monitoring/metrics.py` | Prometheus metrics HTTP-сервер |
| `analytics/performance.py` | Core performance analytics: WR, PF, expectancy, MFE/MAE |
| `analytics/full_report.py` | Full Markdown report с matplotlib-графиками |
| `analytics/daily_report.py` | Daily report generator |
| `backtest/engine.py` | Backtest engine с полным паритетом с live-сканером |

## 2.3 Паттерны проектирования

| Паттерн | Пример |
|---------|--------|
| **Singleton** | `config.config`, `exchange_client`, `db`, `indicator_engine`, `pattern_engine`, `trade_engine`, `thesis_manager`, `weight_manager` |
| **Layered Architecture** | 4-слойный пайплайн: Pattern→Feature→Probability→Risk |
| **Strategy** | `TransitionModelBase` (ABC) с `HeuristicTransitionModel` и `MLTransitionModel` |
| **Observer** | APScheduler cron → `run_scan_cycle()` |
| **Repository** | `database.py` — все запросы к БД инкапсулированы |
| **Dataclass DTO** | `SignalResult`, `TradePlan`, `TargetScore`, `Invalidation`, `StructureState`, `HTFBiasResult` |
| **Pipeline** | `scan_symbol_v2()`: fetch OHLCV → indicators → structure → liquidity → pattern → feature → probability → risk → signal |
| **Lock (PID)** | `.trading_bot.lock` — защита от повторного запуска |
| **Retry** | Telegram send: exponential backoff; Exchange fetch: retry on NetworkError |

## 2.4 Взаимодействие компонентов

| Механизм | Описание |
|----------|---------|
| **Polling** | Telegram bot: `start_polling()` с retry при `Conflict` |
| **Cron** | APScheduler: `scan_all_tfs` каждые 15 мин (`:02, :17, :32, :47`) |
| **Background tasks** | `outcome_tracker_loop()`: проверка TP/SL каждые 300 сек |
| **HTTP** | aiohttp dashboard, Prometheus metrics, webhook |
| **Async SQLite** | Все операции с БД через `aiosqlite` + SQLAlchemy async |
| **ccxt** | REST API к бирже (не websocket) |

## 2.5 Хранилище данных

| Хранилище | Тип | Что хранится |
|-----------|-----|-------------|
| `data/signals.db` | SQLite | Сигналы (Signal), исходы (SignalOutcome), cooldowns, settings, dynamic symbols |
| `trades.db` | SQLite | Дубликат/альтернатива signals.db (для analytics) |
| `.trading_bot.lock` | Файл | PID текущего процесса |
| `logs/bot.log` | Файл | Логи (loguru, ротация) |
| `models/probability_model.pkl` | Pickle | ML-модель (кандидат, не используется live) |
| `ohlcv_cache/` | Parquet файлы | Кэш OHLCV для бэктестов |
| In-memory | Dict | Cooldowns (`_cooldowns`), caches (`_fng_cache`, `_trending_cache`, `_last_oi`) |

---

# 3. Источники данных и анализ рынка

## 3.1 Источники данных

| Источник | Метод | Файл |
|----------|-------|------|
| Binance / BingX / Bybit OHLCV | REST API через `ccxt` | `data/exchange_client.py:58-104` |
| Ticker (текущая цена) | `ccxt.fetch_ticker()` | `data/exchange_client.py:122-140` |
| Order book (spread) | `ccxt.fetch_order_book()` | `data/exchange_client.py` |
| Fear & Greed Index | `alternative.me` API | `context/fetcher.py` |
| Funding Rate | `ccxt` или `coinglass` | `derivatives/funding.py` |
| Open Interest | `ccxt` или `coinglass` | `derivatives/open_interest.py` |
| Trending coins | `coingecko` API | `context/fetcher.py` |
| News/RSS | `feedparser` | `context/fetcher.py` |

**Важно:** Бот работает через **REST API** (polling), а не через WebSocket. Это означает:
- Задержка данных до интервала сканирования
- Нет real-time обновлений между сканами
- Outcome tracker запрашивает ticker каждые 300 сек

## 3.2 Таймфреймы и инструменты

| Параметр | Значение | Источник |
|----------|----------|----------|
| Primary timeframes | `1h, 4h` | `.env: PRIMARY_TIMEFRAMES` |
| Confirm timeframe | `15m` (удалён из пайплайна v2) | `.env: CONFIRM_TIMEFRAME` |
| HTF timeframes | `1d, 4h, 1h` (MTF alignment) | `config.mtf_timeframes` |
| HTF Bias V2 | `1w, 1d, 4h, 1h` | `market_structure/htf_bias_v2.py` |
| Default symbols | BTC/USDT, ETH/USDT, SOL/USDT, BNB/USDT, XRP/USDT | `.env: SYMBOLS` |
| Candles limit | 200 | `.env: CANDLES_LIMIT` |

## 3.3 Индикаторы и фичи

### Технические индикаторы (`indicators/engine.py`)

| Индикатор | Параметры по умолчанию | Назначение |
|-----------|----------------------|-----------|
| EMA Fast | 8 | Быстрая скользящая |
| EMA Slow | 21 | Медленная скользящая |
| EMA Trend | 55 | Трендовая |
| RSI | period=10, OB=72, OS=28 | Перекупленность/перепроданность |
| MACD | 8/21/5 | Гистограмма |
| ADX | period=14, min=20 | Сила тренда |
| ATR | period=14 | Волатильность (SL/TP) |
| Supertrend | period=10, multiplier=2.5 | Направление тренда |
| Volume SMA | period=20 | Средний объём |

### ICT-паттерны (`strategy/pattern_engine.py`)

| Паттерн | Описание |
|---------|---------|
| BOS (Break of Structure) | Продолжение тренда |
| CHoCH (Change of Character) | Смена тренда |
| MSS (Market Structure Shift) | Сильная смена структуры |
| Order Block | Зона концентрации ордеров |
| FVG (Fair Value Gap) | Гэп в ликвидности |
| Sweep | Stop hunt / liquidity grab |
| Equal Levels | Равные highs/lows |

### Контекстные фичи (`context/`, `derivatives/`)

| Фича | Источник |
|------|----------|
| Fear & Greed Index | alternative.me |
| Funding Rate | Биржа |
| Long/Short Ratio | Биржа |
| Open Interest Delta | Биржа |
| BTC/ETH Correlation | Расчёт |
| News Sentiment | RSS/feedparser |
| Trending Coins | CoinGecko |

### Дополнительные фичи (~35 raw features в Feature Builder)

- Market structure (trend, BOS, CHoCH, MSS count)
- Liquidity (equal levels, external levels, FVG count, OB count)
- Volume analysis (volume ratio, compression)
- Multi-timeframe alignment
- HTF bias (W1→D1→H4→H1)
- Context score
- Risk parameters

## 3.4 Предобработка и валидация данных

1. **Последняя свеча отбрасывается** (`df.iloc[:-1]`) — избежание сигнализации на открытой свече (`data/exchange_client.py`)
2. **Dropna** перед анализом ликвидности: `df.dropna(subset=["open", "high", "low", "close", "volume"])`
3. **NaN handling** в индикаторах: проверка `if value is not None`
4. **Price precision** определяется из markets биржи для tick buffer
5. **ATR fallback**: если ATR = 0, используется `entry * atr_fallback_pct / 100`

---

# 4. Торговая стратегия

## 4.1 Логика входа в позицию (Новый пайплайн — scan_symbol_v2)

Архитектура: **Pattern Engine → Feature Builder → Probability Engine → Risk Engine**

### Layer 1: Pattern Engine (`strategy/pattern_engine.py`)

Setup = trigger + confirmation:
- **Trigger:** BOS или sweep
- **Confirmation:** Order Block или FVG

```python
# pattern_engine.py: detect()
# Триггер: sweep (stop hunt) или BOS (break of structure)
# Подтверждение: order block (зона реакции) или FVG (gap ликвидности)
# Setup detected → direction (buy/sell) + components_found
```

**Условия BUY (старый пайплайн, для справки):**
1. Supertrend — восходящий тренд
2. EMA alignment: fast > slow > trend
3. EMA crossover: fast пересекла slow снизу вверх
4. RSI: 55–70 (зона силы)
5. MACD гистограмма положительная
6. ADX > 20 (сильный тренд)
7. Объём > SMA(20) × factor

Минимум 4 из 7 условий + дополнительные фильтры (EMA slope, MACD slope, compression, candle close).

### Layer 2: Feature Builder (`strategy/feature_builder.py`)

~35 raw features в flat vector:
- ICT pattern features (trigger type, confirmation type, components count)
- Market structure (trend, breaks count, swing points)
- Volume (ratio, compression)
- Indicators (RSI, MACD, ADX, ATR)
- MTF alignment
- Context (Fear & Greed, Funding, etc.)
- Risk (regime, volatility)

### Layer 3: Probability Engine (`strategy/probability_engine.py`)

- Оценивает P(TP), expected RR, profit factor
- Rules-based fallback (если нет ML-модели)
- ML (XGBoost/RandomForest) планируется после 100+ historical outcomes
- WeightManager корректирует P(TP) по накопленной статистике

### Layer 4: Risk Engine (`risk/engine.py`)

Hard gates:
- R:R minimum (1.5)
- SL absolute limits (min 1%, max 10%)
- Portfolio risk
- Max active signals
- Position sizing: Kelly criterion с volatility adjustment

### Старый пайплайн (scan_symbol)

Score-based: 4 из 7 индикаторных условий + фильтры:
- EMA slope check
- MACD slope check
- ADX filter (min 20)
- EMA alignment
- EMA spread
- Trigger required
- Candle close
- Min score
- Compression filter

## 4.2 Логика выхода из позиции

### Stop Loss (Invalidation — `strategy/invalidation.py`)

Приоритет определения SL:
1. **Sweep extreme** — low/high sweep level (самый сильный)
2. **OB boundary** — граница Order Block
3. **Swing point** — фрактальный экстремум
4. **BOS level** — уровень break of structure
5. **ATR fallback** — entry ± ATR × 1.5

SL Safety: SL корректируется за пределы текущей свечи (candle low/high + spread + tick + ATR buffer).

### Take Profit (`strategy/trade_engine.py`)

Приоритет TP через Liquidity Map:
1. **External Liquidity** (old highs/lows) — бонус ×2.0
2. **Equal Highs/Lows** — бонус ×1.8
3. **Opposing OB** — бонус ×1.5
4. **Active FVG** — бонус ×1.2
5. **Swept levels** — бонус ×0.5
6. **ATR fallback** — entry ± ATR × 3.0

Score = strength × RR_factor × path_clarity

### Cooldown

- Per `symbol_timeframe`: `SIGNAL_COOLDOWN_MINUTES` (default 45)
- Effective cooldown: `max(base, tf_minutes × SIGNAL_COOLDOWN_TF_MULTIPLIER)` (default 2.0)
- **Persisted in SQLite** — переживает рестарт

## 4.3 Управление размером позиции

**Kelly criterion** с volatility adjustment (`risk/engine.py`):
- Базовая доляKelly: `(p × b - q) / b` где p=winrate, b=RR, q=1-p
- Volatility adjustment: деление на ATR-based volatility
- Режимы: STRONG (1%), MODERATE (0.5%), WEAK (0.25% или отключён)

## 4.4 Управление рисками

| Параметр | Значение | Файл |
|----------|----------|------|
| R:R minimum | 1.5 | `config/settings.py` |
| Min SL distance | 1% | `.env: MIN_SL_DISTANCE_PCT` |
| Max SL distance | 10% | `.env: MAX_SL_DISTANCE_PCT` |
| Max OB distance | 3% | `.env: MAX_OB_DISTANCE_PCT` |
| Risk STRONG | 1% | `.env: RISK_STRONG_PCT` |
| Risk MODERATE | 0.5% | `.env: RISK_MODERATE_PCT` |
| Risk WEAK | 0.25% | `.env: RISK_WEAK_PCT` |
| MIN score for signal | 2 | `.env: MIN_SCORE_FOR_SIGNAL` |
| No-trade min ATR | 0.6% | `.env: NO_TRADE_MIN_ATR_PCT` |
| BLOCK_ALL_SELL | true | `.env: BLOCK_ALL_SELL` |

**Blocked symbols:** JSON-маппинг `{SYMBOL: "buy|sell"}` — блокировка направления по конкретным символам.

## 4.5 Адаптивность стратегии

| Механизм | Описание | Файл |
|----------|----------|------|
| **Market Regime** | ADX/ATR/EMA spread → trending/ranging/volatile | `risk/market_regime.py` |
| **Dynamic Risk** | Размер позиции зависит от regime | `risk/dynamic_risk.py` |
| **Volatility Regime** | ATR-based классификация low/normal/high | `risk/volatility_regime.py` |
| **HTF Bias V2** | W1→D1→H4→H1 multi-TF, block buy continuations against bearish HTF | `market_structure/htf_bias_v2.py` |
| **WeightManager** | Корректировка P(TP) по historical winrate | `strategy/weight_manager.py` |
| **Strategy Mode** | Hot-swappable: v2.5 / confluence / hybrid | `.env: STRATEGY_MODE` |

---

# 5. Исполнение ордеров

## 5.1 Типы ордеров

**Бот НЕ отправляет ордера на биржу.** Он генерирует сигналы с расчётами entry/SL/TP и публикует их в Telegram. Фактическое исполнение — ответственность пользователя.

Однако код содержит клиент для работы с биржей (`data/exchange_client.py`):
- `fetch_ticker_price()` — текущая цена (для outcome tracker)
- `fetch_ohlcv()` — свечные данные
- `create_order()` — создание ордера (заготовка, не используется в основном пайплайне)
- `close_position()` — закрытие позиции (для `/closetrade` admin command)

## 5.2 Обработка комиссий и проскальзывания

В outcome tracker (`scheduler/outcome_tracker.py`):

```python
# PnL after costs:
fee_pct = config.trading.exchange_fee_pct      # комиссия за сторону
slippage_pct = config.trading.slippage_pct      # проскальзывание за сторону
round_trip_cost_pct = (fee_pct + slippage_pct) * 2  # полная стоимость round-trip

# Funding cost для perpetuals:
funding_cost_pct = n_funding_periods * FUNDING_RATE_8H * 100
```

## 5.3 Retry-логика

| Компонент | Стратегия | Файл |
|-----------|----------|------|
| Telegram send_signal | Exponential backoff: `2^attempt` сек | `bot/notifier.py:82-95` |
| Telegram polling start | 5 попыток при Conflict | `main.py:130-140` |
| Telegram error handler | NetworkError/TimedOut → warning, остальное → error | `main.py:119-123` |
| Exchange fetch | Retry через ccxt (built-in) | `data/exchange_client.py` |
| Circuit breaker | After N failures → cooldown | `scheduler/circuit_breaker.py` |

## 5.4 Обработка ошибок API

- **ccxt** автоматически обрабатывает большинство HTTP-ошибок
- **Timeout:** `HTTPXRequest` с `connect_timeout=30.0`, `read_timeout=30.0`
- **Connection pool:** `connection_pool_size=8`
- **Symbol cooldown:** после 3 consecutive failures → skip symbol на 600 сек (`outcome_tracker.py`)
- **ExchangeClient** помечает `connected = False` при ошибке и пытается переподключиться

---

# 6. Функции и модули (детальный разбор)

## 6.1 Точка входа

| Функция | Файл | Назначение |
|---------|------|-----------|
| `main()` | `main.py:55-168` | Основная async функция: init, start, shutdown |
| `acquire_lock()` | `main.py:16-30` | PID lock файл |
| `release_lock()` | `main.py:33-38` | Удаление lock файла |

## 6.2 Конфигурация

| Класс/Функция | Файл | Назначение |
|---------------|------|-----------|
| `AppConfig` | `config/settings.py` | Корневой dataclass конфигурации |
| `TradingConfig` | `config/settings.py` | Параметры индикаторов и торговли |
| `TelegramConfig` | `config/settings.py` | Telegram token, channel, admin IDs |
| `ExchangeConfig` | `config/settings.py` | Exchange name, API keys, market type |
| `RiskConfig` | `config/settings.py` | Risk parameters |
| `SchedulerConfig` | `config/settings.py` | Cron schedule |
| `refresh_runtime_symbols()` | `config/settings.py` | Hot-reload символов из DB |
| `reload_filter_toggles()` | `config/settings.py` | Hot-reload фильтров из DB |

## 6.3 Данные и биржа

| Функция/Класс | Файл | Назначение |
|---------------|------|-----------|
| `ExchangeClient` | `data/exchange_client.py` | Singleton клиент ccxt |
| `fetch_ohlcv()` | `data/exchange_client.py:58-104` | Получение OHLCV (drop last candle) |
| `fetch_ticker_price()` | `data/exchange_client.py:122-140` | Текущая цена |
| `connect()` | `data/exchange_client.py` | Подключение к бирже |
| `close()` | `data/exchange_client.py` | Закрытие сессии |

## 6.4 Индикаторы

| Функция/Класс | Файл | Назначение |
|---------------|------|-----------|
| `IndicatorEngine.calculate()` | `indicators/engine.py:35-180` | Расчёт всех индикаторов |
| `IndicatorValues` | `indicators/engine.py:10-33` | Dataclass с результатами |

## 6.5 Стратегия

| Функция/Класс | Файл | Назначение |
|---------------|------|-----------|
| `PatternEngine.detect()` | `strategy/pattern_engine.py` | Layer 1: ICT pattern detection |
| `FeatureBuilder.build()` | `strategy/feature_builder.py` | Layer 2: ~35 raw features |
| `ProbabilityEngine.estimate()` | `strategy/probability_engine.py` | Layer 3: P(TP), RR, PF |
| `RiskEngine.evaluate()` | `risk/engine.py` | Layer 4: Hard gates + Kelly sizing |
| `TradeEngine.build_trade_plan()` | `strategy/trade_engine.py:33-247` | Liquidity-first trade planning |
| `find_invalidation_buy()` | `strategy/invalidation.py:39-95` | SL для BUY |
| `find_invalidation_sell()` | `strategy/invalidation.py:98-145` | SL для SELL |
| `SignalEngine.generate_signal()` | `strategy/signal_engine.py` | Старый score-based пайплайн |
| `WeightManager.adjust()` | `strategy/weight_manager.py:40-83` | Корректировка P(TP) |
| `TradeThesisManager.update()` | `strategy/trade_thesis.py:158-250` | Lifecycle management |
| `analyze_structure()` | `market_structure/structure.py:450-530` | BOS/CHoCH/MSS detection |
| `get_htf_bias_v2()` | `market_structure/htf_bias_v2.py:72-130` | HTF bias W1→D1→H4→H1 |
| `detect_sweeps()` | `liquidity/sweep.py` | Sweep detection |
| `detect_order_blocks()` | `liquidity/order_blocks.py` | OB detection |
| `detect_fvg()` | `liquidity/fvg.py` | FVG detection |
| `build_liquidity_map()` | `liquidity/pool.py` | Liquidity map construction |

## 6.6 Сканирование

| Функция | Файл | Назначение |
|---------|------|-----------|
| `run_scan_cycle()` | `scheduler/scanner.py` | Основной цикл: scan all symbols × timeframes |
| `scan_symbol_v2()` | `scheduler/scanner.py` | Новый пайплайн (4 layers) |
| `scan_symbol()` | `scheduler/scanner.py` | Старый пайплайн (score-based) |
| `scan_all_tfs()` | `scheduler/tasks.py` | APScheduler entry point |
| `check_open_outcomes()` | `scheduler/outcome_tracker.py` | TP/SL tracking |

## 6.7 Telegram

| Функция | Файл | Назначение |
|---------|------|-----------|
| `send_signal()` | `bot/notifier.py:74-95` | Отправка сигнала в канал |
| `send_signal_blocked()` | `bot/notifier.py:99-130` | Уведомление о заблокированном сигнале |
| `send_error_alert()` | `bot/notifier.py:134-162` | Алерт администраторам |
| `format_context_block()` | `bot/notifier.py:36-72` | Форматирование контекста |
| `register_handlers()` | `bot/handlers.py:155-185` | Регистрация всех команд |
| `handle_menu_callback()` | `bot/menu.py` | Inline menu navigation |
| `_do_full_analysis()` | `bot/menu.py` | Полный анализ для меню |
| `cmd_scan()` | `bot/handlers.py:82-112` | Ручной запуск сканирования |
| `cmd_stats()` | `bot/admin.py` | Статистика торгов |

---

# 7. Конфигурация и параметры

## 7.1 Все настраиваемые параметры

### Telegram

| Параметр | По умолчанию | Описание |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | — | Токен бота (обязательный) |
| `TELEGRAM_CHANNEL_ID` | — | ID канала для сигналов |
| `TELEGRAM_ADMIN_IDS` | — | Список admin ID |

### Exchange

| Параметр | По умолчанию | Описание |
|----------|-------------|---------|
| `EXCHANGE` | `bingx` | Имя биржи |
| `BINANCE_API_KEY` | — | API ключ |
| `BINANCE_API_SECRET` | — | API секрет |
| `MARKET_TYPE` | `swap` | spot / swap / future |

### Trading

| Параметр | По умолчанию | Описание |
|----------|-------------|---------|
| `SYMBOLS` | BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT | Список символов |
| `PRIMARY_TIMEFRAMES` | `1h,4h` | Основные ТФ |
| `CONFIRM_TIMEFRAME` | `15m` | TF подтверждения (удалён из v2) |
| `STRATEGY_MODE` | `v2.5` | Режим стратегии |
| `CANDLES_LIMIT` | `200` | Количество свечей |

### Indicators

| Параметр | По умолчанию | Критичность |
|----------|-------------|-------------|
| `EMA_FAST` | 8 | Средняя |
| `EMA_SLOW` | 21 | Средняя |
| `EMA_TREND` | 55 | Средняя |
| `RSI_PERIOD` | 10 | Средняя |
| `RSI_OVERBOUGHT` | 72 | Средняя |
| `RSI_OVERSOLD` | 28 | Средняя |
| `RSI_BULL_MIN` | 55 | Средняя |
| `RSI_BEAR_MAX` | 45 | Средняя |
| `MACD_FAST/SLOW/SIGNAL` | 8/21/5 | Низкая |
| `ADX_PERIOD` | 14 | Средняя |
| `ADX_MIN` | 20 | **Высокая** (фильтр флэта) |
| `ATR_PERIOD` | 14 | Средняя |
| `ATR_MULTIPLIER_SL` | 1.5 | **Высокая** (SL distance) |
| `ATR_MULTIPLIER_TP` | 3.0 | **Высокая** (TP distance) |
| `SUPERTREND_PERIOD` | 10 | Средняя |
| `SUPERTREND_MULTIPLIER` | 2.5 | Средняя |
| `VOLUME_FACTOR` | 1.5 | Средняя |
| `VOLUME_SMA_PERIOD` | 20 | Низкая |

### Risk

| Параметр | По умолчанию | Критичность |
|----------|-------------|-------------|
| `MIN_RR_THRESHOLD` | 1.5 | **Высокая** |
| `MIN_SL_DISTANCE_PCT` | 1.0% | **Высокая** |
| `MAX_SL_DISTANCE_PCT` | 10.0% | **Высокая** |
| `RISK_STRONG_PCT` | 1.0% | **Высокая** |
| `RISK_MODERATE_PCT` | 0.5% | **Высокая** |
| `RISK_WEAK_PCT` | 0.25% | Средняя |
| `NO_TRADE_MIN_ATR_PCT` | 0.6% | Средняя |

### Filter Toggles

| Параметр | По умолчанию | Описание |
|----------|-------------|---------|
| `EMA_SLOPE_CHECK` | false | Проверка наклона EMA |
| `MACD_SLOPE_CHECK` | false | Проверка наклона MACD |
| `ADX_FILTER_ENABLED` | true | ADX фильтр |
| `EMA_ALIGNMENT_ENABLED` | true | Выравнивание EMA |
| `EMA_SPREAD_ENABLED` | true | Spread EMA |
| `TRIGGER_REQUIRED` | true | Триггер обязателен |
| `CANDLE_CLOSE_ENABLED` | true | Закрытие свечи |
| `MIN_SCORE_ENABLED` | true | Минимальный score |
| `COMPRESSION_ENABLED` | true | Volume compression |

### Market Structure

| Параметр | По умолчанию | Описание |
|----------|-------------|---------|
| `MTF_ENABLED` | true | Multi-TF alignment |
| `MTF_REQUIRED_ALIGNMENT` | 2 | Мин. кол-во aligned HTF |
| `MTF_TIMEFRAMES` | `1d,4h,1h` | HTF для проверки |
| `HTF_HARD_GATE` | true | HTF bias как hard gate |
| `HTF_BIAS_CONTINUATION_PENALTY` | 0.85 | Штраф за контринаправление |

### Context

| Параметр | По умолчанию | Описание |
|----------|-------------|---------|
| `CONTEXT_MIN_VERDICT` | `WEAK` | Минимальный вердикт |
| `NEWS_FILTER_ENABLED` | false | Новостной фильтр |
| `BTC_CORRELATION_ENABLED` | true | BTC correlation |
| `ETH_CORRELATION_ENABLED` | true | ETH correlation |

### Scheduler

| Параметр | По умолчанию | Описание |
|----------|-------------|---------|
| `SCAN_MINUTES` | `:02,:17,:32,:47` | Cron расписание |
| `SIGNAL_COOLDOWN_MINUTES` | 45 | Cooldown между сигналами |
| `SIGNAL_COOLDOWN_TF_MULTIPLIER` | 2.0 | Множитель cooldown по TF |

## 7.2 Критичные параметры для риска и доходности

| Параметр | Влияние |
|----------|---------|
| `ATR_MULTIPLIER_SL` | Определяет размер SL →直接影响 loss per trade |
| `ATR_MULTIPLIER_TP` | Определяет размер TP →直接影响 win size |
| `MIN_RR_THRESHOLD` | Минимальный R:R → фильтрует качество сделок |
| `RISK_STRONG_PCT` | Размер позиции для сильных сигналов →直接影响 capital at risk |
| `ADX_MIN` | Порог фильтра флэта → влияет на количество сигналов |
| `SIGNAL_COOLDOWN_MINUTES` | Частота сигналов → влияет на количество сделок |
| `BLOCK_ALL_SELL` | Глобальная блокировка SELL → исторически WR 33.7% |
| `HTF_HARD_GATE` | HTF bias блокирует buy continuations → фильтрует ~30% сигналов |

---

# 8. Логирование, мониторинг и тестирование

## 8.1 Логирование

**Библиотека:** `loguru==0.7.2`

| Уровень | Использование |
|---------|--------------|
| `DEBUG` | Diagnostic: liquidity map, pattern engine, indicator values |
| `INFO` | Signal sent, scan cycle, bot start/stop |
| `WARNING` | Non-critical errors: context timeout, exchange retries |
| `ERROR` | Critical errors: analysis failures, Telegram send failures |
| `CRITICAL` | Fatal: bot shutdown |

**Направления:**
- Файл: `logs/bot.log` (ротация, `config/logger.py`)
- Telegram: ERROR+ логи отправляются администраторам (`setup_error_sink`)
- Консоль: стандартный вывод

**Особенности:**
- `html.escape()` на динамических подстроках для Telegram HTML (иначе краш на `<`)
- `_do_full_analysis` re-escapes при `BadRequest`

## 8.2 Алерты и уведомления

| Триггер | Канал | Файл |
|---------|-------|------|
| Сигнал (BUY/SELL) | Telegram канал | `bot/notifier.py:74-95` |
| Заблокированный сигнал | Telegram канал | `bot/notifier.py:99-130` |
| Ошибка бота | Telegram admin | `bot/notifier.py:134-162` |
| Закрытие сделки (TP/SL) | Telegram канал | `scheduler/outcome_tracker.py:30-65` |
| Daily report | Telegram канал | `analytics/daily_report.py` |
| Prometheus metrics | HTTP `/metrics` | `monitoring/metrics.py` |

## 8.3 Тестирование

**Фреймворк:** `pytest` + `pytest-asyncio` (auto mode)

**39 тестовых файлов** в `tests/`:

| Категория | Файлы |
|-----------|-------|
| Signal pipeline | `test_signal.py`, `test_new_pipeline.py` |
| Indicators | `test_indicators.py` |
| Market structure | `test_market_structure.py`, `test_htf_bias.py`, `test_htf_bias_v2.py`, `test_mss_diagnostic.py` |
| Liquidity | `test_liquidity.py`, `test_ob_state.py` |
| Risk | `test_risk.py` |
| Backtest | `test_backtest.py`, `test_backtest_parity.py` |
| Database | `test_database.py` |
| Exchange | `test_exchange_client.py` |
| Config | `test_config.py` |
| Admin | `test_admin_commands.py`, `test_admin_symbols.py` |
| Analytics | `test_analytics.py`, `test_scoring.py` |
| Context | `test_context.py` |
| Derivatives | `test_derivatives.py` |
| Metrics | `test_metrics.py` |
| Notifier | `test_notifier.py` |
| Scanner | `test_scanner.py` |
| Architecture | `test_architecture.py` |
| Decision trace | `test_decision_trace.py` |
| Others | `test_cache_ohlcv.py`, `test_levels.py`, `test_ict_menu.py`, `test_main.py`, `test_ops.py`, `test_outcome_tracker.py`, `test_premium_discount.py`, `test_tp_targets.py`, `test_transition_model.py`, `test_webhook.py`, `test_weight_sweep.py` |

**CI/CD:** GitHub Actions (`test.yml`) — `pytest -v --tb=short` на Ubuntu, Python 3.11.

## 8.4 Бэктестинг

**Движок:** `backtest/engine.py` — единый engine с полным паритетом с live-сканером.

```bash
python -m backtest.engine BTC/USDT 1h 336                # console
python -m backtest.engine BTC/USDT 1h 336 --telegram     # post to TG
python -m backtest.engine BTC/USDT 1h 336 --market future
```

**Возможности:**
- Regime detection через `RegimeDetector`
- Structural SL/TP через `calculate_structural_sl/tp`
- Stop hunt buffer
- SL distance guard (min/max)
- RR filter
- Confirmation timeframe
- Комиссия и проскальзывание
- A/B тестирование (`scripts/ab_test_htf_v2.py`)

**Метрики:**
- Win Rate (WR)
- Profit Factor (PF)
- Expectancy
- Max Drawdown
- Sharpe Ratio (в аналитике)
- MFE/MAE (Maximum Favorable/Adverse Excursion)
- Factor analysis (`analysis/factor_analysis_v2.py`)
- Gate funnel analysis (`analytics/gate_funnel.py`)
- Counterfactual analysis (`analytics/counterfactual.py`)

**ML Pipeline (офлайн):**
```bash
python scripts/probe_history_depth.py     # 1. Глубина истории
python -m backtest.cache_ohlcv             # 2. Кэш OHLCV
python -m backtest.replay_dataset          # 3. Размеченный датасет
python scripts/train_prob_model.py         # 4. Walk-forward LR/RF/XGBoost
python scripts/feasibility_report.py       # 5. Go/no-go отчёт
```

---

# 9. Найденные проблемы и риски

## 9.1 Потенциальные баги

| # | Проблема | Файл | Серьёзность |
|---|----------|------|-------------|
| 1 | **Дублирующийся SL adjustment**: в `trade_engine.py` два последовательных `elif signal == SignalType.SELL and sl <= candle_high` блока — второй недостижим | `strategy/trade_engine.py:195-210` | Средняя |
| 2 | **Import deprecated**: `from strategy.scenario_engine import MarketScenario, ScenarioEvaluation` закомментирован, но `TradeThesis` и `TradeThesisManager` всё ещё ссылаются на удалённые типы | `strategy/trade_thesis.py:16,110` | Высокая |
| 3 | **`_dynamic_graphs` и `_dynamic_theses` удалены**, но `hypotheses_command` в `bot/admin.py` всё ещё ссылается на `_dynamic_graphs` | `bot/admin.py:404` | Высокая |
| 4 | **WeightManager** вызывает `scenario_memory.get_stats()`, но `scenario_memory` удалён (import закомментирован) | `strategy/weight_manager.py:73` | Критическая |
| 5 | **Entry trigger** проверяет `hypothesis.entry_price`, но тип `Hypothesis` закомментирован | `strategy/entry_trigger.py:12` | Средняя |
| 6 | **Outcome tracker** не обрабатывает случай, когда `signal.created_at` не имеет timezone (fallback с `replace(tzinfo=None)` может быть некорректным) | `scheduler/outcome_tracker.py:120-130` | Низкая |
| 7 | **`check_positions.py`** в analytics содержит hardcoded позиции (APE, ZEC, ATOM) | `analytics/_check_positions.py` | Низкая |

## 9.2 Проблемы безопасности

| # | Проблема | Файл | Серьёзность |
|---|----------|------|-------------|
| 1 | **`.env` файл присутствует в корне** — содержит реальные API ключи. `.gitignore` исключает `.env`, но файл есть на диске | `.env` | Средняя |
| 2 | **API ключи в `.env`** — единственное хранилище секретов (хорошая практика, но нет шифрования) | `.env` | Информационная |
| 3 | **`BINANCE_API_KEY`/`BINANCE_API_SECRET`** — переменные названы для Binance, но биржа по умолчанию BingX | `.env.example` | Информационная |
| 4 | **pickle models** — `pickle.load()` уязвим для arbitrary code execution | `strategy/transition_model.py:140` | Средняя |
| 5 | **Нет rate limiting на exchange API** — только на Telegram команды | `bot/rate_limit.py` | Средняя |
| 6 | **Web dashboard на `0.0.0.0`** — доступен извне без аутентификации | `.env: WEB_HOST=0.0.0.0` | Высокая |

## 9.3 Узкие места производительности

| # | Проблема | Файл | Влияние |
|---|----------|------|---------|
| 1 | **REST API polling** вместо WebSocket — задержка данных до интервала сканирования | `data/exchange_client.py` | Среднее |
| 2 | **35+ HTTP запросов за цикл** (OHLCV per symbol × TF + HTF + context) | `scheduler/scanner.py` | Среднее |
| 3 | **Outcome tracker** запрашивает ticker каждые 300 сек для каждой открытой сделки | `scheduler/outcome_tracker.py` | Низкое |
| 4 | **In-memory singletons** — контекст кэшируется в модуле, может устаревать | `context/fetcher.py` | Низкое |
| 5 | **SQLite** — нет конкуренции на запись, но может быть bottleneck при большом объёме traces | `storage/database.py` | Низкое |
| 6 | **Pattern engine** анализирует до 100 свечей для OB detection | `liquidity/order_blocks.py` | Низкое |

## 9.4 Архитектурные риски

| # | Проблема | Описание |
|---|----------|---------|
| 1 | **Мёртвый код** | WeightManager, TradeThesis, EntryTrigger ссылаются на удалённые модули (scenario_engine, scenario_memory, hypothesis). Код может крашнуться при вызове. |
| 2 | **Два пайплайна** | Старый (scan_symbol) и новый (scan_symbol_v2) сосуществуют. Сложность поддержки. |
| 3 | **Singleton-зависимость** | Большинство компонентов — module-level singletons. Тестирование затруднено (нужен мокинг). |
| 4 | **Нет graceful degradation** | Если exchange_client не может подключиться, бот падает. |
| 5 | **Конфигурация в ~1000 строк** | `config/settings.py` — монолитный файл со всеми параметрами. |

---

# 10. Рекомендации по улучшению

## 10.1 Критические (незамедлительные)

1. **Удалить мёртвый код:** `WeightManager`, `TradeThesis`, `EntryTrigger`, `_dynamic_graphs`, `_dynamic_theses` — либо восстановить зависимости, либо удалить полностью. Текущее состояние → краш при вызове.

2. **Исправить дублирующийся SL adjustment** в `trade_engine.py:195-210` — убрать недостижимый `elif` блок.

3. **Защитить web dashboard:** добавить basic auth или ограничить доступ по IP (сейчас `0.0.0.0:3001` открыт для всех).

4. **Убрать pickle models** из production: `MLTransitionModel` загружает `.pkl` через `pickle.load()` — уязвимость. Перейти на `json` или `safetensors`.

## 10.2 Архитектурные

5. **Единый пайплайн:** Удалить старый `scan_symbol()` или чётко разделить v1/v2 с feature flags.

6. **WebSocket вместо REST:** Для outcome tracker и real-time данных перейти на WebSocket (ccxt поддерживает).

7. **Decouple singletons:** Внедрить dependency injection для тестирования. Текущие module-level singletons делают unit-тесты хрупкими.

8. **Разделить config:** `config/settings.py` (1042 строки) → несколько модулей по domain (trading, risk, telegram, exchange, etc.).

## 10.3 Стратегия и риск

9. **ML модель в production:** Probability Engine готов к ML, но规则-based fallback используется по умолчанию. Собрать 100+ outcomes и запустить `train_prob_model.py`.

10. **Backtest automation:** Запускать backtest автоматически после каждого логического изменения (CI integration).

11. **Dynamic SELL blocking:** Текущий `BLOCK_ALL_SELL=true` — статичный. Сделать динамическим на основе rolling winrate.

12. **Position sizing в Telegram:** Текущие сигналы не содержат размер позиции. Добавить расчёт (account_size × risk_pct / sl_distance).

## 10.4 Код и тестирование

13. **Type hints:** Большинство функций используют `Optional` и `list` без generic parameters. Добавить полную типизацию.

14. **Docstrings:** Многие функции не имеют docstrings или имеют неполные (особенно в analytics/).

15. **Integration tests:** Текущие тесты в основном unit-тесты. Добавить integration тесты для полного пайплайна (fetch→indicators→signal→notify).

16. **Мониторинг:** Prometheus метрики есть, но не задокументированы. Добавить Grafana dashboard и alerting rules.

## 10.5 Безопасность

17. **Secrets management:** Рассмотреть use of environment variables через Vault или encrypted config для production deployment.

18. **API key rotation:** Добавить support для key rotation без перезапуска.

19. **Audit logging:** Логировать все admin команды с user_id и timestamp.

---

# Дата аудита

**14 августа 2026** — проведён полный технический аудит проекта Trading Signal Bot.

Объём анализа: ~180+ Python-файлов, 39 тестовых файлов, 50+ параметров конфигурации, 4-слойный пайплайн сигналов, 30+ аналитических модулей.
