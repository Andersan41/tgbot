# Аудит текущей версии Trading Signal Bot

**Репозиторий:** Andersan41/tgbot  
**Проверенная ветка:** `tgbot-claude`  
**Дата повторного аудита:** 30 августа 2026  
**Метод:** повторный статический анализ актуального публичного кода и сверка с предыдущими findings/документами репозитория.  
**Статус:** повторный аудит текущего состояния; это не запуск кода и не формальная security certification.

---

## Executive summary

Повторный аудит показывает, что после предыдущего разбора часть важных проблем действительно была исправлена:

- исправлена причинность `Sweep → MSS` в reversal pipeline;
- reversal теперь выбирает **самый новый**, а не первый valid sweep;
- добавлена проверка согласованности направления sweep и MSS;
- `sl_source` расширен и теперь включает `ob`, `fractal`, `bos`;
- SELL больше не выбрасываются из replay dataset;
- per-symbol override переведён на реальный `trade_plan`;
- репозиторий документирует переход к `limit_pending` как канонической модели исполнения.

Однако несколько фундаментальных проблем остаются. Главные:

1. **timestamp preservation после `reset_index(drop=False)` всё ещё реализован неправильно**;
2. **continuation pipeline всё ещё выбирает первый подходящий sweep и не проверяет его временную связь с BOS**;
3. **live scanner всё ещё механически сдвигает structural SL/TP вслед за live price**;
4. **portfolio limits по-прежнему имеют check-then-act race condition**;
5. **regime detection зависит от uptime процесса и состояния памяти**;
6. **sweep quality по-прежнему использует будущие относительно события свечи**;
7. **replay dataset продолжает тихо превращать detector exceptions в отсутствие событий**;
8. **README существенно расходится с текущей стратегией**.

---

# 1. Сверка предыдущего аудита

## Исправлено или существенно улучшено

### FIX-1. `Sweep → MSS` causality

В `strategy/pattern_engine.py` теперь явно есть проверка:

```python
if mss.candle_index <= sweep_candle_index:
    return ICTSetup(
        detected=False,
        rejection_reason="reversal: MSS before sweep (causality violated)",
    )
```

Это исправляет прежнюю ошибку, когда отрицательная разница превращалась через `max(0, ...)` в ложное значение `0 bars`.

**Статус: исправлено.**

---

### FIX-2. Reversal теперь использует newest valid sweep

Теперь:

```python
valid_sweeps = [s for s in sweeps if s.is_valid]
s = valid_sweeps[-1]
```

Это лучше предыдущего выбора первого sweep.

**Статус: исправлено.**

---

### FIX-3. Добавлена direction consistency для reversal

Для BUY теперь требуется bullish sweep, для SELL — bearish sweep.

**Статус: исправлено.**

---

### FIX-4. `sl_source` частично унифицирован

Risk Engine теперь включает:

```text
ob
fractal
bos
```

в набор structural sources.

**Статус: исправлено.**

---

### FIX-5. SELL снова включены в dataset

`replay_dataset.py` теперь явно говорит:

> SELL setups are included for balanced ML training data.

Прежний прямой фильтр всех SELL отсутствует.

**Статус: исправлено.**

---

### FIX-6. Per-symbol overrides больше не опираются на отсутствующие поля ICTSetup

В scanner сначала строится `trade_plan`, после чего overrides получают `trade_plan` и вычисленный `atr_pct`.

Это устраняет ранее найденный риск `AttributeError` из-за несуществующих полей setup.

**Статус: выглядит исправленным по текущему пути scanner.**

---

# 2. Новые и оставшиеся критические проблемы

## 🔴 P0-1. Попытка исправить timestamp preservation фактически всё ещё хранит RangeIndex

**Файлы:**

- `liquidity/sweep.py`
- `liquidity/order_blocks.py`
- `liquidity/fvg.py`

### Текущий код

Используется:

```python
data = df.tail(lookback).reset_index(drop=False)
_orig_index = data.index.tolist()
```

Комментарий говорит:

```text
preserve original datetime index
```

Но после `reset_index(drop=False)`:

- исходный DatetimeIndex превращается в обычную колонку, обычно `index`;
- `data.index` становится новым `RangeIndex(0..N-1)`.

Следовательно:

```python
_orig_index = data.index.tolist()
```

**не сохраняет исходный datetime index**.

### Пример

Было:

