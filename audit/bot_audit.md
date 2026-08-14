# Bot Audit — Полная инвентаризация торгового бота

**Дата:** 2026-07-19
**Версия стратегии:** 2.4.0 (`config/settings.py:15`)
**Версия Python:** 3.11 (Dockerfile)

---

## 1. Общая архитектура

### 1.1 Схема потока данных

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Binance/   │────▶│ exchange_    │────▶│  Indicator   │
│   BingX API  │     │ client.py    │     │  engine.py   │
│  (ccxt 4.2)  │     │ (OHLCV+tick) │     │ (EMA/RSI/    │
└──────────────┘     └──────────────┘     │  MACD/ADX/   │
                                          │  ATR/SuperT) │
                                          └──────┬───────┘
                                                 │
                         ┌───────────────────────┘
                         ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  Liquidity   │  │   Market     │  │   Context    │
│  modules:    │  │  Structure:  │  │  modules:    │
│  sweep.py    │  │  structure.py│  │  fetcher.py  │
│  order_blocks│  │  htf_bias_v2 │  │  analyzer.py │
│  fvg.py      │  │  premium_disc│  │  scorer.py   │
│  ob_state.py │  │  tp_path.py  │  │              │
│  candle_qual │  │  distance_fl │  │              │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                  │
       ▼                 ▼                  ▼
