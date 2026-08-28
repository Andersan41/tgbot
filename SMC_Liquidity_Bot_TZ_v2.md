# ТЕХНИЧЕСКАЯ ДОКУМЕНТАЦИЯ (ТЗ) — ВЕРСИЯ 2.0
## Автоматизированный SMC & Liquidity Scanner Bot
### Дата: 2026-08-21 | Исправления после аудита логики
### Автор стратегии: Егор Круталевич (SMC/Liquidity Concepts)

---

## ИСТОРИЯ ИЗМЕНЕНИЙ

| Версия | Дата | Изменения |
|--------|------|-----------|
| 1.0 | 2026-08-21 | Базовая версия |
| 2.0 | 2026-08-21 | Исправлены: TP-направление, temporal binding Sweep→Confirmation, FVG-алгоритм, Breakeven, Partial Close, Daily Limits, Confirmation scoring, Sweep timeout, Pool lifecycle, Post-fill RR recalc |

---

## СОДЕРЖАНИЕ

1. Общее описание стратегии
2. Архитектура WebSocket-бота
3. Много timeframe анализ (MTF)
4. Модуль 1: Детекция пулов ликвидности (Liquidity Pools)
5. Модуль 2: Детекция манипуляции (Liquidity Sweep)
6. Модуль 3: Подтверждение разворота (Entry Confirmation)
7. Модуль 4: Логика входа в сделку (Execution)
8. Модуль 5: Управление позицией (Trade Management)
9. Модуль 6: Риск-менеджмент (Position Sizing)
10. Модуль 7: Инвалидация и flip bias (Invalidation)
11. Модуль 8: Фильтры и контекст (Market Regime)
12. State Machine (полный жизненный цикл сделки)
13. Псевдокод ядра бота
14. Спецификация WebSocket API
15. Обработка ошибок и отказоустойчивость
16. Метрики и мониторинг
17. Чеклист реализации
18. Параметры по умолчанию
19. Рекомендации по промтингу

---

## 1. ОБЩЕЕ ОПИСАНИЕ СТРАТЕГИИ

### 1.1 Философия
Стратегия основана на концепции **Smart Money Concepts (SMC)** с ключевым постулатом:
> **Цена всегда движется от ликвидности к ликвидности**

Рынок формирует пулы ликвидности (непротестированные экстремумы), к которым стремится цена. Крупные игроки (smart money) манипулируют этой ликвидностью — снимают стоп-лоссы розничных трейдеров (liquidity sweep), после чего происходит разворот. Бот детектирует эту манипуляцию и входит в направлении разворота после получения подтверждения.

### 1.2 Торговые инструменты
- **Активы:** Криптовалютные пары (USDT-M фьючерсы)
- **Таймфреймы:**
  - `4H` — глобальный контекст, поиск крупных пулов ликвидности
  - `1H` — определение опорных областей (Order Blocks), промежуточный контекст
  - `15m` — детекция манипуляции (sweep)
  - `5m` — подтверждение разворота и точечный вход
- **Тип сделок:** Лонг и Шорт (симметричная логика)
- **Тип ордеров:** Market или Limit

### 1.3 Общий пайплайн сделки

```
[4H] Идентификация HTF пула ликвидности (EQH/EQL) — цель движения
    |
[1H] Проверка контекста (тренд, OB, структура)
    |
[15m] Детекция Sweep (манипуляция ликвидностью)
    |
[5m] Подтверждение разворота (FVG + OB + BOS) — ТОЛЬКО после Sweep
    |
[5m] Вход в сделку (Market/Limit)
    |
[5m] Управление (SL/TP/Partial Close/Trailing/Invalidation)
```

---

## 2. АРХИТЕКТУРА WEBSOCKET-БОТА

### 2.1 Общая схема

```
WEBSOCKET INGESTION LAYER
  +-----------+  +-----------+  +-----------+  +-----------+
  |  4H Kline |  |  1H Kline |  | 15m Kline |  |  5m Kline |
  |   Stream  |  |   Stream  |  |   Stream  |  |   Stream  |
  +-----+-----+  +-----+-----+  +-----+-----+  +-----+-----+
        |              |              |              |
        +--------------+--------------+--------------+
                       |
             +---------v---------+
             |  BAR BUFFER POOL  |  (deque, max 500 баров)
             |  (per symbol/TF)  |
             +---------+---------+
                       |
CORE ENGINE LAYER      |
             +---------v---------+
             |  INDICATOR CALC   |  (Fractals, ATR, Structure)
             +---------+---------+
                       |
        +--------------+--------------+
        |              |              |
   +----v----+   +----v----+   +----v----+
   |LIQUIDITY|   |  SWEEP  |   |CONFIRM  |
   | MODULE  |-->| MODULE  |-->| MODULE  |
   +----+----+   +----+----+   +----+----+
        |              |              |
        +--------------+--------------+
                       |
             +---------v---------+
             |   SIGNAL ENGINE   |  (R:R, Filters, Corr)
             +---------+---------+
                       |
EXECUTION LAYER        |
             +---------v---------+
             |   RISK MANAGER    |  (2% rule, Sizing)
             +---------+---------+
                       |
             +---------v---------+
             |  ORDER EXECUTOR   |  (Market/Limit, SL/TP)
             +-------------------+
```

### 2.2 Технологический стек

| Компонент | Рекомендация |
|-----------|-------------|
| Язык | Python 3.11+ (asyncio) |
| WebSocket Client | websockets |
| Хранение баров | collections.deque (in-memory) |
| Индикаторы | Кастомные функции (скорость > точность) |
| API биржи | REST для ордеров, WebSocket для данных |
| Логирование | structlog + файлы + Telegram |

### 2.3 Требования к задержкам

| Метрика | Требование |
|---------|-----------|
| Задержка WebSocket (ping) | < 100 мс |
| Время обработки закрытия бара | < 50 мс |
| Время от сигнала до отправки ордера | < 200 мс |
| Максимально допустимая задержка данных | 500 мс (иначе — пауза) |

---

## 3. МНОГО TIMEFRAME АНАЛИЗ (MTF)

### 3.1 Иерархия таймфреймов

| TF | Назначение | Обновление |
|----|-----------|------------|
| 4H | Глобальный контекст, HTF пулы ликвидности (цели), bias | Каждые 4 часа |
| 1H | Промежуточный контекст, Order Blocks, структура | Каждый час |
| 15m | Детекция манипуляции (sweep) | Каждые 15 минут |
| 5m | Подтверждение разворота, точечный вход, управление | Каждые 5 минут |

### 3.2 Правило согласования (Alignment Rule)

Сделка открывается **только если** направление сигнала на младшем ТФ совпадает с bias старшего ТФ:

**LONG разрешен, если:**
- 4H: цена выше последнего значимого Swing Low (бычий контекст)
- 1H: структура восходящая (higher highs, higher lows)

**SHORT разрешен, если:**
- 4H: цена ниже последнего значимого Swing High (медвежий контекст)
- 1H: структура нисходящая (lower highs, lower lows)

> **Важно:** Если 4H и 1H дают противоречивый сигнал — бот **не торгует** данный актив, даже если на 5m появился идеальный сетап.

---

## 4. МОДУЛЬ 1: ДЕТЕКЦИЯ ПУЛОВ ЛИКВИДНОСТИ (LIQUIDITY POOLS)

### 4.1 Определение фрактала (Fractal / Swing Point)

#### 4.1.1 Математика бычьего фрактала (Swing Low)

Свеча с индексом `i` является **Swing Low**, если:

```
Low[i-2] > Low[i]  AND
Low[i-1] > Low[i]  AND
Low[i+1] > Low[i]  AND
Low[i+2] > Low[i]
```

> **Важно:** Фрактал подтверждается только на свече `i+2`. `confirmed_at = timestamp[i+2]`, а не `timestamp[i]`. Это предотвращает look-ahead bias.

#### 4.1.2 Математика медвежьего фрактала (Swing High)

Свеча с индексом `i` является **Swing High**, если:

```
High[i-2] < High[i]  AND
High[i-1] < High[i]  AND
High[i+1] < High[i]  AND
High[i+2] < High[i]
```

### 4.2 Equal Highs / Equal Lows (EQH / EQL)

Пулы ликвидности формируются, когда несколько фракталов находятся на **близких уровнях**.

#### 4.2.1 Алгоритм кластеризации фракталов

```python
FRACTAL_TOLERANCE_PERCENT = 0.15  # 0.15% от цены
MIN_FRACTALS_IN_POOL = 2

clusters = []
for fractal in swing_highs:  # или swing_lows
    placed = False
    for cluster in clusters:
        if abs(fractal.price - cluster.level) / cluster.level <= FRACTAL_TOLERANCE_PERCENT:
            cluster.fractals.append(fractal)
            cluster.level = median([f.price for f in cluster.fractals])
            placed = True
            break
    if not placed:
        clusters.append(Cluster(level=fractal.price, fractals=[fractal]))

liquidity_pools = [c for c in clusters if len(c.fractals) >= MIN_FRACTALS_IN_POOL]
```

