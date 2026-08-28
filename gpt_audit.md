# Аудит проекта tgbot

**Репозиторий:** Andersan41/tgbot  
**Проверенная ветка:** `tgbot-claude`  
**Дата отчёта:** 29 августа 2026  
**Статус:** промежуточный технический аудит (проверены не все модули проекта)

> Важно: этот отчёт объединяет все результаты аудита, озвученные в ходе текущего разбора. Часть выводов требует финальной проверки полного execution path и тестового прогона. Наиболее критические проблемы относятся к временной причинности, расхождению live/backtest и интеграции модулей.

---

## 1. Краткое резюме

Основной риск проекта находится не в отдельных индикаторах, а в **причинности событий во времени**.

В стратегии присутствуют события с различным временем возникновения и подтверждения:

```text
Pivot появляется
    ↓ через N свечей подтверждается

Sweep происходит
    ↓ следующая свеча подтверждает reclaim
    ↓ до 5 свечей оценивается displacement

MSS происходит
    ↓ сопоставляется с sweep

Signal создаётся
    ↓ определяется entry
    ↓ рассчитываются SL/TP
    ↓ применяется risk engine
    ↓ сигнал записывается в БД / отправляется
```

Для корректного backtest и live pipeline необходимо строго различать:

- `event_timestamp` — когда событие произошло;
- `confirmed_timestamp` — когда оно стало известно алгоритму;
- `decision_timestamp` — когда стратегия получила право принять решение;
- `execution_timestamp` — когда реально происходит вход.

В текущей архитектуре эти моменты не всегда разделены.

---

# 2. Найденные проблемы

## 🔴 P0-1. Неоднозначность SL/TP внутри одной свечи

**Файл:** `backtest/engine.py`  
**Участок:** логика проверки SL и TP для открытой сделки.

### Проблема

Для BUY сначала проверяется SL:

```python
if low <= t.sl:
    ...
    continue

if high >= t.tp:
    ...
```

Для SELL аналогично сначала проверяется SL.

Если одна OHLC-свеча одновременно достигла:

```text
BUY:
low <= SL
high >= TP
```

backtest всегда засчитывает SL.

### Почему это важно

OHLC не содержит внутрисвечной последовательности движения. Нельзя автоматически определить, было:

```text
Open → Low → High → Close
```

или:

```text
Open → High → Low → Close
```

### Последствия

- систематическое искажение статистики;
- неправильная оптимизация параметров;
- возможное расхождение с live;
- скрытая зависимость результата от порядка `if`.

### Рекомендация

Добавить явную модель:

- `conservative`;
- `optimistic`;
- `ohlc_path`;
- лучший вариант — lower-timeframe replay для intrabar resolution.

---

## 🔴 P0-2. Structural SL/TP механически сдвигаются вслед за live price

**Файл:** `scheduler/scanner.py`

### Проблема

После получения live ticker рассчитывается:

```python
_price_offset = _live_price - _candle_close
```

Затем:

```python
entry_price = round(_live_price, 8)
sl = round(sl + _price_offset, 8)
tp = round(tp + _price_offset, 8)
```

### Почему это ошибка

SL/TP могут быть рассчитаны относительно:

- swing;
- order block;
- liquidity;
- BOS;
- FVG;
- ATR.

Механическое добавление одинакового offset делает structural level искусственным.

### Пример

```text
Entry: 100
Structural SL: 95
TP: 110

Live price: 102
```

После offset:

```text
Entry: 102
SL: 97
TP: 112
```

Но реальный structural SL был на 95.

### Рекомендация

Вариант 1:

```text
Entry = live price
SL = original structural SL
TP = original structural TP
```

После этого заново проверить RR и risk.

Вариант 2: при большом отклонении цены пересчитывать setup полностью.

---

## 🔴 P0-3. Race condition в portfolio limits

**Файл:** `scheduler/scanner.py`

### Проблема

До создания сигнала отдельно запрашиваются:

- active signals по symbol;
- общее количество active signals;
- portfolio risk.

После этого проходит длительный pipeline анализа.

