# Prompt for Senior Model Review

## Context

You are reviewing a crypto trading bot (Telegram + Binance Futures) that uses ICT (Inner Circle Trader) methodology.
The bot scans symbols on 1h/4h timeframes every 15 minutes and sends signals to a Telegram channel.

**Repository:** https://github.com/Andersan41/telebot
**Branch:** main (current production)

## What happened

The bot has been running live since July 2026. It produces signals but is deeply unprofitable:
- **143 signals over 2 months, cumulative PnL: -126.12%**
- Win rate 35.3%, Profit Factor 0.61 — system loses money on every timescale
- The funnel is paradoxical: `portfolio_risk` blocks 96% of candidates, yet when signals DO get through, they're mostly losers
- The bot needs structural fixes, not just threshold tuning

---

## I. Strategy: What the Bot Trades

The bot uses **ICT (Inner Circle Trader)** methodology — a price-action approach that identifies institutional order flow through specific market structure patterns. No traditional indicators (RSI, MACD, etc.) are used for entry decisions.

### Core ICT Concepts

**1. Market Structure (Trend Detection)**
- Analyzes swing highs/lows to determine trend: `bullish` (higher highs, higher lows), `bearish` (lower highs, lower lows), or `ranging`
- **BOS (Break of Structure):** Price breaks a recent swing high (bullish BOS) or swing low (bearish BOS), confirming trend continuation
- **MSS (Market Structure Shift):** Price breaks structure in the OPPOSITE direction of the current trend, signaling a potential reversal. MSS requires a strong CHoCH (Change of Character)

**2. Liquidity Sweeps**
- "Liquidity" = clusters of stop-loss orders resting above swing highs or below swing lows
- A **sweep** occurs when price briefly exceeds a swing high/low to trigger those stops, then reverses
- Sweep types: `bullish_sweep` (sweeps below a low), `bearish_sweep` (sweeps above a high)
- Sweep quality measured by wick ratio: `(wick_size / candle_range)`. Minimum wick: 0.01% of price

**3. Order Blocks (OB)**
- The last opposing candle before a strong move. Represents institutional accumulation
- **Bullish OB:** Last bearish candle before a bullish impulse. Price retracing to OB zone = buy opportunity
- **Bearish OB:** Last bullish candle before a bearish impulse. Price retracing to OB zone = sell opportunity
- OB zone: candle body (open-to-close range). Proximity check: price within 2.0% of OB midpoint

**4. Fair Value Gaps (FVG)**
- Three-candle pattern where the middle candle's range doesn't overlap with candle 1 and candle 3
- Represents an imbalance — price tends to return to fill the gap
- **Bullish FVG:** Gap between candle 1 high and candle 3 low (price moved up too fast)
- **Bearish FVG:** Gap between candle 1 low and candle 3 high (price moved down too fast)
- FVG max age: 4 candles. Entry zone: price must touch the FVG median

### Three Setup Types

**Reversal Setup** (Sweep + MSS):
```
1. Price sweeps a swing high/low (liquidity grab)
2. Price reverses and breaks structure (MSS / CHoCH)
3. MSS candle_index must be > sweep candle_index (causality: sweep happens first)
4. Sweep direction must match MSS direction (bullish sweep → bullish MSS for buy)
```

**Continuation Setup** (BOS + Trend):
```
1. Market is trending (not ranging)
2. BOS occurs in trend direction (bullish BOS in uptrend, bearish BOS in downtrend)
3. BOS type must match trend (bullish BOS + bullish trend)
4. Sweep is optional (counter-trend sweep adds quality)
```

**POI Entry Setup** (Order Block / FVG proximity):
```
1. Market is trending (not ranging)
2. Price is near an Order Block or FVG aligned with trend
3. For buy: price ≤ OB midpoint × 1.01 (within 1% above)
4. For sell: price ≥ OB midpoint × 0.99 (within 1% below)
5. FVG must be active, match direction, within 4 candles old
```

### Signal Output