#### 4.2.2 Типы пулов ликвидности

| Тип | Обозначение | Направление цели | Торговая интерпретация |
|-----|------------|------------------|----------------------|
| Equal Highs | EQH | Вверх | Ликвидность для шорта (стопы лонгистов) |
| Equal Lows | EQL | Вниз | Ликвидность для лонга (стопы шортистов) |
| Single Swing High | SSH | Вверх | Слабый пул, но валиден |
| Single Swing Low | SSL | Вниз | Слабый пул, но валиден |

### 4.3 Состояния пула ликвидности (Lifecycle)

```
[UNTESTED] --(цена приближается)--> [APPROACHING]
                                          |
                                          v
                              +-----------------------+
                              |    SWEEP DETECTED     |
                              |  (тень пробила уровень|
                              |   тело закрылось внутри)|
                              +-----------+-----------+
                                          |
                    +---------------------+---------------------+
                    |                     |                     |
                    v                     v                     v
              [CONFIRMED]           [BREACHED]            [EXPIRED]
           (разворот начался)   (тело закрылось за     (timeout без
                                    уровнем)             подтверждения)
                    |                                         |
                    v                                         v
              [TRADED]                                  [ARCHIVED]
           (позиция открыта)
                    |
                    v
              [LIQUIDATED]
           (SL/TP/TimeStop)
```

### 4.4 Хранение пулов ликвидности

```yaml
liquidity_pool:
  id: "uuid"
  symbol: "BTCUSDT"
  timeframe: "4H"              # Источник пула (HTF)
  type: "EQH" | "EQL" | "SSH" | "SSL"
  level: 67342.50
  fractals:
    - { index: 45, confirmed_at: 1234567890, price: 67340.00 }
    - { index: 78, confirmed_at: 1234568120, price: 67345.00 }
  status: "UNTESTED" | "APPROACHING" | "SWEEPED" | "CONFIRMED" | "TRADED" | "BREACHED" | "EXPIRED" | "LIQUIDATED"
  created_at: timestamp
  confirmed_at: timestamp       # Время подтверждения фрактала (i+2)
  tested_at: timestamp | null
  breached_at: timestamp | null
  traded_at: timestamp | null
  expired_at: timestamp | null
  liquidated_at: timestamp | null
```

---

## 5. МОДУЛЬ 2: ДЕТЕКЦИЯ МАНИПУЛЯЦИИ (LIQUIDITY SWEEP)

### 5.1 Определение Sweep

**Liquidity Sweep** — манипуляция, при которой цена **тенью (wick)** пробивает уровень пула ликвидности, но **тело свечи закрывается внутри** предыдущего диапазона. Указывает на отсутствие истинного пробоя и потенциальный разворот.

### 5.2 Математические условия

#### 5.2.1 Медвежий Sweep (Sweep of High — для входа в ШОРТ)

```
БАЗОВЫЕ (обязательные):
1. High[current] > Liquidity_Pool_Level
2. Close[current] < Liquidity_Pool_Level
3. Open[current] < Liquidity_Pool_Level
   ИЛИ
   Open[current] > Liquidity_Pool_Level И Close[current] < Open[current]

УСИЛЕНИЯ (опциональные, добавляют confidence):
4. Объем[current] > 1.5 * SMA(Volume, 20)
5. Длина тени верхней > 2 * длина тела
```

> **Важно:** Пункты 4-5 — НЕ обязательные. Они увеличивают confidence sweep, но не блокируют его.

#### 5.2.2 Бычий Sweep (Sweep of Low — для входа в ЛОНГ)

```
БАЗОВЫЕ (обязательные):
1. Low[current] < Liquidity_Pool_Level
2. Close[current] > Liquidity_Pool_Level
3. Open[current] > Liquidity_Pool_Level
   ИЛИ
   Open[current] < Liquidity_Pool_Level И Close[current] > Open[current]

УСИЛЕНИЯ (опциональные):
4. Объем[current] > 1.5 * SMA(Volume, 20)
5. Длина тени нижней > 2 * длина тела
```

### 5.3 Фильтры ложного Sweep

```python
FALSE_SWEEP_FILTERS = {
    "max_body_beyond_level": 0.3,   # % от ATR(14)
    "min_wick_beyond_level": 0.1,   # % от цены
    "min_body_size": 0.05,          # % от цены
    "max_pool_age_bars": 100,
}
```

### 5.4 Хранение активного Sweep

```yaml
sweep_event:
  id: "setup_uuid"
  symbol: "BTCUSDT"
  pool_id: "pool_uuid"              # Ссылка на пул ликвидности
  direction: "LONG" | "SHORT"
  sweep_candle:                     # Свеча, на которой произошел sweep
    timestamp: 1234567890
    open: 67490.00
    high: 67620.00
    low: 67480.00
    close: 67550.00
    volume: 150.5
  sweep_level: 67500.00             # Уровень пула
  sweep_extreme: 67620.00           # High (для шорта) или Low (для лонга)
  detected_at: timestamp
  status: "ACTIVE" | "CONFIRMED" | "EXPIRED" | "BREACHED" | "TRADED"
  confirmation_window_start: timestamp
  confirmation_window_end: timestamp   # detected_at + timeout
```

### 5.5 Таймаут и инвалидация Sweep

```yaml
sweep_timeout:
  enabled: true
  timeout_bars_5m: 5                # 25 минут
  timeout_bars_15m: 3               # 45 минут

  actions:
    on_timeout: "EXPIRE"
    on_breach: "BREACH"

  breach_conditions:
    long: "Close[current] < Sweep_Low_Level"
    short: "Close[current] > Sweep_High_Level"
```

---

## 6. МОДУЛЬ 3: ПОДТВЕРЖДЕНИЕ РАЗВОРОТА (ENTRY CONFIRMATION)

### 6.0 Принципиальное правило: Temporal Binding

**Каждый признак подтверждения должен быть сформирован СТРОГО ПОСЛЕ момента Sweep.**

```
t(FVG) > t(SWEEP)
t(OB) >= t(SWEEP)
t(BOS) > t(SWEEP)
t(ENTRY) > t(CONFIRMATION)
```

> **Критично:** Бот должен передавать `sweep.detected_at` в `find_confirmation()` и отбрасывать любые FVG/OB/BOS, сформированные до этого времени.

### 6.1 Признак 1: Fair Value Gap (FVG) / Imbalance

#### 6.1.1 Определение

FVG — неэффективность (пропуск) между телами свечей, возникающая при сильном импульсе.

#### 6.1.2 Математика бычьего FVG (для лонга после sweep low)

```
Условия:
1. Свеча[i] — бычья (Close[i] > Open[i])
2. Свеча[i+1] — импульсная (может быть любого цвета, но движение вниз)
3. Свеча[i+2] — бычья (Close[i+2] > Open[i+2])
4. Low[i+2] > High[i]                              (пропуск вверх)

FVG Zone:
  top = Low[i+2]
  bottom = High[i]
  height = top - bottom
```

#### 6.1.3 Математика медвежьего FVG (для шорта после sweep high)

```
Условия:
1. Свеча[i] — медвежья (Close[i] < Open[i])
2. Свеча[i+1] — импульсная (движение вверх)
3. Свеча[i+2] — медвежья (Close[i+2] < Open[i+2])
4. High[i+2] < Low[i]                              (пропуск вниз)

FVG Zone:
  top = Low[i]
  bottom = High[i+2]
  height = top - bottom
```

#### 6.1.4 Параметры FVG

```yaml
fvg_params:
  min_height_percent: 0.05
  max_age_bars: 20
  fill_threshold: 0.7
  temporal_binding: true            # FVG.timestamp > sweep.detected_at
```

### 6.2 Признак 2: Order Block (OB)

#### 6.2.1 Определение

Order Block — последняя свеча **противоположного цвета** перед импульсным движением, которое сняло ликвидность.

#### 6.2.2 Алгоритм поиска OB (для лонга)

```python
def find_bullish_order_block(bars, sweep_index, sweep_timestamp):
    # Ищем последнюю медвежью свечу ПЕРЕД импульсом к sweep
    for i in range(sweep_index - 1, max(0, sweep_index - 20), -1):
        if bars[i].timestamp < sweep_timestamp:
            break  # Temporal binding: OB не может быть до sweep
        if is_bearish(bars[i]):
            if bars[i+1].low < bars[i].low * 0.998:
                return {
                    "type": "BULLISH_OB",
                    "top": bars[i].open,
                    "bottom": bars[i].close,
                    "index": i,
                    "timestamp": bars[i].timestamp
                }
    return None
```

#### 6.2.3 Алгоритм поиска OB (для шорта)

```python
def find_bearish_order_block(bars, sweep_index, sweep_timestamp):
    for i in range(sweep_index - 1, max(0, sweep_index - 20), -1):
        if bars[i].timestamp < sweep_timestamp:
            break
        if is_bullish(bars[i]):
            if bars[i+1].high > bars[i].high * 1.002:
                return {
                    "type": "BEARISH_OB",
                    "top": bars[i].close,
                    "bottom": bars[i].open,
                    "index": i,
                    "timestamp": bars[i].timestamp
                }
    return None
```

