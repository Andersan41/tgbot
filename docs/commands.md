# 📖 Справочник команд

Полный список команд бота, бэктеста и офлайн-пайплайна самообучения на истории.
Все Python-команды запускаются из корня репозитория в venv (Python 3.11) с установленными
зависимостями: `pip install -r requirements.txt -r requirements-dev.txt`.

---

## 🚀 Запуск

```bash
# Запуск бота (загружает .env, берёт .trading_bot.lock, polling + scheduler + web)
python main.py

# Docker
docker-compose build && docker-compose up -d && docker-compose logs -f
```

---

## 🧪 Тесты

```bash
pytest -v                                    # все тесты
pytest tests/test_signal.py -v               # один файл
pytest tests/test_signal.py::test_name -v    # один тест
```

---

## 📊 Бэктест

```bash
# Единый движок (паритет с live-пайплайном)
python -m backtest.engine BTC/USDT 1h 336                 # вывод в консоль
python -m backtest.engine BTC/USDT 1h 336 --telegram      # отправить в Telegram
python -m backtest.engine BTC/USDT 1h 336 --market future # рынок futures
python -m backtest.engine BTC/USDT 1h 336 --preset <name> # пресет

# Офлайн-воронка гейтов для текущего ICT-пайплайна (v2)
python -m backtest.funnel_offline
python -m backtest.funnel_offline --symbols BTC/USDT,ETH/USDT
HTF_HARD_GATE=false MIN_P_TP=0.55 python -m backtest.funnel_offline
```

---

## 💾 Кэш OHLCV

```bash
# Фиксированное число свечей (детерминированные A/B/n прогоны)
python -m backtest.cache_ohlcv
python -m backtest.cache_ohlcv --symbols BTC/USDT,ETH/USDT --candles 3900

# Максимальная доступная глубина истории (для самообучения):
# хранится как <sym>_<tf>_max.parquet; --candles задаёт верхний предохранитель пагинации
python -m backtest.cache_ohlcv --max-history --symbols BTC/USDT,ETH/USDT
python -m backtest.cache_ohlcv --max-history --candles 45000
```

---

## 🤖 Самообучение на истории (feasibility spike)

Офлайн-контур, который отвечает на вопрос: **есть ли обучаемый edge на истории, и что стоит
поменять в системе?** Он ничего не деплоит в живую торговлю и не меняет гейтинг сигналов —
только скачивает историю, строит размеченный датасет по текущему ICT-пайплайну, гоняет
walk-forward валидацию и выдаёт go/no-go отчёт. Модель-кандидат сохраняется в scratch-путь,
а НЕ в живой `models/probability_model.pkl`.

Запускать строго по порядку:

```bash
# 1. Проверить реальную глубину истории по монетам (BTC/ETH — годы, часть альтов мелкие)
python scripts/probe_history_depth.py --symbols BTC/USDT,ETH/USDT,SOL/USDT --years 5
python scripts/probe_history_depth.py --timeframes 1h,15m --years 5

# 2. Скачать максимально доступную историю в parquet-кэш
python -m backtest.cache_ohlcv --max-history --symbols BTC/USDT,ETH/USDT,SOL/USDT

# 3. Построить размеченный датасет офлайн-реплеем текущего ICT-пайплайна (метки TP/SL, без look-ahead)
python -m backtest.replay_dataset --max-history --symbols BTC/USDT,ETH/USDT,SOL/USDT
python -m backtest.replay_dataset --max-history --ttl-days 7 --out reports/dataset/ict_dataset

# 4. Walk-forward сравнение LR/RF/XGBoost + важность фич + замер edge + сериализация кандидата
python scripts/train_prob_model.py --dataset reports/dataset/ict_dataset.parquet
python scripts/train_prob_model.py --dataset reports/dataset/ict_dataset.parquet --embargo 200 --splits 5

# 5. Собрать итоговый отчёт go/no-go
python scripts/feasibility_report.py   # → reports/feasibility/FEASIBILITY_REPORT.md
```

### Флаги скриптов пайплайна

| Скрипт                          | Флаг              | Назначение                                                        |
| ------------------------------- | ----------------- | ----------------------------------------------------------------- |
| `scripts/probe_history_depth.py`| `--symbols`       | список монет через запятую (по умолчанию 5 мейджоров)             |
|                                 | `--timeframes`    | таймфреймы через запятую (по умолчанию `1h,15m`)                  |
|                                 | `--years`         | предел зондирования в годах (по умолчанию 5)                      |
| `backtest.cache_ohlcv`          | `--max-history`   | кэшировать максимально доступную глубину (`<sym>_<tf>_max`)       |
|                                 | `--candles`       | верхний предохранитель пагинации в режиме `--max-history`         |
| `backtest.replay_dataset`       | `--max-history`   | грузить глубокий кэш `<sym>_<tf>_max.parquet`                     |
|                                 | `--candles`       | размер фикс-кэша, если не `--max-history`                         |
|                                 | `--limit`         | оценивать только последние N баров на монету                      |
|                                 | `--ttl-days`      | горизонт удержания до EXPIRED (по умолчанию 7, как в live)        |
|                                 | `--out`           | путь-основа вывода (пишет `.parquet` и `.csv`)                    |
| `scripts/train_prob_model.py`   | `--dataset`       | путь к датасету (parquet/csv)                                     |
|                                 | `--embargo`       | зазор строк между train/test для очистки утечки от пересечений    |
|                                 | `--splits`        | число фолдов TimeSeriesSplit                                      |

### Артефакты пайплайна (всё под `reports/`)

| Файл                                              | Кем создаётся                     |
| ------------------------------------------------- | --------------------------------- |
| `reports/feasibility/history_depth.json`          | `probe_history_depth.py`          |
| `reports/abn/ohlcv_cache/<sym>_<tf>_max.parquet`  | `cache_ohlcv --max-history`       |
| `reports/dataset/ict_dataset.parquet` / `.csv`    | `replay_dataset`                  |
| `reports/feasibility/dataset_summary.json`        | `replay_dataset`                  |
| `reports/feasibility/train_metrics.json`          | `train_prob_model.py`             |
| `reports/feasibility/candidate_probability_model.pkl` | `train_prob_model.py` (scratch) |
| `reports/feasibility/FEASIBILITY_REPORT.md`       | `feasibility_report.py`           |

### Что дальше, если отчёт говорит GO

Безопасная схема внедрения (вне рамок спайка, решение за человеком):

1. Обучить модель офлайн на полном размеченном датасете.
2. Ручное продвижение артефакта в `models/probability_model.pkl` — живой `ProbabilityEngine`
   подхватит его автоматически (сейчас файла нет → работает fallback на правилах).
3. Прогнать в shadow-режиме рядом с live, сверяя предсказанный vs реальный TP 2–4 недели,
   прежде чем пускать модель в гейтинг через `min_p_tp`.
4. Никакого авто-переобучения-и-деплоя — walk-forward валидация на каждом обновлении.

---

## 📲 Команды Telegram-бота

| Команда       | Описание                             |
| ------------- | ------------------------------------ |
| `/start`      | Приветствие                          |
| `/help`       | Справка                              |
| `/status`     | Состояние бота                       |
| `/lastsignal` | Последние 5 сигналов                 |
| `/symbols`    | Отслеживаемые монеты                 |
| `/scan`       | Ручной запуск сканирования (admin)   |
| `/settings`   | Параметры индикаторов (admin)        |
