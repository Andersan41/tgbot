# Trading Signal Bot — Full Architecture (bot_claude.md)

Authoritative architecture map of the SMC/ICT Telegram signal bot: concept, system, data flow,
the complete filter/gate stack, and every weight that drives a decision. Reflects the code on
branch `tgbot-claude` (strategy `VERSION = "2.4.0"`, `config/settings.py:16`).

Scope note: the **live** path is `run_scan_cycle → scan_symbol_v2` (`scheduler/scanner.py`). The
legacy `strategy/signal_engine.py` and the configurable `ScoringConfig` weight model are **not** on
the live path (kept for backtests/compat); this is called out explicitly in the Weights section.

---

## 1. Concept — what the bot is trying to do

The bot trades **Smart Money Concepts (SMC) / Inner Circle Trader (ICT)** price-action setups, not
classic indicator crossovers. Indicators (EMA/RSI/MACD/ADX/ATR/Supertrend) exist only as *features*
and volatility/regime context — they no longer gate signals.

A tradeable setup is one of two archetypes (`strategy/pattern_engine.py`):

- **Reversal** — liquidity **sweep** of a prior high/low, then a **displacement** leg, then a
  **Market Structure Shift (MSS)** — a strong CHoCH confirming the reversal. Direction comes from the
  MSS. Order Blocks (OB) and Fair Value Gaps (FVG) are *entry zones*, not triggers.
- **Continuation** — an established non-ranging **trend** plus a **Break of Structure (BOS)** aligned
  with that trend. OB/FVG again mark the entry zone.

Higher-timeframe (HTF) bias (Weekly→Daily→H4→H1 EMA + structure) filters direction: continuations
against the HTF bias are gated (see §4), reversals against it are penalised.

Every candidate that survives the pattern + structure gates is scored for **P(TP)** (probability of
reaching take-profit), expected R:R and profit factor, then sized by a **Kelly-based Risk Engine**.

---

## 2. System architecture

### 2.1 Startup (`main.py`)
PID lock file → init async SQLite DB → `refresh_runtime_symbols()` + `reload_filter_toggles()` (DB
overrides of config) → connect exchange (ccxt, default **bingx**, `market_type=swap`) → optional web
dashboard → build `python-telegram-bot` Application (polling) → start `TaskScheduler` → launch
background `outcome_tracker_loop`. Prometheus metrics are opt-in (`METRICS_ENABLED`).

### 2.2 The four-layer pipeline (the design intent)
```
Pattern Engine  →  Feature Builder  →  Probability Engine  →  Risk Engine
(ICT detection)    (~35 raw features)   (P(TP), RR, PF)        (capital gates + Kelly sizing)
```
- **Layer 1 — Pattern Engine** (`strategy/pattern_engine.py`): pure ICT detection, no scoring.
- **Layer 2 — Feature Builder** (`strategy/feature_builder.py`): flattens everything into a
  `SetupFeatures` vector (§5).
- **Layer 3 — Probability Engine** (`strategy/probability_engine.py`): rules-based estimate today;
  swaps to an ML model (XGBoost/RandomForest) once ≥100–300 labelled outcomes exist.
- **Layer 4 — Risk Engine** (`risk/engine.py`): capital-protection hard gates + position sizing.

### 2.3 Top-level packages
| Package             | Role                                                                                  |
| ------------------- | ------------------------------------------------------------------------------------- |
| `scheduler/`        | `tasks.py` (APScheduler), `scanner.py` (live `scan_symbol_v2`), `outcome_tracker.py`  |
| `strategy/`         | pattern/feature/probability/trade engines + legacy `signal_engine` + shadow engines   |
| `market_structure/` | BOS/CHoCH/MSS, HTF bias V1 & V2, premium/discount, MTF alignment, distance filter      |
| `liquidity/`        | sweeps, order blocks, FVG, equal highs/lows, pools, OB state, candle quality           |
| `indicators/`       | EMA/RSI/MACD/ADX/ATR/Supertrend via pandas-ta                                          |
| `risk/`             | Risk Engine, regime detector, volatility regime, dynamic risk, news, no-trade zones    |
| `derivatives/`      | funding, open interest, BTC/ETH correlation, SMT divergence                            |
| `context/`          | macro/sentiment collector + scorer → `ContextScore` ∈ [-1, 1]                          |
| `storage/`          | async SQLAlchemy over SQLite + per-scan `DecisionTrace`                                 |
| `bot/`, `web/`      | Telegram handlers/notifier; aiohttp dashboard + WebSocket                              |
| `backtest/`         | live-parity backtest engine, funnels (incl. offline `funnel_offline.py`)               |

