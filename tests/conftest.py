import sys
import os
import shutil
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Loguru sinks are configured at import time by config/logger.py (imported by
# whichever test module loads first), so the redirection has to happen here,
# before any test module is imported — otherwise every run writes into the
# production logs/bot.log.
_TEST_LOG_DIR = tempfile.mkdtemp(prefix="tgbot-pytest-logs-")
os.environ["LOG_FILE"] = os.path.join(_TEST_LOG_DIR, "bot.log")
os.environ["LOG_DIR"] = _TEST_LOG_DIR

collect_ignore = ["test_weight_sweep.py"]


def pytest_sessionfinish(session, exitstatus):
    # Loguru holds the files open — Windows can't rmtree them otherwise.
    try:
        from loguru import logger
        logger.remove()
    except Exception:
        pass
    shutil.rmtree(_TEST_LOG_DIR, ignore_errors=True)

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def sample_ohlcv():
    np.random.seed(42)
    n = 200
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    close = np.random.randn(n).cumsum() + 100
    df = pd.DataFrame(
        {
            "open": close + np.random.randn(n) * 0.5,
            "high": close + np.abs(np.random.randn(n)) * 2,
            "low": close - np.abs(np.random.randn(n)) * 2,
            "close": close,
            "volume": np.random.rand(n) * 1000 + 500,
        },
        index=idx,
    )
    df.index.name = "timestamp"
    return df