### 6.3 Признак 3: Break of Structure (BOS) / Change of Character (CHoCH)

#### 6.3.1 Определение

BOS — **слом структуры**: цена пробивает последний значимый максимум (для лонга) или минимум (для шорта) **после sweep**, подтверждая смену направления.

#### 6.3.2 Математика бычьего BOS

```
Условия:
1. Был Sweep Low
2. После sweep (t > t_sweep) цена формирует новый локальный максимум
3. Этот максимум выше ПОСЛЕДНЕГО значимого Swing High ПЕРЕД sweep

Close[current] > Last_Swing_High_Before_Sweep AND
Low[sweep_candle] < Last_Swing_Low_Before_Sweep
```

#### 6.3.3 Математика медвежьего BOS

```
Условия:
1. Был Sweep High
2. После sweep (t > t_sweep) цена формирует новый локальный минимум
3. Этот минимум ниже ПОСЛЕДНЕГО значимого Swing Low ПЕРЕД sweep

Close[current] < Last_Swing_Low_Before_Sweep AND
High[sweep_candle] > Last_Swing_High_Before_Sweep
```

> **Важно:** BOS ссылается на **конкретный** swing, зафиксированный перед sweep, а не на абстрактный "previous".

### 6.4 Взвешенная система подтверждения

Признаки имеют **разный вес**, отражающий их значимость для разворота:

| Признак | Вес | Почему |
|---------|-----|--------|
| BOS / CHoCH | **2** | Прямое подтверждение смены структуры |
| FVG | **1** | Зона интереса, но не гарантия разворота |
| OB | **1** | Зона интереса, но может быть пробит |

#### 6.4.1 Матрица подтверждений

| BOS | FVG | OB | Сумма | Решение | Размер позиции |
|-----|-----|-----|-------|---------|---------------|
| Да | Да | Да | 4 | **STRONG** | Full (2%) |
| Да | Да | Нет | 3 | **STRONG** | Full (2%) |
| Да | Нет | Да | 3 | **STRONG** | Full (2%) |
| Нет | Да | Да | 2 | **MODERATE** | Reduced (1%) |
| Да | Нет | Нет | 2 | **MODERATE** | Reduced (1%) |
| Нет | Да | Нет | 1 | WEAK | Пропуск |
| Нет | Нет | Да | 1 | WEAK | Пропуск |
| Нет | Нет | Нет | 0 | NO SIGNAL | Пропуск |

> **Минимальный порог для входа: 2 балла.**

---

## 7. МОДУЛЬ 4: ЛОГИКА ВХОДА В СДЕЛКУ (EXECUTION)

### 7.1 Триггер входа

Вход активируется при:
1. Детектирован Sweep на 15m
2. Получено подтверждение (минимум 2 балла, см. 6.4)
3. Все признаки подтверждения строго после Sweep (temporal binding)
4. MTF alignment (4H и 1H поддерживают направление)
5. R:R >= 1:2.5 (рассчитан ДО входа)
6. Risk Manager разрешил открытие

### 7.2 Типы входа

#### 7.2.1 Market Entry

```yaml
market_entry:
  trigger: "Закрытие подтверждающего бара"
  execution: "MARKET ордер"
  expected_entry: "candle.close"
  slippage_max: 0.1%
  post_fill_action: "recalculate_actual_rr"
```

#### 7.2.2 Limit Entry

```yaml
limit_entry:
  trigger: "Формирование FVG или OB после подтверждения"
  execution: "LIMIT ордер на границу зоны"
  price_formula:
    long: "min(FVG_bottom, OB_bottom) * 0.9995"
    short: "max(FVG_top, OB_top) * 1.0005"
  time_in_force: "GTC"
  max_wait_bars: 3
  cancellation_trigger: "Цена закрылась за пределами OB/FVG в неблагоприятную сторону"
```

### 7.3 Pre-Entry Checks

```python
pre_entry_checks = {
    "spread_check": spread < 0.15%,
    "depth_check": volume_within_0.5% > min_required_usdt,
    "trading_hours": current_time in allowed_windows,
    "max_positions_same_direction": 3,
    "no_correlated_entry": not has_open_position(correlated_symbol),
    "no_recent_loss": consecutive_losses < 3,
    "pool_not_breached": pool_status != "BREACHED",
    "risk_reward_ratio": calculated_rr >= 2.5,
    "temporal_binding": all(c.timestamp > sweep.detected_at for c in confirmations),
}
```

### 7.4 Расчет R:R и пересчет после исполнения

```python
def calculate_expected_rr(entry_price, stop_loss, take_profit, direction):
    if direction == "LONG":
        risk = entry_price - stop_loss
        reward = take_profit - entry_price
    else:
        risk = stop_loss - entry_price
        reward = entry_price - take_profit
    return reward / risk if risk > 0 else 0

def recalculate_actual_rr(actual_entry, stop_loss, take_profit, direction):
    # Вызывается ПОСЛЕ получения actual_fill_price от биржи
    actual_rr = calculate_expected_rr(actual_entry, stop_loss, take_profit, direction)

    if actual_rr < MIN_RR:
        logger.warning(f"Actual RR {actual_rr} below minimum {MIN_RR}")

    return actual_rr
```

> **Правило:** Если `expected_rr < 2.5` — сделка **пропускается** ДО отправки ордера.

---

## 8. МОДУЛЬ 5: УПРАВЛЕНИЕ ПОЗИЦИЕЙ (TRADE MANAGEMENT)

### 8.1 Stop-Loss (SL)

#### 8.1.1 Размещение SL

```yaml
stop_loss:
  base_placement:
    long: "Ниже экстремума Sweep Low"
    short: "Выше экстремума Sweep High"

  buffer:
    method: "ATR"
    atr_multiplier: 1.0
    atr_timeframe: "5m"
    atr_period: 14

  final_price:
    long: "sweep_low - (ATR(14, 5m) * 1.0)"
    short: "sweep_high + (ATR(14, 5m) * 1.0)"
```

### 8.2 Take-Profit (TP) — ИСПРАВЛЕНО

#### 8.2.1 Правильное направление TP

| Направление | Таргет | Объяснение |
|------------|--------|-----------|
| **LONG** | Ближайший **EQH / SSH** выше Entry | Цена идет к ликвидности сверху |
| **SHORT** | Ближайший **EQL / SSL** ниже Entry | Цена идет к ликвидности снизу |

```python
def calculate_tp(symbol, direction, entry):
    pools = self.liquidity_pools.get(symbol, [])

    if direction == "LONG":
        targets = [p.level for p in pools 
                  if p.type in ["EQH", "SSH"] and p.level > entry and p.status == "UNTESTED"]
    else:
        targets = [p.level for p in pools 
                  if p.type in ["EQL", "SSL"] and p.level < entry and p.status == "UNTESTED"]

    return min(targets, key=lambda x: abs(x - entry)) if targets else None
```

### 8.3 Partial Close (Scale-Out) — ИСПРАВЛЕНО

```yaml
partial_closes:
  enabled: true
  targets:
    - name: "TP1"
      rr_level: 2.0
      close_percent: 25
      action: "move_sl_to_breakeven"
    - name: "TP2"
      rr_level: 3.0
      close_percent: 35
      action: "activate_trailing"
    - name: "TP3"
      rr_level: 4.0
      close_percent: 40
      action: "close_remaining"
```

```python
async def check_partial_closes(position, candle):
    for target in PARTIAL_CLOSES:
        if target["rr_level"] in position.completed_targets:
            continue

        target_price = position.entry + (position.entry - position.sl) * target["rr_level"] \
                       if position.direction == "LONG" \
                       else position.entry - (position.sl - position.entry) * target["rr_level"]

        if position.direction == "LONG" and candle.high >= target_price:
            await partial_close(position, target["close_percent"])
            position.completed_targets.add(target["rr_level"])

            if target["action"] == "move_sl_to_breakeven":
                position.sl = calculate_breakeven(position)
            elif target["action"] == "activate_trailing":
                position.trailing_active = True
            elif target["action"] == "close_remaining":
                await close_remaining(position)
                return

        elif position.direction == "SHORT" and candle.low <= target_price:
            await partial_close(position, target["close_percent"])
            position.completed_targets.add(target["rr_level"])

            if target["action"] == "move_sl_to_breakeven":
                position.sl = calculate_breakeven(position)
            elif target["action"] == "activate_trailing":
                position.trailing_active = True
            elif target["action"] == "close_remaining":
                await close_remaining(position)
                return
```

### 8.4 Breakeven — ИСПРАВЛЕНО

```yaml
breakeven:
  trigger: "Цена достигла 1.5 R/R"

  long:
    new_sl: "entry_price + fee_buffer"
    fee_buffer: "рассчитывается от entry_fee + exit_fee + slippage_estimate"

  short:
    new_sl: "entry_price - fee_buffer"
    fee_buffer: "рассчитывается от entry_fee + exit_fee + slippage_estimate"
```

