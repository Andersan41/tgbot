# Plan Index — Trading Signal Bot

> Точка входа в планирование и архитектуру.
> Date: 2026-08-14. Sync with code (`config/settings.py:15` → `VERSION = "2.6.0"`).

## Contents

| File | Description |
|------|-------------|
| [01-architecture.md](01-architecture.md) | Модульное дерево, синглтоны, слои пайплайна |
| [07-scheduler.md](07-scheduler.md) | Расписание, циклы сканирования, shadow, circuit breaker |
| [11-pipeline.md](11-pipeline.md) | Полный пайплайн от OHLCV до Telegram (scan_symbol_v2) |
| [14-env-config.md](14-env-config.md) | Справочник переменных окружения |
| [15-candidate-dataset.md](15-candidate-dataset.md) | Датасет-кандидаты для ML |
| [16-decision-intelligence.md](16-decision-intelligence.md) | Decision Intelligence слой |
| [17-scenario-engine.md](17-scenario-engine.md) | Scenario Engine и Trade Thesis |
| [pipeline.md](pipeline.md) | Легаси-справочник пайплайна (историческая справка) |

## Быстрый обзор

Production-канал — `scan_symbol_v2()` в `scheduler/scanner.py`:

```
Scheduler (tasks.py, каждые 15 мин)
  → run_scan_cycle() (scanner.py:1542)            # circuit breaker → символы × TF
    → scan_symbol_v2() (scanner.py:216)           # 10 гейтов + фазовые фильтры
      → Pattern Engine → Feature Builder → Probability Engine → Risk Engine
      → SignalResult → save_signal → create_outcome → notify Telegram
```

Живая воронка гейтов (порядок — `storage/trace.py:31-35`):

```
cooldown → portfolio_risk → indicators → pattern_engine →
structure_alignment → sweep_required → regime_block → sl_tp → risk_engine → dedup
```

Плюс фазовые фильтры внутри `scan_symbol_v2`: direction/symbol filter,
confluence-mode (опционально), news filter (опционально), HTF Bias V2,
Premium/Discount (off по умолчанию), min P(TP) (опционально).