# Как работает стратегия в бэктесте — backtest.md

> Справочник по `backtest/engine.py` (BacktestEngine) — как именно считается
> каждый сигнал, SL/TP, позиция и метрика. Дополняет `plan/11-pipeline.md`
> (там — live-пайплайн `scan_symbol_v2()`), здесь — только бэктест.
> Актуально на 2026-08-18.

---

## 1. Поток данных

```
BacktestEngine.run()
  ├── df=... (override)          → используется как есть + HTF из htf_base
  ├── source="local"             → ohlcv_cache/{SYM}_{market}_15m.parquet
  │                                → resample_ohlcv() до self.timeframe
  └── source="live"              → exchange_client.fetch_ohlcv(limit=…)
```

- `run(df=None, htf_base=None)` — `backtest/engine.py:305`. Если передан `df`,
  он используется напрямую (источник `local`, `_df_override=True`), HTF-серии
  (1w/1d/4h) строятся из `htf_base` (обычно 15m-кадр), чтобы не апсемплировать
  уже ресемпленый `df`.
- `_load_local()` — `engine.py:362`: читает unified-кэш 15m, ресемплит.
- Для каждого таймфрейма в `_build_htf_from_df` (`engine.py:345`) строится
  словарь `{"1w": df, "1d": df, "4h": df}` — нужен HTF Bias V2.
- `limit = max(candles_limit + 100, 200)` — `engine.py:408`.
- Данных < 100 свечей → пустой результат.

**Ключевое (паритет с live):** на каждой свече `i` анализируется окно
`df.iloc[max(0, i - candle_limit + 1): i + 1]` (`engine.py:459`) — ровно
`candles_limit` свечей, как live-сканер (`fetch_ohlcv limit=candles_limit`).
Раньше окно росло до всей истории (бэктест «видел» больше, чем live, + O(n²)),
теперь это O(n × candles_limit).

---

## 2. Главный цикл `_run_backtest()` (engine.py:406)

```
for i in range(warmup, len(df)):
    window  = df.iloc[max(0, i - candle_limit + 1): i + 1]
    ind     = indicator_engine.values_at(df, symbol, tf, i)   # предрасчёт O(n)
    ...
    # 1) ВЫХОДЫ по всем открытым позициям (SL/TP)
    # 2) НОВЫЙ СИГНАЛ — только если лимит позиций не достигнут
```

- `warmup = max(80, candles_limit // 2)` (`engine.py:417`).
- Индикаторы считаются ОДИН раз на весь фрейм (`precompute`, `engine.py:425`),
  дальше берутся `values_at` — это и есть устранение O(n²).
- Индикаторы (IndicatorValues): EMA fast/slow/trend, RSI, MACD, ADX/DMI, ATR,
  Supertrend, volume/sma (pandas-ta: `SUPERT_…`/`SUPERTd_…`, `ADX_…/DMP_…/DMN_…`).

---

## 3. Выходы (exit) — engine.py:471

Для каждой открытой позиции на текущей свече, **SL проверяется раньше TP**
(в рамках одного бара):

| Направление | SL | TP |
|---|---|---|
| BUY | `low <= sl` → exit по `sl` | `high >= tp` → exit по `tp` |
| SELL | `high >= sl` → exit по `sl` | `low <= tp` → exit по `tp` |

- Цена выхода = ровно уровень SL/TP (не close).
- MFE/MAE обновляются каждый бар, пока позиция открыта.
- В конце данных все незакрытые позиции закрываются по последнему close
  с `exit_reason="eob"` (`engine.py:803`).

> **Важно:** если в одном баре и SL и TP пройдены — сработает SL (проверяется
> первым). Это консервативно и зеркалит live-outcome tracker.

---

## 4. Гейт лимита позиций (engine.py:507)

Новый сигнал генерируется **только если**:

```python
if len(open_trades) < config.max_active_signals:
    _open_risk = sum(t.risk_pct for t in open_trades)
    if _open_risk >= config.max_portfolio_risk_pct: continue
```

- `max_active_signals` — максимум одновременных открытых позиций (в `.env`
  = **5**).
- `max_portfolio_risk_pct` — потолок суммы риск% открытых позиций (default
  **3.0**). При риске ~1%/сделку это реально ограничивает **~3** одновременных.
- Это **глобальный** лимит на число позиций, не «на уровень». Повторные входы
  в один и тот же FVG на границе cooldown допустимы — каждый раз когда
  позиция закрылась, слот освобождается.