When a setup is detected, the bot outputs:
- **Direction:** BUY or SELL
- **Setup type:** reversal / continuation / poi_entry
- **Components detected:** sweep, MSS, BOS, OB, FVG (each present/absent)
- **Components count:** total number of ICT components found (0-5)
- **Quality scores:** per-component quality (0-100) and overall quality (weighted average)
- **Entry price:** candle close (or live price after alignment)
- **SL (Stop Loss):** structural invalidation level ± ATR buffer
- **TP (Take Profit):** best liquidity target from LiquidityMap (OB, FVG, swing high/low)
- **P(TP):** estimated probability of hitting TP (rule-based formula)
- **Risk %:** Kelly-sized position (0.1% - 1.0% of capital)

---

## II. Real Bot Stats

### All-Time (from `data/signals.db` — 2 months, 143 signals)

| Metric | Value |
|--------|-------|
| Total signals | 143 |
| Closed trades | 139 |
| Open trades | 4 |
| **Win Rate** | **35.3%** (49 wins / 90 losses) |
| **Cumulative PnL** | **-126.13%** |
| Profit Factor | 0.61 |
| Best trade | +24.17% |
| Worst trade | -12.29% |
| Avg trade duration | 41.7 hours |

### By outcome:

| Outcome | Count | Avg PnL% | Total PnL% |
|---------|-------|----------|------------|
| HIT_SL | 81 | -3.19% | -258.53% |
| HIT_TP | 37 | +3.21% | +118.90% |
| MANUAL_CLOSE | 12 | +1.12% | +13.50% |
| EXPIRED | 9 | 0.00% | 0.00% |

### By direction:

| Direction | Signals | Avg PnL% |
|-----------|---------|----------|
| BUY | 76 | **+0.11%** |
| SELL | 63 | **-2.14%** |

### By timeframe:

| TF | Signals | Avg PnL% |
|----|---------|----------|
| 4h | 80 | -1.09% |
| 1h | 59 | -0.66% |

### Live Funnel (from `logs/bot.log` — 250 cycles, Sep 8-14):

| Metric | Value |
|--------|-------|
| Total scan cycles | 250 |
| Cycles with 0 signals | 245 (98%) |
| Total signals emitted | 8 |
| **portfolio_risk blocks** | **~96%** of all candidates |

---

## III. Complete Gate Pipeline — How Each Filter Works

Data flows through the pipeline sequentially. Each gate receives the output of the previous gate.
A BLOCK at any gate = candidate rejected, pipeline stops.

