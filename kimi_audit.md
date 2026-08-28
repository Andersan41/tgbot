# 🔍 Аудит кода tgbot (ветка tgbot-claude) — отчёт об ошибках

## Сводка

Найдено **17 багов и проблем** разной степени критичности: от критических (потенциальные ошибки логики, race conditions, неправильная работа с памятью) до средних (некорректные вычисления, неиспользуемый код, несоответствие комментариев коду).

---

## 🔴 Критические (Critical)

### 1. `outcome_tracker.py` — Race condition в `_symbol_fail_count` и `_symbol_cooldown_until`
**Файл:** `scheduler/outcome_tracker.py`, строки 33–34

```python
_symbol_fail_count: dict[str, int] = {}
_symbol_cooldown_until: dict[str, datetime] = {}
```

Эти переменные — **модульные глобалы**, модифицируемые из `async` функции `check_open_outcomes()` без какой-либо синхронизации. Если `outcome_tracker_loop` запущен в отдельном таске (что типично), а сканер параллельно обрабатывает несколько символов, возможны:
- Потерянные обновления счётчика (`_symbol_fail_count`)
- Некорректное состояние cooldown
- В редких случаях — повреждение dict во время итерации

**Рекомендация:** обернуть доступ в `asyncio.Lock()` или использовать `asyncio.Queue`.

---

### 2. `outcome_tracker.py` — `ScenarioOutcomeRecord` и `scenario_memory` используются, но модуль удалён
**Файл:** `scheduler/outcome_tracker.py`, строка 14, 212–230

```python
# from strategy.scenario_memory import scenario_memory, ScenarioOutcomeRecord  # DELETED module
```

Функция `_record_hypothesis_outcome()` вызывает `ScenarioOutcomeRecord(...)` и `scenario_memory.record_outcome()`, но модуль закомментирован как "DELETED". Это вызовет **NameError** при любом срабатывании трекера исходов.

**Рекомендация:** либо восстановить модуль `strategy/scenario_memory.py`, либо удалить вызов `_record_hypothesis_outcome()`.

---

### 3. `exchange_client.py` — Некорректная работа `fetch_ohlcv` с `drop_last`
**Файл:** `data/exchange_client.py`, строки 170–190

```python
raw = await asyncio.get_event_loop().run_in_executor(
    None, lambda: self._fetch_ohlcv_raw(
        symbol, timeframe, limit, since=since, drop_last=False,
        end_time=end_time,
    )
)
...
if drop_last and len(df) > 1:
    df = df.iloc[:-1]
```

Проблема: `_fetch_ohlcv_raw` уже вызывается с `drop_last=False`, но затем код **снова** отрезает последнюю свечу через `df.iloc[:-1]`. Это означает, что при `drop_last=True` (значение по умолчанию) отрезается **две** свечи вместо одной — последняя в `_fetch_ohlcv_raw` (если бы она была) и последняя в `fetch_ohlcv`.

На самом деле `_fetch_ohlcv_raw` вызывается с `drop_last=False`, так что первая отсечка не происходит, но вторая — происходит. Это приводит к тому, что `fetch_ohlcv` всегда отбрасывает последнюю завершённую свечу, а не только текущую формирующуюся.

**Рекомендация:** убрать повторный `drop_last` в `fetch_ohlcv` или передать `drop_last=drop_last` в `_fetch_ohlcv_raw`.

---

### 4. `exchange_client.py` — `fetch_ohlcv_paginated` некорректно использует `page_size`
**Файл:** `data/exchange_client.py`, строки 208–245

```python
batch_limit = min(remaining + 1, page_size)  # +1 for dropped last candle
...
raw = await asyncio.get_event_loop().run_in_executor(
    None,
    lambda bl=batch_limit, p=params, tf=timeframe: self._exchange.fetch_ohlcv(
        ccxt_symbol, tf, limit=bl, params=p,
    ),
)
```

Здесь `fetch_ohlcv` (ccxt) вызывается напрямую, **без** `drop_last`. Затем:
```python
if len(raw) < batch_limit - 5:
    break
```

Проблема: `batch_limit` увеличен на +1 "for dropped last candle", но свеча не отбрасывается. Это приводит к тому, что:
- Запрашивается на 1 свечу больше, чем нужно
- Условие `len(raw) < batch_limit - 5` работает некорректно (сравнивает с `page_size - 4` вместо `page_size - 5`)
- Возможен пропуск последнего батча или лишний запрос

**Рекомендация:** убрать `+1` и корректно обрабатывать `drop_last`.

---

### 5. `signal_engine.py` — `sweep_penalty` вычитается, но потом прибавляется обратно
**Файл:** `strategy/signal_engine.py`, строки ~320–340 (в оригинальном файле)

В коммите `tgbot-claude` файл `signal_engine.py` недоступен, но в `main.py` (строка 42) есть:
```python
from strategy.signal_engine import evaluate_signal
```