---

## 5. Режим (regime) и гейт компрессии (engine.py:518, 520)

- `_compute_regime(ind, atr_history, ema_spread_history, volume_history)` —
  использует production `RegimeDetector` (ADX, ATR-percentile, EMA-spread,
  volume). Возвращает `MarketRegime`.
- `block_compression_regime=True` (default):
  - `regime == "compression"` → сигнал отклонён;
  - `regime == "range"` и `ADX >= 26` и узкий диапазон (<1.5%) у середины →
    отклонён.

---

## 6. Детекция паттерна (Layer 1 — engine.py:543)

На окне `window` (до `candles_limit` свечей):

```python
sweeps         = detect_sweeps(df, lookback=50)
order_blocks   = detect_order_blocks(df, lookback=100)
candle_quality = analyze_last_candle(df, atr_value=ind.atr)
fvgs           = detect_fvg(df, lookback=config.liquidity_fvg_lookback or 100)
structure      = analyze_structure(df, lookback=50, sweeps=sweeps,
                                   displacement_atr=…, reclaim_bars=…, atr_value=…)
setup          = pattern_engine.detect(sweeps, order_blocks, structure,
                                       fvgs, candle_quality,
                                       current_price=ind.close, atr=ind.atr)
```

`pattern_engine.detect()` (`strategy/pattern_engine.py:160`):

- **Сначала REVERSAL**: sweep → displacement (инфо) → MSS (strong CHoCH).
  Требуется валидный sweep **и** `structure.last_mss`.
- **Если не найден — CONTINUATION**: trend ≠ ranging + BOS (aligned с трендом)
  + sweep в направлении, **противоположном** тренду (bullish-cont → bearish
  sweep).
- Затем `_detect_entry_zones` (OB/FVG — НЕ гейты, только маркеры) и
  `_check_entry_armed` (мягкий, не блокирует).
- `_validate_setup_quality` — гейт качества (`min_components_required=2`,
  `min_overall_quality`/`min_setup_confidence` из конфига pattern_engine).

**Гейты setup-type (engine.py:594, `enable_pattern_engine_gates=True`):**
- reversal: обязан иметь `has_sweep` + `has_mss`;
- continuation: обязан иметь `has_bos` + `has_sweep`.

---

## 7. HTF Bias V2 (engine.py:623)

- `config.htf_bias_v2=True` (default) — bias считается по W1→D1→H4→H1
  EMA21/55 + structure (`market_structure/htf_bias_v2.py`).
- **Local-путь:** bias пересчитывается на каждой свече на истории до `i`
  (без look-ahead): `df.iloc[:i+1]`, HTF-фреймы обрезаются до текущего ts.
- **Live-путь:** `_htf_once` — текущее состояние HTF (зеркалит live).
- `enable_htf_bias_gate=True` + `htf_opposition()`:
  - `hard_block` (continuation против bias и `config.htf_hard_gate=True`
    default) → сигнал отклонён (`reject_stats.htf_bias_blocked`);
  - `opposed` (иначе) → P(TP) умножается на `htf_bias_continuation_penalty`
    (default 0.85).

---

## 8. Trade Plan — entry, SL, TP (engine.py:656 → strategy/trade_engine.py)

`trade_engine.build_trade_plan(ind, direction, structure, order_blocks, sweeps, fvgs, df, timeframe)`:

### 8.1 Entry (`trade_engine.py:52-67`)

```python
entry = float(ind.close)                      # по умолчанию close бара
if fvgs:                                      # если есть активный FVG того же направления
    for f in fvgs:
        if f.is_active and f_dir == direction:
            entry = round((f.top + f.bottom) / 2.0, 8)   # медиана FVG
            break
```

> **Критично для честных метрик:** entry = **медиана активного FVG**, даже если
> текущая цена далеко от неё. `require_entry_zone=False` (default) — вход по
> FVG-медиане не проверяется на «дотянулась ли цена до уровня». Это даёт
> «фантомные филлы»: SELL «входит» по 13.5185, когда рынок уже на 12.2, и TP
> (рассчитанный от фантомного входа) пробивается на следующей свече. Именно
> так возникают стеки из 8 одинаковых SELL в один persistent FVG и завышенный
> winrate. Это же поведение у live-сканера (`scanner.py:484` `entry_price =
> float(trade_plan.entry_price)`) — паритет соблюдён, но допущение о филле
> нереалистично.