```python
def calculate_breakeven(position):
    fee_buffer = position.entry * 0.0005  # ~0.05% для покрытия комиссий
    if position.direction == "LONG":
        return position.entry + fee_buffer
    else:
        return position.entry - fee_buffer
```

### 8.5 Trailing Stop

```yaml
trailing_stop:
  activation: "После достижения TP2 (3.0 R/R)"
  applies_to: "remaining_position_only"  # Только на оставшиеся 40%

  method: "ATR_BASED"
  atr_timeframe: "5m"
  atr_period: 14
  atr_multiplier: 1.5

  step: "Обновлять при закрытии каждого 5m бара"
  min_distance_from_entry: "0.5%"
```

### 8.6 Time Stop — ИСПРАВЛЕНО

```yaml
time_stop:
  enabled: true
  max_duration_minutes: 100   # 20 баров на 5m

  check_frequency: "Каждый закрытый бар 5m"
  action: "CLOSE_MARKET"
  condition: "Если ни SL, ни TP1 не достигнуты"
```

```python
def check_time_stop(position, current_time):
    elapsed = current_time - position.entry_time
    if elapsed > TIME_STOP_MAX_MINUTES:
        return True
    return False
```

---

## 9. МОДУЛЬ 6: РИСК-МЕНЕДЖМЕНТ (POSITION SIZING)

### 9.1 Фиксированный риск

Автор видео использует **2% риска на сделку** от общего капитала.

### 9.2 Формула расчета позиции

```python
def calculate_position_size(
    capital_usdt: float,
    risk_percent: float,
    entry_price: float,
    stop_loss_price: float,
    direction: str
) -> dict:
    risk_amount = capital_usdt * (risk_percent / 100)

    if direction == "LONG":
        price_distance = entry_price - stop_loss_price
    else:
        price_distance = stop_loss_price - entry_price

    if price_distance <= 0:
        raise ValueError("Invalid SL placement")

    position_size_usdt = risk_amount / (price_distance / entry_price)
    quantity = position_size_usdt / entry_price

    min_notional = 5.0
    if position_size_usdt < min_notional:
        return {"valid": False, "reason": "Position below minimum"}

    return {
        "valid": True,
        "risk_amount_usdt": round(risk_amount, 2),
        "position_size_usdt": round(position_size_usdt, 2),
        "quantity": round(quantity, 6),
        "risk_percent": risk_percent,
    }
```

### 9.3 Дневные лимиты — ИСПРАВЛЕНО

```yaml
daily_limits:
  max_risk_per_day_percent: 6.0
  max_trades_per_day: 5
  max_consecutive_losses: 3
  max_drawdown_percent: 10
  profit_target_daily_percent: 10
```

```python
def can_open_trade():
    remaining_risk = DAILY_MAX_RISK - daily_risk_used

    if remaining_risk <= 0:
        return False, "Daily risk limit reached"

    # Размер риска новой сделки = min(2%, remaining_risk)
    actual_risk = min(RISK_PER_TRADE, remaining_risk)

    if daily_trades_count >= MAX_TRADES_PER_DAY:
        return False, "Daily trades limit reached"

    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        return False, "Consecutive losses limit reached"

    return True, actual_risk
```

> **Пример:**
> - Сделка 1: 2% риска -> remaining = 4%
> - Сделка 2: 2% риска -> remaining = 2%
> - Сделка 3: 2% риска -> remaining = 0%
> - Сделка 4: BLOCKED (независимо от max_trades_per_day)

### 9.4 Корреляционные ограничения

```yaml
correlation_limits:
  max_positions_total: 5
  max_long_positions: 3
  max_short_positions: 3
```

---

## 10. МОДУЛЬ 7: ИНВАЛИДАЦИЯ И FLIP BIAS (INVALIDATION)

### 10.1 Инвалидация Order Block

```yaml
ob_invalidation:
  condition: "Цена полнотелой свечой закрылась за пределами OB"

  bullish_ob_invalidated:
    "Close[current] < OB_bottom AND body_size > 50% of range"

  bearish_ob_invalidated:
    "Close[current] > OB_top AND body_size > 50% of range"

  action:
    - "Удалить OB из активных зон"
    - "Flip bias в противоположную сторону"
    - "Начать поиск сетапа в новом направлении"
```

### 10.2 Инвалидация Sweep

```yaml
sweep_invalidation:
  breach_condition:
    long: "Close[current] < Sweep_Low_Level"
    short: "Close[current] > Sweep_High_Level"

  timeout_condition:
    "current_time > sweep.confirmation_window_end"

  action:
    - "Пометить пул как BREACHED / EXPIRED"
    - "Отменить ожидающий сигнал"
    - "Удалить active_sweep"
```

### 10.3 Flip Bias

```python
class MarketBias:
    BULLISH = 1
    BEARISH = -1
    NEUTRAL = 0

def update_bias(symbol, event):
    if event.type == "BULLISH_BOS_INVALIDATED":
        state.bias[symbol] = MarketBias.BEARISH if confirm_bearish() else MarketBias.NEUTRAL
    elif event.type == "BEARISH_BOS_INVALIDATED":
        state.bias[symbol] = MarketBias.BULLISH if confirm_bullish() else MarketBias.NEUTRAL

    clear_pending_signals_against_bias(symbol, state.bias[symbol])
```

---

## 11. МОДУЛЬ 8: ФИЛЬТРЫ И КОНТЕКСТ (MARKET REGIME)

### 11.1 Трендовый фильтр (HTF Bias)

```python
def calculate_htf_bias(bars_4h, bars_1h):
    # 4H: Структура + EMA как фильтр
    ema_50_4h = calculate_ema(bars_4h, 50)
    ema_200_4h = calculate_ema(bars_4h, 200)

    # 1H: Структура
    recent_swings_1h = detect_swing_points(bars_1h)
    structure_1h = analyze_structure(recent_swings_1h)

    if ema_50_4h > ema_200_4h and structure_1h == "UPTREND":
        return "BULLISH"
    elif ema_50_4h < ema_200_4h and structure_1h == "DOWNTREND":
        return "BEARISH"
    else:
        return "NEUTRAL"
```

### 11.2 Волатильностный фильтр

```yaml
volatility_filter:
  atr_timeframe: "1H"
  atr_period: 14
  conditions:
    min_atr_percent: 0.3
    max_atr_percent: 5.0
  action: "SKIP_ALL_SIGNALS"
```

### 11.3 Фильтр спреда и проскальзывания

```yaml
execution_filter:
  max_spread_percent: 0.15
  max_slippage_percent: 0.1
  min_depth_0.5_percent: 10000
  action_if_failed: "POSTPONE_ENTRY"
  postpone_max_bars: 2
```

---

## 12. STATE MACHINE (ПОЛНЫЙ ЖИЗНЕННЫЙ ЦИКЛ СДЕЛКИ)

```
                    +-----------------+
                    |   NO SETUP      |
                    +--------+--------+
                             |
                       liquidity found
                             |
                             v
                    +-----------------+
                    | LIQUIDITY ARMED |
                    +--------+--------+
                             |
                         sweep occurs
                             |
                             v
                    +-----------------+
                    | SWEEP DETECTED  |
                    | (15m candle)    |
                    +--------+--------+
                             |
                +------------+------------+
                |                         |
             invalid                   valid
                |                         |
                v                         v
             EXPIRED              WAIT_CONFIRMATION
           (timeout)            (5m confirmation window)
                |                         |
                |            +------------+------------+
                |            |            |            |
                |          BOS          FVG          OB
                |            |            |            |
                |            +------------+------------+
                |                         |
                |                         v
                |                CONFIRMATION_VALID
                |                         |
                |                 MTF ALIGNMENT
                |                         |
                |                     RR CHECK
                |                         |
                |                    RISK CHECK
                |                         |
                |                         v
                |                  ENTRY_PENDING
                |                         |
                |            +------------+------------+
                |            |                         |
                |          MARKET                    LIMIT
                |            |                         |
                |            v                         v
                |         FILLED                    PENDING
                |            |                         |
                |            +------------+------------+
                |                         |
                |                         v
                |                      POSITION
                |                         |
                |        +----------------+----------------+
                |        |                |                |
                |        v                v                v
                |       SL               TP1              TIME
                |        |                |                |
                |        v                v                v
                |   STOP_LOSS      PARTIAL_CLOSE      TIME_STOP
                |        |                |                |
                |        |                v                |
                |        |           TP2 (trail)           |
                |        |                |                |
                |        |           TP3 (close)           |
                |        |                |                |
                |        +----------------+----------------+
                |                         |
                |                         v
                |                      CLOSED
                |                         |
                +-------------------------+
                                          |
                                          v
                                      ARCHIVED
```

### 12.1 Переходы состояний

