# SYSTEM ARCHITECTURE — Trading Signal Bot

> **Reverse Engineering Document** — No code changes. Pure analysis.
> Generated: 2026-06-27. Updated: 2026-08-14 (sync with `scan_symbol_v2`).

## Quick Start: How a Signal Reaches Telegram

A signal must pass **10 sequential gates** in `scan_symbol_v2` (`scheduler/scanner.py:216`).
First BLOCKED = signal dies. Canonical order — `storage/trace.py:31-35`:

```
cooldown → portfolio_risk → indicators → pattern_engine → structure_alignment →
sweep_required → regime_block → sl_tp → risk_engine → dedup
```

```
Scheduler (cron minute=config.scheduler.scan_minutes, default 2,17,32,47)
  → run_scan_cycle (scanner.py:1542) — circuit breaker → symbols × TFs
    → scan_symbol_v2 (scanner.py:216) — 10 gates + phase filters
      → Pattern Engine (trigger + confirmation, no indicators)
        → Feature Builder (~35 features) → Probability Engine → Risk Engine
          → Save to DB → Telegram → Outcome tracker → Circuit breaker
```

Phase filters inside `scan_symbol_v2` (opt-in/configured):
direction/symbol filter (Rec 3, default SELL + WIF-BUY blocked), news filter
(Rec 4a, opt-in), confluence mode (`STRATEGY_MODE=confluence`), HTF Bias V2,
Premium/Discount (off), min P(TP).

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    SCHEDULER LAYER                       │
│  APScheduler CronTrigger → config.scheduler.scan_minutes│
│  Circuit Breaker (3 losses → 30min pause)               │
│  Shadow mode (SHADOW_ENABLED → run_shadow_cycle)        │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│                    SCANNER LAYER                         │
│  10 gates in sequential funnel (scan_symbol_v2)         │
│  Parallel: symbols × timeframes via asyncio.gather      │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│                 PATTERN ENGINE LAYER                     │
│  ICT setups: trigger (BOS/sweep) + confirmation (OB/FVG)│
│  Direction from BOS (primary) or sweep (secondary)      │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│                  CONTEXT LAYER                           │
│  ContextScorer → ContextScore [-1,1] (never blocks)     │
│  F&G, CoinGecko, Binance Futures funding/OI/LS, news    │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│                   RISK LAYER                             │
│  Risk Engine hard gates (R:R, SL abs limits, portfolio, │
│  max active signals) + Kelly sizing; dynamic risk,      │
│  regime/volatility gates                                │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│                  OUTPUT LAYER                            │
│  Database (signals.db)                                  │
│  Telegram Channel                                       │
│  Outcome Tracker (SL/TP monitoring)                     │
└─────────────────────────────────────────────────────────┘
```

## Key Numbers (current)

- **Gates**: 10 canonical (`storage/trace.py:31-35`)
- **R:R minimum**: Risk Engine ≥ 1.2
- **SL absolute limits**: 0.25%–5.0%
- **Portfolio risk cap**: ≤3%
- **Max active signals**: 3
- **Cooldown**: max(45min, TF_min × 2.0) — persisted in SQLite
- **Circuit Breaker**: 3 consecutive losses → 30min pause (window 60min)
- **P(TP) model**: rules fallback → ML (XGBoost/RandomForest) after 100+ outcomes
- **Quality label**: strong ≥0.65 / moderate ≥0.50 / weak

## Files Index

| File | Purpose |
|------|---------|
| scheduler/scanner.py | Main pipeline — `scan_symbol_v2` (10 gates), `run_scan_cycle` |
| strategy/pattern_engine.py | Pure ICT pattern detection |
| strategy/feature_builder.py | ~35 features → flat vector |
| strategy/probability_engine.py | P(TP), expected RR, PF (rules/ML) |
| risk/engine.py | Risk Engine — hard gates + Kelly sizing |
| storage/trace.py | DecisionTraceBuilder — GATE_ORDER, FEATURE_KEYS |
| storage/database.py | SQLite persistence (signals, outcomes, cooldown) |
| scheduler/tasks.py | APScheduler (scan + daily report) |
| scheduler/shadow.py | Shadow/paper mode |
| scheduler/outcome_tracker.py | TP/SL/EXPIRED closes, PnL after costs |
| scheduler/circuit_breaker.py | Loss circuit breaker |
| bot/notifier.py | Telegram formatting |