> **Модели исполнения (`config.execution_model`, см. `docs/decisions/execution_model.md`):**
> - `median_immediate` (default) — фантомный вход по медиане на баре сигнала
>   (описание выше). Только для совместимости, метрики НЕ публиковать как
>   «результаты стратегии».
> - `close` — вход по close бара сигнала; зеркалит live-P&L
>   (`outcome_tracker` считает от `signal.close_price`). Risk-гейт при этом видит
>   реальную цену → отсекает сигналы с RR < 2.0 (LINK 4h: risk_rej 2 → 106).
> - `limit_pending` — лимитка на медиане FVG; филл только на более позднем баре
>   при касании цены, истечение через `execution_pending_max_bars` (default 50),
>   per-FVG dedup (один сигнал → одна сделка на FVG). Risk-гейт видит медиану
>   (как и `median_immediate`), разница — только реалистичность филла.

### 8.2 SL — инвалидация (`trade_engine.py:106-191`)

`find_invalidation_buy/sell` (`strategy/invalidation.py`) — приоритет:

1. **Sweep extreme** (ближайший sweep low/high за entry, min 0.5% от entry);
2. **OB boundary** (граница ордерблока);
3. **Swing point** (фрактал);
4. **BOS level** (уровень break of structure);
5. **ATR fallback** (entry ± 1.5×ATR).

Затем SL = уровень ± `ATR * 0.15` (буфер). И SL-safety: если SL пробит текущей
свечой — SL отодвигается за wick свечи (spread + tick + ATR*5%).

### 8.3 TP — карта ликвидности (`trade_engine.py:274-370`)

`_find_targets` — из `build_liquidity_map` (equal levels, external liquidity,
sweeps, OB, FVG). Приоритет (type_bonus):

| Приоритет | Тип | Bonus |
|---|---|---|
| 1 | old_high / old_low (external liquidity) | 2.0 |
| 2 | equal_high / equal_low | 1.8 |
| 3 | ob_bearish / ob_bullish | 1.5 |
| 4 | fvg_bearish / fvg_bullish | 1.2 |
| 5 | swept_high / swept_low | 0.5 |
| 6 | ATR fallback | — |

- Минимальная дистанция TP = 0.5×ATR.
- Выбирается кандидат с макс. `strength * bonus`; `path_clear` — нет сильного
  встречного уровня между entry и TP.
- RR считается как `dist(tp)/dist(sl)`; `min_rr_threshold` (`.env` = **2.0**)
  — soft-предупреждение в TradePlan, жёстко решает Risk Engine.

---

## 9. Аналитика, direction/symbol-фильтры (engine.py:678-702)

- `atr_pct = atr / close * 100`.
- `config.direction_filter.block_all_sell` → SELL отклоняется.
- `apply_symbol_overrides()` (`strategy/signal_evaluator.py:109`) — per-symbol:
  `adx_min`, `max_sl_pct`, `max_atr_pct`, `min_quality`, `block_setup_types`.
  (В `.env` для ETH: adx_min 20, max_sl 3.0, max_atr 3.0, min_quality 70,
  `SELL_continuation` заблокирован.)

---

## 10. Вероятность (Layer 3 — engine.py:704)

`estimate_p_tp()` (`strategy/signal_evaluator.py:70`) — единый с live:

- baseline 0.45; +0.15 при ≥4 компонентах, +0.08 при ≥3;
- `context_score=0.0`, `mtf_aligned=False` (исторически недоступны);
- +0.05 если setup.direction совпадает с HTF-bias;
- +0.05 если `mss_score > 70`;
- умножение на `htf_penalty` (0.85 при opposed);
- clamp [0.15, 0.85]; `confidence = min(0.85, p_tp)`.

`enable_probability_gate=False` default (флаг A/B), `min_p_tp` в `.env` = 0.0
→ гейт выключен.

---

## 11. Risk Engine (Layer 4 — engine.py:722 → risk/engine.py)

`risk_engine.evaluate(PortfolioState(active_count, total_risk_pct, max_active_signals, max_portfolio_risk_pct), entry_price, sl, tp, atr_pct, p_tp, confidence, mss_quality, atr, sl_source)`:

- **Hard gate:** RR < `min_rr_ratio` (singleton построен из
  `config.trading.min_rr_threshold`, `.env` = 2.0) → `should_trade=False`.
  (Заметь: сам RiskEngine **не** проверяет `active_count`/`max_portfolio_risk_pct`
  внутри `evaluate` — эти гейты внешние, см. §4.)