| From | To | Trigger | Action |
|------|-----|---------|--------|
| NO SETUP | LIQUIDITY ARMED | Фрактал найден и кластеризован | Сохранить пул |
| LIQUIDITY ARMED | SWEEP DETECTED | 15m свеча sweep | Создать sweep_event, запустить таймер |
| SWEEP DETECTED | EXPIRED | Таймаут (25 мин) | Пометить пул EXPIRED, удалить sweep |
| SWEEP DETECTED | WAIT_CONFIRMATION | Sweep валиден | Начать поиск confirmation на 5m |
| WAIT_CONFIRMATION | CONFIRMATION_VALID | >= 2 балла | Проверить MTF, R:R, Risk |
| CONFIRMATION_VALID | ENTRY_PENDING | Все checks пройдены | Подготовить ордер |
| ENTRY_PENDING | POSITION | Ордер исполнен | Установить SL/TP, начать управление |
| POSITION | STOP_LOSS | Цена достигла SL | Закрыть позицию, зафиксировать убыток |
| POSITION | PARTIAL_CLOSE | Цена достигла TP1 (2R) | Закрыть 25%, перенести SL в BE |
| POSITION | TP2 | Цена достигла 3R | Закрыть 35%, активировать trailing |
| POSITION | TP3 | Цена достигла 4R / liquidity pool | Закрыть оставшиеся 40% |
| POSITION | TIME_STOP | 100 минут без SL/TP1 | Закрыть по рынку |

---

## 13. ПСЕВДОКОД ЯДРА БОТА

### 13.1 Главный цикл обработки WebSocket

