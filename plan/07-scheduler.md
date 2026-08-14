# Scheduler — Trading Signal Bot

> Расписание и циклы. Актуально для `VERSION = "2.6.0"`.

## Задачи (scheduler/tasks.py)

`AsyncIOScheduler(timezone="UTC")` (`tasks.py:15`).

| Job | Триггер | Описание |
|-----|---------|----------|
| `scan_all_tfs` | `CronTrigger(minute=config.scheduler.scan_minutes)` (`tasks.py:25-32`); default `SCAN_MINUTES=2,17,32,47` | `run_scan_cycle` по всем `primary_timeframes`; `max_instances=1`, `coalesce=True` |
| `daily_report` | 00:05 UTC (`tasks.py:34-42`) | `analytics/daily_report` → summary в Telegram |

`_scan_job` (`tasks.py:46-57`):
- `run_scan_cycle(notify_callback, blocked_callback=send_signal_blocked, timeframes)`
- после него → `run_shadow_cycle()` (`scheduler.shadow.py`), активен при `SHADOW_ENABLED=true` (Rec 2).

## Цикл сканирования (scheduler/scanner.py)

### `run_scan_cycle()` — `scanner.py:1542`

```
1. check_recent_losses()                     → circuit breaker
2. is_circuit_breaker_active()?              → пропуск скана
3. get_active_symbols() − disabled_symbols
4. tfs = timeframes or config.trading.primary_timeframes
5. tasks = scan_symbol_v2(symbol, tf, notify, blocked) для каждого symbol×tf
6. asyncio.gather(*tasks, return_exceptions=True)
7. лог: signals_found / total
```

### `scan_symbol_v2()` — `scanner.py:216`

Каноническая воронка гейтов (`storage/trace.py:31-35`):

```
1. cooldown           (db.get_cooldown, effective = max(base, tf×mult))
2. portfolio_risk     (active_count, portfolio_risk_sum)
3. indicators         (_get_indicators)
4. pattern_engine     (ICTSetup; reversal/continuation)
   + direction/symbol filter (config.direction_filter — Rec 3)
   + news filter      (config.risk.news_filter_enabled — Rec 4a, opt-in)
   + confluence-mode  (STRATEGY_MODE == "confluence")
5. structure_alignment
6. sweep_required     (reversal: sweep+MSS; continuation: BOS+sweep)
7. regime_block
8. sl_tp              (SL/TP calc)
9. risk_engine        (RR, портфель, Kelly)
10. dedup
```

Следом — фазовые Фильтры: HTF Bias V2 (`htf_bias_v2=True`), Premium/Discount
(`premium_discount=False` default), min P(TP) (`min_p_tp=0.45` default).

## Shadow-режим (сравнение версий)

`scheduler/shadow.py` — опциональное A/B-сравнение после каждого цикла:

```
SHADOW_ENABLED=true    # включается в .env (default false)
SHADOW_VERSION=shadow-simplified
SHADOW_PRESET=simplified
```

`run_shadow_cycle()` (`shadow.py:30-106`) пропускается при активном circuit breaker,
прогоняет `scan_symbol_v2` с `notify_callback=None` и логирует решения с меткой версии.

## Circuit breaker и outcome tracker

- **Circuit breaker** (`circuit_breaker.py`): 3 consecutive `HIT_SL` →
  пауза `PAUSE_MINUTES=30`, окно `WINDOW_MINUTES=60`; in-memory. `check_recent_losses()`
  вызывается не чаще 1/мин; после паузы учитываются только новые убытки.
- **Outcome tracker** (`outcome_tracker.py`): `outcome_tracker_loop()` каждые
  `OUTCOME_CHECK_INTERVAL_SECONDS=300`; TTL `OUTCOME_TTL_DAYS=7`; PnL после издержек:
  комиссия×2 + slippage×2 + funding (`FUNDING_RATE_8H=0.0001`, scaled by hold time).
  Per-symbol fail count: `SYMBOL_FETCH_FAIL_THRESHOLD=3` → cooldown 600 c.
  Уведомления: ✅ HIT_TP / 🛑 SL (`_send_close_notification`).
  Исход записывается в `ScenarioMemory` (`_record_hypothesis_outcome`).

## Cooldown

- `SIGNAL_COOLDOWN_MINUTES` default 45; effective = `max(base, tf_minutes × SIGNAL_COOLDOWN_TF_MULTIPLIER)`.
- **Persisted в SQLite** (`db.get_cooldown`/`db.set_cooldown`, `storage/database.py:630-642`).
- Устанавливается после успешного сигнала (`scanner.py:1479`).

## Ручной запуск

`/scan` (admin) → `run_scan_cycle()` (bot/admin.py).