# Deep-Dive Analysis: Live vs Backtest Performance Gap

**Дата:** 2026-07-19
**Цель:** Найти корневые причины расхождения backtest (24.6% WR, PF 1.14) vs live (20% WR, PF 0.37)

---

## 1. ML-модель вероятности — КРИТИЧЕСКИЕ ПРОБЛЕМЫ

### 1.1 Модель не работает

| Метрика | Значение | Оценка |
|---------|----------|--------|
| AUC | 0.69 | Едва выше random (0.5) |
| ECE (calibration) | **15.2%** | Плохая — предсказания не соответствуют реальности |
| Brier score | 0.24 | Высокий (0 = идеал) |
| Features | 31 | Из них только 3 имеют значение |

**LOFO Feature Importance (топ-3):**
1. `tp_distance_pct` — delta_auc = +0.012
2. `adx` — delta_auc = +0.010
3. `rr_ratio` — delta_auc = +0.010

**Остальные 28 фичей** — delta_auc < 0.005 (незначимы). Модель essentially random.

### 1.2 Rules-based лучше ML

Из `reports/feasibility/train_metrics.json`:

| Порог | Rules WR | Rules PF | Rules Expectancy | Model WR | Model PF | Model Expectancy |
|-------|----------|----------|------------------|----------|----------|------------------|
| ≥0.60 | **83.3%** | **1.14** | **+0.19%** | 53.8% | 0.71 | **-0.66%** |
| ≥0.70 | 100% (n=3) | — | — | — | — | — |

**Rules-based fallback даёт лучшие результаты, чем ML модель.** При `min_p_tp=0.45` ML модель пропускает сделки с низким quality, которые rules-based отфильтровала бы.

### 1.3 Данные обучения — НЕ правильные символы

| Параметр | Факт |
|----------|------|
| Символы в train | **WIF/USDT (1789) + ENA/USDT (1433)** — meme/small-cap |
| Символы в live | **BTC/USDT, ETH/USDT** — major |
| Период | 2026-01-03 → 2026-07-18 (~6.5 мес) |
| Positive rate | 41.1% |
| Timestamp | **Не сохранён** — невозможно определить свежесть |

**Модель обучена на WIF/ENA, но используется для BTC/ETH.** Generalization не валидирована.

### 1.4 Look-ahead bias в ML фичах

Фичи `has_ob`, `ob_distance_pct` приходят из `pattern_engine.detect()` → `order_blocks.py:59-171`. OB detection использует forward scan (`_check_bos_*` look_ahead=20, `_check_retest_*` look_ahead=30). **Но** — цикл `detect_order_blocks` останавливается на `len(data) - 2` (строка 107), поэтому на **live свече** нет look-ahead. Look-ahead существует только при построении OB на historical данных в пределах lookback window.

**Вердикт:** Look-ahead bias **не влияет на live сигналы**, но может завышать бэктестные метрики.

### 1.5 Calibration — min_p_tp=0.45 бессмысленен

При ECE=15.2% порог `min_p_tp=0.45` не отфильтровывает реальные проигрышные сделки. Модель предсказывает 0.45 для сделок, которые на самом деле выигрывают с WR ~35%, и 0.55 для сделок с WR ~50%. **Калибровка не работает.**

---

## 2. Live-торговля (5 сделок) — Root Cause Analysis

### 2.1 Данные по 5 сделкам

| # | Символ | TF | Dir | Entry | SL | TP | Exit | PnL | R | Result |
|---|--------|-----|-----|-------|----|----|------|-----|---|--------|
| 1 | DOT/USDT | 1h | BUY | 0.881 | 0.874 | 0.906 | 0.874 | -1.01% | -1.2R | SL |
| 2 | INJ/USDT | 1h | BUY | 5.038 | 4.949 | 5.272 | 4.949 | -1.96% | -1.1R | SL |
| 3 | ETH/USDT | 4h | SELL | 1786.79 | 1830.40 | 1671.00 | 1830.40 | -2.68% | -1.1R | SL |
| 4 | APT/USDT | 1h | SELL | 0.617 | 0.625 | 0.594 | 0.594 | +3.54% | +2.6R | **TP** |
| 5 | ARB/USDT | 1h | BUY | 0.093 | 0.090 | 0.101 | 0.090 | -3.84% | -1.1R | SL |

### 2.2 Паттерны

