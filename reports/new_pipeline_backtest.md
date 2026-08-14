# New Pipeline Backtest — 2026-07-20 14:07

**Symbols:** BTC/USDT, ETH/USDT, ZRO/USDT
**Timeframe:** 4h | **Days:** 90
**Commission:** 0.06% | **Slippage:** 0.02%

## Aggregate

| Metric | Value |
|---|---|
| Total trades | 116 |
| Winrate | 30.2% |
| Profit Factor | 1.16 |
| Sharpe Ratio | 5.54 |
| Total PnL (net) | +31.64% |

## Setup Type Breakdown (aggregate)

| Setup | Trades | WR% | PF | Avg R | PnL% |
|---|---|---|---|---|---|
| MSS Reversal | 18 | 22.2% | 0.78 | -0.25 | -5.53% |
| BOS Continuation LONG | 67 | 32.8% | 1.47 | +0.49 | +52.94% |
| BOS Continuation SHORT | 31 | 29.0% | 0.71 | -0.13 | -15.77% |
| BOS Continuation ALL | 98 | 31.6% | 1.22 | +0.29 | +37.17% |

## Per-Symbol Overview

| Symbol | Trades | WR% | PF | PnL% | Sharpe | MaxDD | Rev | Cont |
|---|---|---|---|---|---|---|---|---|
| BTC/USDT | 61 | 41.0% | 2.06 | +74.24% | 26.74 | 41.21% | 8 | 53 |
| ETH/USDT | 50 | 20.0% | 0.75 | -26.66% | -10.19 | 46.97% | 10 | 40 |
| ZRO/USDT | 5 | 0.0% | 0.0 | -15.94% | -196.59 | 13.84% | 0 | 5 |

## Per-Symbol × Setup Type

| Symbol | Setup | Trades | WR% | PF | Avg R | PnL% |
|---|---|---|---|---|---|---|
| BTC/USDT | MSS Reversal | 8 | 37.5% | 3.8 | +0.56 | +14.49% |
| BTC/USDT | BOS LONG | 40 | 37.5% | 1.76 | +0.70 | +42.61% |
| BTC/USDT | BOS SHORT | 13 | 53.8% | 3.09 | +0.59 | +17.13% |
| ETH/USDT | MSS Reversal | 10 | 10.0% | 0.01 | -0.89 | -20.03% |
| ETH/USDT | BOS LONG | 22 | 31.8% | 1.66 | +0.44 | +26.26% |
| ETH/USDT | BOS SHORT | 18 | 11.1% | 0.29 | -0.64 | -32.90% |
| ZRO/USDT | BOS LONG | 5 | 0.0% | 0.0 | -1.00 | -15.94% |

## MSS Score Bucket Attribution

| Bucket | Trades | WR% | PF | Avg R | PnL% |
|---|---|---|---|---|---|
| 0-30 | 98 | 31.6% | 1.22 | +0.29 | +37.17% |
| 30-50 | 18 | 22.2% | 0.78 | -0.25 | -5.53% |

## Regime Attribution

| Regime | Trades | WR% | PF | Avg R | PnL% |
|---|---|---|---|---|---|
| bullish | 69 | 31.9% | 1.41 | +0.44 | +47.83% |
| bearish | 47 | 27.7% | 0.78 | -0.14 | -16.19% |

## Regime × Setup Type

| Regime | Setup | Trades | WR% | PF | Avg R | PnL% |
|---|---|---|---|---|---|---|
| bullish | MSS Reversal | 2 | 0.0% | 0.0 | -1.00 | -5.11% |
| bullish | BOS LONG | 67 | 32.8% | 1.47 | +0.49 | +52.94% |
| bearish | MSS Reversal | 16 | 25.0% | 0.98 | -0.15 | -0.42% |
| bearish | BOS SHORT | 31 | 29.0% | 0.71 | -0.13 | -15.77% |

## Asset Type Clustering

| Asset Type | Symbols | Trades | WR% | PF | Avg R | PnL% |
|---|---|---|---|---|---|---|
| L1 | ZRO/USDT | 5 | 0.0% | 0.0 | -1.00 | -15.94% |
| major | BTC/USDT, ETH/USDT | 111 | 31.5% | 1.27 | +0.26 | +47.57% |

## MSS Reversal by Asset Type

| Asset Type | Trades | WR% | PF | Avg R | PnL% |
|---|---|---|---|---|---|
| major | 18 | 22.2% | 0.78 | -0.25 | -5.53% |

## BOS Continuation by Asset Type × Direction

| Asset Type | Dir | Trades | WR% | PF | Avg R | PnL% |
|---|---|---|---|---|---|---|
| L1 | LONG | 5 | 0.0% | 0.0 | -1.00 | -15.94% |
| major | LONG | 62 | 35.5% | 1.72 | +0.61 | +68.88% |
| major | SHORT | 31 | 29.0% | 0.71 | -0.13 | -15.77% |

## Rejection Reasons

| Reason | Count |
|---|---|
| reversal: no MSS (strong CHoCH) | 1706 |
| continuation: ranging market | 1538 |
| continuation: no sweep in sell direction | 702 |
| continuation: no sweep in buy direction | 700 |
| continuation: no BOS | 648 |
| low_p_tp | 639 |
| continuation: BOS bearish vs trend bullish | 75 |
| l1_short_blocked | 71 |
| continuation: BOS bullish vs trend bearish | 16 |
| RR=1.40 < 1.5 | 2 |
| RR=1.09 < 1.5 | 1 |
| SL too tight: 0.23% < 0.25% | 1 |
| SL too wide: 5.23% > 5.0% | 1 |
| RR=0.92 < 1.5 | 1 |
| RR=1.23 < 1.5 | 1 |
| RR=1.49 < 1.5 | 1 |
