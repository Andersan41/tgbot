"""
backtest/__main__.py — `python -m backtest` запускает загрузку истории OHLCV.

Делегирует в backtest/download_history.py (CLI).
"""
from backtest.download_history import main

if __name__ == "__main__":
    main()