- SL% вне [0.4, 5.0] для не-структурных источников — soft (log).
- **Sizing:** Kelly `f = (p*b - q)/b`, clamp [0, 0.20], ×confidence,
  ×vol_adj (ATR% >4→0.5, >2.5→0.75), ×mss_adj (0.8-1.1), ±SL-distance bonus;
  финальный clamp `[min_risk_pct=0.1, max_risk_pct=2.0]`.
- `risk_decision.should_trade=False` → сигнал отклонён. Иначе используется
  `sl_price`/`tp_price` из RiskDecision (те же, что пришли — RiskEngine их не
  двигает).

---

## 12. Dedup / cooldown (engine.py:752)

Единая функция `dedup_block_window()` (`strategy/signal_evaluator.py:43`),
same как live Phase 6:

- effective cooldown = `max(base_minutes, tf_minutes × multiplier)`
  (`get_cooldown_minutes`). В `.env`: `SIGNAL_COOLDOWN_MINUTES=240`,
  `SIGNAL_COOLDOWN_TF_MULTIPLIER=1.0`.
- **1h:** effective = max(240, 60×1.0) = **240 мин**.
- **4h:** effective = max(240, 240×1.0) = **240 мин** = ровно 1 свеча 4h.
- same-direction: блок, если elapsed < cooldown; cross-direction: блок при
  elapsed < cooldown/2.

> **Следствие:** на 4h effective cooldown = 240 мин = длина одной свечи → каждый
> следующий бар проходит dedup, и повторный вход в тот же persistent FVG
> возможен на каждой свече. Это не регрессия и не двойная вставка — это
> поведение cooldown при `multiplier=1.0` на 4h.

---

## 13. Фиксация сделки (engine.py:773)

`BacktestTrade` создаётся при прохождении всех гейтов; **ровно один** объект на
сигнал. При выходе (SL/TP/eob) он добавляется в `trades` один раз. Двойных
вставок нет — в бэктесте это подтверждено: ни у каких двух сделок нет общего
`entry_index`.

`last_signal_time`/`last_signal_direction` обновляются на каждом принятом
сигнале — это и есть источник cooldown-логики.

---

## 14. PnL и издержки (engine.py:813)

```python
gross    = (exit - entry)/entry * 100        # BUY
gross    = (entry - exit)/entry * 100        # SELL
total_cost = (fee_pct*2 + slip_pct*2) * 100  # комиссия×2 + slippage×2
funding  = -funding_rate_8h × periods        # каждый 8h-интервал удержания
net      = gross - total_cost + funding
```

- `.env`: `EXCHANGE_FEE_PCT`/`SLIPPAGE_PCT` default 0.05% каждая; funding
  `FUNDING_RATE_PCT_8H` default 0.01% (консервативно — всегда платим).
- `rr = |exit - entry| / |entry - sl|`.

---

## 15. Метрики (`_build_result`, engine.py:855)

| Метрика | Формула |
|---|---|
| winrate | wins / total × 100 |
| avg_pnl / avg_net_pnl | среднее gross / net |
| profit_factor | gross_profit / |gross_loss| |
| expectancy | `winrate × avg_win − (1−winrate) × avg_loss` |
| sharpe_ratio | `avg_net / std(net, ddof=1)` (per-trade) |
| sharpe_annualized | per-trade Sharpe × √(trades_per_year) |
| max_drawdown | max drawdown по **cumsum(net_pnl)** — честно, после издержек |
| max_drawdown_sized | DD по sizing-aware equity: `eq *= (1 + net×risk%/sl_dist%/100)` |
| exposure_time_pct | Σ duration / total_candles |
| avg_mfe/mae | средняя лучшая/худшая экскурсия |
| long/short/regime_stats | разбивка по направлениям и режимам |

---

## 16. Конфигурация, влияющая на бэктест (фактические значения)