```
For each symbol × timeframe:
│
│  ┌─────────────────────────────────────────────────────────────┐
│  │ PHASE 0: CAPITAL PROTECTION (before any analysis)           │
│  └─────────────────────────────────────────────────────────────┘
│
├─ GATE 1: Cooldown ─────────────────────────────────────────────
│  Input: symbol, timeframe
│  Logic: DB query → last signal time for this symbol+TF
│  Threshold: effective = max(45 min, TF_minutes × 2.0)
│             e.g. 1h → max(45, 120) = 120 min
│             e.g. 4h → max(45, 480) = 480 min (8 hours)
│  Pass: enough time has passed since last signal
│  Block: "cooldown Xm active"
│
├─ GATE 2: Portfolio Risk ★ #1 KILLER ────────────────────────────
│  Input: symbol (atomic lock)
│  Logic: 3 sequential checks:
│    (a) active_signals_for_this_symbol >= max_active_per_symbol (1)
│    (b) total_active_signals >= max_active_signals (10)
│    (c) sum_of_active_risk% >= max_portfolio_risk_pct (3.0%)
│  Pass: ALL 3 checks pass
│  Block: any check fails → "max active (X/Y)" or "portfolio risk X% >= 5%"
│  ★ This blocks ~96% of candidates in live operation
│
├─ GATE 3: Indicators ───────────────────────────────────────────
│  Input: symbol, timeframe
│  Logic: fetch_ohlcv() + indicator_engine.calculate()
│  Pass: both return valid non-empty data
│  Block: "OHLCV/indicator unavailable"
│
├─ GATE 4: Compression Regime ────────────────────────────────────
│  Input: MarketRegime object, ADX value
│  Logic (2 conditions):
│    (a) ATR percentile < 20th percentile → "compression regime"
│    (b) range < 1.5% AND ADX >= 26 AND price within 0.3% of midpoint
│        → "high ADX in tight range"
│  Pass: market is volatile enough for trading
│  Block: low volatility / tight range = no edge
│
│  ┌─────────────────────────────────────────────────────────────┐
│  │ PHASE 1: PATTERN ENGINE (ICT setup detection)              │
│  └─────────────────────────────────────────────────────────────┘
│
├─ GATE 5: Pattern Engine ────────────────────────────────────────
│  Input: sweeps, order_blocks, structure, fvgs, candle_quality, price, atr, df
│  Logic: tries 3 paths sequentially:
│
│    PATH A — Reversal:
│      1. At least one valid sweep (sweep.is_valid == True)
│      2. MSS exists (structure.last_mss is not None)
│      3. MSS candle_index > sweep candle_index (causality)
│      4. Sweep type matches MSS direction
│      → If any step fails: rejection_reason set
│
│    PATH B — Continuation:
│      1. Trend != "ranging"
│      2. BOS exists (structure.last_bos is not None)
│      3. BOS type matches trend direction
│      → If any step fails: rejection_reason set
│
│    PATH C — POI Entry (if enabled):
│      1. Trend != "ranging"
│      2. Price within 2.0% of OB midpoint (aligned with trend)
│      3. OR active FVG within 4 candles (aligned with direction)
│      → If no OB/FVG found: rejection_reason set
│
│    After detection: quality validation
│      - min_components_required = 2 (default)
│      - min_overall_quality check
│      - min_setup_confidence check
│
│  Pass: valid setup detected with enough components
│  Block: "no ICT setup" + specific rejection_reason
│
│  ┌─────────────────────────────────────────────────────────────┐
│  │ PHASE 1.35: DIRECTION & SYMBOL FILTERS                     │
│  └─────────────────────────────────────────────────────────────┘
│
├─ GATE 6: Direction Filter ──────────────────────────────────────
│  Input: setup.direction
│  Logic:
│    (a) config.block_all_sell == True AND direction == "sell" → BLOCK
│    (b) symbol in blocked_symbol_directions[symbol] → BLOCK
│  Pass: direction not blocked
│  Block: "direction blocked"
│
├─ GATE 7: Symbol Filter ────────────────────────────────────────
│  Input: symbol
│  Logic: per-symbol override config (e.g. "WIF/USDT: block BUY")
│  Pass: symbol not blocked
│  Block: "symbol blocked: {symbol} {direction}"
│
├─ GATE 8: Confluence Mode ───────────────────────────────────────
│  Input: setup_type, has_ob
│  Logic (only when STRATEGY_MODE == "confluence"):
│    (a) reversal → BLOCK (WR 3.6% historically)
│    (b) continuation without OB → BLOCK (WR 93.7% with OB vs 44.6% without)
│  Pass: setup meets confluence criteria
│  Block: "confluence: reversal blocked" or "no OB for continuation"
│
│  ┌─────────────────────────────────────────────────────────────┐
│  │ PHASE 1.4: SETUP-TYPE-SPECIFIC GATES                       │
│  └─────────────────────────────────────────────────────────────┘
│
├─ GATE 9: Sweep Required (reversal only) ───────────────────────
│  Input: setup.has_sweep
│  Block: "reversal: no sweep"
│
├─ GATE 10: MSS Gate (reversal only) ────────────────────────────
│  Input: setup.has_mss
│  Block: "reversal: no MSS (strong CHoCH)"
│
├─ GATE 11: BOS Gate (continuation only) ────────────────────────
│  Input: setup.has_bos
│  Block: "continuation: no BOS"
│
├─ GATE 12: POI Gate (poi_entry only) ───────────────────────────
│  Input: setup.has_ob, setup.has_fvg
│  Block: "poi_entry: no OB or FVG"
│
│  ┌─────────────────────────────────────────────────────────────┐
│  │ PHASE 1.5: TRADE PLAN (SL/TP calculation)                  │
│  └─────────────────────────────────────────────────────────────┘
│
├─ GATE 13: SL/TP Calculation ────────────────────────────────────
│  Input: indicators, direction, structure, OBs, sweeps, FVGs, df, timeframe
│  Logic:
│    Entry: candle_close (or FVG median if price is in FVG zone)
│    SL: structural invalidation level (swing low/high, OB edge, BOS level)
│        ± buffer (15% of ATR). If SL is inside candle wick, pushed beyond wick.
│    TP: LiquidityMap scans targets (OB, FVG, swing high/low, equal highs/lows)
│        Each scored by type (swing=2.0, equal=1.8, OB=1.5, FVG=1.2)
│        Min distance = 0.5 ATR. Path clarity checked (no opposing OB/FVG).
│        Fallback: ATR × atr_tp multiplier.
│  Pass: sl and tp are not None
│  Block: "SL/TP calculation failed"
│
├─ GATE 14: Live Price Alignment ─────────────────────────────────
│  Input: live ticker price
│  Logic: if offset > 0.1% from candle close → rebuild trade plan with live entry
│  Pass: recalculation succeeds
│  Block: "SL/TP recalculation failed"
│
├─ GATE 15: Direction Check ──────────────────────────────────────
│  Input: entry_price, live_price, direction
│  Logic:
│    BUY: entry_price > live_price × 1.001 → BLOCK (entry too high)
│    SELL: entry_price < live_price × 0.999 → BLOCK (entry too low)
│  Threshold: 0.1% tolerance
│
│  ┌─────────────────────────────────────────────────────────────┐
│  │ PHASE 1.46-1.47: OVERRIDES & ENTRY ZONE                    │
│  └─────────────────────────────────────────────────────────────┘
│
├─ GATE 16: Symbol Overrides ─────────────────────────────────────
│  Input: symbol config overrides
│  Logic: per-symbol checks:
│    (a) ADX < adx_min → BLOCK
│    (b) SL distance > max_sl_pct → BLOCK
│    (c) ATR% > max_atr_pct → BLOCK
│    (d) quality < min_quality → BLOCK
│    (e) setup type in block_setup_types → BLOCK
│  Pass: all per-symbol checks pass
│
├─ GATE 17: Entry Zone ───────────────────────────────────────────
│  Input: config.require_entry_zone, FVG list, candle high/low
│  Logic:
│    If require_entry_zone=True:
│      BUY: bar_low must touch at least one FVG top
│      SELL: bar_high must touch at least one FVG bottom
│    If require_entry_zone=False: soft check only (log, don't block)
│  Pass: price touched FVG zone (or check disabled)
│  Block: "price not in FVG entry zone"
│
│  ┌─────────────────────────────────────────────────────────────┐
│  │ PHASE 1.45: HTF BIAS (Higher Timeframe Alignment)          │
│  └─────────────────────────────────────────────────────────────┘
│
├─ GATE 18: HTF Bias V2 ─────────────────────────────────────────
│  Input: df_1w, df_1d, df_4h, df_1h
│  Logic:
│    1. Each TF: compute EMA21 and EMA55
│       - bullish: price > ema21 > ema55
│       - bearish: price < ema21 < ema55
│       - neutral: mixed
│    2. Majority vote: W1 + D1 + H4 → ≥2 bullish = bullish, ≥2 bearish = bearish
│    3. Override rules:
│       - W1 conflicts but D1==H4==direction → override W1
│       - H4 conflicts but W1==D1==direction → pullback (still pass)
│    Gate logic:
│      - continuation + HTF mismatch + hard_gate=True → BLOCK
│      - continuation + mismatch + hard_gate=False → PASS with penalty
│      - reversal mismatch → PASS with penalty (never blocks)
│      - neutral → PASS, no penalty
│  Pass: aligned with HTF, or reversal, or hard_gate disabled
│  Block: "continuation {dir} vs HTF {bias}"
│
│  ┌─────────────────────────────────────────────────────────────┐
│  │ PHASE 4: RISK ENGINE (final validation)                    │
│  └─────────────────────────────────────────────────────────────┘
│
├─ GATE 19: Portfolio Risk Recheck ───────────────────────────────
│  Input: fresh DB query (may have changed during pipeline)
│  Logic: same as Gate 2 (active count + risk sum)
│  Block: "max active [re-check]" or "portfolio risk X% [re-check]"
│
├─ GATE 20: Risk Engine ──────────────────────────────────────────
│  Input: portfolio state, entry, sl, tp, atr%, p_tp, confidence, mss_quality
│  Hard gates (BLOCK):
│    (a) entry/sl/tp <= 0 → "invalid price data"
│    (b) |entry - sl| <= 0 → "zero risk distance"
│    (c) |tp-entry| / |entry-sl| < min_rr_ratio (1.5) → "RR X < 1.5"
│  Soft checks (log only, NOT blocked):
│    - SL distance < 0.25% → "tight SL"
│    - SL distance > 5.0% and not structural → "wide SL"
│  Position sizing (Kelly):
│    kelly = (p_tp × rr - (1-p_tp)) / rr
│    kelly = clamp(kelly, 0, 0.20)  # half-Kelly
│    kelly *= confidence
│    risk% = min(kelly × 100, 1.0%)
│    Volatility adj: atr>4% → ×0.5, atr>2.5% → ×0.75
│    MSS quality adj: 0.8 + (mss_score/100) × 0.3
│    SL distance adj: <1% → ×1.1, >3% → ×0.8
│    Final clamp: [0.1%, 1.0%]
│  Pass: hard gates pass
│  Block: "RR violation" or "invalid data"
│
│  ┌─────────────────────────────────────────────────────────────┐
│  │ PHASE 6: DEDUPLICATION                                     │
│  └─────────────────────────────────────────────────────────────┘
│
├─ GATE 21: Dedup ────────────────────────────────────────────────
│  Input: last signal for this symbol from DB
│  Logic:
│    effective_cooldown = max(45 min, TF_minutes × 2.0)
│    Same direction: block if elapsed < effective_cooldown
│    Opposite direction: block if elapsed < effective_cooldown / 2
│  Pass: enough time since last signal
│  Block: "dedup cooldown Xm"
│
│  ┌─────────────────────────────────────────────────────────────┐
│  │ PHASE 7-8: SAVE & NOTIFY                                   │
│  └─────────────────────────────────────────────────────────────┘
│
└─ Signal saved to DB + sent to Telegram channel
```