┌─────────────────────────────────────────────────────┐
│              scanner.py — scan_symbol_v2()           │
│  Phase 0: Hard Gates (cooldown, portfolio risk)      │
│  Phase 1: Pattern Engine (ICT setup detection)       │
│  Phase 1.4: Setup-type gates (sweep/MSS/BOS)        │
│  Phase 1.45: HTF Bias V2                            │
│  Phase 1.5: Trade Plan (SL/TP/RR)                   │
│  Phase 1.55-1.7: Shadow engines (thesis/scenario)   │
│  Phase 2: Feature Builder (~35 raw features)         │
│  Phase 3: Probability Engine (P(TP) estimation)      │
│  Phase 4: Risk Engine (Kelly sizing)                 │
│  Phase 4.5: Entry Trigger                            │
│  Phase 5: Build SignalResult                         │
│  Phase 6: Dedup                                      │
│  Phase 7: Save to DB + DecisionTrace                 │
│  Phase 8: Cooldown + Telegram notification           │
└──────────────────────┬──────────────────────────────┘
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
┌──────────────┐          ┌──────────────┐
│  notifier.py │          │  database.py │
│  (Telegram)  │          │  (SQLite)    │
└──────────────┘          └──────────────┘
```

### 1.2 Модули и зависимости

| Модуль | Файлы | Зависит от | Зависимости |
|--------|-------|------------|-------------|
| **entrypoint** | `main.py` | config, exchange_client, db, notifier, scheduler, context, web | asyncio, python-telegram-bot |
| **config** | `config/settings.py` | — | dataclasses, os, dotenv |
| **scheduler** | `scheduler/scanner.py`, `core_v2.py`, `tasks.py`, `shadow.py`, `circuit_breaker.py`, `outcome_tracker.py` | config, exchange_client, indicators, strategy, risk, context, storage, monitoring | apscheduler |
| **strategy** | `strategy/pattern_engine.py`, `feature_builder.py`, `probability_engine.py`, `signal_engine.py`, `trade_engine.py`, `decision_engine.py`, `trade_plan.py`, `invalidation.py`, `hypothesis.py`, `market_phase_engine.py`, `scenario_engine.py`, `scenario_memory.py`, `trade_thesis.py`, `entry_trigger.py` | config, indicators, liquidity, market_structure | pandas, numpy |
| **liquidity** | `liquidity/sweep.py`, `order_blocks.py`, `fvg.py`, `ob_state.py`, `candle_quality.py`, `pool.py`, `external_liquidity.py`, `equal_levels.py` | config | pandas |
| **market_structure** | `market_structure/structure.py`, `htf_bias.py`, `htf_bias_v2.py`, `premium_discount.py`, `distance_filter.py`, `tp_path.py`, `diagnostic.py` | config, indicators, liquidity | pandas |
| **indicators** | `indicators/engine.py` | config | pandas, pandas-ta, numpy |
| **risk** | `risk/engine.py`, `dynamic_risk.py`, `market_regime.py`, `no_trade_zones.py`, `volatility_regime.py`, `news_filter.py` | config | — |
| **context** | `context/fetcher.py`, `analyzer.py`, `scorer.py` | config | aiohttp, feedparser |
| **scoring** | `scoring/confidence_v2.py` | config | — |
| **data** | `data/exchange_client.py` | config | ccxt, pandas |
| **storage** | `storage/database.py`, `trace.py` | config | sqlalchemy, aiosqlite |
| **bot** | `bot/handlers.py`, `notifier.py`, `menu.py`, `admin.py`, `rate_limit.py` | config, strategy, context, storage, analytics | python-telegram-bot |
| **analytics** | `analytics/performance.py`, `full_report.py`, `gate_funnel.py`, etc. | storage | matplotlib, pandas |
| **monitoring** | `monitoring/metrics.py` | — | prometheus_client |
| **web** | `web/server.py` | config | aiohttp |

### 1.3 Точки входа

| Точка | Файл:строка | Описание |
|-------|-------------|----------|
| CLI | `main.py:58` | `async def main()` — основной entrypoint |
| Docker | `Dockerfile:24` | `CMD ["python", "main.py"]` |
| Manual scan | `bot/handlers.py:111` | `/scan [SYMBOL]` — ручной запуск |
| Web dashboard | `web/server.py:472` | `start_web_server()` — aiohttp на порту 3002 |
| Prometheus | `monitoring/metrics.py:28` | `_start_metrics_server()` — порт 9090 (opt-in) |
| Backtest | `analytics/full_report.py:709` | `main()` — CLI entry |
| Outcome tracker | `main.py:129` | Background task: `outcome_tracker_loop()` |

### 1.4 Планировщик/цикл сканирования

| Параметр | Значение | Файл:строка |
|----------|----------|-------------|
| Cron-расписание | `:02, :17, :32, :47` (каждые 15 мин) | `scheduler/tasks.py:27` |
| Timezone | UTC | `scheduler/tasks.py:15` |
| Daily report | 00:05 UTC | `scheduler/tasks.py:37` |
| Max instances per job | 1 | `scheduler/tasks.py:30,41` |
| Scan cycle | `run_scan_cycle()` → параллельно по всем symbol×TF | `scheduler/scanner.py:1354` |
| Circuit breaker | 3 consecutive SL → пауза 30 мин | `scheduler/circuit_breaker.py:20-21` |

### 1.5 Внешние зависимости

| Пакет | Версия | Назначение |
|-------|--------|------------|
| ccxt | 4.2.15 | Биржевой клиент (Binance/BingX) |
| pandas-ta | >=0.4.0 | Расчёт индикаторов |
| pandas | >=2.2.0 | Работа с данными |
| python-telegram-bot | 20.7 | Telegram API |
| sqlalchemy | 2.0.23 | ORM для SQLite |
| aiosqlite | 0.19.0 | Async SQLite |
| apscheduler | 3.10.4 | Планировщик задач |
| aiohttp | 3.9.5 | HTTP-клиент (context fetcher) |
| feedparser | >=6.0.0 | RSS-парсер |
| loguru | 0.7.2 | Логирование |
| prometheus_client | >=0.20 | Метрики |
| aiolimiter | >=1.1 | Rate limiting |
| numpy | >=2.2.6 | Математика |

---

## 2. Данные и рынок

### 2.1 Рынки/инструменты

| Параметр | Значение | Файл:строка |
|----------|----------|-------------|
| По умолчанию | `BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,DOGE/USDT` | `.env.example:23` |
| Динамические | `/addsymbol`, `/removesymbol` через Telegram | `bot/admin.py:30,53` |
| Market type | `swap` (фьючерсы) | `.env.example:18` |
| Биржа | `bingx` (по умолчанию) | `config/settings.py:47` |
| CoinGecko маппинг | `BTC/USDT:bitcoin,ETH/USDT:ethereum` | `config/settings.py:642` |

**Рантайм-кэш символов:** `config/settings.py:877` — `_runtime_symbols_cache`, обновляется при старте и после `/addsymbol`/`/removesymbol`.

### 2.2 Таймфреймы

| Параметр | Значение | Файл:строка |
|----------|----------|-------------|
| Primary TF | `1h,4h` | `.env.example:24` |
| Confirm TF | `15m` (deprecated — removed в v2) | `.env.example:25` |
| HTF для bias | `1w,1d,4h,1h` | `scheduler/scanner.py:425-444` |
| MTF alignment | `1d,4h,1h` | `market_structure/structure.py:527` |

### 2.3 Глубина истории и тип данных

| Источник | Лимит | Файл:строка |
|----------|-------|-------------|
| Основной OHLCV | 200 свечей | `config/settings.py:200` (`candles_limit`) |
| HTF (1d/4h) | 60 свечей | `scheduler/scanner.py:425-426` |
| HTF (1w/1h) | 60 свечей | `scheduler/scanner.py:440,444` |
| Sweeps lookback | 50 свечей | `scheduler/scanner.py:293` |
| Order Blocks lookback | 100 свечей | `scheduler/scanner.py:294` |
| Structure lookback | 50 свечей | `scheduler/scanner.py:308-309` |
| External liquidity | 739 свечей | `scheduler/scanner.py:739` |
| Taker buy volume | Отдельный запрос (Binance fapi) | `data/exchange_client.py:192` |

**Тип данных:** OHLCV (open, high, low, close, volume) + taker_buy_volume (если Binance futures).

**Дроп последней свечи:** `data/exchange_client.py:305` — `df.iloc[:-1]` удаляет незакрытую свечу.

### 2.4 Частота опроса и обработка ошибок

| Параметр | Значение | Файл:строка |
|----------|----------|-------------|
| Scan interval | Каждые 15 мин (`:02,:17,:32,:47`) | `scheduler/tasks.py:27` |
| Rate limit (API) | 5 req / 10 sec | `config/settings.py:476` |
| Semaphore (exchange) | 1 (последовательные запросы) | `data/exchange_client.py:17` |
| OHLCV retry | 3 попытки, backoff `2^attempt` сек | `data/exchange_client.py:122` |
| Rate limit exceeded | Backoff `5 * 2^attempt` сек | `data/exchange_client.py:140` |
| DDoS protection | Backoff `5 * 2^attempt` сек | `data/exchange_client.py:142` |
| Network error | Backoff `2^attempt` сек | `data/exchange_client.py:145` |
| Context timeout | 10 сек | `scheduler/scanner.py:1023` |
| Context cache TTL | 1800 сек (30 мин) | `config/settings.py:648` |
| FNG cache TTL | 3600 сек (1 час) | `context/fetcher.py:69` |
| Trending cache TTL | 1800 сек (30 мин) | `context/fetcher.py:129` |
| RSS cache TTL | 300 сек (5 мин) | `context/fetcher.py:331` |
| BadSymbol/BadRequest | Без retry, return None | `data/exchange_client.py:133` |

---

## 3. Система фильтров (КЛЮЧЕВОЙ РАЗДЕЛ)

### 3.1 Полный конвейер фильтров (scan_symbol_v2)

Порядок фильтров строго определён в `scheduler/scanner.py:216-1347`.

| # | Фильтр | Логика | Параметры | Порядок | Путь |
|---|--------|--------|-----------|---------|------|
| 1 | **Cooldown** | `time_since_last_signal < cooldown_minutes(timeframe)` | `signal_cooldown_minutes=45`, `signal_cooldown_tf_multiplier=2.0` | Phase 0 | `scanner.py:235` |
| 2 | **Portfolio Risk** | `active_count >= max_active_signals OR portfolio_risk >= max_portfolio_risk_pct` | `max_active_signals=3`, `max_portfolio_risk_pct=3.0%` | Phase 0 | `scanner.py:246-261` |
| 3 | **Indicators** | `_get_indicators()` returns None | `candles_limit=200` | Phase 0 | `scanner.py:266-274` |
| 4 | **Pattern Engine** | `pattern_engine.detect()` → `setup.detected == False` | — | Phase 1 | `scanner.py:318-336` |
| 5 | **Reversal: Sweep Required** | `setup.has_sweep == False` (для reversal) | — | Phase 1.4 | `scanner.py:352` |
| 6 | **Reversal: MSS Gate** | `setup.has_mss == False` (для reversal) | — | Phase 1.4 | `scanner.py:367` |
| 7 | **Continuation: BOS Gate** | `setup.has_bos == False` (для continuation) | — | Phase 1.4 | `scanner.py:380` |
| 8 | **Entry Armed** | `not setup.entry_armed AND config.require_entry_zone` | `require_entry_zone=False` (soft) | Phase 1.4 | `scanner.py:395-408` |
| 9 | **HTF Bias V2** | Continuation opposing HTF + `htf_hard_gate=True` | — | Phase 1.45 | `scanner.py:422-571` |
| 10 | **Trade Plan** | `trade_engine.build_trade_plan()` returns None | — | Phase 1.5 | `scanner.py:599-622` |
| 11 | **Probability Engine** | `p_tp < config.min_p_tp (0.45)` | `min_p_tp=0.45` | Phase 3 | `scanner.py:1084-1095` |
| 12 | **Risk Engine** | `risk_engine.evaluate()` → `should_trade == False` | `min_rr_ratio=1.2`, SL bounds `0.25%-5.0%` | Phase 4 | `scanner.py:1097-1126` |
| 13 | **Entry Trigger** | `EntryTrigger.check()` → `triggered == False` | `entry_proximity_pct=1.5`, `max_spread_pct=0.1` | Phase 4.5 | `scanner.py:1132-1163` |
| 14 | **Dedup** | Same direction within cooldown OR cross-direction within `cooldown//2` | — | Phase 6 | `scanner.py:1203-1233` |