```text
2026-08-30 10:00
2026-08-30 11:00
```

После:

```python
reset_index(drop=False)
```

получается концептуально:

```text
index column: 2026-08-30 10:00, 2026-08-30 11:00
DataFrame.index: 0, 1
```

Но код берёт:

```python
data.index
```

то есть `0, 1`.

### Последствие

`SweepEvent.timestamp`, `OrderBlock.timestamp` и `FairValueGap.timestamp` могут быть ложными.

### Исправление

Либо вообще не сбрасывать индекс:

```python
data = df.tail(lookback).copy()
orig_index = data.index
```

либо после reset:

```python
orig_index = data["index"].tolist()
```

но только если исходный индекс действительно был перенесён именно в колонку `index`.

**Приоритет: P0.**

---

## 🔴 P0-2. Continuation pipeline всё ещё выбирает первый sweep

**Файл:** `strategy/pattern_engine.py`

В continuation:

```python
for s in valid_sweeps:
    if direction == "buy" and s.type == "bearish":
        ...
        break
```

В отличие от reversal pipeline здесь не используется newest/nearest sweep.

### Последствие

Если список хронологический, continuation получает самый старый подходящий sweep.

Это снова может связать:

```text
старый sweep
+
современный BOS
```

как будто это один setup.

### Исправление

Минимум:

```python
for s in reversed(valid_sweeps):
    ...
```

Но правильнее:

```text
выбрать последний sweep
который произошёл до BOS
и находится в max-age window
```

**Приоритет: P0/P1.**

---

## 🔴 P0-3. Continuation не имеет строгой causality между sweep и BOS

Текущая continuation логика проверяет:

- trend;
- BOS;
- наличие sweep противоположного типа.

Но не видно строгой проверки:

```text
sweep event time
    <
BOS event time
    <
current decision time
```

### Последствие

Система может склеивать два несвязанных исторических события.

### Исправление

У BOS и Sweep должны быть совместимые:

```text
absolute candle_index
event_timestamp
confirmed_timestamp
```

И continuation должен требовать:

```text
0 < BOS.index - sweep.index <= configured window
```

**Приоритет: P0.**

---

## 🔴 P0-4. Structural SL/TP всё ещё сдвигаются вслед за live price

**Файл:** `scheduler/scanner.py`

По-прежнему:

```python
_price_offset = _live_price - _candle_close

entry_price = round(_live_price, 8)
sl = round(sl + _price_offset, 8)
tp = round(tp + _price_offset, 8)
```

### Проблема

Структурные уровни:

- OB boundary;
- swing;
- liquidity;
- BOS;

не должны автоматически двигаться вместе с live ticker.

### Последствие

Получается synthetic structural level, которого рынок фактически не сформировал.

### Рекомендация

Либо:

```text
live entry + original structural SL/TP
```

с повторным risk/RR calculation,

либо при значительном отклонении:

```text
invalidate setup → recalculate from current market structure
```

**Приоритет: P0.**

---

# 3. Высокие проблемы

## 🟠 P1-1. Race condition в portfolio risk limits остаётся

**Файл:** `scheduler/scanner.py`

По-прежнему схема:

```text
read active count
→ read portfolio risk
→ долго анализировать setup
→ later create signal
```

Параллельные scanner tasks могут одновременно пройти проверку.

### Исправление

Атомарная операция:

```text
BEGIN
check portfolio limits
check dedup
reserve signal
COMMIT
```

Локальный `asyncio.Lock` допустим только как временная защита внутри одного процесса.

**Приоритет: P1.**

---

## 🟠 P1-2. Regime detection зависит от uptime процесса

**Файл:** `scheduler/scanner.py`

Состояние:

```python
_ema_spread_history
```

живёт между scan cycles.

После restart:

```text
history = empty
```

После долгой работы:

```text
history = accumulated
```

Одинаковые OHLCV могут дать разные regime.

### Исправление

Строить EMA spread history непосредственно из последних N свечей.

**Приоритет: P1.**

---

## 🟠 P1-3. Sweep использует будущие свечи относительно event timestamp

Sweep подтверждается следующей свечой, а displacement может анализировать ещё несколько последующих баров.

Это допустимо только если setup становится доступным **после последнего использованного confirmation bar**.

### Проблема

Объект хранит:

```text
timestamp
candle_index
```

но не хранит явно:

```text
confirmed_at
decision_ready_at
```

