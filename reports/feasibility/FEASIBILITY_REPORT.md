# Self-Learning Feasibility Report

Offline feasibility spike: can a model learn from multi-year history to rank ICT setups better than the current rules, and what does it say we should change?

## 1. Data reality — history depth

Probe cap: 5.0 years. '>=cap' means real history may be deeper.

| Symbol   | TF  | Candles | Earliest   | Depth |
| -------- | --- | ------- | ---------- | ----- |
| BTC/USDT | 1h  | 19497   | 2024-04-27 | 2.22y |
| BTC/USDT | 15m | 24212   | 2025-11-08 | 0.69y |
| ETH/USDT | 1h  | 19497   | 2024-04-27 | 2.22y |
| ETH/USDT | 15m | 24212   | 2025-11-08 | 0.69y |
| SOL/USDT | 1h  | 19497   | 2024-04-27 | 2.22y |
| SOL/USDT | 15m | 24212   | 2025-11-08 | 0.69y |

## 2. Dataset (offline ICT replay)

- Total setups: **4891**, label-complete: **4831**
- Date range: 2025-12-22 → 2026-07-18

| Outcome | Count | Share |
| ------- | ----- | ----- |
| HIT_SL  | 3026  | 62.6% |
| HIT_TP  | 1731  | 35.8% |
| EXPIRED | 74    | 1.5%  |

- Win rate (net>0): **36.6%**  |  TP-before-SL: **35.8%**  |  Avg net PnL/setup: **-0.352%**
- Setups the RiskEngine would accept today (passed_risk): **690**

| Symbol   | Setups |
| -------- | ------ |
| ETH/USDT | 1689   |
| SOL/USDT | 1574   |
| BTC/USDT | 1568   |

## 3. Model comparison & edge

_Not yet run (scripts/train_prob_model.py)._

## 4. Verdict — go / no-go

**INCOMPLETE** — training metrics missing; run scripts/train_prob_model.py.