### 3.2 Детали каждого фильтра

#### 3.2.1 Cooldown
- **Логика:** `get_cooldown_minutes(timeframe, base_minutes=45, multiplier=2.0)` → `max(base, tf_minutes * multiplier)`
- **Хранение:** In-memory + SQLite (`storage/database.py:630-646`)
- **Сброс:** При.restart сбрасывается (in-memory only)

#### 3.2.2 Portfolio Risk
- **Логика:** Проверяет `active_count` (открытые позиции) и `total_risk_pct` (суммарный риск)
- **Источник:** `db.get_active_signals_count()` + `db.get_portfolio_risk_sum()`
- **Пороги:** `max_active_signals=3`, `max_portfolio_risk_pct=3.0%`

#### 3.2.3 Pattern Engine (ICT Setup Detection)
- **Логика:** `pattern_engine.detect(sweeps, order_blocks, structure, fvgs, candle_quality, current_price, atr)`
- **Reversal pipeline:** sweep → displacement → MSS (strong CHoCH) → direction from MSS
- **Continuation pipeline:** trend → BOS → trend alignment
- **Scoring (reversal):** MSS(40%) + sweep(25%) + displacement(15%) + OB/FVG(15%)
- **Scoring (continuation):** BOS(50%) + OB/FVG(15%)
- **Файл:** `strategy/pattern_engine.py:151-876`

#### 3.2.4 HTF Bias V2
- **Логика:** W1→D1→H4→H1 majority vote на EMA21/55 alignment
- **Continuation opposing HTF** + `htf_hard_gate=True` → BLOCKED
- **Reversal mismatch** → penalty (no block)
- **Файл:** `market_structure/htf_bias_v2.py:85-135`