---

## IV. Probability Estimation (P(TP))

Before the Risk Engine, the bot estimates probability of hitting TP:

```
p_tp = 0.45                                          (base)
      + 0.15  if components_count >= 4               (strong setup)
      + 0.08  if components_count == 3               (moderate setup)
      + min(context_score × 0.1, 0.10)               (positive context)
      + max(context_score × 0.1, -0.10)              (negative context)
      + 0.05  if HTF direction matches setup         (HTF aligned)
      + 0.03  if MTF aligned across timeframes
      + 0.05  if mss_score > 70                      (high MSS quality)
      × htf_penalty                                  (1.0 default; 0.7 if HTF mismatch)
      clamp(p_tp, 0.15, 0.85)

confidence = min(0.85, p_tp)
```

---

## V. Config Defaults

| Parameter | Default | Description |
|-----------|---------|-------------|
| max_active_signals | 10 | Max global concurrent signals |
| max_active_signals_per_symbol | 1 | Max concurrent per symbol |
| **max_portfolio_risk_pct** | **3.0%** | **Max sum of risk across all active trades** |
| signal_cooldown_minutes | 45 | Base cooldown between signals |
| SIGNAL_COOLDOWN_TF_MULTIPLIER | 2.0 | Cooldown = max(base, TF × multiplier) |
| min_rr_ratio | 1.5 | Minimum risk:reward ratio |
| require_entry_zone | False | Require price in FVG zone to emit signal |
| htf_bias_v2 | True | Use HTF Bias V2 (W1→D1→H4→H1 EMA) |
| htf_hard_gate | True | Block continuation vs HTF (soft penalty if False) |
| htf_bias_continuation_penalty | 0.7 | P(TP) multiplier when HTF mismatches |
| compression_atr_percentile_low | 20 | ATR percentile below which = compression |
| compression_range_pct | 1.5% | Max range for range detection |
| min_components_required | 2 | Min ICT components for valid setup |
| min_overall_quality | 0.0 | Min quality score (0-100) |
| min_setup_confidence | 0.0 | Min confidence (0-1) |
| base_risk_pct | 1.0% | Max position size per signal |
| Kelly cap | 0.20 | Half-Kelly cap |
| ob_proximity_pct | 2.0% | Max distance from OB midpoint |
| max_fvg_age_candles | 4 | Max candles since FVG formed |
| max_sweep_age_bars | 40 | Max bars since sweep occurred |