При параллельном сканировании несколько задач могут одновременно увидеть:

```text
active_count = 2
limit = 3
```

и все пройти проверку.

### Последствие

Фактическое количество сигналов может превысить лимит.

То же относится к:

- portfolio risk;
- per-symbol limits;
- deduplication;
- cooldown.

### Рекомендация

Проверка + резервирование/создание сигнала должны быть атомарными:

```text
BEGIN TRANSACTION
    check limits
    check dedup
    reserve/create signal
COMMIT
```

---

## 🔴 P0-4. Несовместимые `sl_source` между Signal Engine и Risk Engine

**Файлы:**

- `strategy/signal_engine.py`
- `risk/engine.py`

### Signal Engine возвращает

```text
ob
fractal
bos
atr
```

### Risk Engine считает structural

```text
sweep_extreme
ob_boundary
swing_point
bos_level
structural
```

### Последствие

Например:

```text
sl_source = "ob"
```

не считается structural в Risk Engine.

То же возможно для:

```text
fractal
bos
```

### Рекомендация

Не передавать свободные строковые литералы.

Использовать общий enum:

```python
class SLSource(Enum):
    OB = "ob"
    FRACTAL = "fractal"
    BOS = "bos"
    ATR = "atr"
```

---

## 🔴 P0-5. Потеря реальных timestamp после `reset_index(drop=True)`

**Файлы:**

- `liquidity/order_blocks.py`
- `liquidity/fvg.py`
- `liquidity/sweep.py`

### Проблема

Используется:

```python
data = df.tail(lookback).reset_index(drop=True)
```

После этого:

```text
data.index = 0, 1, 2, ...
```

Но timestamp затем строится из этого индекса.

### Последствие

Объекты могут получить:

```text
timestamp = 17
```

вместо реального времени свечи.

Это влияет на:

- свежесть событий;
- causality;
- дедупликацию;
- аналитику;
- ML dataset.

### Рекомендация

Не сбрасывать timestamp index:

```python
data = df.tail(lookback).copy()
```

---

## 🔴 P0-6. Sweep использует будущие свечи

**Файл:** `liquidity/sweep.py`

### Проблема

Sweep подтверждается следующей свечой:

```python
_close[i + 1]
```

Кроме того displacement проверяется до нескольких будущих свечей:

```text
i+1 ... i+5
```

### Почему это опасно

Информация о sweep становится доступной позже момента самого sweep.

Если стратегия использует timestamp события как будто setup был доступен на свече `i`, возникает look-ahead.

### Рекомендация

У каждого sweep хранить:

```text
event_timestamp
reclaim_confirmed_timestamp
displacement_confirmed_timestamp
decision_ready_timestamp
```

---

## 🔴 P0-7. `Sweep → MSS` causality не гарантируется

**Файл:** `strategy/pattern_engine.py`

Используется:

```python
sweep_to_mss = max(
    0,
    mss.candle_index - sweep_candle_index
)
```

### Проблема

Если MSS произошёл раньше sweep:

```text
MSS index < sweep index
```

результат становится:

```text
0
```

То есть неправильный порядок маскируется как «0 баров между событиями».

### Правильная логика

```python
if mss.candle_index <= sweep.candle_index:
    reject
```

После этого:

```python
bars = mss.candle_index - sweep.candle_index
```

без `max(0, ...)`.

---

## 🔴 P0-8. Reversal path не гарантирует согласованность sweep и MSS

**Файл:** `strategy/pattern_engine.py`

### Проблема

Для reversal выбирается valid sweep, затем направление определяется из MSS.

Нет строгой проверки:

- направление sweep;
- направление MSS;
- sweep произошёл раньше MSS;
- MSS действительно является реакцией на этот sweep.

### Последствие

Возможны несвязанные комбинации событий.

### Рекомендация

Ввести явную state machine:

```text
1. Sweep detected
2. Sweep confirmed
3. Displacement confirmed
4. MSS after sweep
5. Setup valid
```

---

## 🔴 P0-9. Pattern Engine может выбирать самый старый valid sweep