| Параметр | Default | `.env` | Эффект |
|---|---|---|---|
| `candles_limit` | 200 | 200 | размер окна анализа на свечу |
| `max_active_signals` | 3 | **5** | макс. одновременных позиций |
| `max_portfolio_risk_pct` | 3.0 | (нет) 3.0 | потолок Σ risk% открытых |
| `signal_cooldown_minutes` | 45 | **240** | базовый cooldown |
| `signal_cooldown_tf_multiplier` | 2.0 | **1.0** | effective = max(base, tf×mult) |
| `min_rr_threshold` | 1.5 | **2.0** | RR hard gate (RiskEngine) |
| `htf_bias_v2` | true | (нет) | HTF Bias V2 вкл |
| `htf_hard_gate` | true | (нет) | continuation против bias → block |
| `htf_bias_continuation_penalty` | 0.85 | (нет) | множитель P(TP) при opposed |
| `premium_discount` | false | (нет) | ВЫКЛ (A/B: PF 1.28→0.91) |
| `require_entry_zone` | false | (нет) | ВЫКЛ → фантомные филлы по FVG-медиане |
| `execution_model` | `median_immediate` | (нет) | цена входа: медиана (фантом) / close / limit_pending (см. §18) |
| `execution_pending_max_bars` | 50 | (нет) | жизнь pending-ордера для `limit_pending` |
| `min_p_tp` | 0.45 | **0.0** | гейт P(TP) выключен |
| `block_compression_regime` | true | (нет) | блок compression-режима |
| `exchange_fee_pct` / `slippage_pct` | 0.05 | (нет) | издержки 0.1% на round-trip |
| `funding_rate_pct_8h` | 0.01 | (нет) | funding за каждый 8h-период |
| `atr_multiplier_sl` / `_tp` | 1.5 / 3.0 | 1.5/3.0 | ATR fallback SL/TP |
| `atr_multipliers_per_tf` | {} | `{"4h": {"sl": 2.5, "tp": 5.0}}` | per-TF ATR-множители |

---

## 17. Ключевые особенности поведения (чтобы не удивляться результатам)

1. **Паритет с live:** бэктест и сканер используют один `signal_evaluator`
   (P(TP), overrides, HTF-opposition, dedup) и один `trade_engine` (entry/SL/TP),
   окно = `candles_limit`. Результаты для одинаковых входов бит-в-бит совпадают.
2. **Multi-position:** лимит `max_active_signals` (5) — глобальный, не «на
   уровень». Стек из N сделок в один уровень = N **последовательных** входов,
   каждый после выхода предыдущего (при тестах: максимум одновременных 5, в
   стеках 13.5185 — максимум 2).
3. **Фантомный филл:** entry = медиана активного FVG даже при цене далеко от
   уровня (`require_entry_zone=False`). Это главный источник «нечестных»
   winrate/PF и одинаковых сделок (7.2560×3, 13.5185×8 и т.п.) — в реальности
   лимитный SELL по 13.5185 при рынке 12.2 не заполнился бы.
4. **Cooldown = 1 свеча на 4h:** effective 240 мин при multiplier=1.0 → каждый
   бар 4h проходит dedup. Повторные входы в один persistent unfilled FVG —
   ожидаемо.
5. **SL раньше TP** в одном баре — консервативно.
6. **`min_p_tp=0.0` и `enable_probability_gate=False`** — Probability Engine
   пока только сизинг, не селектор.
7. **Блокировка компрессии** включена (`block_compression_regime=true`).

---

## 18. Модели исполнения и multi-symbol результат (2026-08-18)

Подробно: `docs/decisions/execution_model.md`. Пулл 12 символов × 3 года
(2023-08-17 → 2026-08-18):

| tf | модель | trades | WR | exp/сделку |
|---|---|---|---|---|
| 4h | `median_immediate` | 3063 | 54.3 | +5.96 |
| 4h | `close` | 1781 | 18.1 | -0.47 |
| 4h | `limit_pending` | 1838 | 17.4 | -0.41 |
| 1h | `median_immediate` | 6731 | 46.7 | +2.54 |
| 1h | `close` | 4597 | 22.0 | -0.12 |
| 1h | `limit_pending` | 4795 | 21.2 | -0.23 |

**Вывод: вся edge стратегии существовала только в фантомном исполнении на
медиане FVG.** Две независимые реалистичные модели согласованно дают ~0 на
большом периоде. `median_immediate`-цифры использовать для выводов о стратегии
нельзя.

Уточнение «4-5 прибыльных из 12»: это пересечение моделей **внутри одного
таймфрейма** — 4h: {ADA, AVAX, DOGE, LINK}, 1h: {ADA, ATOM, DOGE, XRP} — а НЕ
пересечение по таймфреймам. Пересечение 4h∩1h в `limit_pending` — лишь
{ADA, DOGE} с exp≈0.03/0.004 (шум). Robustness-анализ IS/OOS показал, что эта
«прибыльность» нестабильна во времени (см. `docs/decisions/robustness_analysis.md`).