```python
import asyncio
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict
import json
import time

class Direction(Enum):
    LONG = 1
    SHORT = -1
    NEUTRAL = 0

class PoolStatus(Enum):
    UNTESTED = "UNTESTED"
    APPROACHING = "APPROACHING"
    SWEEPED = "SWEEPED"
    CONFIRMED = "CONFIRMED"
    TRADED = "TRADED"
    BREACHED = "BREACHED"
    EXPIRED = "EXPIRED"
    LIQUIDATED = "LIQUIDATED"

class SweepStatus(Enum):
    ACTIVE = "ACTIVE"
    CONFIRMED = "CONFIRMED"
    EXPIRED = "EXPIRED"
    BREACHED = "BREACHED"
    TRADED = "TRADED"

@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str

@dataclass
class LiquidityPool:
    id: str
    symbol: str
    timeframe: str
    type: str
    level: float
    fractals: List[dict]
    status: PoolStatus = PoolStatus.UNTESTED
    confirmed_at: Optional[int] = None
    tested_at: Optional[int] = None
    breached_at: Optional[int] = None
    traded_at: Optional[int] = None

@dataclass
class SweepEvent:
    id: str
    symbol: str
    pool_id: str
    direction: Direction
    sweep_candle: Candle
    sweep_level: float
    sweep_extreme: float
    detected_at: int
    status: SweepStatus = SweepStatus.ACTIVE
    confirmation_window_end: int = 0

@dataclass
class Position:
    id: str
    symbol: str
    direction: Direction
    entry_price: float
    actual_entry: float
    stop_loss: float
    take_profit: float
    quantity: float
    size_usdt: float
    entry_time: int
    breakeven_moved: bool = False
    trailing_active: bool = False
    completed_targets: set = field(default_factory=set)
    remaining_percent: float = 100.0

class SMCTradingBot:
    def __init__(self, symbols, exchange_ws_url):
        self.symbols = symbols
        self.exchange_ws_url = exchange_ws_url

        self.bars = {
            sym: {
                "4H": deque(maxlen=300),
                "1H": deque(maxlen=300),
                "15m": deque(maxlen=300),
                "5m": deque(maxlen=300),
            }
            for sym in symbols
        }

        self.liquidity_pools: Dict[str, List[LiquidityPool]] = {sym: [] for sym in symbols}
        self.active_sweeps: Dict[str, Optional[SweepEvent]] = {sym: None for sym in symbols}
        self.positions: Dict[str, Optional[Position]] = {sym: None for sym in symbols}
        self.market_bias = {sym: Direction.NEUTRAL for sym in symbols}

        self.risk_per_trade = 0.02
        self.min_rr = 2.5
        self.fractal_lookaround = 2
        self.sweep_timeout_5m_bars = 5

    async def run(self):
        while True:
            try:
                async with websockets.connect(self.exchange_ws_url) as ws:
                    await self.subscribe(ws)
                    async for message in ws:
                        await self.process_message(json.loads(message))
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                await asyncio.sleep(5)

    async def process_message(self, msg):
        if msg.get("e") == "kline":
            await self.process_kline(msg)

    async def process_kline(self, msg):
        symbol = msg["s"]
        tf = msg["k"]["i"]

        if not msg["k"]["x"]:
            return

        candle = Candle(
            timestamp=msg["k"]["t"] // 1000,
            open=float(msg["k"]["o"]),
            high=float(msg["k"]["h"]),
            low=float(msg["k"]["l"]),
            close=float(msg["k"]["c"]),
            volume=float(msg["k"]["v"]),
            timeframe=tf
        )

        self.bars[symbol][tf].append(candle)

        if tf == "4H":
            await self.process_4h(symbol, candle)
        elif tf == "1H":
            await self.process_1h(symbol, candle)
        elif tf == "15m":
            await self.process_15m(symbol, candle)
        elif tf == "5m":
            await self.process_5m(symbol, candle)

    async def process_4h(self, symbol, candle):
        bars = list(self.bars[symbol]["4H"])
        new_fractals = self.detect_fractals(bars)
        self.update_liquidity_pools(symbol, new_fractals)
        self.market_bias[symbol] = self.calculate_htf_bias(symbol)

    async def process_1h(self, symbol, candle):
        bars = list(self.bars[symbol]["1H"])
        self.update_market_structure(symbol, bars)
        self.update_order_blocks(symbol, bars)

    async def process_15m(self, symbol, candle):
        if self.positions.get(symbol):
            return

        # Проверить таймаут существующего sweep
        if self.active_sweeps.get(symbol):
            if self.is_sweep_expired(self.active_sweeps[symbol], candle.timestamp):
                self.active_sweeps[symbol].status = SweepStatus.EXPIRED
                self.active_sweeps[symbol] = None
                return

        bars = list(self.bars[symbol]["15m"])
        for pool in self.liquidity_pools.get(symbol, []):
            if pool.status not in [PoolStatus.UNTESTED, PoolStatus.APPROACHING]:
                continue

            sweep = self.detect_sweep(candle, pool, bars)
            if sweep:
                pool.status = PoolStatus.SWEEPED
                pool.tested_at = candle.timestamp
                self.active_sweeps[symbol] = sweep
                logger.info(f"Sweep detected: {symbol} {pool.type} at {pool.level}")
                break

    async def process_5m(self, symbol, candle):
        # 1. Управление открытой позицией
        if self.positions.get(symbol):
            await self.check_position_management(symbol, candle)
            return

        # 2. Проверка активного sweep
        sweep = self.active_sweeps.get(symbol)
        if not sweep:
            return

        # 3. Проверка таймаута
        if self.is_sweep_expired(sweep, candle.timestamp):
            sweep.status = SweepStatus.EXPIRED
            self.active_sweeps[symbol] = None
            return

        # 4. Проверка breach
        if self.is_sweep_breached(candle, sweep):
            sweep.status = SweepStatus.BREACHED
            self.active_sweeps[symbol] = None
            return

        # 5. Поиск подтверждения (только после sweep)
        bars = list(self.bars[symbol]["5m"])
        confirmation = self.find_confirmation(bars, sweep)

        if confirmation.score >= 2:
            # MTF alignment
            if not self.is_mtf_aligned(symbol, sweep.direction):
                return

            # Расчет уровней
            entry = candle.close
            sl = self.calculate_sl(sweep)
            tp = self.calculate_tp(symbol, sweep.direction, entry)

            # R:R check
            expected_rr = self.calculate_rr(entry, sl, tp, sweep.direction)
            if expected_rr < self.min_rr:
                logger.info(f"Signal skipped: expected R:R {expected_rr:.2f} < {self.min_rr}")
                return

            # Размер позиции (зависит от confirmation score)
            risk_percent = self.risk_per_trade if confirmation.score >= 3 else self.risk_per_trade * 0.5
            size = self.calculate_position_size(entry, sl, risk_percent)
            if not size["valid"]:
                return

            # Pre-entry checks
            if not await self.pre_entry_checks(symbol, entry, sl, tp, confirmation, sweep):
                return

            # Исполнение
            await self.execute_entry(symbol, sweep, entry, sl, tp, size, confirmation)
            self.active_sweeps[symbol] = None

    # ==================== МАТЕМАТИЧЕСКИЕ МЕТОДЫ ====================

    def detect_fractals(self, bars):
        fractals = []
        n = self.fractal_lookaround

        for i in range(n, len(bars) - n):
            is_high = all(bars[i].high >= bars[j].high 
                         for j in range(i-n, i+n+1) if j != i)
            if is_high and bars[i].high > bars[i-1].high and bars[i].high > bars[i+1].high:
                fractals.append({
                    "type": "HIGH", 
                    "price": bars[i].high, 
                    "index": i,
                    "confirmed_at": bars[i+n].timestamp
                })

            is_low = all(bars[i].low <= bars[j].low 
                        for j in range(i-n, i+n+1) if j != i)
            if is_low and bars[i].low < bars[i-1].low and bars[i].low < bars[i+1].low:
                fractals.append({
                    "type": "LOW", 
                    "price": bars[i].low, 
                    "index": i,
                    "confirmed_at": bars[i+n].timestamp
                })

        return fractals

    def detect_sweep(self, candle, pool, bars):
        # Базовые условия (без обязательного volume)
        if pool.type in ["EQH", "SSH"]:
            if candle.high > pool.level and candle.close < pool.level:
                return SweepEvent(
                    id=uuid(),
                    symbol=pool.symbol,
                    pool_id=pool.id,
                    direction=Direction.SHORT,
                    sweep_candle=candle,
                    sweep_level=pool.level,
                    sweep_extreme=candle.high,
                    detected_at=candle.timestamp,
                    confirmation_window_end=candle.timestamp + (self.sweep_timeout_5m_bars * 300)
                )
        elif pool.type in ["EQL", "SSL"]:
            if candle.low < pool.level and candle.close > pool.level:
                return SweepEvent(
                    id=uuid(),
                    symbol=pool.symbol,
                    pool_id=pool.id,
                    direction=Direction.LONG,
                    sweep_candle=candle,
                    sweep_level=pool.level,
                    sweep_extreme=candle.low,
                    detected_at=candle.timestamp,
                    confirmation_window_end=candle.timestamp + (self.sweep_timeout_5m_bars * 300)
                )
        return None

    def is_sweep_expired(self, sweep, current_timestamp):
        return current_timestamp > sweep.confirmation_window_end

    def is_sweep_breached(self, candle, sweep):
        if sweep.direction == Direction.LONG:
            return candle.close < sweep.sweep_level
        else:
            return candle.close > sweep.sweep_level

    def find_confirmation(self, bars, sweep):
        score = 0
        details = {}

        # Найти индекс sweep в 5m барах
        sweep_idx = None
        for i, b in enumerate(bars):
            if b.timestamp == sweep.sweep_candle.timestamp:
                sweep_idx = i
                break

        if sweep_idx is None:
            return Confirmation(score=0, details={})

        # 1. FVG (только после sweep)
        fvg = self.find_fvg_after_sweep(bars, sweep_idx, sweep.direction, sweep.detected_at)
        if fvg:
            score += 1
            details["fvg"] = fvg

        # 2. OB (только после sweep)
        ob = self.find_order_block_after_sweep(bars, sweep_idx, sweep.direction, sweep.detected_at)
        if ob:
            score += 1
            details["ob"] = ob

        # 3. BOS (только после sweep)
        bos = self.find_bos_after_sweep(bars, sweep_idx, sweep.direction, sweep.detected_at)
        if bos:
            score += 2
            details["bos"] = bos

        return Confirmation(score=score, details=details)

    def find_fvg_after_sweep(self, bars, sweep_idx, direction, sweep_timestamp):
        if len(bars) < sweep_idx + 5:
            return None

        for i in range(sweep_idx, len(bars) - 3):
            # Temporal binding
            if bars[i].timestamp <= sweep_timestamp:
                continue

            if direction == Direction.LONG:
                if (bars[i].close > bars[i].open and           # bullish
                    bars[i+2].close > bars[i+2].open and       # bullish
                    bars[i+2].low > bars[i].high):             # gap up
                    return {
                        "top": bars[i+2].low,
                        "bottom": bars[i].high,
                        "type": "BULLISH",
                        "timestamp": bars[i].timestamp
                    }
            else:
                if (bars[i].close < bars[i].open and           # bearish
                    bars[i+2].close < bars[i+2].open and       # bearish
                    bars[i+2].high < bars[i].low):             # gap down
                    return {
                        "top": bars[i].low,
                        "bottom": bars[i+2].high,
                        "type": "BEARISH",
                        "timestamp": bars[i].timestamp
                    }
        return None

    def find_order_block_after_sweep(self, bars, sweep_idx, direction, sweep_timestamp):
        for i in range(sweep_idx - 1, max(0, sweep_idx - 20), -1):
            if bars[i].timestamp < sweep_timestamp:
                break

            if direction == Direction.LONG:
                if bars[i].close < bars[i].open:  # bearish
                    if i + 1 < len(bars) and bars[i+1].low < bars[i].low * 0.998:
                        return {
                            "type": "BULLISH_OB",
                            "top": bars[i].open,
                            "bottom": bars[i].close,
                            "timestamp": bars[i].timestamp
                        }
            else:
                if bars[i].close > bars[i].open:  # bullish
                    if i + 1 < len(bars) and bars[i+1].high > bars[i].high * 1.002:
                        return {
                            "type": "BEARISH_OB",
                            "top": bars[i].close,
                            "bottom": bars[i].open,
                            "timestamp": bars[i].timestamp
                        }
        return None

    def find_bos_after_sweep(self, bars, sweep_idx, direction, sweep_timestamp):
        # Найти последний swing перед sweep
        last_swing = self.find_last_swing_before_sweep(bars, sweep_idx, direction)
        if not last_swing:
            return None

        for i in range(sweep_idx + 1, len(bars)):
            if bars[i].timestamp <= sweep_timestamp:
                continue

            if direction == Direction.LONG:
                if bars[i].close > last_swing["price"]:
                    return {
                        "type": "BULLISH_BOS",
                        "broken_level": last_swing["price"],
                        "timestamp": bars[i].timestamp
                    }
            else:
                if bars[i].close < last_swing["price"]:
                    return {
                        "type": "BEARISH_BOS",
                        "broken_level": last_swing["price"],
                        "timestamp": bars[i].timestamp
                    }
        return None

    def calculate_sl(self, sweep):
        atr = self.calculate_atr(self.bars[sweep.symbol]["5m"], 14)
        buffer = atr * 1.0

        if sweep.direction == Direction.LONG:
            return sweep.sweep_extreme - buffer
        else:
            return sweep.sweep_extreme + buffer

    def calculate_tp(self, symbol, direction, entry):
        pools = self.liquidity_pools.get(symbol, [])

        if direction == Direction.LONG:
            targets = [p.level for p in pools 
                      if p.type in ["EQH", "SSH"] and p.level > entry 
                      and p.status == PoolStatus.UNTESTED]
        else:
            targets = [p.level for p in pools 
                      if p.type in ["EQL", "SSL"] and p.level < entry 
                      and p.status == PoolStatus.UNTESTED]

        return min(targets, key=lambda x: abs(x - entry)) if targets else None

    def calculate_rr(self, entry, sl, tp, direction):
        if tp is None:
            return 0
        if direction == Direction.LONG:
            risk = entry - sl
            reward = tp - entry
        else:
            risk = sl - entry
            reward = entry - tp
        return reward / risk if risk > 0 else 0

    def calculate_position_size(self, entry, sl, risk_percent):
        capital = self.get_balance()
        risk_amount = capital * risk_percent
        price_distance = abs(entry - sl)
        if price_distance == 0:
            return {"valid": False}
        risk_ratio = price_distance / entry
        position_size = risk_amount / risk_ratio
        return {
            "valid": position_size >= 5,
            "position_size": position_size,
            "quantity": position_size / entry,
            "risk_amount": risk_amount
        }

    async def execute_entry(self, symbol, sweep, entry, sl, tp, size, confirmation):
        # Отправить MARKET ордер
        actual_fill = await self.place_market_order(symbol, sweep.direction, size["quantity"])

        if not actual_fill:
            return

        # Пересчитать R:R с actual fill
        actual_rr = self.calculate_rr(actual_fill, sl, tp, sweep.direction)

        # Создать позицию
        position = Position(
            id=uuid(),
            symbol=symbol,
            direction=sweep.direction,
            entry_price=entry,
            actual_entry=actual_fill,
            stop_loss=sl,
            take_profit=tp,
            quantity=size["quantity"],
            size_usdt=size["position_size"],
            entry_time=int(time.time())
        )

        self.positions[symbol] = position

        # Обновить пул
        for pool in self.liquidity_pools[symbol]:
            if pool.id == sweep.pool_id:
                pool.status = PoolStatus.TRADED
                pool.traded_at = int(time.time())

        logger.info(f"Position opened: {symbol} {sweep.direction.name} @ {actual_fill}")

    async def check_position_management(self, symbol, candle):
        pos = self.positions[symbol]
        if not pos:
            return

        # Stop-Loss
        if pos.direction == Direction.LONG and candle.low <= pos.stop_loss:
            await self.close_position(symbol, pos.stop_loss, "STOP_LOSS")
            return
        elif pos.direction == Direction.SHORT and candle.high >= pos.stop_loss:
            await self.close_position(symbol, pos.stop_loss, "STOP_LOSS")
            return

        # Partial closes / TP
        await self.check_partial_closes(pos, candle)

        if not self.positions.get(symbol):
            return  # Позиция закрыта partial closes

        # Breakeven
        current_rr = self.calculate_current_rr(pos, candle.close)
        if current_rr >= 1.5 and not pos.breakeven_moved:
            pos.stop_loss = self.calculate_breakeven(pos)
            pos.breakeven_moved = True
            logger.info(f"Breakeven moved for {symbol} to {pos.stop_loss}")

        # Trailing Stop
        if pos.trailing_active and current_rr >= 3.0:
            new_sl = self.calculate_trailing_stop(pos, candle)
            if (pos.direction == Direction.LONG and new_sl > pos.stop_loss) or \
               (pos.direction == Direction.SHORT and new_sl < pos.stop_loss):
                pos.stop_loss = new_sl
                await self.update_stop_loss(symbol, pos.stop_loss)

        # Time Stop
        elapsed = candle.timestamp - pos.entry_time
        if elapsed > 6000:  # 100 минут
            await self.close_position(symbol, candle.close, "TIME_STOP")

    def calculate_breakeven(self, position):
        fee_buffer = position.entry_price * 0.0005
        if position.direction == Direction.LONG:
            return position.entry_price + fee_buffer
        else:
            return position.entry_price - fee_buffer

    async def check_partial_closes(self, position, candle):
        targets = [
            {"rr": 2.0, "percent": 25, "action": "breakeven"},
            {"rr": 3.0, "percent": 35, "action": "trail"},
            {"rr": 4.0, "percent": 40, "action": "close"},
        ]

        for target in targets:
            if target["rr"] in position.completed_targets:
                continue

            if position.direction == Direction.LONG:
                target_price = position.entry_price + (position.entry_price - position.stop_loss) * target["rr"]
                hit = candle.high >= target_price
            else:
                target_price = position.entry_price - (position.stop_loss - position.entry_price) * target["rr"]
                hit = candle.low <= target_price

            if hit:
                position.completed_targets.add(target["rr"])

                if target["action"] == "breakeven":
                    position.stop_loss = self.calculate_breakeven(position)
                    await self.partial_close(position.symbol, target["percent"])
                elif target["action"] == "trail":
                    position.trailing_active = True
                    await self.partial_close(position.symbol, target["percent"])
                elif target["action"] == "close":
                    await self.close_position(position.symbol, candle.close, "TAKE_PROFIT")
                    return

    async def close_position(self, symbol, price, reason):
        pos = self.positions.get(symbol)
        if not pos:
            return

        pnl = self.calculate_pnl(pos, price)
        logger.info(f"Position closed: {symbol} @ {price} | Reason: {reason} | PnL: {pnl}")

        self.positions[symbol] = None

        # Обновить статистику
        if pnl < 0:
            self.consecutive_losses += 1
            self.daily_risk_used += pos.size_usdt * (abs(pnl) / pos.entry_price)
        else:
            self.consecutive_losses = 0

---

## 14. СПЕЦИФИКАЦИЯ WEBSOCKET API

### 14.1 Подключение к BingX (пример)

```python
WEBSOCKET_CONFIG = {
    "base_url": "wss://open-api-swap.bingx.com/swap-market",

    "streams": {
        "kline_4h": "{symbol}@kline_4h",
        "kline_1h": "{symbol}@kline_1h",
        "kline_15m": "{symbol}@kline_15m",
        "kline_5m": "{symbol}@kline_5m",
        "trade": "{symbol}@trade",
        "depth": "{symbol}@depth20",
        "ticker": "{symbol}@ticker",
    },

    "heartbeat_interval": 30,
    "reconnect_backoff": [1, 2, 5, 10, 30, 60],
    "max_reconnect_attempts": 10,
}
```

### 14.2 Формат данных Kline

```json
{
  "e": "kline",
  "E": 1234567890000,
  "s": "BTC-USDT",
  "k": {
    "t": 1234567800000,
    "T": 1234568099999,
    "s": "BTC-USDT",
    "i": "5m",
    "f": 100,
    "L": 200,
    "o": "65000.00",
    "c": "65100.00",
    "h": "65200.00",
    "l": "64900.00",
    "v": "100.5",
    "n": 100,
    "x": true,
    "q": "6525000.00",
    "V": "50.2",
    "Q": "3260000.00"
  }
}
```

### 14.3 Формат данных Depth (стакан)

```json
{
  "e": "depth",
  "E": 1234567890000,
  "s": "BTC-USDT",
  "b": [
    ["65000.00", "1.5"],
    ["64990.00", "2.3"]
  ],
  "a": [
    ["65100.00", "0.8"],
    ["65110.00", "1.2"]
  ]
}
```

---

## 15. ОБРАБОТКА ОШИБОК И ОТКАЗОУСТОЙЧИВОСТЬ

### 15.1 Классификация ошибок

| Код | Тип | Действие |
|-----|-----|----------|
| WS_001 | WebSocket disconnect | Переподключение с backoff |
| WS_002 | Задержка данных > 500мс | Пауза торговли, алерт |
| WS_003 | Невалидный бар | Пропуск, запрос истории |
| API_001 | Отклонение ордера | Логирование, ретрай x3 |
| API_002 | Недостаточно маржи | Уменьшение размера, алерт |
| API_003 | Rate limit | Экспоненциальный backoff |
| SYS_001 | Падение процесса | Docker restart, алерт |
| SYS_002 | Неконсистентность данных | Перезагрузка буферов |

### 15.2 Стратегия восстановления

```python
class RecoveryManager:
    async def handle_disconnect(self):
        # 1. Отменить все pending ордера
        await self.cancel_all_orders()

        # 2. Перезагрузить исторические данные
        await self.load_historical_bars()

        # 3. Пересчитать все индикаторы
        self.recalculate_all_indicators()

        # 4. Проверить открытые позиции через REST API
        await self.sync_positions()

        # 5. Возобновить торговлю
        self.resume_trading()