**Файл:** `strategy/pattern_engine.py`

### Проблема

Используется первый valid sweep:

```python
for s in valid_sweeps:
    ...
    break
```

Если список идёт от старых событий к новым, будет выбран старый sweep.

### Последствие

Новый актуальный sweep может игнорироваться.

### Рекомендация

Явно выбирать:

- последний causal sweep;
- либо лучший по quality score;
- либо ближайший перед MSS.

Например:

```text
select latest sweep where:
sweep.confirmed_at < mss.timestamp
and direction compatible
and age <= max_age
```

---

## 🔴 P0-10. Replay dataset может быть BUY-only

**Файл:** `backtest/replay_dataset.py`

Присутствует логика:

```python
if setup.direction == "sell":
    continue
```

### Последствие

Если dataset используется для ProbabilityEngine/ML:

- SELL distribution отсутствует;
- модель может быть directional-biased;
- live SELL predictions могут быть плохо откалиброваны.

### Рекомендация

Либо:

1. добавить SELL в dataset;

либо:

2. явно объявить стратегию LONG-ONLY во всех слоях.

---

# 3. Серьёзные логические проблемы

## 🟠 P1-1. Live и backtest используют разные execution models

Live корректирует entry/SL/TP по live ticker.

Backtest работает по candle-level модели.

### Последствие

Backtest проверяет не ту систему, которая реально работает в production.

### Рекомендация

Зафиксировать единый контракт:

```text
Decision timestamp
Execution timestamp
Entry model
SL/TP model
First eligible exit candle
Fees/slippage
```

И использовать его в:

- live;
- backtest;
- replay dataset.

---

## 🟠 P1-2. Возможный HTF look-ahead

**Файл:** `backtest/engine.py`

HTF данные могут загружаться как текущее состояние и затем использоваться историческим проходом.

Если historical bar использует HTF, сформированный из будущих данных, появляется leakage.

### Рекомендация

На каждом decision timestamp:

```python
htf_slice = htf_df[htf_df.index <= decision_time]
```

Причём использовать только полностью закрытые HTF candles.

---

## 🟠 P1-3. Partial HTF candle при resampling

**Файл:** `backtest/replay_dataset.py`

При resampling H1 → H4/D1/W1 есть риск использовать текущую незакрытую higher-timeframe свечу.

### Последствие

HTF EMA/bias может знать часть будущего относительно момента принятия решения.

### Рекомендация

Явно использовать только completed HTF bars.

---

## 🟠 P1-4. Swing points требуют будущих свечей

**Файл:** `market_structure/structure.py`

Swing определяется окном:

```text
N свечей до pivot
N свечей после pivot
```

### Проблема

Pivot timestamp не равен моменту, когда pivot стал известен алгоритму.

### Рекомендация

Хранить:

```text
pivot_timestamp
confirmed_timestamp
```

и запрещать использование pivot до `confirmed_timestamp`.

---

## 🟠 P1-5. BOS определяется как сравнение pivot, а не как фактическое break event

**Файл:** `market_structure/structure.py`

Bullish BOS может определяться логикой:

```text
новый swing high > предыдущий swing high
```

Это не обязательно момент фактического пробоя уровня.

### Последствия

Искажается:

- timing BOS;
- causality;
- возраст структуры;
- sweep → BOS/MSS последовательность.

### Рекомендация

Хранить отдельное событие:

```text
structure level established
↓
price breaks level
↓
break confirmed
```

---

## 🟠 P1-6. MTF может работать fail-open при ошибках API

**Файл:** `market_structure/structure.py`

Ошибки получения HTF могут просто игнорироваться:

```python
except Exception:
    continue
```

### Риск

Недоступность данных может превратиться в разрешение сигнала.

### Рекомендация

Использовать:

```text
ALIGNED
NOT_ALIGNED
UNKNOWN
```

Если HTF обязателен:

```text
UNKNOWN → reject
```

---

## 🟠 P1-7. Ranging timeframe может частично засчитываться как alignment

В MTF logic ranging состояние может одновременно иметь признаки alignment.

### Проблема