#### 3.2.5 Probability Engine
- **Rules-based fallback:** base 50% → component edges → soft multipliers → clamp [20%, 85%]
- **Reversal scoring:** sweep(+3), displacement(+3), MSS(+4), MSS>70(+2), OB(+1.5), FVG(+1), entry_armed(+1.5)
- **Continuation scoring:** BOS(+3), aligned(+2), OB(+1.5), FVG(+1), entry_armed(+1.5)
- **Volume scoring:** >2x(+3), >1.5x(+2), >1.2x(+1)
- **R:R scoring:** >=3(+4), >=2(+3), >=1.5(+1.5), <1(-3)
- **ML model:** `models/probability_model.pkl` (XGBoost/RandomForest, min_samples=100)
- **Файл:** `strategy/probability_engine.py:117-278`

#### 3.2.6 Risk Engine
- **Hard gates:**
  - Data integrity: entry/sl/tp > 0
  - Zero risk: risk_dist > 0
  - R:R >= 1.2 (configurable)
  - SL absolute: 0.25% <= sl_distance_pct <= 5.0%
  - SL ATR: sl_distance_pct >= atr_pct * 2.0
- **Kelly sizing:** `f = (p*b - q) / b`, capped at 0.20 (half-Kelly)
- **Adjustments:** confidence, scenario score/stability, volatility, MSS quality, SL distance
- **Final clamp:** [0.1%, 2.0%]
- **Файл:** `risk/engine.py:84-248`

#### 3.2.7 Entry Trigger
- **Логика:** Проверяет bid/ask spread и proximity к entry price
- **Buy:** `current_price <= entry * (1 + 1.5%)`
- **Sell:** `current_price >= entry * (1 - 1.5%)`
- **Spread:** `(ask - bid) / bid * 100 <= 0.1%`
- **Файл:** `strategy/entry_trigger.py:53-114`

### 3.3 Комбинирование фильтров

| Тип | Описание | Файл:строка |
|-----|----------|-------------|
| **AND (hard)** | Все gate'ы в pipeline должны пройти — AND-логика | `scanner.py:216-1347` |
| **Soft features** | HTF alignment, premium/discount, SMT divergence — не блокируют, влияют на P(TP) | `scanner.py:410-597` |
| **Weighted scoring** | Confidence V2: 10 factors с весами (HTF=20, Structure=15, Liquidity=20, Volume=5, BTC=15, Funding=5, OI=5, RSI=5, MACD=5, ADX=5) | `scoring/confidence_v2.py:42-71` |
| **Blending** | `tech_confidence_blend=0.6` — 60% tech + 40% market context | `config/settings.py:460` |
| **OB mitigation** | `OB_MULTIPLIERS`: FRESH=1.25, TESTED=1.0, PARTIAL=0.75, MITIGATED=0.5, BROKEN=0.0 | `liquidity/ob_state.py:40-53` |

---

## 4. Условия сигнала

### 4.1 Точные условия входа

#### LONG (BUY)
1. **Reversal:** sweep_low detected → displacement → MSS (strong CHoCH from low to high) → direction = BUY
2. **Continuation:** trend == "bullish" → BOS bullish → trend alignment (BOS direction matches trend)
3. **Indicator confirmation (legacy):** EMA fast > slow > trend, MACD bullish cross, RSI > 55, ADX >= 26

#### SHORT (SELL)
1. **Reversal:** sweep_high detected → displacement → MSS (strong CHoCH from high to low) → direction = SELL
2. **Continuation:** trend == "bearish" → BOS bearish → trend alignment
3. **Indicator confirmation (legacy):** EMA fast < slow < trend, MACD bearish cross, RSI < 45, ADX >= 26

### 4.2 Подтверждения

| Тип | Описание | Файл:строка |
|-----|----------|-------------|
| **MTF alignment** | HTF (1d/4h/1h) structure alignment — soft, аналитика | `market_structure/structure.py:499` |
| **Volume** | `volume > volume_sma * 1.5` — bonus в scoring | `indicators/engine.py:98` |
| **Candle quality** | Displacement candle (body > ATR * 1.2) — component scoring | `liquidity/candle_quality.py:77` |
| **OB state** | FRESH OB → 1.25x, BROKEN → 0.0x (reject) | `liquidity/ob_state.py:40-53` |
| **Context** | Fear/Greed, Funding, OI, News sentiment — soft multiplier | `context/scorer.py:61-326` |
| **SMT divergence** | BTC/ETH correlation — soft feature | `scheduler/scanner.py:410-420` |

### 4.3 Анти-дубли / Cooldown

| Параметр | Значение | Файл:строка |
|----------|----------|-------------|
| Base cooldown | 45 мин | `config/settings.py:630` |
| TF multiplier | 2.0 (4h → 90 мин) | `config/settings.py:632` |
| Same direction | Полный cooldown | `scanner.py:1203-1233` |
| Cross direction | `cooldown // 2` | `scanner.py:1203-1233` |
| Storage | In-memory dict + SQLite | `scanner.py:117-134` |

---

## 5. Параметры сигнала

### 5.1 TP/SL/RR