```

### 15.3 Circuit Breaker

```yaml
circuit_breaker:
  conditions:
    - "3 убыточные сделки подряд -> пауза 1 час"
    - "Просадка 5% за день -> пауза 4 часа"
    - "Просадка 10% -> остановка до ручного перезапуска"
    - "WebSocket disconnect > 5 минут -> остановка входов"

  recovery:
    manual_restart: true
    auto_resume_after: "4h"
    require_confirmation: true
```

---

## 16. МЕТРИКИ И МОНИТОРИНГ

### 16.1 Real-time метрики

| Метрика | Частота | Алерт при |
|---------|---------|-----------|
| WebSocket latency | Каждые 30 сек | > 100 мс |
| Signal-to-execution time | По сигналу | > 200 мс |
| Slippage | По сделке | > 0.1% |
| Win rate (rolling 50) | Каждая сделка | < 40% |
| Expectancy | Каждая сделка | < 0 |
| Max drawdown | Каждый час | > 5% |
| Unfilled orders | Каждые 5 мин | > 2 |

### 16.2 Логирование сделок

```json
{
  "timestamp": "2026-08-21T12:34:56Z",
  "event": "TRADE_EXECUTED",
  "symbol": "BTC-USDT",
  "direction": "SHORT",
  "entry_type": "MARKET",
  "expected_entry": 67500.00,
  "actual_entry": 67508.00,
  "stop_loss": 67665.00,
  "take_profit": 66200.00,
  "position_size_usdt": 26008.00,
  "quantity": 0.3853,
  "risk_percent": 2.0,
  "expected_rr": 3.2,
  "actual_rr": 3.15,
  "confirmation_score": 4,
  "sweep_level": 67500.00,
  "sweep_high": 67620.00,
  "fvg_zone": {"top": 67450, "bottom": 67420},
  "order_block": {"top": 67580, "bottom": 67550},
  "market_bias_4h": "BEARISH",
  "market_bias_1h": "BEARISH",
  "execution_slippage": 0.012,
  "latency_ms": 145
}
```

### 16.3 Алерты

```yaml
alerts:
  telegram:
    bot_token: "${TELEGRAM_BOT_TOKEN}"
    chat_id: "${TELEGRAM_CHAT_ID}"

  events:
    - "SWEEP_DETECTED" -> "Пул {symbol} {level} просвечен"
    - "TRADE_EXECUTED" -> "Вход {direction} {symbol} @ {price} (score: {confirmation_score})"
    - "STOP_LOSS" -> "SL {symbol} @ {price} (-{loss_usdt} USDT)"
    - "TAKE_PROFIT" -> "TP {symbol} @ {price} (+{profit_usdt} USDT)"
    - "PARTIAL_CLOSE" -> "Partial {symbol} {percent}% @ {price}"
    - "CIRCUIT_BREAKER" -> "Торговля остановлена: {reason}"
    - "WEBSOCKET_DOWN" -> "Потеря соединения > {duration}"
