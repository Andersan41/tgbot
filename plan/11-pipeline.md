# Signal Pipeline — Complete Reference

> Актуальный справочник production-пайплайна `scan_symbol_v2()`.
> Date: 2026-08-14. Версия: `VERSION = "2.6.0"` (`config/settings.py:15`).

---

## 0. ARCHITECTURE

**Production pipeline:** `scan_symbol_v2()` in `scheduler/scanner.py:216`.
Old `scan_symbol()` retained for backward compatibility.

```
Pattern Engine → Feature Builder → Probability Engine → Risk Engine → Telegram
```

### Layer 1: Pattern Engine (`strategy/pattern_engine.py`)
- Pure ICT pattern detection — no indicators, no scoring.
- Setup = trigger (BOS or sweep) + confirmation (OB or FVG).
- Reversal: Sweep → Displacement → MSS → OB/FVG.
- Continuation: Trend → Pullback → BOS → retrace (вход в OB).
- `ICTSetup` dataclass: direction, setup_type, has_bos/has_sweep/has_mss/has_ob/has_fvg,
  entry_armed, mss_score, sl/tp_distance_pct, atr_pct, overall_setup_quality.

### Layer 2: Feature Builder (`strategy/feature_builder.py`)
- ~35 raw features в плоский вектор (`to_vector()`).
- Категории: ICT pattern, market structure, volume, indicators, MTF, context, risk.
- `to_reasoning()` — человекочитаемые факторы.
- Нет скоринга и блокировок — только данные.

### Layer 3: Probability Engine (`strategy/probability_engine.py`)
- `TradeProbability(p_tp, expected_rr, profit_factor, confidence, model_type)`.
- `quality_label`: strong ≥0.65 / moderate ≥0.50 / weak.
- Rules fallback (`_predict_rules`) — калиброванные таблицы PD×setup 2D.
- ML (XGBoost/RandomForest) заменяет правила после `PROBABILITY_MIN_SAMPLES_FOR_ML` (default 100) исходов.
- `estimate_scenario()` + `weight_manager.adjust()` (Rec 4b).

### Layer 4: Risk Engine (`risk/engine.py`)
- Hard gates: R:R ≥ 1.2, SL 0.25–5.0%, portfolio ≤3%, ≤3 active signals.
- Soft: Kelly sizing, volatility, SL distance.
- `RiskDecision`: should_trade, risk_pct, rr_ratio, rejection_reason.

---

## 1. ВОРОНКА ГЕЙТОВ (10 гейтов)

Канонический порядок — `storage/trace.py:31-35` и `scheduler/scanner.py:70-74`:

| # | Gate | Что проверяет | Fail → |
|---|------|---------------|--------|
| 1 | cooldown | `db.get_cooldown(symbol, tf)`; effective = max(base, tf×mult) | block |
| 2 | portfolio_risk | active_count ≤ max, portfolio_risk ≤ max | block |
| 3 | indicators | `_get_indicators` (EMA/RSI/MACD/ADX/ATR/Supertrend/volume) | block |
| 4 | pattern_engine | `pattern_engine.detect()` → ICTSetup.detected | block |
| 5 | structure_alignment | структура vs направление | block |
| 6 | sweep_required | reversal: sweep+MSS; continuation: BOS+sweep | block |
| 7 | regime_block | `_detect_regime` + блок compression-режима | block |
| 8 | sl_tp | SL/TP расчёт | block |
| 9 | risk_engine | `risk_engine.evaluate()` (RR, портфель, Kelly) | block |
| 10 | dedup | `get_last_signal` + cooldown | block |

Фазовые фильтры внутри `scan_symbol_v2`:

| Фильтр | Где | Условие |
|--------|-----|---------|
| direction/symbol | scanner.py ~373 | `config.direction_filter` (Rec 3) |
| news | scanner.py ~399 | `config.risk.news_filter_enabled` (Rec 4a, opt-in) |
| confluence-mode | scanner.py ~409 | `STRATEGY_MODE == "confluence"` |
| symbol overrides | scanner.py ~487 | `config.trading.symbol_overrides` (adx/sl/atr/quality/blocked types) |
| entry zone | scanner.py ~554 | `config.require_entry_zone` (opt-in) |
| HTF Bias V2 | scanner.py ~598 | `htf_bias_v2=True`; continuation против bias → block/penalty |
| Premium/Discount | scanner.py ~744 | `premium_discount=False` default |
| min P(TP) | scanner.py ~1257 | `min_p_tp=0.45` default (Probability selector) |

---

## 2. SCAN CYCLE

### `run_scan_cycle()` — scanner.py:1542
```
1. check_recent_losses()                       # circuit breaker
2. is_circuit_breaker_active()? → skip
3. symbols = get_active_symbols() − disabled
4. tfs = timeframes or config.trading.primary_timeframes
5. tasks = scan_symbol_v2(symbol, tf, notify, blocked) for each
6. asyncio.gather(*tasks, return_exceptions=True)
7. log signals_found / total
```

### `scan_symbol_v2()` — scanner.py:216
Фазы:
- Phase 1.1: cooldown → portfolio_risk → indicators
- Phase 1.2: pattern_engine.detect()
- Phase 1.35: direction/symbol filter → news filter → confluence-mode
- Phase 1.4: setup-specific gates (reversal/continuation) + symbol overrides
- Phase 1.44: SMT divergence (soft)
- Phase 1.45: HTF Bias V2 + Premium/Discount
- Phase 1.5: SL/TP (invalidation.py, risk/dynamic_risk.py structural)
- Phase 1.6: Hypothesis Engine (decision_engine, 12 narrative types)
- Phase 1.65: Scenario Engine (shadow) + weight_manager.adjust (Rec 4b)
- Phase 2: Feature Builder
- Phase 3: Probability Engine (predict)
- Phase 3.5: min P(TP) selector (opt-in)
- Phase 4: Risk Engine
- Phase 5: save_signal → create_outcome → set_cooldown → notify → metrics

---

## 3. ДАННЫЕ

- `data/exchange_client.py` — ccxt (sync в run_in_executor); `_symbol_map`:
  `BTC/USDT` → `BTC/USDT:USDT` для `market_type == "swap"`; `fetch_ohlcv` **дроп последней свечи**.
- `indicators/engine.py` — pandas-ta: `SUPERT_…`/`SUPERTd_…`, `ADX_…`/`DMP_…`/`DMN_…`.
- `market_structure/structure.py` — MSS: sweep ≤5 свечей + displacement ≥1 ATR + reclaim ≤2.
- `market_structure/htf_bias_v2.py` — W1→D1→H4→H1, `get_tf_bias` ≥55 баров, EMA21/55.
- `context/scorer.py` — ContextScore [-1,1], никогда не блокирует (фича для Probability Engine).

---

## 4. КОНТЕКСТ (не блокирует)

- `ContextVerdict` (legacy) — может BLOCKED.
- `ContextScore` (новый) — score, feeds Probability Engine.
- Кэш: TTL 30 мин; timeout 10 c; `context_enabled=true`.

---

## 5. ИСПОЛНЕНИЕ И ИСХОДЫ

- `save_signal` → execution snapshot (entry_price_source CLOSE, spread, atr, tick_size).
- `outcome_tracker.py`: SL/TP/EXPIRED; PnL после издержек (комиссия×2 + slippage×2 + funding).
- `scenario_memory.record_outcome` — ожидаемое vs наблюдаемое для ML/weight_manager.
- `circuit_breaker.py`: 3 HIT_SL → пауза 30 мин.