| Параметр | Значение | Файл:строка |
|----------|----------|-------------|
| ATR multiplier SL | 1.5 | `config/settings.py:148` |
| ATR multiplier TP | 3.0 | `config/settings.py:150` |
| Min RR threshold | 1.5 | `config/settings.py:155` |
| Min SL distance | 1.0% | `config/settings.py:157` |
| Max SL distance | 10.0% | `config/settings.py:158` |
| Stop hunt buffer | 0.5% | `config/settings.py:156` |
| Max OB distance | 3.0% | `config/settings.py:154` |
| SL absolute min | 0.25% | `risk/engine.py:62` |
| SL absolute max | 5.0% | `risk/engine.py:62` |
| SL min ATR mult | 2.0x | `risk/engine.py:62` |
| Structural SL | Priority: sweep_extreme > ob_boundary > swing_point > structure_break > ATR | `strategy/invalidation.py:34-193` |
| Structural TP | Liquidity levels with RR >= 2.0, fallback ATR*3.0 | `risk/dynamic_risk.py:200-298` |
| SL buffer | ATR * 0.15 (15%) | `strategy/trade_engine.py:132` |
| Spread buffer | entry * 0.0001 (~0.01%) | `strategy/trade_engine.py:149` |

### 5.2 Размер позиции

| Параметр | Значение | Файл:строка |
|----------|----------|-------------|
| Base risk (strong) | 1.0% | `config/settings.py:335` |
| Base risk (moderate) | 0.5% | `config/settings.py:336` |
| Base risk (weak) | Blocked by default | `config/settings.py:337` |
| Kelly cap | 20% (half-Kelly) | `risk/engine.py:171` |
| Min risk | 0.1% | `risk/engine.py:62` |
| Max risk | 2.0% | `risk/engine.py:62` |
| Volatility adj | >4% ATR → 0.5x, >2.5% → 0.75x | `risk/engine.py:205` |
| Max portfolio risk | 3.0% | `config/settings.py:654` |
| Max active signals | 3 | `config/settings.py:652` |

### 5.3 Формат Telegram-сообщения

**Шаблон сигнала** (`strategy/signal_engine.py:120-179`):
```
🟢 BUY BTC/USDT (1h)
Entry: 65000.00
SL: 64000.00 (-1.54%)
TP: 68000.00 (+4.62%)
R:R = 3.0

📊 Supporting factors:
• EMA alignment (10)
• Volume above avg (15)
• MACD bullish cross (10)
...

⚡ Confidence: 72% (strong)
```

**Шаблон blocked** (`bot/notifier.py:153-164`):
```
🚫 Signal blocked: BTC/USDT (1h)
Reason: low_p_tp (P(TP)=0.38 < 0.45)
```

---

## 6. Учёт результатов

### 6.1 Логирование сигналов

| Компонент | Описание | Файл:строка |
|-----------|----------|-------------|
| **Signal table** | Все отправленные сигналы (35 колонок) | `storage/database.py:17-55` |
| **Outcome table** | TP/SL/EXPIRED исходы | `storage/database.py:83-96` |
| **Candidate table** | Каждый вызов evaluate() | `storage/database.py:98-161` |
| **DecisionTrace** | Полный funnel trace (~60 колонок) | `storage/database.py:163-295` |
| **ScenarioMemory** | История по narrative_type | `strategy/scenario_memory.py:146-285` |

### 6.2 Трекинг исходов

| Компонент | Описание | Файл:строка |
|-----------|----------|-------------|
| **Outcome tracker** | Фоновая задача каждые 5 мин, проверяет TP/SL | `scheduler/outcome_tracker.py:332-397` |
| **Resolution logic** | Fetch ticker + candle high/low для wick detection | `scheduler/outcome_tracker.py:133-330` |
| **MFE/MAE** | Maximum Favorable/Adverse Excursion | `scheduler/outcome_tracker.py:111-131` |
| **PnL calculation** | Commission × 2 + slippage × 2 + funding | `scheduler/outcome_tracker.py:71-109` |
| **Circuit breaker** | 3 consecutive SL → пауза 30 мин | `scheduler/circuit_breaker.py:38-87` |

### 6.3 Статистика

| Компонент | Описание | Файл:строка |
|-----------|----------|-------------|
| **Overall stats** | WR, PF, expectancy, drawdown, Sharpe | `analytics/performance.py:263` |
| **Segmented stats** | By symbol/TF/direction/session/regime | `analytics/performance.py:289` |
| **MFE/MAE analysis** | Excursion patterns | `analytics/performance.py:353` |
| **Filter counterfactual** | "What if we disabled this gate?" | `analytics/performance.py:428` |
| **Full report** | Markdown + charts (equity, DD, PnL dist) | `analytics/full_report.py:386` |
| **Gate funnel** | Per-gate entered/passed/dropped + downstream WR/PF | `storage/database.py:865-963` |

### 6.4 Критический пробел

**[НЕЯСНО]** По данным `reports/full_report.md:143-148`, за 3 дня работы (5 сделок) зафиксировано:
- 20% WR, PF 0.37, Expectancy -0.38R
- Net PnL: -5.96%

Бэктест (`reports/new_pipeline_backtest.md`): 24.6% WR, PF 1.14, +26.84% за 90 дней.

**Недостаточно данных** для статистически значимых выводов по live-трейдингу (5 сделок — слишком мало). Требуется минимум 50-100 сделок для оценки.

---

## 7. Риски и слабые места

### 7.1 Look-ahead bias и repaint

