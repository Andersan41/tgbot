# 📊 Полный отчёт по торговле бота

**Дата отчёта:** 2026-07-14 12:39
**Период:** 2026-07-11 → 2026-07-13
**Всего сделок:** 5

---
## 1. История сделок

| # | Дата входа | Символ | TF | Направление | Entry | SL | TP | Exit | Результат | R | Статус |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 07-11 21:03 | DOT/USDT | 1h | BUY | 0.88100 | 0.87385 | 0.90600 | 0.87385 | -1.01% | -1.2R | ❌ SL |
| 2 | 07-11 22:03 | INJ/USDT | 1h | BUY | 5.03800 | 4.94921 | 5.27231 | 4.94921 | -1.96% | -1.1R | ❌ SL |
| 3 | 07-12 00:03 | ETH/USDT | 4h | SELL | 1786.79000 | 1830.39534 | 1671.00000 | 1830.39534 | -2.68% | -1.1R | ❌ SL |
| 4 | 07-12 22:03 | APT/USDT | 1h | SELL | 0.61710 | 0.62540 | 0.59400 | 0.59400 | +3.54% | +2.6R | ✅ TP |
| 5 | 07-13 01:03 | ARB/USDT | 1h | BUY | 0.09290 | 0.08954 | 0.10127 | 0.08954 | -3.84% | -1.1R | ❌ SL |

---
## 2. Метрики по сделкам

| Метрика | Значение |
|---|---|
| Всего сделок | 5 |
| Wins / Losses | 1 / 4 |
| Winrate | 20.0% |
| Profit Factor | 0.37 |
| Expectancy (R) | -0.38R |
| Net PnL | -5.96% |
| Avg Win | +3.54% (+2.6R) |
| Avg Loss | -2.37% (-1.1R) |
| Best Trade | +3.54% |
| Worst Trade | -3.84% |
| Max Drawdown | -4.94% |
| Max DD Duration | 0.0h |
| Max Consecutive Wins | 1 |
| Max Consecutive Losses | 3 |
| Recovery Factor | -1.21 |
| Sharpe Ratio | -38.63 |
| Avg Hold Time | 11.6h |

### Распределение по R-множителю

| R-диапазон | Кол-во | % |
|---|---|---|
| +2R | 1 | 20.0% |
| -2R | 4 | 80.0% |

---
## 3. Анализ по времени

### По дням недели

| День | Сделки | PnL | Winrate |
|---|---|---|---|
| Monday | 1 | -3.8% | 0% |
| Saturday | 2 | -3.0% | 0% |
| Sunday | 2 | +0.9% | 50% |

### По часам (UTC)

| Час | Сделки | PnL | Winrate |
|---|---|---|---|
| 00:00 | 1 | -2.7% | 0% |
| 01:00 | 1 | -3.8% | 0% |
| 21:00 | 1 | -1.0% | 0% |
| 22:00 | 2 | +1.6% | 50% |

---
## 4. Анализ по инструментам

| Символ | Сделки | PnL | Avg PnL | WR | Лучшая | Худшая |
|---|---|---|---|---|---|---|
| APT/USDT | 1 | +3.5% | +3.54% | 100% | +3.54% | 3.54% |
| DOT/USDT | 1 | -1.0% | -1.01% | 0% | +-1.01% | -1.01% |
| INJ/USDT | 1 | -2.0% | -1.96% | 0% | +-1.96% | -1.96% |
| ETH/USDT | 1 | -2.7% | -2.68% | 0% | +-2.68% | -2.68% |
| ARB/USDT | 1 | -3.8% | -3.84% | 0% | +-3.84% | -3.84% |

**Лучший инструмент:** APT/USDT | **Худший:** ARB/USDT

---
## 5. Анализ по направлению

| Направление | Сделки | PnL | Avg PnL | WR |
|---|---|---|---|---|
| Long (BUY) | 3 | -6.8% | -2.27% | 0% |
| Short (SELL) | 2 | +0.9% | +0.43% | 50% |

---
## 6. Анализ риск-менеджмента

- **Средний размер позиции:** 0.95% от депозита
- **Мин. размер позиции:** 0.80%
- **Макс. размер позиции:** 1.00%
- **Средний R:R выигрышных:** +2.6R
- **Средний R:R проигрышных:** -1.1R
- **Expectancy:** -0.38R на сделку

### Распределение результатов (R-множитель)

   +2R: █ (1)
   -2R: ████ (4)

---
## 7. Анализ логов

- **Всего сканов:** 253
- **API ошибок:** 44

### Последние ошибки