---

## VI. Key Observations for Auditor

1. **`portfolio_risk` blocks ~96% of candidates** — the dominant bottleneck. Most candidates never reach pattern_engine. Config: `max_portfolio_risk_pct=3.0%` (code default), `max_active_signals=10` (.env override), `max_active_signals_per_symbol=1`.
2. **BUY signals are slightly profitable (+0.11%), SELL signals are deeply negative (-2.14%).** The short side destroys performance.
3. **SL hits (81) outnumber TP hits (37) by 2.2:1.** The win/loss ratio is inverted.
4. **Average TP is +3.21%, average SL is -3.19%** — R:R is approximately 1:1. The problem is the low win rate (35.3%), not the reward/risk ratio.
5. **No `score_too_low` or `time_of_day_blocked` gates exist** in the current codebase.
6. **`decision_traces` table is empty** — no per-candidate gate data was logged.
7. **Config mismatch note:** `max_active_signals` is overridden to 10 in `.env` (code default is 3). `max_active_signals_per_symbol=1` and `max_portfolio_risk_pct=3.0%` are code defaults (not overridden in `.env`).
8. **Data sources not in git:** `data/signals.db` (22.8 MB, 143 signals) and `logs/bot.log` (3.7 MB) are in `.gitignore`. Local paths: `E:\Projects\tgbot-claude\data\signals.db`, `E:\Projects\tgbot-claude\logs\bot.log`.

