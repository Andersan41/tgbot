# 📡 Trading Signal Bot

Telegram-бот, который в режиме мультитаска сканирует рынок и выдаёт сигналы **BUY/SELL** на основе технических индикаторов.

---

## 🏗 Архитектура

```
trading_bot/
├── main.py                  # Точка входа
├── config/
│   ├── settings.py          # Все настройки через .env
│   └── logger.py            # Loguru логгер
├── data/
│   └── exchange_client.py   # Получение OHLCV (ccxt/Binance)
├── indicators/
│   └── engine.py            # EMA, RSI, MACD, ADX, ATR, Supertrend
├── strategy/
│   └── signal_engine.py     # Логика BUY/SELL/NO_SIGNAL
├── scheduler/
│   ├── scanner.py           # Параллельный сканер с подтверждением
│   └── tasks.py             # APScheduler (1H каждый час, 4H каждые 4ч)
├── bot/
│   ├── handlers.py          # Telegram команды
│   └── notifier.py          # Отправка сигналов в канал
├── storage/
│   └── database.py          # SQLite (SQLAlchemy async)
├── data/                    # БД (создаётся автоматически)
├── logs/                    # Логи (создаются автоматически)
├── .env.example
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

---

## ⚙️ Установка

### 1. Клонируй и настрой окружение

```bash
git clone <repo>
cd trading_bot
cp .env.example .env
```

### 2. Заполни `.env`

```env
TELEGRAM_BOT_TOKEN=ваш_токен_от_BotFather
TELEGRAM_CHANNEL_ID=-100xxxxxxxxxx    # ID вашего канала
TELEGRAM_ADMIN_IDS=123456789          # Ваш Telegram ID

BINANCE_API_KEY=ваш_ключ
BINANCE_API_SECRET=ваш_секрет

SYMBOLS=BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT
PRIMARY_TIMEFRAMES=1h,4h
CONFIRM_TIMEFRAME=15m
```

> **Как получить ID канала:** добавьте бота в канал как администратора, отправьте любое сообщение, затем откройте
`https://api.telegram.org/bot<TOKEN>/getUpdates`

### 3. Запуск через Docker (рекомендуется)

```bash
docker-compose up -d
docker-compose logs -f
```

### 4. Запуск без Docker

```bash
python -m venv .venv
source .venv/bin/activate # Linux/Mac
# .venv\Scripts\activate         # Windows
pip install -r requirements.txt
python main.py
```

---

## 🧠 Логика сигналов

### Условия BUY (нужно 4 из 7):

1. Supertrend — восходящий тренд
2. EMA выравнивание: fast > slow > trend
3. EMA пересечение: fast пересекла slow снизу вверх
4. RSI: 50–70 (зона силы, не перекуплен)
5. MACD гистограмма положительная
6. ADX > 20 (сильный тренд)
7. Объём выше SMA(20) × 1.2

### Условия SELL (нужно 4 из 7):

1. Supertrend — нисходящий тренд
2. EMA выравнивание: fast < slow < trend
3. EMA пересечение: fast пересекла slow сверху вниз
4. RSI: 30–50 (зона слабости, не перепродан)
5. MACD гистограмма отрицательная
6. ADX > 20 (сильный тренд)
7. Объём выше SMA(20) × 1.2

### Фильтры:

- **ADX < 20** → флэт, сигналы игнорируются
- **Подтверждение на 15M** → сигнал 1H/4H должен совпадать с 15M

### SL/TP:

- Stop Loss = цена ± ATR × 1.5 (или BOS-based / structural)
- Take Profit = цена ± ATR × 3.0 (или structural)
- Комиссия: 0.05% за сторону + 0.05% проскальзывание

---

## 📊 Backtesting

Единый backtest engine: `backtest/engine.py`

```bash
# Console output
python -m backtest.engine BTC/USDT 1h 336

# With Telegram output
python -m backtest.engine BTC/USDT 1h 336 --telegram

# Future market type
python -m backtest.engine BTC/USDT 1h 336 --market future
```