```
2026-07-12 06:00:16 | ERROR    | data.exchange_client:185 - Exchange error fetching BTC/USDT 1h: bingx {"code":109500,"msg":"quote service unavailable","data":{}}
2026-07-12 08:00:19 | ERROR    | data.exchange_client:185 - Exchange error fetching BTC/USDT 1h: bingx {"code":109500,"msg":"quote service unavailable","data":{}}
2026-07-12 11:56:33 | ERROR    | bot.notifier:112 - Unexpected error sending signal: API error
2026-07-12 11:56:33 | ERROR    | bot.notifier:110 - Failed to send signal after 2 attempts: persistent error
2026-07-12 11:59:26 | ERROR    | bot.notifier:112 - Unexpected error sending signal: API error
2026-07-12 11:59:27 | ERROR    | bot.notifier:110 - Failed to send signal after 2 attempts: persistent error
2026-07-12 12:00:47 | ERROR    | bot.notifier:112 - Unexpected error sending signal: API error
2026-07-12 12:00:48 | ERROR    | bot.notifier:110 - Failed to send signal after 2 attempts: persistent error
2026-07-12 12:30:17 | ERROR    | data.exchange_client:185 - Exchange error fetching BTC/USDT 1h: bingx {"code":109500,"msg":"quote service unavailable","data":{}}
2026-07-12 15:32:03 | ERROR    | bot.notifier:112 - Unexpected error sending signal: API error
```

### Последние сводки воронки

```
2026-07-14 10:18:10 | INFO     | scheduler.scanner:82 - [FUNNEL SUMMARY] entered=70, sent=0, pattern_engine=47, regime_block=22, displacement_gate=1
2026-07-14 10:33:09 | INFO     | scheduler.scanner:82 - [FUNNEL SUMMARY] entered=70, sent=0, pattern_engine=47, regime_block=22, displacement_gate=1
2026-07-14 10:48:09 | INFO     | scheduler.scanner:82 - [FUNNEL SUMMARY] entered=70, sent=0, pattern_engine=47, regime_block=22, displacement_gate=1
2026-07-14 11:03:26 | INFO     | scheduler.scanner:82 - [FUNNEL SUMMARY] entered=70, sent=1, pattern_engine=46, regime_block=22, displacement_gate=1
2026-07-14 11:18:08 | INFO     | scheduler.scanner:82 - [FUNNEL SUMMARY] entered=70, sent=0, pattern_engine=46, regime_block=22, cooldown=1, displacement_gate=1
2026-07-14 11:33:09 | INFO     | scheduler.scanner:82 - [FUNNEL SUMMARY] entered=70, sent=0, pattern_engine=46, regime_block=22, cooldown=1, displacement_gate=1
2026-07-14 11:48:08 | INFO     | scheduler.scanner:82 - [FUNNEL SUMMARY] entered=70, sent=0, pattern_engine=46, regime_block=22, cooldown=1, displacement_gate=1
2026-07-14 12:03:25 | INFO     | scheduler.scanner:82 - [FUNNEL SUMMARY] entered=70, sent=0, pattern_engine=46, regime_block=21, cooldown=1, displacement_gate=1, risk_engine=1
2026-07-14 12:18:25 | INFO     | scheduler.scanner:82 - [FUNNEL SUMMARY] entered=70, sent=0, pattern_engine=46, regime_block=21, cooldown=1, displacement_gate=1, risk_engine=1
2026-07-14 12:33:19 | INFO     | scheduler.scanner:82 - [FUNNEL SUMMARY] entered=70, sent=0, pattern_engine=46, regime_block=21, cooldown=1, displacement_gate=1, risk_engine=1
```

---
## 8. Анализ стратегии

- **Всего сигналов сгенерировано:** 6
- **Исполнено (закрыто):** 5
- **Открыто сейчас:** 1
- **TP:** 1 (20%)
- **SL:** 4 (80%)
- **EX (Expired):** 0

---
## 9. Визуализация

### Equity Curve
![Equity Curve](charts\equity_curve.png)

### Drawdown
![Drawdown](charts\drawdown.png)

### PnL Distribution
![PnL Distribution](charts\pnl_distribution.png)

### Monthly PnL
![Monthly PnL](charts\monthly_pnl.png)

### PnL by Symbol
![PnL by Symbol](charts\pnl_by_symbol.png)

### Winrate by Hour
![Winrate by Hour](charts\wr_by_hour.png)


---
## 10. Выводы и рекомендации

- ⚠️ **Низкий винрейт** (20%) — система фильтрует много ложных сигналов или SL слишком близко.
- ❌ **PF < 1.0** — система убыточна. Требуется доработка стратегии.
- ❌ **Отрицательный expectancy** — на каждой сделке бот в среднем теряет. Стратегия нуждается в корректировке.
- ⏰ **Лучшее время:** 22:00 UTC | **Худшее:** 01:00 UTC

### Что отслеживать

- Rolling PF (последние 20 сделок) — стабильность системы
- Winrate по дням недели — выявление паттернов
- MFE/MAE — качество входов и стопов
- Калибровка confidence — предсказываемость модели

---
*Отчёт сгенерирован автоматически*