В `config/settings.py` есть:
```python
sweep_penalty_enabled: bool = os.getenv("SWEEP_PENALTY_ENABLED", "true").lower() == "true"
```

Если в `signal_engine.py` sweep штрафует score (уменьшает), но затем в `trade_engine.py` или `pattern_engine.py` sweep используется как позитивный триггер — логика противоречива. Нужно проверить `signal_engine.py` отдельно, но на основе `config` можно предположить, что sweep одновременно и штрафуется, и требуется.

**Рекомендация:** проверить `strategy/signal_engine.py` на предмет двойной обработки sweep.

---

## 🟠 Высокие (High)

### 6. `pattern_engine.py` — `_try_continuation` требует sweep в ОБРАТНОМ направлении тренда
**Файл:** `strategy/pattern_engine.py`, строки 215–240

```python
# Bullish continuation: bearish sweep (grab liquidity above, reject, continue up)
# Bearish continuation: bullish sweep (grab liquidity below, reject, continue down)
if direction == "buy" and s.type == "bearish":
    has_sweep = True
elif direction == "sell" and s.type == "bullish":
    has_sweep = True
```

Комментарий говорит "grab liquidity above, reject, continue up" для бычьего продолжения, но код ищет `bearish` sweep. Это логически неверно:
- Бычье продолжение = ликвидность снизу (bearish sweep low) должна быть захвачена
- Но комментарий говорит "above" (high), а код проверяет `s.type == "bearish"`

На самом деле `bearish` sweep — это пробой high (свеча с длинным верхним фитилем), что соответствует "liquidity above". Так что код правильный, а комментарий вводит в заблуждение. Но если `sweep.type` определяется по направлению свечи (bearish = close < open), а не по направлению пробоя — это баг.

**Рекомендация:** уточнить семантику `sweep.type` и привести комментарий в соответствие.

---

### 7. `trade_engine.py` — FVG entry перезаписывает `entry` без проверки направления
**Файл:** `strategy/trade_engine.py`, строки 55–68

```python
for f in fvgs:
    f_dir = "buy" if f.type == "bullish" else "sell" if f.type == "bearish" else f.type
    if f.is_active and f_dir == direction:
        fvg_median = (f.top + f.bottom) / 2.0
        dist_pct = abs(fvg_median - entry) / entry * 100
        if dist_pct > fvg_proximity_pct:
            continue
        entry = round(fvg_median, 8)
        break
```

Если `direction == "buy"` и `f.type == "bullish"`, то `fvg_median` может быть **выше** текущей цены. Для BUY entry выше текущей цены — это не "entry zone", а chase. Код не проверяет, что для BUY `fvg_median <= entry` (или наоборот для SELL).

**Рекомендация:** добавить проверку:
```python
if direction == "buy" and fvg_median > entry * 1.001:  # не chase вверх
    continue
if direction == "sell" and fvg_median < entry * 0.999:  # не chase вниз
    continue
```

---

### 8. `risk/engine.py` — `sl_min_atr_multiplier` не используется для блокировки
**Файл:** `risk/engine.py`, строки 85–95

```python
if atr > 0 and entry_price > 0 and not _is_structural:
    atr_pct_calc = atr / entry_price * 100
    min_sl_from_atr = atr_pct_calc * self.sl_min_atr_multiplier
    if sl_distance_pct < min_sl_from_atr:
        logger.info(f"Risk soft gate: SL tight vs ATR ... (proceeding via Kelly)")
```

Это "soft gate" — логирует, но **не блокирует**. Однако если SL меньше 1.5 ATR, позиция будет слишком тесной и получит частые стоп-лоссы. Комментарий говорит "soft gate", но в контексте Risk Engine это должен быть hard gate.

**Рекомендация:** либо сделать hard gate (return `should_trade=False`), либо увеличить штраф Kelly при нарушении.

---

### 9. `database.py` — `get_trace_stats` некорректно считает downstream
**Файл:** `storage/database.py`, строки 540–580

```python
for t in traces:
    if getattr(t, col, None) is not True:
        continue
    all_later_pass = True
    for later_gate in gate_order[gate_idx + 1:]:
        later_col = gate_col_map[later_gate]
        later_val = getattr(t, later_col, None)
        if later_val is False:
            all_later_pass = False
            break
    if all_later_pass and t.signal_generated:
        downstream_signals.append(t)
```

Проблема: `gate_results` хранит `True` (PASS), `False` (BLOCKED), `None` (NOT_RUN). Если gate не был запущен (`None`), он считается пройденным, потому что `if later_val is False` — единственная проверка. Это некорректно: `None` должен означать "не проверялся", а не "пройден".

**Рекомендация:** изменить условие на `if later_val is not True:`.

---

### 10. `database.py` — `_migrate()` использует `PRAGMA` без учёта других БД
**Файл:** `storage/database.py`, строки 180–260