### 2.4 Data flow of one scan
`TaskScheduler._scan_job` → `run_scan_cycle()` iterates every `symbol × primary_timeframe` →
`scan_symbol_v2()` runs the gate chain (§3), builds features, scores probability, sizes risk, and on
success writes a signal + `DecisionTrace`, sends Telegram, sets cooldown. A per-cycle **funnel**
(`_FunnelCounter`) records where each candidate died.

---

## 3. The live gate chain (`scan_symbol_v2`, in execution order)

Each gate returns `None` (kills the candidate) on failure unless marked SOFT. "Toggle" is the config
flag that controls it (— = always on / no toggle).

| # | Gate                | Type            | Toggle / config                                  | What it checks |
| - | ------------------- | --------------- | ------------------------------------------------ | -------------- |
| 1 | cooldown            | BLOCK           | `signal_cooldown_minutes` × `_tf_multiplier`     | per symbol+TF window, DB-backed |
| 2 | portfolio_risk      | BLOCK           | `max_active_signals` (3), `max_portfolio_risk_pct` (3%) | too many / too risky open signals |
| 3 | indicators          | BLOCK           | —                                                | OHLCV/indicator availability |
| 4 | pattern_engine      | BLOCK           | —                                                | a reversal or continuation setup exists |
| 5a| sweep_required      | BLOCK (reversal)| —                                                | reversal has a valid sweep |
| 5b| displacement_gate   | BLOCK (reversal)| `reversal_require_displacement` (**new**, def on)| current candle is a displacement candle |
| 5c| mss_gate            | BLOCK (reversal)| —                                                | strong CHoCH / MSS present |
| 5d| bos_gate            | BLOCK (contin.) | —                                                | BOS present and trend-aligned |
| 6 | entry_zone          | SOFT / BLOCK    | `require_entry_zone` (**new**, def off→soft)     | price inside OB/FVG zone (else chases at close) |
| 7 | SMT divergence      | SCORE (feature) | `derivatives.smt_enabled`                        | cross-asset divergence → feature |
| 8 | htf_bias            | BLOCK or PENALTY| `htf_bias_v2` (picks V2/V1), `htf_hard_gate` (**wired**, def on) | continuation vs HTF bias |
| 9 | premium_discount    | SCORE (mult.)   | `premium_discount` (def **off**)                 | fib zone → P(TP) multiplier |
| 10| sl_tp               | BLOCK           | —                                                | trade plan yields SL and TP |
| 11| market phase        | SHADOW          | —                                                | never blocks (evaluation only) |
| 12| thesis / scenario   | SHADOW          | —                                                | never blocks (sets `_decision`) |
| 13| regime + vol_regime | SCORE (feature) | —                                                | two detectors, feature-only |
| 14| mtf_alignment       | ANALYTICS       | `market_structure.mtf_enabled`                   | "not a gate" (comment in code) |
| 15| context             | SOFT (score)    | `context_enabled`                                | ContextScore feature; verdict discarded |
| 16| probability         | BLOCK           | `min_p_tp` (**new**, def 0.0 = off)              | P(TP) below floor |
| 17| risk_engine         | BLOCK           | — (RiskEngine limits)                            | R:R, SL bounds, SL-vs-ATR |
| 18| entry_trigger       | BLOCK (cond.)   | — (only if shadow `_decision` set)               | price in hypothesis entry zone |
| 19| dedup               | BLOCK           | `signal_cooldown_minutes` × `_tf_multiplier`     | same/opposite-direction recent signal |

Bold toggles are the Phase-3 signal-recovery flags added on this branch (all default to the prior
behavior). See §6.

**Cooldown math** (`get_cooldown_minutes`): `max(base_minutes, tf_minutes × multiplier)`. With
defaults (base 45, mult 2.0): 1h → 120 min, 4h → 480 min, per symbol+TF. Enforced twice — gate 1
(entry) and gate 19 (dedup).