```

---

## 17. ЧЕКЛИСТ РЕАЛИЗАЦИИ

### Этап 1: Инфраструктура
- [ ] WebSocket подключение к бирже
- [ ] Буферы баров (deque per symbol/TF)
- [ ] Система логирования
- [ ] Алерты (Telegram)

### Этап 2: Анализ (без торговли)
- [ ] Детекция фракталов с confirmed_at
- [ ] Кластеризация в пулы ликвидности
- [ ] Детекция Sweep (визуальная валидация)
- [ ] Детекция FVG с temporal binding
- [ ] Детекция Order Blocks с temporal binding
- [ ] Детекция BOS/CHoCH с привязкой к конкретному swing
- [ ] MTF alignment

### Этап 3: Сигналы (без исполнения)
- [ ] Взвешенная система confirmation (BOS=2, FVG=1, OB=1)
- [ ] Расчет R:R (expected + actual после fill)
- [ ] Фильтры (спред, глубина, волатильность)
- [ ] Генерация сигналов в лог
- [ ] Валидация на истории (бэктест)

### Этап 4: Исполнение
- [ ] Расчет размера позиции (2% / 1% в зависимости от score)
- [ ] Отправка Market/Limit ордеров
- [ ] Установка SL/TP
- [ ] Трекинг исполнения
- [ ] Пересчет actual_rr после fill

### Этап 5: Управление
- [ ] Breakeven (исправленное направление)
- [ ] Partial Close (TP1 25%, TP2 35%, TP3 40%)
- [ ] Trailing stop (только на остаток)
- [ ] Time stop (100 минут)

### Этап 6: Защита
- [ ] Circuit breaker
- [ ] Daily limits (remaining_risk)
- [ ] Recovery after disconnect
- [ ] Invalidation logic (timeout + breach)
- [ ] Pool lifecycle (TRADED, LIQUIDATED)

---

## 18. ПАРАМЕТРЫ ПО УМОЛЧАНИЮ

```yaml
bot_config:
  # Таймфреймы
  timeframes: ["4H", "1H", "15m", "5m"]

  # Фракталы
  fractal:
    lookaround: 2
    tolerance_percent: 0.15
    min_fractals_in_pool: 2

  # Sweep
  sweep:
    volume_multiplier: 1.5           # Опциональное усиление
    volume_required: false           # Не обязательно
    max_body_beyond_level: 0.3       # % от ATR
    min_wick_beyond_level: 0.1       # % от цены
    min_body_size: 0.05              # % от цены
    max_pool_age_bars: 100
    confirmation_timeout_bars: 5     # На 5m = 25 минут

  # FVG
  fvg:
    min_height_percent: 0.05
    max_age_bars: 20
    fill_threshold: 0.7
    temporal_binding: true

  # Order Block
  order_block:
    max_lookback_bars: 20
    min_impulse_percent: 0.2
    temporal_binding: true

  # BOS
  bos:
    reference_swing: "LAST_BEFORE_SWEEP"
    temporal_binding: true

  # Confirmation Scoring
  confirmation:
    bos_weight: 2
    fvg_weight: 1
    ob_weight: 1
    min_score: 2
    full_size_threshold: 3           # >= 3 балла = 2% риска
    reduced_size_threshold: 2        # 2 балла = 1% риска

  # Entry
  entry:
    min_rr: 2.5
    type: "MARKET"
    limit_buffer_percent: 0.05
    max_wait_bars: 3

  # Stop Loss
  stop_loss:
    buffer_method: "ATR"
    atr_multiplier: 1.0
    atr_timeframe: "5m"
    atr_period: 14

  # Take Profit
  take_profit:
    method: "OPPOSITE_LIQUIDITY_POOL"
    partial_close:
      - { rr: 2.0, percent: 25, action: "breakeven" }
      - { rr: 3.0, percent: 35, action: "trail" }
      - { rr: 4.0, percent: 40, action: "close" }

  # Breakeven
  breakeven:
    activation_rr: 1.5
    fee_buffer_percent: 0.05

  # Trailing
  trailing_stop:
    activation_rr: 3.0
    applies_to: "remaining_only"
    method: "ATR_BASED"
    atr_multiplier: 1.5
    min_distance_from_entry: 0.5

  # Time Stop
  time_stop:
    enabled: true
    max_duration_minutes: 100

  # Risk Management
  risk:
    risk_per_trade_percent: 2.0
    max_risk_per_day_percent: 6.0
    max_trades_per_day: 5
    max_consecutive_losses: 3
    max_drawdown_percent: 10
    profit_target_daily_percent: 10

  # Position Limits
  limits:
    max_positions_total: 5
    max_long_positions: 3
    max_short_positions: 3

  # Execution Filters
  execution:
    max_spread_percent: 0.15
    max_slippage_percent: 0.1
    min_depth_0_5_percent: 10000

  # Volatility Filter
  volatility:
    atr_timeframe: "1H"
    atr_period: 14
    min_atr_percent: 0.3
    max_atr_percent: 5.0

  # Circuit Breaker
  circuit_breaker:
    consecutive_losses_pause: 3
    drawdown_5_percent_pause_hours: 4
    drawdown_10_percent_stop: true
    ws_disconnect_max_minutes: 5
```

---

## 19. РЕКОМЕНДАЦИИ ПО ПРОМТИНГУ ДЛЯ ИИ-КОДЕРОВ

При передаче этого ТЗ моделям (Mimo, Qwen3, Claude, GPT-4) рекомендуется разбивать на этапы:

### Этап 1: Инфраструктура
> Напиши модуль WebSocket-подключения к BingX для получения kline данных на таймфреймах 4H, 1H, 15m, 5m. Используй asyncio, websockets. Реализуй буферы bars через collections.deque (maxlen=300) для каждого символа и ТФ. Добавь автоматический reconnect с exponential backoff.

### Этап 2: Детекция фракталов и пулов
> Напиши функцию detect_fractals(bars, lookaround=2), которая находит Swing Highs и Swing Lows по правилу 5 свечей. Каждый фрактал должен иметь confirmed_at = timestamp[i+2] (не timestamp[i]). Затем функцию cluster_fractals(fractals, tolerance=0.15%), которая группирует близкие фракталы в пулы ликвидности (EQH/EQL). Результат храни в словаре per symbol.

### Этап 3: Sweep с temporal binding
> Напиши функцию detect_sweep(candle, pool), которая проверяет базовые условия Liquidity Sweep (тень пробила уровень, тело закрылось внутри). Объем НЕ должен быть обязательным. Функция должна возвращать sweep_event с confirmation_window_end = detected_at + 25 минут. Добавь функции is_sweep_expired() и is_sweep_breached().

### Этап 4: Confirmation с temporal binding
> Напиши функцию find_confirmation(bars, sweep), которая ищет FVG, OB и BOS ТОЛЬКО после sweep.detected_at (temporal binding). BOS имеет вес 2, FVG и OB — вес 1. Минимальный порог для входа: 2 балла. При 2 баллах — размер позиции 1%, при 3+ — 2%.

### Этап 5: Execution и Risk
> Напиши функцию calculate_position_size(capital, risk_percent, entry, sl, direction) по формуле фиксированного риска. Затем функцию execute_entry(symbol, direction, entry, sl, tp, size), которая отправляет MARKET ордер, получает actual_fill_price, пересчитывает actual_rr и создает объект Position. Добавь daily risk tracker: remaining_daily_risk = 6% - used_risk.

### Этап 6: Trade Management
> Напиши функцию check_position_management(position, candle), которая проверяет SL, TP (partial closes: 25% at 2R, 35% at 3R, 40% at 4R), Breakeven (при 1.5R, SL = entry +/- fee_buffer), Trailing Stop (при 3R, ATR-based, только на остаток) и Time Stop (100 минут).

### Этап 7: Сборка
> Собери все модули в единый класс SMCTradingBot с главным циклом async run(). Добавь логирование сделок в JSON, алерты в Telegram и Circuit Breaker.

---

## РЕЗЮМЕ ИСПРАВЛЕНИЙ (ВЕРСИЯ 2.0)

| Проблема | Исправление |
|----------|------------|
| TP направление перепутано в тексте | Исправлено: LONG → EQH/SSH выше, SHORT → EQL/SSL ниже |
| Нет temporal binding | Добавлено: все confirmation только после sweep.detected_at |
| Confirmation может быть до Sweep | Исправлено: жесткая проверка timestamp |
| Нет timeout Sweep | Добавлено: confirmation_window_end, is_sweep_expired() |
| FVG алгоритм не соответствует описанию | Исправлено: проверка цвета i и i+2, не только i+1 |
| Breakeven в неправильную сторону | Исправлено: LONG BE = entry + buffer, SHORT BE = entry - buffer |
| Partial Close не работает | Реализовано: TP1 25% + BE, TP2 35% + trail, TP3 40% close |
| Time Stop только на 5m | Исправлено: универсальная проверка по elapsed minutes |
| Volume filter обязательный | Сделан опциональным (volume_required: false) |
| Нет TRADED статуса | Добавлен TRADED и LIQUIDATED в lifecycle |
| Daily limits противоречат | Исправлено: remaining_daily_risk вместо фиксированного 2% |
| Post-fill RR не пересчитывается | Добавлен recalculate_actual_rr() |
| "2 из 3" равнозначно | Заменено на взвешенную систему: BOS=2, FVG=1, OB=1 |

---

*Документация подготовлена на основе стратегии Smart Money Concepts (SMC) с акцентом на анализ ликвидности, описанной в видео Егора Круталевича. Версия 2.0 содержит исправления после аудита логической состоятельности.*