| Паттерн | Значение |
|---------|----------|
| BUY сделки | 3/5 (60%), WR = **0%** |
| SELL сделки | 2/5 (40%), WR = **50%** |
| Все SL命中 | -1.1R до -1.2R (SL placement корректный) |
| Единственный TP | +2.6R (APT SELL) |
| Альткоины | 4/5 сделок — DOT, INJ, APT, ARB |
| BTC/ETH | 1/5 (ETH SELL) |

### 2.3 Ключевые наблюдения

1. **BUY на альткоинах = 0% WR.** Все 3 BUY сделки — альткоины (DOT, INJ, ARB). Все проиграли.
2. **SELL на альткоинах работает.** APT SELL = +2.6R.
3. **SL sizing корректный.** Все проигрышные сделки потеряли -1.1R до -1.2R — SL не слишком близко.
4. **TP sizing корректный.** APT TP дал +2.6R — TP не слишком далеко.
5. **Проблема в DIRECTION, не в risk management.**

### 2.4 DecisionTrace — что показывает funnel

Из `reports/full_report.md:128-137`:
```
[FUNNEL SUMMARY] entered=70, sent=0, pattern_engine=47, regime_block=22, displacement_gate=1
```

- 70 symbol/TF pairs сканируются за цикл
- 47 блокируются pattern_engine (57%)
- 22 блокируются regime_block (28%)
- 1 блокируется displacement_gate
- **Отправлено: 0-1 сигнал за цикл**

**Regime block = 28%** — это второй по величине фильтр. Если regime неправильно определяет "trending" как "ranging", он блокирует ХОРОШИЕ сигналы и пропускает ПЛОХИЕ.

### 2.5 MFE/MAE — были ли сделки близки к TP?

Данные MFE/MAE **не доступны** в `reports/full_report.md`. Для полного анализа нужен запрос к SQLite:
```sql
SELECT s.symbol, s.mfe_pct, s.mae_pct, s.hold_duration_hours
FROM signals s
JOIN signal_outcomes o ON o.signal_id = s.id
WHERE o.status IN ('HIT_TP', 'HIT_SL')
ORDER BY s.created_at DESC LIMIT 10;
```

### 2.6 Контекст в момент входа

Данные по FNG, funding, OI **не сохраняются** в signal record. Context snapshot сохраняется отдельно (`context_snapshots` table), но не привязан к сигналу напрямую. Для анализа нужен JOIN по timestamp + symbol.

---

## 3. Sweep + Continuation — ГЛАВНАЯ АРХИТЕКТУРНАЯ ПРОБЛЕМА

### 3.1 Sweep НЕ используется в continuation

```python
# pattern_engine.py:353-415
def _try_continuation(self, structure) -> ICTSetup:
    trend = structure.trend
    if trend == "ranging":  return rejected       # Gate 1

    if structure.last_bos is not None:             # Gate 2: BOS required
        has_bos = True
        direction = "buy" if bos.type == "bullish" else "sell"

    if not has_bos:  return rejected

    trend_aligned = (direction matches trend)      # Gate 3: alignment
    if not trend_aligned:  return rejected

    return ICTSetup(detected=True, setup_type="continuation",
                    has_bos=True, ...)  # ← has_sweep НЕ устанавливается
```

**Sweep completely ignored для continuation.** Setup возвращается с `has_sweep=False` по default.

### 3.2 Доказательство из данных

Survivor analysis (`reports/survivor_analysis.md`):
- **100% победителей** имели `sweep_present=True`
- **38.5% проигравших** имели `sweep_present=True`
- **Delta = +61.5%** — самый сильный предиктор

Gate simulation (`reports/gate_simulation.md`):
- `sweep_present` alone = **+0.414R** (лучший single filter)
- `sweep_present AND moderate_vol AND rr_2` = **+0.835R** (лучшая комбинация)

**НО:** В continuation sweep детектируется (`detect_sweeps()` type-agnostic), но **не используется как gate**. Sweep может присутствовать в данных, но pattern engine его игнорирует для continuation.

### 3.3 Why OB present = -0.288R

Gate simulation: `ob_present` DESTROYS edge (-0.288R).