**Fail modes:** most gates fail *open* (an exception skips the gate, candidate proceeds); the hard
data/pattern gates fail *closed*.

---

## 4. Filtering — hard vs soft, and where the "conflicts" live

**Hard gates (can kill a signal):** cooldown, portfolio_risk, indicators, pattern_engine, the
setup-type gates (sweep/displacement/mss/bos), htf_bias (continuation, when `htf_hard_gate`),
entry_zone (when `require_entry_zone`), sl_tp, probability (when `min_p_tp>0`), risk_engine,
entry_trigger, dedup.

**Soft / feature-only (never kill):** SMT divergence, premium/discount zone, regime, volatility
regime, MTF alignment, context score. These adjust P(TP) or sizing, not pass/fail.

**HTF bias detail** (`scheduler/scanner.py`, both V2 and V1 branches):
- Continuation vs HTF bias mismatch → **hard block** if `htf_hard_gate` (default), else P(TP) ×
  `htf_bias_continuation_penalty` (0.85) and pass.
- Reversal vs HTF bias mismatch → always a penalty (× 0.85), never a block.
- HTF neutral → no effect.

**Known structural issues (from the diagnosis, still present unless a flag is flipped):**
- All-hard-gate AND-chain: the Probability Engine historically did **not** gate (only sized); the new
  `min_p_tp` flag lets it select.
- `displacement_gate` requires the *current* candle be a displacement candle even though the pattern
  engine treats displacement as informational — the `reversal_require_displacement` flag disables it.
- Config toggles that don't reach the live path: `no_trade_zones_enabled`, `distance_filter_enabled`,
  `news_filter_enabled` (stub), `require_ob_or_fvg` — enabled in config but never called by
  `scan_symbol_v2`. Tuning them has no live effect.
- `_FUNNEL_GATES` still advertises `structure_alignment` and `regime_block`, which the live path no
  longer runs.

---

## 5. The feature vector (`SetupFeatures`, `strategy/feature_builder.py`)

~35 fields flattened for the Probability Engine. Grouped:

- **ICT pattern:** `setup_type`, `has_bos`, `has_sweep`, `has_ob`, `has_fvg`, `has_displacement`,
  `components_count`, `has_mss`, `mss_score` (0–100), `mss_causality` (0–1), `displacement_atr_ratio`,
  `sweep_to_mss_bars`, `sweep_reclaim_speed`, `sweep_strength` (0–1), `ob_distance_pct`, `fvg_size_pct`,
  `entry_armed`.
- **Structure:** `structure_trend`, `structure_bos_aligned`, `is_reversal`.
- **Volume:** `volume_ratio` (vol/SMA), `volume_delta_pct`, `volume_above_avg`.
- **Volatility / regime:** `atr`, `atr_pct`, `regime`.
- **Indicators (features only):** `rsi`, `adx`, `ema_spread_pct`, `dmi_diff`, `macd_hist_pct`.
- **MTF:** `mtf_aligned`, `mtf_htf_count`, `is_4h_aligned`.
- **Context / derivatives:** `fear_greed`, `funding_rate`, `context_score` (−1..1),
  `smt_divergence_score` (−1..1).
- **Trade geometry:** `rr_ratio`, `sl_distance_pct`, `tp_distance_pct`,
  `nearest_support_pct`, `nearest_resistance_pct`.
- **Session / timing:** `session`, `candle_close_pct`.
- **Soft multipliers:** `htf_alignment_score` (0..1|None), `premium_discount_score` (0..1|None),
  `htf_bias_penalty` (0.85|1.0), `ob_state_multiplier` (0.0 broken → 0.6–1.2).

`to_vector()` emits the numeric dict consumed by the (future) ML model; `to_reasoning()` builds the
human-readable reason list shown in Telegram.

---

## 6. Weights — what actually drives a live decision

There are **two** weight systems in the repo. Only the first is live.

### 6.1 LIVE — Probability Engine rules (`strategy/probability_engine.py::_predict_rules`)
A rules bootstrap (equal-weight edges, explicitly "not hand-tuned", to be replaced by ML). Starts
from a base winrate (`historical_winrate` or 50.0) and adds edge points:

Reversal core: sweep **+3.0**, displacement **+3.0**, MSS **+4.0** (strongest); MSS quality ≥70
**+2.0**, 50–70 **+1.0**. Confirmation: OB **+1.5**, FVG **+1.0**, entry_armed **+1.5**.
Continuation core: BOS **+3.0**, structure aligned **+2.0**; same OB/FVG/entry_armed bonuses.

Common edges (both types):
| Factor    | Rule → edge |
| --------- | ----------- |
| structure | bos_aligned +2.0 |
| volume    | >2.0×: +3.0 · >1.5×: +2.0 · >1.2×: +1.0 |
| R:R       | ≥3.0: +4.0 · ≥2.0: +3.0 · ≥1.5: +1.5 · <1.0: −3.0 |
| MTF       | aligned +2.0 |
| session   | overlap/london/new_york +1.0 |
| ATR%      | 1–3%: +1.5 · >5%: −2.0 · <0.5%: −1.5 |
| context   | >0.3: +1.0 · <−0.3: −1.5 |

Soft multipliers (each maps a [0,1] score to a [0.5,1.0] factor, so context influences but never
kills): `× (0.5 + 0.5·htf_alignment_score)`, `× (0.5 + 0.5·premium_discount_score)`,
`× htf_bias_penalty`, `× ob_state_multiplier` (0.0 broken OB → **signal rejected**, P(TP)=0).

Final: `winrate = clamp(20, 85, (base + Σ edges) × Σ multipliers)`; `p_tp = winrate/100`;
`expected_rr = rr_ratio × p_tp × 1.1`; `profit_factor = (p·expected_rr) / max(q, 0.01)`. Rules-mode
`confidence = 0.4`. Quality label: p_tp ≥0.65 strong, ≥0.50 moderate, else weak. ML mode caps p_tp at
0.85 and confidence at 0.80.

### 6.2 Risk Engine sizing (`risk/engine.py`)
Hard gates first: `rr < 1.5` reject; `sl_distance_pct` outside [0.25%, 5.0%] reject; SL below
`2.0 × ATR%` reject. Then Kelly sizing:
`kelly = (p·b − q)/b`, capped at 0.20 (half-Kelly), `× confidence`; `risk_pct = min(kelly·100,
base_risk 1.0%)`, then multiplied by scenario (0.6–1.2), stability (0.7–1.15), volatility
(ATR%>4 → 0.5, >2.5 → 0.75), MSS quality (0.8–1.1), SL-tightness (<1% → ×1.1, >3% → ×0.8), finally
clamped to [`min_risk_pct` 0.1%, `max_risk_pct` 1.0–2.0%].

### 6.3 LEGACY / NOT LIVE — `ScoringConfig` weighted model (`config/settings.py`)
Configurable integer weights used by the legacy `signal_engine` and `confidence_v2`, **not** by
`scan_symbol_v2`. Kept for backtests/compat; the dead-code audit confirms `max_signal_score` (their
only consumer) is never called live. For reference:
- Weighted factor model: `w_supertrend 5, w_ema 10, w_macd 10, w_rsi 5, w_volume 15, w_adx 5,
  w_dmi 5, w_bos 15, w_sweep 10, w_ob 10, w_btc 10, w_funding 5, w_oi 10`.
- Confidence V2 (10-factor): `w_htf_trend 20, w_structure 15, w_liquidity 20, w_conf_volume 5,
  w_btc_corr 15, w_conf_funding 5, w_conf_oi 5, w_conf_rsi 5, w_conf_macd 5, w_conf_adx 5`.
- Blends: `tech_confidence_blend 0.6`, `market_confidence_blend 0.4`, `historical_wr_blend 0.4`.
- Verdict thresholds: strong/moderate confidence 65/40, quality 65/30, `min_score_for_signal 2`.

---

## 7. Configuration & feature flags

`config/settings.py` — nested `@dataclass` configs read from `.env`, exposed as the `config`
singleton. `VERSION` bumps on every logic change. Hot-reload: `reload_config()` re-reads `.env`;
`reload_filter_toggles()` applies DB overrides (`filter:toggle:<k>` / `filter:param:<k>`).

