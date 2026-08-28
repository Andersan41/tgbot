# Technical Audit — Trading Signal Bot

- **Дата:** 2026-08-17
- **Версия:** `2.7.0`, `STRATEGY_MODE=v2.5`, python 3.11, ccxt, Binance swap
- **Метод:** статический анализ кода по слоям (config → data → indicators → market_structure → liquidity → strategy → risk → scheduler → backtest → storage → web → bot → tests)
- **Способ проверки гипотез:** чтение исходников; тесты не запускались (статический аудит)

---

## 1. Проверка заявленных гипотез

| # | Гипотеза | Вердикт | Доказательство |
|---|----------|---------|----------------|
| 1 | В коде остались ссылки на удалённые модули (`scenario_engine`, `scenario_memory`, `hypothesis`) | **ПОДТВЕРЖДЕНА** | `scheduler/outcome_tracker.py:375,391` вызывает `ScenarioOutcomeRecord`/`scenario_memory` при закомментированном импорте (строка 18) → NameError, глотается `try/except`. `strategy/weight_manager.py:58` (`scenario_memory.get_stats`), `strategy/trade_thesis.py:318` (`ScenarioEvaluation(...)`), `strategy/entry_trigger.py` (`Hypothesis`) — все ссылаются на удалённые модули. `transition_model.py` используется только тестами. |
| 2 | Формула RR для SELL неправильная | **ОПРОВЕРГНУТА** | `strategy/trade_engine.py:240-242`: `abs(tp-entry)/abs(entry-sl)` — корректна для обоих направлений. `strategy/signal_engine.py:240-289` (`_calculate_sl_tp`) — SL для SELL: `ob.high*(1+buffer)`, BOS: `bos.level*(1+buffer)`, направленно-корректно. |
| 3 | Бэктест берёт cooldown от `datetime.now()` | **ОПРОВЕРГНУТА** | `backtest/engine.py:487-492` — cooldown считается по timestamp свечи (`df.index[i]`), детерминированно. |
| 4 | Риск-гейты не используются в бэктесте | **ЧАСТИЧНО ПОДТВЕРЖДЕНА** | Бэктест не вызывает `risk_engine.evaluate()` — он **переписывает** гейты (structural SL, buffer, distance guard, RR) в своей копии (`tests/test_backtest_parity.py` проверяет именно эти inline-копии). RR-гейт работает, но:
  - в `RiskEngine.evaluate()` `PortfolioState.max_active_signals`/`max_portfolio_risk_pct` не используются вовсе (`risk/engine.py:31-34` — мёртвые поля);
  - два независимых порога RR: `config.trading.min_rr_threshold` (используется) и `config.risk_engine.min_rr_ratio` (`RISK_ENGINE_MIN_RR`, мёртвый);
  - бэктест пропускает `BLOCK_ALL_SELL`, HTF hard gate (по умолчанию `enable_htf_bias_gate=False`), per-symbol overrides, MTF/context/HTF-boost к p_tp, funding, 1 позиция против live 3. |
| 5 | Дублирующийся `elif` в `trade_engine.py` | **ПОДТВЕРЖДЕНА** | `strategy/trade_engine.py:185-197` — два одинаковых `elif signal == SignalType.SELL and sl <= candle_high:`; второй блок (192-197) недостижим. |

---

## 2. Найденные баги (по severity)

### P0 — Critical

#### BUG-1. Веб-дашборд и API не защищены аутентификацией (dev-режим открыт наружу)
- **File:** `web/server.py:330-351`, `web/server.py:502-529`, `config/settings.py:577-581`
- **Evidence:** middleware `backtest_auth_middleware` проверяет **только** `/backtest` и `/api/backtest/*`. `/api/filters` (POST — включает/выключает фильтры стратегии и пишет в БД), `/api/open-trades` (отдаёт **живые позиции со SL/TP**), `/ws`, `/webhook/tradingview` — без auth. `DASHBOARD_USER`/`DASHBOARD_PASS` в `.env` **отсутствуют** → режим "dev: пропускаем всё". `WEB_HOST=0.0.0.0` (default), HTTP без TLS.
- **Impact:** Любой в сети может читать открытые сделки (охота за SL), переключать конфигурацию стратегии, спамить. Критично для торгового бота.
- **Fix:** (a) fail-closed: при пустых кредах не стартовать web; (b) закрыть auth'ом ВСЕ `/api/*`, `/ws`, `/webhook/*`; (c) обязательный TLS/закрытый порт; (d) POST `/api/filters` и `/api/open-trades` — отдельная проверка прав.
- **Priority:** 1