Backtest engine реализует полный паритет с live-пайплайном (scheduler/scanner.py):
- Regime detection через `RegimeDetector`
- Structural SL/TP через `calculate_structural_sl/tp`
- Stop hunt buffer
- SL distance guard (min/max)
- RR filter
- Confirmation timeframe
- Комиссия и проскальзывание

---

## 🤖 Самообучение на истории (feasibility spike)

Офлайн-контур, который отвечает на вопрос: **есть ли обучаемый edge на многолетней истории,
и что стоит поменять в системе?** Он скачивает историю, строит размеченный датасет по
**текущему** ICT-пайплайну (метки TP/SL, без look-ahead), гоняет walk-forward валидацию и
выдаёт go/no-go отчёт с важностью фич и замером edge. Живой торговли, авто-переобучения и
изменения гейтинга сигналов он **не** трогает: модель-кандидат пишется в scratch-путь, а не в
живой `models/probability_model.pkl`.

Запуск строго по порядку (в venv, Python 3.11):

```bash
# 1. Проверить реальную глубину истории по монетам (BTC/ETH — годы, часть альтов мелкие)
python scripts/probe_history_depth.py --symbols BTC/USDT,ETH/USDT,SOL/USDT --years 5

# 2. Скачать максимально доступную историю в parquet-кэш
python -m backtest.cache_ohlcv --max-history --symbols BTC/USDT,ETH/USDT,SOL/USDT

# 3. Построить размеченный датасет офлайн-реплеем (метки TP/SL, без look-ahead)
python -m backtest.replay_dataset --max-history --symbols BTC/USDT,ETH/USDT,SOL/USDT

# 4. Walk-forward LR/RF/XGBoost + важность фич + замер edge + сериализация кандидата
python scripts/train_prob_model.py --dataset reports/dataset/ict_dataset.parquet

# 5. Итоговый отчёт go/no-go
python scripts/feasibility_report.py   # → reports/feasibility/FEASIBILITY_REPORT.md
```

Отчёт (`reports/feasibility/FEASIBILITY_REPORT.md`) показывает OOS AUC, PF/expectancy модели
против правил и против того, что бот торгует сейчас, важность каждой из 46 фич и вердикт по
полноценному контуру. Начинать стоит с BTC/ETH/SOL (глубокая история), затем добавлять монеты
из зонда. Полный справочник флагов и артефактов — в [docs/commands.md](docs/commands.md).

---

## 📲 Команды бота

| Команда       | Описание                             |
|---------------|--------------------------------------|
| `/start`      | Приветствие                          |
| `/help`       | Справка                              |
| `/status`     | Состояние бота                       |
| `/lastsignal` | Последние 5 сигналов                 |
| `/symbols`    | Отслеживаемые монеты                 |
| `/scan`       | Ручной запуск (только admin)         |
| `/settings`   | Параметры индикаторов (только admin) |

---

## 🔒 Безопасность

- Токены хранятся только в `.env` (никогда не коммитить)
- Чувствительные команды защищены `TELEGRAM_ADMIN_IDS`
- `.gitignore` исключает `.env`, БД и логи

---

## 📊 Расписание сканирования

| Таймфрейм | Расписание                                      |
|-----------|-------------------------------------------------|
| 1H        | Каждый час на 2-й минуте (после закрытия свечи) |
| 4H        | В 00:05, 04:05, 08:05, 12:05, 16:05, 20:05 UTC  |
| 15M       | Только для подтверждения основного сигнала      |

---

## 🛠 Кастомизация

Все параметры индикаторов задаются в `config/settings.py`:

```python
ema_fast: int = 9
ema_slow: int = 21
ema_trend: int = 50
rsi_period: int = 14
adx_min: float = 20.0  # Фильтр флэта
atr_multiplier_sl: float = 1.5
atr_multiplier_tp: float = 3.0
```

Для изменения стратегии — редактируй `strategy/signal_engine.py`.