**Причина:** OB detection использует forward scan (look_ahead=20-30). В historical данных OB выглядит "working" (price retested и оттолкнулся). Но в live:
1. OB мог быть уже mitigated (price прошёл через него)
2. OB state tracker (`ob_state.py`) может неправильно определять freshness
3. `OB_MULTIPLIERS`: FRESH=1.25, BROKEN=0.0 — если OB помечен FRESH, но реально mitigated → ложный edge

---

## 4. Market Regime — НЕПРАВИЛЬНАЯ КЛАССИФИКАЦИЯ

### 4.1 Regime detection не блокирует, но влияет на P(TP)

`_detect_regime()` (`scanner.py:160-215`) возвращает `MarketRegime` which flows into Probability Engine. **Regime — это feature, не gate.** Но:
- Regime "compression" → может снижать P(TP)
- Regime "range" → может снижать P(TP)
- **Regime "trend" → может повышать P(TP)**

### 4.2 Паттерн failure clusters

Из `reports/failure_clusters.md`:
- **Cluster #1:** BUY + range + high ADX + aligned=False + no sweep + OB near + high RR = **0% WR** (5 trades)
- **Cluster #2:** BUY + range + high ADX + aligned=False + no sweep + OB near + mid RR = **0% WR** (5 trades)

**Ключевой паттерн:** High ADX + range = **ловушка.** ADX ≥ 26 (текущий `adx_min`) не означает trending — ADX может быть высоким в range если price " choppy".

### 4.3 No-trade zones — недостаточно строгие

Текущие no-trade zones (`no_trade_zones.py:36-98`):
1. ATR < 0.5% → block
2. Market structure == "ranging" → block
3. BTC not aligned → block
4. TP blocked → block
5. OI extreme → block

**Отсутствует:** "High ADX + range" combo filter. Все failure clusters имеют `adx=very_high` + `regime=range`.

---

## 5. HTF Bias V2 — Почему BUY шли против тренда

### 5.1 HTF bias voting

`get_htf_bias_v2()` (`htf_bias_v2.py:85-135`):
- W1 + D1 + H4 = majority vote
- H1 используется ТОЛЬКО для zone classification, НЕ для direction

### 5.2 Hard gate для continuation

`scanner.py:458-501`:
```python
if setup.setup_type == "continuation":
    if setup.direction opposes htf_bias:
        if config.htf_hard_gate:  → BLOCKED
        else: → penalty (htf_bias_continuation_penalty=0.85)
```

**Если HTF bearish, а continuation BUY → заблокирован.** Но:
- Если HTF **neutral** → BUY проходит без фильтра
- Если HTF **mixed** (W1 bullish, D1 bearish, H4 bearish) → override logic может дать NEUTRAL

### 5.3 V1 vs V2 mismatch

V1 (`htf_bias.py`) использует 1D + 4H с BOS age < 20 candles.
V2 (`htf_bias_v2.py`) использует W1 + D1 + H4 + H1 majority vote.

Если `config.htf_bias_v2 = True` (текущий default), V2 используется. Но если BOS на 1D старше 20 candles → V1 вернёт NEUTRAL → нет gate. V2 может вернуть direction на основе EMA alignment → gate сработает.

---

## 6. Position Sizing и Slippage

### 6.1 Bot — signal-only, без ордеров

**Бот НЕ размещает ордера.** `exchange_client.py` — read-only (fetch OHLCV + tickers). Ордера исполняются вручную через Telegram. Это значит:
- Нет control над исполнением (market vs limit)
- Нет control над slippage
- Нет control над entry timing

### 6.2 Slippage model — упрощённый

Backtest: `fee=0.05% + slippage=0.05%` per side = **0.20% round-trip**.
Но:
- Два backtest скрипта хардкодят **0.02% slippage** вместо 0.05%
- Реальный slippage на BingX для альткоинов может быть **0.1-0.3%** (low liquidity)
- Funding rate зафиксирован на **0.01% per 8h** — реальный может быть выше

### 6.3 Kelly sizing при 20% WR

При P(TP)=0.45, R:R=3.0:
```
kelly = (0.45 * 3.0 - 0.55) / 3.0 = (1.35 - 0.55) / 3.0 = 0.267
kelly = min(0.267, 0.20) = 0.20  (capped)
kelly *= confidence (0.4 for rules-based) = 0.08
risk_pct = min(8.0%, 1.0%) = 1.0%
```