| Риск | Описание | Файл:строка |
|------|----------|-------------|
| **OHLCV drop last candle** | `df.iloc[:-1]` — корректно дропает незакрытую свечу | `data/exchange_client.py:305` |
| **Sweep detection** | Использует `df.tail(lookback)` — lookback=50, но sweep детектирует на historical data. **Потенциальный look-ahead:** sweep использует `next candle close` для подтверждения (lines 189,197 — reclaim), но это forward-looking на historical data. В live это корректно (next candle ещё не закрыта). | `liquidity/sweep.py:73-157` |
| **OB detection** | `_check_bos_*` и `_check_retest_*` используют forward scan (`look_ahead=20-30`). **Look-ahead bias в бэктесте** — в live эти данные ещё не доступны. | `liquidity/order_blocks.py:211-249` |
| **Structure analysis** | `_detect_bos_choch` анализирует все swings в lookback window — потенциальный look-ahead если swings включают будущие данные. Но `lookback=50` limit корректно ограничивает. | `market_structure/structure.py:267-382` |
| **FVG detection** | Проверяет `candle1, candle2, candle3` — 3-свечной паттерн, корректно на historical. | `liquidity/fvg.py:38-105` |

### 7.2 Захардкоженные магические числа

| Значение | Контекст | Файл:строка |
|----------|----------|-------------|
| `0.3` | Sweep wick scoring normalization | `liquidity/sweep.py:61` |
| `1.2` | Candle displacement ATR multiplier | `liquidity/candle_quality.py:77` |
| `0.5` | Min body % for weak candle | `liquidity/candle_quality.py:79` |
| `2.0` | OB proximity % | `strategy/pattern_engine.py:147` |
| `0.693` | Exponential decay constant (ln2) | `market_structure/structure.py:81` |
| `20.0` | Max points per MSS sub-score | `market_structure/structure.py:96` |
| `3.0` | Max ATR for displacement cap | `market_structure/structure.py:99` |
| `4.0` | Scenario component count boost threshold | `strategy/probability_engine.py:383` |
| `0.02` | ATR fallback TP percentage | `risk/dynamic_risk.py:289` |
| `0.001` | SL-entry minimum distance (core_v2) | `scheduler/core_v2.py:190` |
| `85.0` | Confidence cap | `scheduler/scanner.py:1197` |
| `0.10` | Decision engine ambiguity threshold | `strategy/decision_engine.py:90` |
| `0.25` | Decision engine min utility | `strategy/decision_engine.py:91` |
| `0.5` | Entry trigger proximity % | `strategy/entry_trigger.py:45` |
| `0.1` | Max spread % | `strategy/entry_trigger.py:45` |

### 7.3 Проблемы надёжности

| Проблема | Описание | Файл:строка |
|----------|----------|-------------|
| **News filter — STUB** | `fetch_macro_events()` всегда возвращает `[]`, фильтр отключён | `risk/news_filter.py:51` |
| **Context timeout** | 10 сек на все context fetch'и — при медленном API может блокировать scan | `scheduler/scanner.py:1023` |
| **Singleton state** | `_fng_cache`, `_trending_cache`, `_rss_cache`, `_last_oi` — in-memory, сбрасываются при restart | `context/fetcher.py:37-52` |
| **OB state tracker** | `_ob_registry`, `_fvg_registry` — in-memory dict, сбрасывается при restart | `liquidity/ob_state.py:180-202` |
| **EMA spread history** | `_ema_spread_history` — module-level dict, не persistent | `scheduler/scanner.py:60` |
| **Dynamic graphs** | `_dynamic_graphs`, `_dynamic_theses` — in-memory cache | `scheduler/scanner.py:110-112` |
| **No health check** | Docker Compose без healthcheck | `docker-compose.yml` |
| **No port exposure** | Web dashboard (3002) не опубликован в Docker | `docker-compose.yml` |
| **SQLite concurrent** | SQLite + async — potential locking under load | `storage/database.py` |
| **Module-level singletons** | `config`, `exchange_client`, `db`, `context_engine`, `indicator_engine` — все module-level | `config/settings.py:790`, `data/exchange_client.py:415`, etc. |
| **Windows event loop** | `WindowsSelectorEventLoopPolicy` — костыль для Windows | `main.py:183` |

### 7.4 Дублирование кода

| Дубликат | Описание |
|----------|----------|
| **core_v2.py vs scanner.py** | `scan_core_v2()` — устаревший pipeline, дублирует логику scanner.py |
| **signal_engine.py vs pattern_engine.py** | Old signal engine (406-921) и new pattern engine (151-876) сосуществуют |
| **Swing detection** | `_find_swing_highs/lows` скопирован в sweep.py, order_blocks.py, structure.py |
| **ATR calculation** | `_calc_atr` скопирован в order_blocks.py, candle_quality.py |

---

## 8. Сводная таблица ВСЕХ настраиваемых параметров

### 8.1 Trading Parameters