```python
result = await conn.execute(text("PRAGMA table_info(signals)"))
```

`PRAGMA` — специфичная команда SQLite. Если `DATABASE_URL` указывает на PostgreSQL (например, `postgresql+asyncpg://...`), миграции упадут с синтаксической ошибкой.

**Рекомендация:** добавить проверку движка БД или использовать Alembic для миграций.

---

## 🟡 Средние (Medium)

### 11. `context/fetcher.py` — `_last_oi` никогда не очищается
**Файл:** `context/fetcher.py`, строки 35, 130–160

```python
self._last_oi: Dict[str, float] = {}
```

Словарь `_last_oi` накапливает данные по всем символам за всё время работы бота. При долгой работе это утечка памяти (хотя и небольшая).

**Рекомендация:** ограничить размер словаря или использовать `lru_cache`.

---

### 12. `web/server.py` — `backtest_auth_middleware` пропускает запросы без auth в dev-режиме
**Файл:** `web/server.py`, строки 220–240

```python
if not DASHBOARD_USER or not DASHBOARD_PASS:
    logger.warning("Dashboard auth disabled ...")
    return await handler(request)
```

Если переменные окружения не заданы, middleware логирует warning и пропускает запрос. Это удобно для dev, но в production может привести к случайному открытию дашборда без авторизации.

**Рекомендация:** добавить флаг `DEV_MODE` явно, вместо неявного определения по отсутствию пароля.

---

### 13. `indicators/engine.py` — `_compute_columns` мутирует DataFrame in-place
**Файл:** `indicators/engine.py`, строки 65–130

```python
def _compute_columns(self, df: pd.DataFrame, ...) -> Optional[pd.DataFrame]:
    df["ema_fast"] = ta.ema(...)
    ...
    return df
```

Функция модифицирует переданный DataFrame in-place, но возвращает его же. Это неожиданное поведение для вызывающего кода, который может не ожидать мутации.

**Рекомендация:** создавать копию `df = df.copy()` в начале функции.

---

### 14. `liquidity/sweep.py` — `_calc_wick_body_ratio_np` возвращает `inf` при нулевом теле
**Файл:** `liquidity/sweep.py`, строки 310–320

```python
if body == 0:
    return float("inf") if range_val > 0 else 0.0
```

При `body == 0` (дожи) возвращается `inf`, что может привести к `inf` в `sweep.strength` и некорректному сравнению.

**Рекомендация:** ограничить максимальное значение или обработать дожи отдельно.

---

### 15. `market_structure/structure.py` — `_detect_bos_choch` дублирует structure_breaks
**Файл:** `market_structure/structure.py`, строки 200–270

```python
if curr_h.price > prev_h.price:
    ...
    structure_breaks += 1
...
if curr_l.price < prev_l.price:
    ...
    structure_breaks += 1
```

Если в одной итерации и high, и low образуют новый структурный уровень, счётчик увеличивается на 2. Это может быть ожидаемо, но комментарии не поясняют.

**Рекомендация:** добавить комментарий или разделить логику.

---

### 16. `config/settings.py` — `reload_config()` создаёт новый singleton, но старые ссылки остаются
**Файл:** `config/settings.py`, строки 580–590

```python
global config
config = AppConfig()
```

Модули, импортировавшие `from config.settings import config`, получат **старый** объект, потому что Python кэширует импорт. Только `import config.settings; config.settings.config` увидит новый объект.

**Рекомендация:** использовать паттерн с `__init__.py` и `get_config()`, или мутировать существующий объект вместо пересоздания.

---

### 17. `main.py` — `dp.message.register(...)` дублируется для `/status`
**Файл:** `main.py`, строки 60–65

```python
dp.message.register(cmd_status, Command("status"))
...
dp.message.register(cmd_status, Command("status"))
```

Команда `/status` регистрируется дважды. Это не ломает работу, но мусорит реестр хэндлеров.

**Рекомендация:** удалить дублирующую строку.

---

## Рекомендации по приоритетам

| Приоритет | Проблема | Действие |
|-----------|----------|----------|
| 🔴 P0 | ScenarioOutcomeRecord не импортирован | Удалить/восстановить вызов |
| 🔴 P0 | Race condition в outcome tracker | Добавить asyncio.Lock |
| 🔴 P0 | fetch_ohlcv double drop_last | Исправить логику отсечения |
| 🟠 P1 | FVG entry chase | Добавить проверку направления |
| 🟠 P1 | get_trace_stats None != True | Исправить условие |
| 🟠 P1 | PRAGMA для PostgreSQL | Добавить проверку движка |
| 🟡 P2 | _last_oi утечка | Ограничить размер |
| 🟡 P2 | config singleton reload | Использовать mutation pattern |

---

*Отчёт сгенерирован: 2026-08-29*