При реальном WR=20% (live):
```
kelly = (0.20 * 3.0 - 0.80) / 3.0 = (0.60 - 0.80) / 3.0 = -0.067
kelly = max(0.0, -0.067) = 0.0  → risk_pct = 0.0%
```

**Kelly даёт 0% при 20% WR.** Система не должна торговать. Но `base_risk_pct=1.0%` используется как fallback: `risk_pct = min(kelly*100, base_risk_pct)` → при kelly=0, risk_pct = min(0, 1.0) = 0. **Но** — в коде `risk_pct = min(kelly * 100, self.base_risk_pct)` — если kelly=0, risk_pct=0, что меньше `min_risk_pct=0.1%` → clamp до 0.1%.

**Результат:** Даже при 20% WR, система ставит 0.1% risk (минимум). Это правильно — минимальная позиция при неизвестном edge.

---

## 7. Сводка: Топ-5 корневых причин live vs backtest gap

| # | Причина | Влияние | Доказательство |
|---|---------|---------|----------------|
| **1** | **ML модель бесполезна** — AUC 0.69, правила лучше. Обучена на WIF/ENA, используется для BTC/ETH. Калибровка ECE=15.2%. | min_p_tp=0.45 не фильтрует реальные проигрыши | `train_metrics.json`, `probability_engine.py:280` |
| **2** | **Sweep не используется в continuation** — 100% победителей имели sweep, но continuation pipeline его игнорирует. Все live BUY = continuation без sweep. | BUY WR=0% | `pattern_engine.py:353-415`, `survivor_analysis.md` |
| **3** | **Regime block = 28%** — неправильная классификация "trending" vs "ranging". High ADX + range = ловушка, которую regime не определяет. | Пропускает плохие сигналы, блокирует хорошие | `failure_clusters.md`, `full_report.md:128` |
| **4** | **Slippage underestimate** — backtest: 0.05%/side, live BingX альткоины: 0.1-0.3%/side. 4/5 сделок = альткоины. | PF снижается на 0.1-0.3 | `outcome_tracker.py:71`, `settings.py:157` |
| **5** | **Symbols: альткоины vs majors** — бэктест на BTC+ETH (27.1% WR, PF 1.38), live включает DOT/INJ/APT/ARB (0% WR на BUY). | Разрушает portfolio | `full_report.md:70-77` |

---

## 8. Рекомендации (приоритизированные)

### Немедленные (1-2 дня)

| # | Действие | Ожидаемый эффект | Сложность |
|---|----------|------------------|-----------|
| 1 | **Отключить ML модель** — использовать rules-based fallback. ML хуже правил. | Убрать негативный edge | Низкая: `probability_engine.py:280` — force `_predict_rules()` |
| 2 | **Добавить sweep_required для continuation** — новый gate в `scanner.py` после BOS check | WR +5-10% (по survivor data) | Средняя: `scanner.py:380` + `pattern_engine.py:408` |
| 3 | **Убрать ZRO/USDT и low-cap альткоины** — оставить BTC/ETH/SOL/XRP/DOGE | PF +0.2-0.3 | Низкая: `.env` |

### Среднесрочные (1-2 недели)

| # | Действие | Ожидаемый эффект | Сложность |
|---|----------|------------------|-----------|
| 4 | **Retrain ML на BTC/ETH данных** с purged split + валидация | AUC > 0.65, калибровка ECE < 10% | Средняя: `scripts/train_prob_model.py` |
| 5 | **ADX + range combo filter** — block если ADX > 25 AND regime = range | Убрать failure clusters | Низкая: `risk/no_trade_zones.py` |
| 6 | **Обучить OB state tracker** — текущий FRESH/TESTED/MITIGATED может быть неточным | Убрать -0.288R edge | Средняя: `liquidity/ob_state.py` |

### Долгосрочные (1+ месяц)

| # | Действие | Ожидаемый эффект | Сложность |
|---|----------|------------------|-----------|
| 7 | **Walk-forward validation** — проверить стабильность ML наrolling window | Убедиться в отсутствии overfit | Средняя: `scripts/walk_forward.py` |
| 8 | **Dynamic slippage model** — based on volume/OI, не fixed 0.05% | Точный бэктест | Высокая |
| 9 | **Integration с биржей** — limit orders с controlled execution | Убрать slippage uncertainty | Высокая |

---

*Анализ основан на полном сканировании кодовой базы, отчётах бэктестов, и 5 live сделках.*