Смешиваются:

- trend confirmation;
- отсутствие тренда;
- локальный BOS.

### Рекомендация

```text
Bullish aligned → +1 BUY
Bearish aligned → +1 SELL
Ranging → 0
Unknown → fail
```

BOS внутри range использовать отдельно.

---

## 🟠 P1-8. Order Block volume baseline может включать будущее

**Файл:** `liquidity/order_blocks.py`

Средний объём окна может рассчитываться с использованием свечей, которые произошли после OB.

### Последствие

Historical feature может использовать future information относительно момента события.

### Рекомендация

Использовать rolling average только до текущей свечи:

```text
volume[i-N:i]
```

---

## 🟠 P1-9. Offline dataset features не соответствуют live feature distribution

**Файл:** `backtest/replay_dataset.py`

В offline dataset часть features фиксируется или отключается:

```text
MTF = False
MTF count = 0
Context = 0
Fear/Greed = None
Funding = None
```

При этом заявляется соответствие live ProbabilityEngine.

### Последствие

ML train distribution и live inference distribution могут различаться.

### Рекомендация

Либо генерировать feature parity, либо явно исключить эти поля из модели.

---

## 🟠 P1-10. Silent exceptions превращают баги в «нет сигнала»

**Файл:** `backtest/replay_dataset.py`

Есть паттерн:

```python
try:
    detector(...)
except Exception:
    result = []
```

### Последствие

Регрессия или баг могут незаметно уничтожить часть сетапов.

### Рекомендация

Для backtest/research:

- логировать exception;
- считать ошибки;
- падать при превышении порога;
- сохранять timestamp/symbol.

---

## 🟠 P1-11. Execution model dataset не полностью определён

Dataset может использовать:

```text
Signal on candle i close
Entry = current close
Exit simulation starts at candle i+1
```

Это одна возможная модель, но она должна совпадать с live и backtest.

### Рекомендация

Формально описать execution contract.

---

# 4. Проблемы воспроизводимости

## 🟡 P2-1. Regime history зависит от uptime бота

**Файл:** `scheduler/scanner.py`

EMA spread history хранится в runtime memory:

```text
бот работает → история есть
бот перезапущен → история пуста
```

### Последствие

Одинаковый рынок может получить разный regime после рестарта.

### Рекомендация

Рассчитывать regime непосредственно из последних N свечей.

---

## 🟡 P2-2. Portfolio state может устареть до Risk Engine

Состояние портфеля читается до прохождения полного pipeline.

Пока сигнал анализируется, другой сигнал может быть добавлен.

### Рекомендация

Финальную проверку risk/portfolio limits выполнять непосредственно перед атомарным созданием сигнала.

---

## 🟡 P2-3. Недостаток HTF history может интерпретироваться как neutral

`get_htf_bias_v2()` при недостатке данных возвращает neutral.

### Риск

Недостаток данных становится не ошибкой, а отсутствием направления.

### Рекомендация

Разделить:

```text
NEUTRAL — достаточно данных, реального bias нет
UNKNOWN — недостаточно данных
```

---

## 🟡 P2-4. Индексы lookback-окон могут быть локальными

После:

```python
df.tail(lookback).reset_index(drop=True)
```

`candle_index` становится относительным.

Если такие индексы сравниваются с абсолютными индексами других модулей, causality становится ненадёжной.

---

# 5. Проблемы документации

README описывает старую индикаторную модель:

- Supertrend;
- EMA;
- EMA cross;
- RSI;
- MACD;
- ADX;
- Volume;
- правило «4 из 7».

Но текущий `signal_engine.py` заявляет, что индикаторные gates удалены и Pattern Engine является основным источником сигналов.

## Риск

Пользователь и разработчик могут считать, что меняют параметры активной стратегии, хотя реальный pipeline уже другой.

## Рекомендация

Обновить README и добавить:

```text
Current live strategy pipeline
Legacy components
Backtest execution model
Feature model
Signal lifecycle
```

---

# 6. Архитектурная корневая причина

Большинство критических проблем имеют общую причину:

## Нет единого контракта событий

Для каждого объекта нужно определить:

```text
Event ID
Symbol
Timeframe
Event timestamp
Confirmation timestamp
Decision-ready timestamp
Absolute candle index
Source data window
Causal dependencies
```

Например:

```text
Sweep:
event at candle 100
reclaim confirmed at 101
displacement confirmed at 104
decision-ready at 104
```

Только после `decision-ready` событие может использоваться Pattern Engine.

---

# 7. Рекомендуемый план исправления

## P0 — исправить немедленно

1. Исправить causality `Sweep → MSS`.
2. Исправить потерю timestamp после `reset_index`.
3. Унифицировать `SLSource`.
4. Устранить arbitrary SL/TP shifting.
5. Определить intrabar policy для SL/TP.
6. Проверить/исправить BUY-only dataset.
7. Добавить atomic portfolio reservation.

## P1 — затем

1. Переписать event timing model.
2. Добавить confirmed timestamps для pivots/sweeps.
3. Исправить BOS как break event.
4. Сделать HTF строго completed-only.
5. Устранить MTF fail-open.
6. Сделать dataset/live feature parity.
7. Убрать silent exceptions из research pipeline.

## P2 — качество и воспроизводимость

1. Убрать regime dependence от uptime.
2. Разделить UNKNOWN и NEUTRAL.
3. Унифицировать absolute candle indices.
4. Добавить property-based и regression tests.
5. Обновить README.

---

# 8. Критические тесты, которых должны быть

## Sweep causality

```text
MSS before sweep → REJECT
MSS after sweep → POSSIBLE
```

## Future leakage

Для каждого feature:

```text
feature at t
```

должен быть идентичен при запуске:

```text
df[:t]
```

и:

```text
full_df, evaluated as of t
```

## Pivot confirmation

```text
pivot at 100
window = 5
```

Pivot не должен быть доступен до candle 105.

## Intrabar SL/TP

Отдельные тесты:

```text
SL only
TP only
SL and TP same bar
gap through SL
gap through TP
```

## Restart determinism

После restart:

```text
same OHLCV
→ same regime
→ same signal
```

## Parallel portfolio limit

Запустить одновременно несколько scan tasks:

```text
limit = 3
active = 2
parallel candidates = 5
```

После завершения:

```text
active <= 3
```

## Timestamp integrity

После любого:

```python
tail()
reset_index()
resample()
```

проверить, что event timestamp соответствует исходной свече.

---

# 9. Итоговая оценка

## Сильные стороны

- проект имеет разделение на market structure, liquidity, risk и strategy;
- есть попытка обеспечить parity live/backtest;
- есть replay dataset;
- есть структурная модель вместо простого набора индикаторов;
- код содержит заметную модульность.

## Основные риски

На текущем этапе самые серьёзные риски:

1. **временная причинность и look-ahead**;
2. **несовпадение момента события и момента его подтверждения**;
3. **расхождение live/backtest execution model**;
4. **интеграционные несоответствия между модулями**;
5. **race conditions при portfolio limits**;
6. **потеря timestamp/index identity**.

## Общий вердикт

Проект выглядит архитектурно амбициозным, но в текущем состоянии требует исправления критических вопросов **causality и execution model** до того, как результаты backtest можно будет считать надёжной оценкой live-стратегии.

Самый важный архитектурный рефакторинг:

```text
Event
→ Confirmation
→ Decision-ready
→ Signal
→ Execution
→ Position lifecycle
```

Каждый этап должен иметь собственный timestamp и строгие causal ограничения.

---

# 10. Ограничение текущего отчёта

Это объединённый отчёт по уже разобранным слоям. Полный финальный аудит ещё должен отдельно проверить:

- Trade Engine;
- полный Probability/ML pipeline;
- полный Risk Engine;
- scanner concurrency;
- database lifecycle;
- Telegram delivery/idempotency;
- тестовое покрытие всех критических сценариев.

Поэтому документ следует считать **промежуточным аудитом с уже найденными конкретными проблемами**, а не окончательной сертификацией всего репозитория.