#### BUG-2. TradingView webhook без секрета — подделка сигналов
- **File:** `web/webhook.py:350-403`, `web/webhook.py:127-343`
- **Evidence:** `webhook_handler` проверяет только `config.webhook.enabled` и rate-limit 30/мин. Секрет/токен не сверяется. `process_webhook_signal` проводит сигнал через весь пайплайн и **сохраняет его в БД** (`db.save_signal`, строка 307) с `confirmed=False`.
- **Impact:** Любой может постить фейковые "buy/sell" → ввод в заблуждение подписчиков/торгового слоя.
- **Fix:** требовать `X-Webhook-Secret` (из config), отклонять без него; подписывать.
- **Priority:** 1

#### BUG-3. ETH/USDT крашится на per-symbol override (сигналы ETH теряются)
- **File:** `scheduler/scanner.py:495,507,519`; `strategy/pattern_engine.py:49-69` (ICTSetup)
- **Evidence:** `.env` `SYMBOL_OVERRIDES` для `ETH/USDT` содержит `max_sl_pct`, `max_atr_pct`, `min_quality`. Код читает `setup.sl_distance_pct`, `setup.atr_pct`, `setup.overall_setup_quality` — этих полей в `ICTSetup` **нет** → AttributeError на каждой фазе 1.46. `run_scan_cycle` ловит через `asyncio.gather(return_exceptions=True)` (scanner.py:1104-1111) и логирует — символ молча выпадает из каждого цикла.
- **Impact:** ETH/USDT не генерирует сигналы вообще (если доходит до 1.46). Тихая потеря данных.
- **Fix:** считать SL distance/ATR%/quality из `ind`/`trade_plan` (как в Phase для `max_sl_pct`), либо удалить несуществующие ключи из override.
- **Priority:** 1

---

### P1 — High

#### BUG-4. `_htf_bias_penalty` вычисляется, но никогда не применяется
- **File:** `scheduler/scanner.py:582,625,641,690,706`
- **Evidence:** переменная инициализируется и перезаписывается `config.htf_bias_continuation_penalty`, но нигде не читается дальше. HTF-ветка даёт только `+0.05` к p_tp при alignment; штраф за continuation/reversal против HTF отсутствует.
- **Impact:** HTF Bias V2 работает не по задумке: penalизация против-тренда не влияет на вероятность → завышенные P(TP).
- **Fix:** применить penalty к p_tp (или явно удалить переменную).
- **Priority:** 2

#### BUG-5. `entry_candle_open` всегда None
- **File:** `scheduler/scanner.py:964-970`
- **Evidence:** `last_candle_ts = df.iloc[-1].get("timestamp")` — `timestamp` является DatetimeIndex'ом, а не колонкой → всегда `None`; `_entry_candle_open` остаётся `None`; в outcome_tracker (155-168) срабатывает fallback на `signal_detected_at`.
- **Impact:** loss-калибровка по «открытию свечи входа» неточная (сдвиг на время сигнала). Не крах, но данные искажены.
- **Fix:** `df.index[-1]`.
- **Priority:** 3

#### BUG-6. Race condition в portfolio risk-гейте
- **File:** `scheduler/scanner.py:228-244`
- **Evidence:** check-then-act без лока: все `scan_symbol_v2` запускаются параллельно через `gather`; несколько тасков могут пройти `get_active_signals_count < max_sigs` до вставки → превышение лимитов. Плюс `RiskEngine.evaluate` (risk/engine.py:109-203) вообще не читает `max_active_signals`/`max_portfolio_risk_pct` из `PortfolioState`.
- **Impact:** может открыться больше 3 активных сигналов / превышен портфельный риск.
- **Fix:** атомарная вставка/лок (asyncio.Lock вокруг check+insert) или SQL-атомарность; убрать/задействовать мёртвые поля.
- **Priority:** 2

#### BUG-7. `exchange_client.connect()` без try/except в main.py
- **File:** `main.py:77`
- **Evidence:** при недоступности биржи — необработанное исключение → крах бота. Web-server (main.py:81-85) обёрнут try/except — несоответствие.
- **Impact:** бот падает при сетевой ошибке на старте; нет graceful degradation/retry.
- **Fix:** обернуть + retry/backoff, лог и продолжение без биржевых данных.
- **Priority:** 2