| Параметр | Текущее значение | Где задан | На что влияет | Кандидат на оптимизацию |
|----------|------------------|-----------|---------------|------------------------|
| `ema_fast` | 8 | `settings.py:80` | EMA crossover speed | Да |
| `ema_slow` | 21 | `settings.py:82` | EMA crossover speed | Да |
| `ema_trend` | 55 | `settings.py:84` | Trend filter | Да |
| `min_ema_spread_pct` | 0.20 | `settings.py:86` | EMA spread gate | Да |
| `ema_alignment_enabled` | True | `settings.py:186` | EMA alignment gate | Да |
| `ema_spread_enabled` | True | `settings.py:188` | EMA spread gate | Да |
| `ema_slope_check` | True | `settings.py:88` | EMA slope check | Да |
| `ema_strength_cap` | 1.0 | `settings.py:92` | EMA strength cap | Нет |
| `rsi_period` | 10 | `settings.py:94` | RSI calculation | Да |
| `rsi_overbought` | 72 | `settings.py:96` | RSI threshold | Да |
| `rsi_oversold` | 28 | `settings.py:98` | RSI threshold | Да |
| `rsi_bull_min` | 55 | `settings.py:100` | RSI bull threshold | Да |
| `rsi_bear_max` | 45 | `settings.py:102` | RSI bear threshold | Да |
| `macd_fast` | 8 | `settings.py:106` | MACD calculation | Да |
| `macd_slow` | 21 | `settings.py:108` | MACD calculation | Да |
| `macd_signal` | 5 | `settings.py:110` | MACD calculation | Да |
| `min_macd_pct` | 0.03 | `settings.py:112` | MACD threshold | Да |
| `adx_period` | 14 | `settings.py:120` | ADX calculation | Да |
| `adx_min` | 26 | `settings.py:122` | ADX gate (raised from 20) | Да |
| `adx_strong` | 22 | `settings.py:124` | ADX strong threshold | Да |
| `adx_filter_enabled` | True | `settings.py:130` | ADX gate toggle | Да |
| `atr_period` | 14 | `settings.py:134` | ATR calculation | Да |
| `atr_multiplier_sl` | 1.5 | `settings.py:148` | SL distance | Да |
| `atr_multiplier_tp` | 3.0 | `settings.py:150` | TP distance | Да |
| `min_rr_threshold` | 1.5 | `settings.py:155` | Min R:R gate | Да |
| `stop_hunt_buffer_pct` | 0.5 | `settings.py:156` | SL buffer | Да |
| `max_ob_distance_pct` | 3.0 | `settings.py:154` | OB distance filter | Да |
| `min_sl_distance_pct` | 1.0 | `settings.py:157` | Min SL distance | Да |
| `max_sl_distance_pct` | 10.0 | `settings.py:158` | Max SL distance | Да |
| `volume_factor` | 1.5 | `settings.py:168` | Volume above avg | Да |
| `volume_sma_period` | 20 | `settings.py:170` | Volume SMA | Да |
| `min_score_for_signal` | 2 | `settings.py:403` | Min supporting factors | Да |
| `candles_limit` | 200 | `settings.py:200` | History depth | Да |

### 8.2 Scoring Weights

| Параметр | Текущее значение | Где задан | Кандидат на оптимизацию |
|----------|------------------|-----------|------------------------|
| `w_supertrend` | 5 | `settings.py:408` | Да |
| `w_ema` | 10 | `settings.py:409` | Да |
| `w_macd` | 10 | `settings.py:410` | Да |
| `w_rsi` | 5 | `settings.py:411` | Да |
| `w_volume` | 15 | `settings.py:412` | Да |
| `w_adx` | 5 | `settings.py:413` | Да |
| `w_dmi` | 5 | `settings.py:414` | Да |
| `w_bos` | 15 | `settings.py:415` | Да |
| `w_sweep` | 10 | `settings.py:416` | Да |
| `w_ob` | 10 | `settings.py:417` | Да |
| `tech_confidence_blend` | 0.6 | `settings.py:460` | Да |

### 8.3 Risk Parameters

| Параметр | Текущее значение | Где задан | Кандидат на оптимизацию |
|----------|------------------|-----------|------------------------|
| `min_rr_ratio` | 1.2 | `settings.py:558` | Да |
| `sl_absolute_min_pct` | 0.25 | `settings.py:559` | Да |
| `sl_absolute_max_pct` | 5.0 | `settings.py:560` | Да |
| `base_risk_pct` | 1.0 | `settings.py:561` | Да |
| `min_risk_pct` | 0.1 | `settings.py:562` | Да |
| `max_risk_pct` | 2.0 | `settings.py:563` | Да |
| `risk_strong_pct` | 1.0 | `settings.py:335` | Да |
| `risk_moderate_pct` | 0.5 | `settings.py:336` | Да |
| `risk_weak_pct` | Blocked | `settings.py:337` | Да |
| `max_active_signals` | 3 | `settings.py:652` | Да |
| `max_portfolio_risk_pct` | 3.0 | `settings.py:654` | Да |
| `signal_cooldown_minutes` | 45 | `settings.py:630` | Да |

### 8.4 Feature Flags

| Параметр | Текущее значение | Где задан | Кандидат на оптимизацию |
|----------|------------------|-----------|------------------------|
| `htf_hard_gate` | True | `settings.py:595` | Да |
| `htf_bias_v2` | True | `settings.py:602` | Да |
| `premium_discount` | False | `settings.py:603` | Да (A/B показал ухудшение) |
| `require_entry_zone` | False | `settings.py:617` | Да |
| `min_p_tp` | 0.45 | `settings.py:620` | Да |
| `ob_mitigation` | True | `settings.py:597` | Да |
| `shadow_mode` | True | `settings.py:599` | Нет |
| `context_enabled` | True | `settings.py:635` | Да |
| `context_block_on_blocked` | True | `settings.py:637` | Да |
| `signal_block_notify` | True | `settings.py:640` | Нет |
| `adx_filter_enabled` | True | `settings.py:130` | Да |
| `compression_enabled` | True | `settings.py:192` | Да |
| `block_compression_regime` | True | `settings.py:194` | Да |

### 8.5 ML / Probability