Key defaults: default exchange **bingx**, `market_type=swap`; `htf_bias_v2` **on**;
`premium_discount` **off** (hurt PF in A/B).

**Phase-3 signal-recovery flags (this branch — all default to prior behavior):**
| Flag                             | Default | Effect when flipped |
| -------------------------------- | ------- | ------------------- |
| `htf_hard_gate`                  | `true`  | `false` → continuation vs HTF bias becomes a P(TP) penalty, not a block |
| `htf_bias_continuation_penalty`  | `0.85`  | penalty magnitude when `htf_hard_gate=false` |
| `reversal_require_displacement`  | `true`  | `false` → drop the current-candle displacement requirement for reversals |
| `require_entry_zone`             | `false` | `true` → only emit when price is in the OB/FVG zone (stop chasing at close) |
| `min_p_tp`                       | `0.0`   | `>0` → Probability Engine drops setups below the P(TP) floor |

Runtime-tunable set: only 4 booleans (`context`, `confidence_v2`, `signal_block`, `dynamic_risk`) plus
~41 numeric params were DB-tunable; the Phase-3 flags were added to `FILTER_TOGGLE_KEYS` /
`FILTER_PARAM_KEYS` so they can be tuned without a restart.

---

## 8. Scheduling & lifecycle

`scheduler/tasks.py`: `scan_all_tfs` on cron `minute=config.scheduler.scan_minutes` (default
`2,17,32,47` → 4×/hour) over all `primary_timeframes`; daily report at 00:05 UTC. `/scan` (admin)
triggers a cycle manually. `outcome_tracker_loop` follows open signals to TP/SL for post-trade
labelling (feeds the future ML model).

---

## 9. Persistence, observability, delivery

- **DB** (`storage/database.py`): async SQLAlchemy/SQLite (`data/signals.db`). Tables for signals,
  cooldowns, settings, and `DecisionTrace` (per-scan gate results + feature snapshot + config
  snapshot + gate_path). Trace columns cover the known gates; unknown gate names (e.g. the new
  `entry_zone`/`probability`) are captured via `final_stage`/`blocked_reason`, not dedicated columns.
- **Funnel** (`_FunnelCounter`): per-cycle blocked-by counts, logged as a summary line.
- **Metrics**: Prometheus (opt-in).
- **Delivery**: Telegram channel (HTML — every dynamic substring must be `html.escape()`d), plus the
  aiohttp web dashboard with live WebSocket signal pushes.

---

## 10. Backtesting & analytics

- `backtest/engine.py` — single engine targeting live parity (regime, structural SL/TP, buffers, RR
  filter, fees/slippage). Presets incl. `full_new`.
- `backtest/run_new_pipeline.py` — exercises the v2 pipeline (pattern→feature→probability→risk) with
  live-fetched data; tracks `rejection_reasons` incl. `htf_bias_*`.
- `backtest/funnel_offline.py` (this branch) — offline gate-block funnel over cached 1h OHLCV
  (`reports/abn/ohlcv_cache/`), HTF resampled from 1h; honors the Phase-3 flags for offline A/B.
- `analytics/` — daily/full reports, calibration, drift, gate funnels. `reports/` holds prior
  experiment output, cached parquet, and the `system_map/` architecture docs.

---

## 11. Data-integrity gotchas

- `exchange_client.fetch_ohlcv` drops the last (open) candle (`df.iloc[:-1]`) to avoid signalling off
  an unclosed bar.
- pandas-ta column names are resolved dynamically in `indicators/engine.py`; verify prefixes after a
  pandas-ta upgrade.
- Context fetcher module-level singletons cache state; tests should use fresh instances.
- `premium_discount`: fib is measured from swing low (0.0) to high (1.0) — `≤0.3` = DISCOUNT (near
  low), `≥0.7` = PREMIUM (near high). (The docstring was previously inverted; fixed on this branch.)

---

*Cross-references: `AGENTS.md` (quick reference), `CLAUDE.md` (working guidance),
`reports/system_map/SYSTEM_ARCHITECTURE.md` and `reports/system_map/*` (deep dives, gate/conflict
maps), `plan/` (design notes).*