#### BUG-8. Hot reload конфигурации не работает
- **File:** `config/settings.py:878-891`
- **Evidence:** `reload_config()` делает `config = AppConfig()` — **rebind**, а не in-place. Модули, сделавшие `from config.settings import config`, держат старую ссылку (docstring ложно утверждает "заменяется in-place"). `reload_filter_toggles` (911-939) мутирует новый объект; старые ссылки остаются устаревшими.
- **Impact:** `/setparam` вроде бы применяется, но зависящие модули видят старое значение; расхождение runtime vs config.
- **Fix:** мутировать поля существующего singleton'а in-place.
- **Priority:** 3

#### BUG-9. Бинарные данные в git
- **File:** repo root — `signals.db`, `candidate_probability_model.pkl` закоммичены (несмотря на `*.db`/`*.pkl` в .gitignore)
- **Impact:** утечка торговых данных в репозиторий; pickle-модель = риск произвольного кода при открытии.
- **Fix:** удалить из истории (`git rm --cached` + filter-branch/BFG), подтвердить актуальность .gitignore.
- **Priority:** 2

#### BUG-10. Гипотезный трекинг (F3) полностью сломан + мёртвый код пайплайна
- **File:** `scheduler/outcome_tracker.py:18,375,391`; `strategy/weight_manager.py`, `strategy/trade_thesis.py`, `strategy/entry_trigger.py`, `strategy/transition_model.py`
- **Evidence:** `ScenarioOutcomeRecord` и `scenario_memory` не определены → NameError глотается `except Exception` (396). WeightManager/TradeThesis/EntryTrigger ссылаются на удалённые модули (гипотеза #1). transition_model используется только тестами.
- **Impact:** каждая закрытая сделка не записывает hypothesis-статистику (тихо); слой «self-learning» мёртв.
- **Fix:** либо восстановить импорты/удалить слой, либо убрать вызовы и очистить модули.
- **Priority:** 3

---

### P2 — Medium / Low

#### BUG-11. Бэктест не паритетен live-пайплайну (выводы не переносимы на live)
- **File:** `backtest/engine.py` (BacktestConfig presets, htf-гейт), `scheduler/scanner.py`
- **Evidence/Impact:**
  - `BLOCK_ALL_SELL` не проверяется в бэктесте;
  - HTF hard gate по умолчанию выключен (`enable_htf_bias_gate=False` в presets baseline/full);
  - per-symbol overrides (adx_min/max_sl_pct/…) не применяются;
  - compression-regime гейт не воспроизведён;
  - MTF alignment / context scoring / HTF-alignment `+0.05` к p_tp не воспроизведены → другие p_tp/risk;
  - **HTF bias считается один раз на всей истории** → статичен и содержит «будущее» для ранних сделок (look-ahead);
  - 1 позиция за раз против live `max_active_signals=3`;
  - funding-косты не моделируются; MFE/MAE не считаются;
  - Sharpe per-trade, без annualization и risk-free rate — вводит в заблуждение; DD на gross PnL без учёта sizing.
- **Fix:** синхронизировать пресеты (включить гейты по умолчанию), применить overrides, реплицировать p_tp-бонусы, считать HTF bias по «срезу» на момент сделки, funding, позиционный лимит, нормальные метрики.
- **Priority:** 2

#### BUG-12. Усечение истории в бэктесте + формирующаяся свеча в кэше
- **File:** `backtest/engine.py` (`_load_local`), `backtest/cache_ohlcv.py:38-41`, `data/exchange_client.py` (`fetch_ohlcv_since` drop_last=False)
- **Evidence:** двойной trim: `max(candles_limit+100,200)` → затем `candle_limit+60`. «3-летний» бэктест при дефолтных 200-336 свечах невозможен. В unified-кэш (15m) при докачке может попасть незакрытая свеча (backward-пагинация `endTime=now`).
- **Impact:** короткие выборки + до 1 бара look-ahead на границе.
- **Fix:** брать историю из полного кэша без trim (или явный параметр), отбрасывать последнюю формирующуюся свечу.
- **Priority:** 3

#### BUG-13. Look-ahead окно OB захардкожено; разнородная семантика `candle_index`
- **File:** `liquidity/order_blocks.py:121` (`look_ahead=20`), `liquidity/sweep.py:93-128` (`candle_index=offset+i`), `liquidity/order_blocks.py:138,165` (`candle_index=i` — относительно окна), `liquidity/fvg.py:82,94` (`index=i+1` — относительно окна)
- **Impact:** в live OB «состаривается» иначе, чем sweep; смешение объектов из разных срезов даёт off-by-window при проверках (retest/age).
- **Fix:** единая абсолютная семантика + `look_ahead` из конфига.
- **Priority:** 3

#### BUG-14. Entry price рассинхронизирован с базой SL/TP
- **File:** `scheduler/scanner.py:754-780` (`entry_price=ind.close`, SL/TP из `trade_plan`), `strategy/trade_engine.py` (entry_option 'fvg' → midpoint, SL = fvg_low − buffer, не invalidation), `backtest/engine.py` (использует `trade_plan.entry_price`)
- **Impact:** RR, посчитанный в риск-гейте (entry=close) и фактический RR (по SL/TP от FVG-median) могут различаться; в live и бэктесте по-разному.
- **Fix:** единый источник entry для сигнала, риск-гейта, бэктеста и outcome_tracker.
- **Priority:** 3

#### BUG-15. Конфиг-дрейф: `PREMIUM_DISCOUNT` / `HTF_HARD_GATE` дефолты vs документация
- **File:** `config/settings.py` (дефолт `PREMIUM_DISCOUNT="true"`), `.env` (не задан), `AGENTS.md` (заявляет OFF)
- **Impact:** premium/discount зоны фактически включены вопреки заявленному A/B-результату.
- **Fix:** выровнять дефолт и .env с документацией (explicit), добавить валидацию/`pydantic` для типов (сейчас `int("abc")` роняет импорт).
- **Priority:** 3

#### BUG-16. Два порога RR + декоративный Kelly
- **File:** `risk/engine.py:61,216` (`min_rr_ratio=config.trading.min_rr_threshold`); `config.risk_engine.min_rr_ratio` (`RISK_ENGINE_MIN_RR`) — мёртвый; Kelly при `b>0` в `evaluate` фактически не меняет `risk_pct` (упирается в base).
- **Fix:** один источник порога; решить судьбу Kelly (либо реальный расчёт sizing).
- **Priority:** 3

#### BUG-17. Whitelist `/setparam` шире, чем `FILTER_PARAM_KEYS`
- **File:** `bot/admin.py:88-97` vs `config/settings.py:822-872`
- **Evidence:** `CANDLES_LIMIT`, `RSI_BULL_MIN`, `RSI_BEAR_MAX` есть в admin whitelist, но не в `FILTER_PARAM_KEYS` → пишется в БД, но `reload_filter_toggles` не применяет → "Применено (live)" вводит в заблуждение.
- **Fix:** синхронизировать списки.
- **Priority:** 4

#### BUG-18. Rate limiting только на callback'ах меню
- **File:** `bot/rate_limit.py` (не используется вне menu), `bot/menu.py:150-161`
- **Evidence:** `/scan`, `/setparam`, текстовые сообщения rate limit'ом не покрыты; `rate_limit.py` — по сути мёртвый код.
- **Fix:** подключить лимитер на все команды.
- **Priority:** 4

#### BUG-19. Parity-тесты не проверяют паритет
- **File:** `tests/test_backtest_parity.py` (387 стр.)
- **Evidence:** тесты переписывают ту же логику inline и проверяют её саму; `BacktestEngine`/scanner не инстанцируются; `TestPipelineStepOrder` сравнивает два захардкоженных списка. `conftest.py` не мокает exchange/БД — тесты зависят от `.env`.
- **Impact:** регрессия паритета не ловится.
- **Fix:** юнит-тесты против реального `BacktestEngine.run()` и `scan_symbol_v2` с моками exchange+БД; зафиксировать snapshots.
- **Priority:** 3

#### BUG-21. `risk_engine` и `pattern_engine` кэшируют конфиг при импорте (silent no-op)
- **File:** `risk/engine.py:215-224` (singleton, `min_rr_ratio=config.trading.min_rr_threshold` в `__init__`), `strategy/pattern_engine.py:1072` (singleton, quality-гейты в `__init__`); `config/engine.py:739` HTF-пенальти читается live
- **Evidence:** смена `config.trading.min_rr_threshold` внутри процесса не влияет на результат (ADA 4h: 1.8 vs 2.5 → trades=146/146, risk_rej=187/187 идентичны). `pattern_engine.min_overall_quality`/`min_setup_confidence`/`min_components_required` тоже запекаются при импорте. Значит любой «тюнинг» через смену конфига/`.env` без перезапуска процесса молча не работает, а воспроизводимые цифры создают иллюзию, что параметр подхватился.
- **Fix:** вынести конфиг-зависимые поля в read-time (читать `config.trading.*`/`config.pattern_engine.*` в момент вызова), либо явный `rebuild()`/`reload()`; в `/setparam` добавить предупреждение, какие параметры требуют рестарта. Найден в robustness-анализе (`docs/decisions/robustness_analysis.md` §5).
- **Priority:** 3

#### BUG-20. Мелочи
- `backtest/cache_ohlcv.py:401-408` и docstring упоминают BingX, но `EXCHANGE=binance` (7 симв.) — дрейф документации.
- `liquidity/pool.py:191` — `detect_external_liquidity(df,…)` вызывается повторно для `external_levels`.
- `bot/menu.py:779-791` — `getattr(structure,"bos")`/`"swing_highs"` не существуют в `StructureState` (структура.py:62-71 имеет `last_bos`/`last_choch`/`swing_points`) → секция «Структура» в анализе всегда пустая.
- `bot/admin.py:429` — `_dynamic_graphs` не определён (NameError), но ветка недостижима (thesis всегда None) — мёртвый код.
- `data/exchange_client.py` — docstring «самую старую» противоречит поведению (`raw[:-1]` отбрасывает самую новую — правильно); `_fetch_taker_buy_volumes` — лишний API-вызов на каждый fetch_ohlcv (2x нагрузка, но в семафоре).
- `liquidity/fvg.py:105-114` — `_is_fvg_filled` итерирует текущую (формирующуюся) свечу → FVG может быть помечен filled преждевременно в live.
- `strategy/trade_engine.py` — `swing_highs`/`swing_lows` оба читают `structure.swing_points` и перезаписываются (мёртвый код); `Invalidation.sl_with_buffer` с `buffer_pct=0`.
- `web/webhook.py:168` — `tf_map` неполный ("1D" нет в ключах; `tf_map.get(tf, tf)` пропускает неизвестные).

#### BUG-21. `risk_engine` и `pattern_engine` кэшируют конфиг при импорте (silent no-op)
- **File:** `risk/engine.py:215-224` (singleton, `min_rr_ratio=config.trading.min_rr_threshold` в `__init__`), `strategy/pattern_engine.py:1072` (singleton, quality-гейты в `__init__`); `config/engine.py:739` HTF-пенальти читается live
- **Evidence:** смена `config.trading.min_rr_threshold` внутри процесса не влияет на результат (ADA 4h: 1.8 vs 2.5 → trades=146/146, risk_rej=187/187 идентичны). `pattern_engine.min_overall_quality`/`min_setup_confidence`/`min_components_required` тоже запекаются при импорте. Значит любой «тюнинг» через смену конфига/`.env` без перезапуска процесса молча не работает, а воспроизводимые цифры создают иллюзию, что параметр подхватился.
- **Fix:** вынести конфиг-зависимые поля в read-time (читать `config.trading.*`/`config.pattern_engine.*` в момент вызова), либо явный `rebuild()`/`reload()`; в `/setparam` добавить предупреждение, какие параметры требуют рестарта. Найден в robustness-анализе (`docs/decisions/robustness_analysis.md` §5).
- **Priority:** 3

---

## 3. Сводная таблица

| ID | Severity | File | Priority |
|----|----------|------|----------|
| BUG-1 | Critical | web/server.py | 1 |
| BUG-2 | Critical | web/webhook.py | 1 |
| BUG-3 | Critical | scheduler/scanner.py | 1 |
| BUG-4 | High | scheduler/scanner.py | 2 |
| BUG-5 | Medium | scheduler/scanner.py | 3 |
| BUG-6 | High | scheduler/scanner.py, risk/engine.py | 2 |
| BUG-7 | High | main.py | 2 |
| BUG-8 | Medium | config/settings.py | 3 |
| BUG-9 | High | git (signals.db, *.pkl) | 2 |
| BUG-10 | Medium | outcome_tracker + strategy dead-code | 3 |
| BUG-11 | High | backtest/engine.py | 2 |
| BUG-12 | Medium | backtest/engine.py, cache_ohlcv.py | 3 |
| BUG-13 | Medium | liquidity/* | 3 |
| BUG-14 | Medium | scanner.py, trade_engine.py | 3 |
| BUG-15 | Medium | config/settings.py, .env | 3 |
| BUG-16 | Low-Med | risk/engine.py | 3 |
| BUG-17 | Low | bot/admin.py | 4 |
| BUG-18 | Low-Med | bot/rate_limit.py | 4 |
| BUG-19 | Medium | tests/test_backtest_parity.py | 3 |
| BUG-20 | Low | (мелочи) | 4 |
| BUG-21 | Medium | risk/engine.py, pattern_engine.py | 3 |

---

## 4. Roadmap

**Неделя 1 (P0+P1):**
1. BUG-1: fail-closed аутентификация всех `/api/*`, `/ws`, `/webhook/*`; запрет старта web без кредов.
2. BUG-2: секрет для webhook.
3. BUG-3: починить override-гейты для ETH (вычислять поля или убрать ключи).
4. BUG-7: обернуть `connect()` в retry.
5. BUG-9: вычистить бинарники из git.
6. BUG-10: убрать сломанный hypothesis-слой (или восстановить).

**Неделя 2 (P1→P2):**
7. BUG-4: применить HTF-penalty или удалить.
8. BUG-6: atomic portfolio-гейт + задействовать поля PortfolioState.
9. BUG-8: in-place reload конфига.
10. BUG-11: паритет бэктеста (дефолты гейтов, overrides, block_all_sell, funding, статичный HTF bias).
11. BUG-12: история из полного кэша.

**Неделя 3 (P2 / чистота):**
12. BUG-13..16, BUG-20, BUG-21 — унификация семантики, конфиг-гигиена.
13. BUG-19: переписать parity-тесты против реальных движков + моки exchange/БД.
14. Обновить `AGENTS.md` и `plan/*` в соответствии с фактическим поведением.

---

## 5. Оценка валидности бэктеста

**Сильные стороны (корректно):**
- Exit проверяется со свечи `i+1` — нет same-bar look-ahead.
- Cooldown на timestamp свечи — детерминирован.
- Комиссия смоделирована корректно: `(fee*2 + slip*2)*100 = 0.2%` round-trip.
- Ресемплер W-MON/closed=left согласован с htf_bias_v2.

**Слабые стороны (выводы НЕ переносимы на live):**
- HTF bias статичен на всю историю → look-ahead для ранних сделок.
- Отключённые по умолчанию HTF-гейт и отсутствующий `BLOCK_ALL_SELL` — live строже.
- p_tp и sizing в бэктесте отличаются от live (нет MTF/context/HTF-boost) → RR-распределение иное.
- 1 позиция vs 3; нет funding; нет MFE/MAE.
- Метрики: Sharpe без annualization, DD на gross — вводят в заблуждение.

**Вывод:** бэктест пригоден **только для относительных A/B сравнений внутри себя** (обе ветки страдают одинаковыми смещениями — так он и используется: HTF Bias V2, premium/discount). Абсолютные PF/WR/expectancy из бэктеста как прогноз live использовать нельзя.

---

## 6. Область аудита (покрытие)

Покрыто: `main.py`, `config/settings.py`, `data/exchange_client.py`, `indicators/engine.py` (частично), `market_structure/structure.py` (начало), `market_structure/tp_path.py` (начало), `market_structure/htf_bias_v2.py` (начало), `liquidity/*` (sweep, order_blocks, fvg, pool, equal_levels/external частично), `strategy/*` (trade_engine, invalidation, signal_engine, pattern_engine частично, trade_plan частично, dead-code модули), `risk/engine.py`, `risk/dynamic_risk.py`, `scheduler/*` (scanner, tasks, circuit_breaker, outcome_tracker, shadow — частично), `backtest/*` (engine, resampler, cache_ohlcv), `storage/database.py` (частично), `web/*` (server, backtest_api, webhook), `bot/*` (handlers, admin, menu, rate_limit, notifier частично), `tests/conftest.py`, `test_backtest_parity.py`.

Не проверено глубоко: полный `pattern_engine.py`, `indicators/engine.py` расчёт, `context/*`, `derivatives/*`, `scoring/confidence_v2.py`, `analytics/performance.py`, `storage/database.py` миграции, полный набор тестов (не запускались), реальные данные `signals.db` (заявление «27 сигналов за 30 часов»: при нескольких символах × 1h/4h и эффективном cooldown 120 мин на `symbol_timeframe` такое количество **не нарушает** cooldown — требуется проверка по БД).