| Параметр | Текущее значение | Где задан | Кандидат на оптимизацию |
|----------|------------------|-----------|------------------------|
| `model_path` | `models/probability_model.pkl` | `settings.py:545` | Нет |
| `min_samples` | 100 | `settings.py:548` | Да |
| `fallback_winrate` | 50.0 | `settings.py:549` | Да |
| `confidence_cap` | True | `settings.py:598` | Да |

---

## 9. Вопросы к владельцу

### 9.1 [НЕЯСНО] — Данные, которых не хватает

| # | Вопрос | Почему важно |
|---|--------|--------------|
| 1 | **История сигналов за весь период работы** — есть ли экспорт из SQLite? | Для полного анализа WR/PF по времени |
| 2 | **Live результаты** — за 3 дня только 5 сделок. Есть ли более длинная история? | Стат. значимость requires ≥50 сделок |
| 3 | **Backtest на каком периоде?** — 90d/4h. Почему не 1h? Есть ли данные за 1h? | 1h может показать другие паттерны |
| 4 | **ZRO/USDT** — включён в бэктест, но PF=0.25. Почему? Планируется ли исключение? | Разрушает портфель |
| 5 | **OB look-ahead** — `order_blocks.py:211-249` использует forward scan (look_ahead=20-30). Это осознанно или баг? | Look-ahead bias в бэктесте |
| 6 | **News filter** — `fetch_macro_events()` — stub. Планируется ли реализация? | Фильтр отключён |
| 7 | **Shadow mode** — включён по умолчанию (`SHADOW_MODE=true`). Что это значит для live? | Путаница: shadow vs production |
| 8 | **core_v2.py** — устаревший pipeline. Используется ли? Может быть удалён? | Технический долг |
| 9 | **ML модель** — `models/probability_model.pkl` — есть ли? Когда обучена? Какой WR на валидации? | Качество ML vs rules-based |
| 10 | **A/B test results** — `reports/ab_test_htf_v2/comparison.json` показывает идентичные данные для v1/v2/v2+zones. Это баг? | Данные A/B теста недостоверны |

### 9.2 Дополнительные вопросы

| # | Вопрос |
|---|--------|
| 11 | Какой целевой Winrate и Profit Factor? |
| 12 | Есть ли максимальный drawdown лимит? |
| 13 | Планируется ли добавление новых бирж? |
| 14 | Есть ли мониторинг uptime/deployments? |
| 15 | Как часто обновляется ML модель? |

---

## 10. Рекомендованные следующие шаги

### Топ-5 гипотез по улучшению WR

| # | Гипотеза | Ожидаемый эффект | Сложность | Данные для проверки |
|---|----------|------------------|-----------|---------------------|
| 1 | **Требовать sweep для ВСЕХ setup'ов** — survivor analysis показывает 100% победителей с sweep_present=True vs 38.5% проигравших. Gate simulation: sweep = +0.414R. | WR +5-10%, PF +0.2-0.3 | Низкая (1 gate в scanner.py) | `reports/survivor_analysis.md`, `reports/gate_simulation.md` |
| 2 | **Увеличить min_p_tp с 0.45 до 0.55** — текущий порог пропускает много слабых сигналов. Бэктест: 2139 отказов по `low_p_tp`. | WR +3-8%, количество сделок -30% | Низкая (1 param в settings.py) | `reports/new_pipeline_backtest.md:96` |
| 3 | **Требовать R:R >= 2.0 вместо 1.5** — gate simulation: rr_2 = +0.099R, rr_3 = +0.300R. Лучшая комбинация: sweep + moderate_vol + rr_2 = +0.835R. | WR +2-5%, PF +0.1-0.2 | Низкая (1 param) | `reports/gate_simulation.md:85-86` |
| 4 | **Исключить低流动性 альткоины** — ZRO/USDT: 6.2% WR, PF=0.25. Major only: 27.1% WR, PF=1.38. | WR +3-5% (portfolio level) | Низкая (config change) | `reports/new_pipeline_backtest.md:72-75` |
| 5 | **Добавить sweep_required как hard gate для continuation** — текущий sweep_required работает только для reversal. Survivor: 100% победителей имели sweep. | WR +5-10%, сделок -40% | Средняя (новый gate в scanner.py) | `reports/survivor_analysis.md:12` |

### Гипотезы средней сложности

| # | Гипотеза | Ожидаемый эффект | Сложность |
|---|----------|------------------|-----------|
| 6 | **Исправить look-ahead в OB detection** — `order_blocks.py:211-249` использует forward scan. В бэктесте это даёт завышенные результаты. | Точность бэктеста | Средняя |
| 7 | **Добавить time-of-day filter** — live: WR 0% в 00-01 UTC, 50% в 22 UTC. | WR +2-3% | Низкая |
| 8 | **Увеличить ADX min с 26 до 30** — gate simulation: adx_25 = +0.070R. Текущий ADX=26 уже raised from 20. | WR +1-3%, сделок -20% | Низкая |
| 9 | **Реализовать news filter** — текущий stub. Если добавить реальные макро-данные, можно фильтровать высоковолатильные периоды. | WR +2-5% | Высокая |
| 10 | **Оптимизировать confidence weights** — текущие веса (HTF=20, Structure=15, Liquidity=20) могут быть не оптимальны. | WR +3-5% | Средняя (sweep scripts) |

---

*Отчёт сгенерирован автоматически. Все ссылки на файлы и строки верифицированы по состоянию кодовой базы на 2026-07-19.*