---

## VII. What I need you to review

### 1. Why is `portfolio_risk` blocking 96% of candidates?

This is the REAL #1 bottleneck. The gate fires at Phase 0, before any setup analysis.

Look at:
- `scheduler/scanner.py:223-250` — portfolio_risk gate logic
- `config.settings`: `max_portfolio_risk_pct=3.0`, `max_active_signals=10`, `max_active_signals_per_symbol=1`
- With active risk typically 2-3%, only 0-1% budget remains → almost everything blocked

Questions:
- Is 3% max portfolio risk too conservative?
- Should we increase to 5-8% to allow more concurrent signals?
- Is the risk sum calculation correct? Could stale trades inflate it?

### 2. Why is the win rate only 35.3%?

The bot produces signals with roughly 1:1 R:R (avg TP +3.21% vs avg SL -3.19%), but only 35.3% hit TP. At 1:1 R:R, you need >50% WR to be profitable. Current WR is 35.3%.

Look at:
- `strategy/trade_engine.py` — `build_trade_plan()` method
- Is SL placement too tight (getting stopped out before price reaches TP)?
- Are entries at bad prices (entering too late in the move)?
- Is the pattern engine letting through low-quality setups?

### 3. Why are SELL signals deeply negative (-2.14%) while BUY signals are positive (+0.11%)?

