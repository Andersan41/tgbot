🔴 Критические изменения (сделать немедленно)

1\. Отключить ML-модель, перейти на rules-based probability

Проблема: AUC 0.69, ECE=15.2%, обучена на WIF/ENA → работает на BTC/ETH. min\_p\_tp=0.45 бессмысленен.

Решение: В strategy/probability\_engine.py отключить ветку ML, оставить rules-based fallback как primary.

Python

\# probability\_engine.py:117-278

\# Текущая логика:

\# if model\_exists and n\_samples >= min\_samples:

\#     p\_tp = model.predict(...)  ← убрать

\# else:

\#     p\_tp = rules\_based(...)  ← оставить как primary



\# Новая логика:

p\_tp = rules\_based\_probability(setup, context, features)

\# ML оставить как sanity check: если model.predict доступен,

\# использовать только если |model - rules| < 0.15 (agreement)

Ожидаемый эффект: Устранение ECE=15.2% → более точная оценка P(TP). WR +3-5% за счёт правильного отсева.

2\. Требовать sweep для continuation (гипотеза #5)

Проблема: detect\_sweeps() type-agnostic, но \_try\_continuation() (pattern\_engine.py:353) не принимает sweeps. Все 3 live BUY = continuation без sweep. Survivor: 100% победителей имели sweep.

Решение: Модифицировать pattern\_engine.py:353 и scanner.py:380 (BOS Gate).

Python

\# pattern\_engine.py:353 — \_try\_continuation()

def \_try\_continuation(self, structure, order\_blocks, fvgs, 

&#x20;                     candle\_quality, current\_price, atr, sweeps):

&#x20;   # ДОБАВИТЬ: sweeps как аргумент

&#x20;   setup = ContinuationSetup()

&#x20;   

&#x20;   # НОВЫЙ GATE: sweep в направлении тренда

&#x20;   trend\_sweep = self.\_find\_trend\_aligned\_sweep(

&#x20;       sweeps, structure.trend\_direction

&#x20;   )

&#x20;   setup.has\_sweep = trend\_sweep is not None

&#x20;   

&#x20;   # Существующая логика BOS...

&#x20;   setup.has\_bos = self.\_detect\_bos(...)

&#x20;   

&#x20;   # Scoring: BOS(50%) + sweep(25%) + OB/FVG(15%) + displacement(10%)

&#x20;   # Было: BOS(50%) + OB/FVG(15%)

&#x20;   return setup



\# scanner.py:380 — Continuation: BOS Gate

\# ДОБАВИТЬ:

if setup\_type == "continuation" and not setup.has\_sweep:

&#x20;   # БЛОКИРОВКА — sweep required для continuation

&#x20;   return GateResult(blocked=True, reason="no\_sweep\_continuation")

Ожидаемый эффект: WR +5-10%, сделок -40%. Это самый сильный предиктор — 100% победителей имели sweep.

3\. Исключить альткоины / ограничить universe

Проблема: 4/5 live сделок = альткоины (DOT/INJ/APT/ARB), BUY на них = 0% WR. Backtest на BTC+ETH: PF=1.38.

Решение: Жёсткий фильтр в scanner.py:235 (Phase 0, до всех вычислений).

Python

\# config/settings.py — добавить

ALLOWED\_SYMBOLS = \["BTC/USDT", "ETH/USDT"]  # или расширить до SOL/XRP

\# ИЛИ: минимальный объём/ликвидность gate



\# scanner.py:235 — Phase 0, перед cooldown

if symbol not in config.ALLOWED\_SYMBOLS:

&#x20;   return GateResult(blocked=True, reason="low\_liquidity\_symbol")

&#x20;   

\# Альтернатива: фильтр по 24h volume (если доступен через exchange API)

min\_daily\_volume\_usdt = 100\_000\_000  # $100M

if daily\_volume < min\_daily\_volume\_usdt:

&#x20;   return GateResult(blocked=True, reason="insufficient\_volume")

Ожидаемый эффект: WR +3-5% на уровне портфеля. PF с 0.37 → 1.0+.

4\. Исправить OB state detection (latency bug)

Проблема: ob\_state.py:79-123 определяет state по последним 10 свечам — опаздывает. OB помечается FRESH (1.25x), но price уже прошёл через него → False positive. Gate simulation: ob\_present = -0.288R.

Решение: Ужесточить определение FRESH и добавить price proximity check.

Python

\# ob\_state.py:79-123

def classify\_ob\_state(ob, price\_series, current\_price, lookback=10):

&#x20;   # Текущая логика: проверяет последние 10 свечей на касание

&#x20;   # Проблема: если price прошёл через OB за 1 свечу, 

&#x20;   # остальные 9 свечей покажут "не касался" → FRESH

&#x20;   

&#x20;   # НОВАЯ ЛОГИКА:

&#x20;   # 1. Проверить, был ли price ВНУТРИ OB за последние N свечей

&#x20;   ob\_high = max(ob.high, ob.low)  # bullish OB

&#x20;   ob\_low = min(ob.high, ob.low)

&#x20;   

&#x20;   candles\_inside = 0

&#x20;   for i in range(lookback):

&#x20;       if ob\_low <= price\_series.iloc\[-i] <= ob\_high:

&#x20;           candles\_inside += 1

&#x20;   

&#x20;   if candles\_inside > 0:

&#x20;       # Price был внутри OB → MITIGATED или PARTIAL

&#x20;       if candles\_inside >= lookback \* 0.5:

&#x20;           return OBState.MITIGATED

&#x20;       else:

&#x20;           return OBState.PARTIAL

&#x20;   

&#x20;   # 2. Проверить distance от current\_price до OB

&#x20;   ob\_mid = (ob\_high + ob\_low) / 2

&#x20;   distance\_pct = abs(current\_price - ob\_mid) / current\_price \* 100

&#x20;   

&#x20;   if distance\_pct > config.max\_ob\_distance\_pct:  # 3.0%

&#x20;       return OBState.BROKEN  # Слишком далеко = неактуален

&#x20;   

&#x20;   # 3. Если price не касался OB и близко → FRESH

&#x20;   return OBState.FRESH

Ожидаемый эффект: Устранение false positive FRESH OB. WR +2-4%.

5\. Исправить regime classification (high ADX + range = 0% WR)

Проблема: Regime block = 28% — неправильная классификация. Failure clusters: high ADX + range + BUY + no sweep = 0% WR (10 сделок).

Решение: Добавить "compression regime" detection — когда ADX высокий, но price в range.

Python

\# risk/market\_regime.py или scanner.py:Phase 0

def detect\_compression\_regime(df, adx\_threshold=26, range\_threshold=0.02):

&#x20;   """

&#x20;   Compression = high ADX + price within X% of range midpoint

&#x20;   """

&#x20;   adx = df\['ADX\_14'].iloc\[-1]

&#x20;   if adx < adx\_threshold:

&#x20;       return False  # Не compression

&#x20;   

&#x20;   # Проверить, находится ли price в середине range

&#x20;   recent\_high = df\['high'].tail(20).max()

&#x20;   recent\_low = df\['low'].tail(20).min()

&#x20;   range\_pct = (recent\_high - recent\_low) / recent\_low

&#x20;   

&#x20;   if range\_pct < range\_threshold:  # Слишком узкий range

&#x20;       return True  # Compression — не торговать

&#x20;   

&#x20;   # Проверить, близок ли price к середине range

&#x20;   range\_mid = (recent\_high + recent\_low) / 2

&#x20;   price = df\['close'].iloc\[-1]

&#x20;   distance\_to\_mid = abs(price - range\_mid) / range\_mid

&#x20;   

&#x20;   if distance\_to\_mid < 0.005:  # В 0.5% от середины

&#x20;       return True  # Compression — не торговать

&#x20;   

&#x20;   return False



\# scanner.py — добавить gate:

if detect\_compression\_regime(df):

&#x20;   return GateResult(blocked=True, reason="compression\_regime")

Ожидаемый эффект: WR +2-3%, устранение 0% WR кластера.

🟡 Средние изменения (сделать в ближайшие 2 недели)

6\. Поднять min\_p\_tp с 0.45 до 0.55 (или выше)

Проблема: ECE=15.2% означает, что predicted 0.45 = actual \~0.30. Нужен запас.

Решение:

Python

\# config/settings.py:620

min\_p\_tp = 0.55  # было 0.45

\# ИЛИ динамический: min\_p\_tp = base + ECE\_correction

\# min\_p\_tp = 0.45 + 0.15 = 0.60

Ожидаемый эффект: WR +3-8%, сделок -30%.

7\. Требовать R:R >= 2.0 вместо 1.5

Проблема: Gate simulation: rr\_2 = +0.099R, rr\_3 = +0.300R. Лучшая комбинация: sweep + moderate\_vol + rr\_2 = +0.835R.

Решение:

Python

\# config/settings.py:155

min\_rr\_threshold = 2.0  # было 1.5

\# ИЛИ динамический: min\_rr = 2.0 для continuation, 1.5 для reversal

Ожидаемый эффект: WR +2-5%, PF +0.1-0.2.

8\. Заблокировать BUY в bearish HTF bias

Проблема: Live BUY 0% WR (3 сделки). Survivor: 100% проигравших были BUY.

Решение: Ужесточить HTF Bias V2 для directional setups.

Python

\# market\_structure/htf\_bias\_v2.py:85-135

\# Текущая логика: continuation opposing HTF + htf\_hard\_gate=True → BLOCKED

\# Reversal mismatch → penalty (no block)



\# НОВАЯ ЛОГИКА:

if setup.direction == "BUY" and htf\_bias == "BEARISH":

&#x20;   # Было: penalty для reversal, block для continuation

&#x20;   # Стало: hard block для ВСЕХ BUY в bearish HTF

&#x20;   return GateResult(blocked=True, reason="bearish\_htf\_no\_buy")

&#x20;   

if setup.direction == "SELL" and htf\_bias == "BULLISH":

&#x20;   return GateResult(blocked=True, reason="bullish\_htf\_no\_sell")

Ожидаемый эффект: WR +5-10% (устранение 0% WR BUY), сделок -50%.

9\. Исправить look-ahead в OB detection для бэктеста

Проблема: order\_blocks.py:211-249 scan forward 20-30 candles. В бэктесте это даёт завышенные результаты. В live нет look-ahead, но бэктестные ожидания завышены.

Решение: Для бэктеста — rolling window с lag. Для live — оставить как есть.

Python

\# order\_blocks.py:211-249

def detect\_order\_blocks(df, look\_ahead=20, mode="live"):

&#x20;   """

&#x20;   mode="live": текущая логика (forward scan до len(data)-2)

&#x20;   mode="backtest": rolling window, OB формируется только если 

&#x20;                    подтверждение произошло ВНУТРИ окна

&#x20;   """

&#x20;   if mode == "backtest":

&#x20;       # Использовать только данные до текущей свечи

&#x20;       # OB считается валидным только если retest произошёл

&#x20;       # в пределах look\_ahead, но данные для retest берутся

&#x20;       # из уже пройденного периода

&#x20;       pass  # Реализовать rolling detection

Ожидаемый эффект: Точность бэктеста — live gap уменьшится с 4.6% до 1-2%.

🟢 Оптимизационные изменения (после стабилизации)

10\. Оптимизировать confidence weights

Текущие веса: HTF=20, Structure=15, Liquidity=20, Volume=5, BTC=15, Funding=5, OI=5, RSI=5, MACD=5, ADX=5.

На основе данных:

Sweep (Liquidity) = главный предиктор → увеличить вес

OB (Liquidity) = -0.288R → уменьшить вес или исключить

Volume = weak predictor → уменьшить

Python

\# scoring/confidence\_v2.py:42-71

weights = {

&#x20;   'htf\_alignment': 15,      # было 20

&#x20;   'structure': 20,          # было 15 (BOS/MSS важны)

&#x20;   'liquidity\_sweep': 25,    # НОВЫЙ: sweep отдельно

&#x20;   'liquidity\_ob': 5,        # было 20 (OB убивает edge)

&#x20;   'volume': 3,              # было 5

&#x20;   'btc\_correlation': 10,    # было 15

&#x20;   'funding': 5,

&#x20;   'oi': 5,

&#x20;   'rsi': 5,

&#x20;   'macd': 5,

&#x20;   'adx': 7,                 # было 5 (regime важен)

}

📋 План внедрения

Таблица

Этап	Изменения	Ожидаемый WR	Ожидаемый PF

Неделя 1	#1 (ML off), #2 (sweep для continuation), #3 (только BTC/ETH)	25-30%	1.0-1.2

Неделя 2	#4 (OB fix), #5 (compression regime), #6 (min\_p\_tp 0.55)	30-35%	1.2-1.5

Неделя 3	#7 (RR>=2.0), #8 (block BUY bearish), #9 (backtest fix)	35-40%	1.5-2.0

Неделя 4	#10 (weights optimization), walk-forward validation	35-45%	1.5-2.5