### Исправление

Разделить:

```text
event_timestamp
reclaim_confirmed_timestamp
displacement_confirmed_timestamp
decision_ready_timestamp
```

**Приоритет: P1.**

---

## 🟠 P1-4. Replay dataset тихо проглатывает ошибки detector-ов

В `replay_dataset.py`:

```python
try:
    sweeps = detect_sweeps(...)
except Exception:
    sweeps = []
```

Аналогично для других компонентов.

### Последствие

Регрессия может выглядеть как:

```text
стало меньше setup-ов
```

вместо:

```text
detector сломан
```

### Исправление

Для research pipeline:

- считать exceptions;
- логировать symbol/timeframe/timestamp;
- падать при превышении порога;
- не считать silent fallback нормальным состоянием.

**Приоритет: P1.**

---

## 🟠 P1-5. Dataset и live всё ещё не имеют полного feature parity

В replay dataset по-прежнему видны фиксированные offline значения:

```text
mtf_aligned=False
mtf_count=0
context_score=0.0
fear_greed=None
funding_rate=None
```

Если эти признаки участвуют в live ProbabilityEngine, distribution отличается.

### Исправление

Либо:

1. строить те же признаки исторически;
2. удалить их из model feature vector;
3. явно обучать и использовать модель только на совпадающем подмножестве features.

**Приоритет: P1.**

---

# 4. Средние проблемы

## 🟡 P2-1. Order Block использует средний volume всего окна

`avg_vol = mean(full lookback)` затем используется для оценки исторического OB.

Для события в начале окна baseline может включать volume после самого события.

### Риск

Будущая относительно OB информация влияет на feature.

### Исправление

Rolling baseline:

```text
volume before/current event only
```

---

## 🟡 P2-2. FVG/OB/Sweep всё ещё используют разные семантики индекса

Даже после попытки исправления timestamp:

- Sweep использует offset для `candle_index`;
- OB хранит локальный `i`;
- FVG хранит локальный `i+1`.

Это означает, что объекты из одного и того же historical window могут иметь несопоставимые индексы.

### Исправление

Один контракт:

```text
absolute index in original input dataframe
```

для всех событий.

---

## 🟡 P2-3. Displacement gate документирован неоднозначно

В Pattern Engine заявлено:

```text
Reversal: sweep + displacement + MSS
```

Но scanner говорит, что displacement текущей свечи informational, потому что MSS уже валидирует displacement.

Это может быть корректной новой стратегической логикой, но документация и implementation должны использовать одинаковую терминологию:

```text
displacement component
```

не равно:

```text
current candle is displacement
```

---

# 5. Документация и конфигурационный drift

## 🟡 P2-4. README существенно устарел

README описывает:

```text
4 из 7 indicator conditions
ADX gate
EMA / RSI / MACD
```

Но текущий scanner прямо говорит:

```text
ICT Core pipeline
Pattern Engine → Risk Engine
All indicator-based filtering removed
```

Также в текущем коде присутствует:

```text
STRATEGY_MODE == CONFLUENCE
```

с отдельными правилами:

- reversal blocked;
- OB required for continuation.

README этого не отражает.

### Последствие

Пользователь может считать, что текущая стратегия работает по одному набору параметров, хотя фактический live pipeline другой.

### Исправление

Сделать один canonical strategy specification:

```text
Strategy version
Active mode
Hard gates
Soft gates
Execution model
Entry model
SL/TP model
Direction filters
Risk limits
Backtest parity rules
```

---

# 6. Новые параметры и текущая архитектурная точка

По текущему публичному коду и документам репозитория видны следующие изменения по сравнению с предыдущим аудитом.

## Стратегический режим

Scanner содержит режим:

```text
STRATEGY_MODE = CONFLUENCE
```

В этом режиме:

- reversal setups блокируются;
- для continuation требуется Order Block.

Документированные статистические причины в комментариях:

```text
reversal WR = 3.6%
BOS + OB WR = 93.7%
```

Эти цифры должны рассматриваться как **исследовательские исторические метрики**, а не как гарантия будущей эффективности.

---

## Каноническая модель исполнения

В `audit_closure_and_next_phase.md` зафиксировано:

```text
Execution model: limit_pending
```

Также документ говорит, что:

- pooled edge на проверенном наборе символов была отрицательной;
- текущая стратегия не прошла проверку edge на выбранном временном split;
- дальнейший параметрический тюнинг прежней логики признан бесперспективным;
- следующий этап должен быть пересмотром источника сигналов, а не простым изменением порогов.

Это важное изменение по сравнению с ранним состоянием проекта.

---

# 7. Внутреннее противоречие, которое нужно проверить перед production

Документ аудита фиксирует `limit_pending` как каноническую модель.

Но текущий scanner содержит live price alignment:

```text
ticker
→ market-like current entry
→ offset SL/TP
```

Это выглядит как другая execution semantics.

Нужно формально определить один ответ на вопрос:

```text
Бот signal-only?
```

или:

```text
Бот моделирует limit entry?
```

или:

```text
Бот моделирует market entry?
```

Пока одновременно присутствуют признаки нескольких моделей.

---

# 8. Приоритетный план исправления

## P0 — исправить первым

1. Исправить timestamp extraction после `reset_index(drop=False)`.
2. Унифицировать absolute candle indices для Sweep/OB/FVG.
3. Исправить continuation sweep selection: newest causal sweep, а не первый.
4. Добавить строгую `Sweep → BOS` causality.
5. Убрать offset-shift structural SL/TP или заменить полноценным recalculation.

## P1

6. Сделать portfolio limits атомарными.
7. Убрать uptime-dependent regime history.
8. Добавить confirmation timestamps для sweep/pivot events.
9. Сделать replay detector failures observable/fail-loud.
10. Проверить полный offline/live feature parity.

## P2

11. Обновить README.
12. Унифицировать event model.
13. Убрать локальные индексные семантики.
14. Добавить regression tests для всех fixed findings.

---

# 9. Обязательные regression tests

## Timestamp preservation

Проверить:

```python
df.index = DatetimeIndex(...)
event.timestamp == df.index[absolute_event_index]
```

после любого:

```python
tail()
reset_index()
```

---

## Continuation causality

```text
old sweep + new BOS outside allowed window → reject
new sweep before BOS → accept candidate
sweep after BOS → reject
```

---

## Sweep selection

```text
old valid sweep
new valid sweep
```

Должен использоваться newest causal event.

---

## Structural SL

Проверить:

```text
live price changes
```

но:

```text
OB / swing / BOS invalidation level
```

не сдвигается механически.

---

## Parallel portfolio limit

Одновременно:

```text
active = 2
limit = 3
5 parallel candidates
```

Итог:

```text
active <= 3
```

---

## Restart determinism

```text
same OHLCV
same config
restart process
```

должны давать:

```text
same regime
same setup decision
```

---

# 10. Итоговый вердикт

Повторный аудит показывает **реальный прогресс**: несколько ранее найденных критических ошибок действительно исправлены, особенно reversal causality, direction consistency, выбор newest reversal sweep, SELL dataset и часть интеграции risk/strategy.

Но проект пока нельзя считать полностью очищенным от фундаментальных логических рисков.

### Главный остаточный риск

Сейчас наиболее опасна не формула индикатора, а согласование:

```text
event time
→ confirmation time
→ absolute candle identity
→ causal relationship
→ decision time
→ execution model
```

Пока timestamp/index contract остаётся нарушенным после `reset_index(drop=False)`, а continuation не связывает sweep и BOS строгой временной причинностью, часть структурных setup-ов может быть построена на несогласованных событиях.

### Общая оценка

**Код стал заметно лучше после предыдущего аудита, но P0/P1 проблемы ещё остаются.**

Прежде чем доверять новым результатам backtest или использовать стратегию как основу для автоматического исполнения, я рекомендую сначала закрыть:

1. timestamp/index identity;
2. continuation causality;
3. execution-model consistency;
4. atomic portfolio limits;
5. deterministic regime calculation.

---

## Ограничение этого повторного отчёта

Отчёт основан на статическом анализе текущего публичного состояния репозитория, доступного 30 августа 2026. Он не включает:

- запуск полного тестового набора;
- запуск бота против Binance;
- нагрузочное тестирование SQLite/async;
- независимую проверку всех исторических результатов;
- penetration testing web/Telegram интерфейса.

Следующий полноценный этап должен проверить полный execution path:

```text
scanner
→ Pattern Engine
→ Trade Plan
→ execution model
→ Risk Engine
→ DB transaction
→ notifier
→ outcome tracker
```

с реальными regression/integration tests.