65 SELL signals drag the entire system into loss.

Look at:
- Directional bias in pattern engine?
- HTF bias V2 blocking good shorts while allowing bad longs?
- Do SELL signals have worse R:R than BUY signals?

### 4. What is the real throughput if we fix portfolio_risk?

Current: 35 entries/cycle → 0 signals (98% blocked by portfolio_risk)

If we increase `max_portfolio_risk_pct` from 3% to 5%:
- How many more candidates reach pattern_engine?
- Expected signals per week?

### 5. What should we prioritize?

Given (35.3% WR, PF 0.61, -126% PnL, 96% blocked by portfolio_risk):
- A: Fix portfolio_risk gate
- B: Improve win rate (entries too late, SL too tight, or setup quality too low)
- C: Fix SELL signal quality (-2.14% avg vs BUY +0.11%)
- D: Increase signal throughput (currently 8/week)
- E: Something else

---

## VIII. How to review

1. Read `scheduler/scanner.py` — `scan_symbol_v2()` (line 191+), each gate
2. Read `strategy/trade_engine.py` — `build_trade_plan()` (SL/TP logic)
3. Read `strategy/pattern_engine.py` — `detect()`, reversal/continuation/poi_entry
4. Read `risk/engine.py` — `evaluate()` (hard gates, Kelly sizing)
5. Read `strategy/signal_evaluator.py` — `estimate_p_tp()`, dedup, entry_zone
6. Read `config/settings.py` — all thresholds
7. Query `data/signals.db` — per-symbol, per-direction breakdowns

---

## IX. Constraints

- Bot trades multiple symbols on 1h/4h timeframes
- Exchange: Binance Futures (via ccxt)
- Must NOT send false signals (risk management is priority)
- Currently losing money — PF 0.61, WR 35.3%, cumulative PnL -126%
- Need PF > 1.2 and WR > 50% for system to be viable (at 1:1 R:R)
- 8 signals per week is too few — need 2-5 quality signals per week

---

## Output: create file fix/bot_fix_v2.0.md

After completing your review, save your findings to `fix/bot_fix_v2.0.md` using the
template below. This file will be given to a junior AI (mimo) that will implement
every fix you specify. Your instructions must be **precise, unambiguous, and
machine-actionable**.

### Template for bot_fix_v2.0.md:

```markdown
# bot_fix_v2.0.md — Senior model audit

## Date: [YYYY-MM-DD]
## Auditor: [model name]
## Branch: main

---

## I. Executive summary

[2-3 paragraphs: overall health, top 3 critical issues, expected impact]

---

## II. Findings

For EACH finding:

### [ID] — [Short title] — [Severity: CRITICAL/HIGH/MEDIUM/LOW]

**File:** `path/to/file.py`
**Lines:** XX-YY
**Current behavior:** [what code does now]
**Expected behavior:** [what it should do]
**Why it matters:** [impact on signals/risk]

**Evidence:**
```python
# exact code snippet
```

**Fix:**
```python
# exact replacement code (copy-paste ready)
```

**Verification:** [how to confirm]

---

## III. Summary table

| ID | Title | Severity | File | Status |
|----|-------|----------|------|--------|

---

## IV. Priority order

Ranked by (signal_volume_impact × confidence):

1. B-XXX — [title] — [why first]

---

## V. Config recommendations

| Param | Current | Recommended | Reason |
|-------|---------|-------------|--------|

---

## VI. Questions for the team

---

## VII. Implementation notes for mimo

- Safe to apply immediately
- Need A/B testing
- Require server config changes
- Dependencies between fixes
- Test files to update
```

### Rules:

1. Every finding MUST have a code snippet.
2. Every fix MUST be syntactically correct Python.
3. Number findings sequentially (B-001, B-002, ...).
4. Severity: CRITICAL = crashes/wrong signals, HIGH = >5% signal loss, MEDIUM = suboptimal, LOW = code quality.
5. Mark uncertain findings with "(UNCERTAIN)".
