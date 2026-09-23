"""
backtest/resampler.py — Resample OHLCV from the base 15m timeframe to any higher TF.

Только базовый таймфрейм 15m хранится на диске (ohlcv_cache/).
Все остальные TF (1h, 2h, 4h, 1d, 1w) строятся ресемплингом 15m на лету.

Контракт:
    resample_ohlcv(df_15m, "1h") -> pd.DataFrame с DatetimeIndex,
    колонки: open, high, low, close, volume.
"""
from __future__ import annotations

import pandas as pd
from loguru import logger

# Маппинг бот-таймфреймов в pandas-нотацию.
# W-MON — неделя, начинающаяся с понедельника 00:00 UTC (согласовано с
# market_structure/htf_bias_v2.py, где W1-EMA рассчитывается по Monday-барам).
#
# pandas 3.0 удалил псевдонимы 'H'/'D' для часов/дней (остались только
# строчные 'h'/'d'), поэтому часы пишутся строчными, а дни/недели — в
# каноническом виде 'D'/'W-MON' (строчные варианты deprecated).
_TF_MAP: dict[str, str] = {
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "1d": "1D",
    "1w": "W-MON",
}

# Допустимые целевые таймфреймы
VALID_TARGET_TFS: frozenset[str] = frozenset(_TF_MAP.keys())

# Агрегационные функции для OHLCV
_AGG: dict[str, str] = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
}


def _normalize_target_tf(target_tf: str) -> str:
    """Приводит target_tf к нижнему регистру и проверяет валидность."""
    tf = target_tf.strip().lower()
    if tf not in _TF_MAP:
        raise ValueError(
            f"Unsupported target timeframe '{target_tf}'. "
            f"Valid: {sorted(VALID_TARGET_TFS)}"
        )
    return tf


def _ensure_datetime_index(df: pd.DataFrame) -> pd.DataFrame:
    """Проверяет, что df имеет DatetimeIndex, и приводит к UTC.

    Если индекс — не DatetimeIndex, пытается преобразовать колонку 'timestamp'.
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        if "timestamp" in df.columns:
            df = df.set_index("timestamp")
        df.index = pd.to_datetime(df.index, utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df


def resample_ohlcv(
    df_15m: pd.DataFrame,
    target_tf: str,
    source_tf: str = "15m",
) -> pd.DataFrame:
    """Aggregate closed source bars; reject partial buckets and missing samples."""
    tf = _normalize_target_tf(target_tf)
    if df_15m is None or len(df_15m) == 0:
        return df_15m

    def duration(value: str) -> pd.Timedelta:
        units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
        unit = value[-1].lower()
        if unit not in units or int(value[:-1]) <= 0:
            raise ValueError(f"Unsupported timeframe: {value}")
        return pd.Timedelta(seconds=int(value[:-1]) * units[unit])

    source_step = duration(source_tf)
    target_step = duration(tf)
    if source_step > target_step or target_step.value % source_step.value:
        raise ValueError(f"Cannot aggregate {source_tf} into {tf}")
    expected = target_step.value // source_step.value
    df = _ensure_datetime_index(df_15m.copy()).sort_index()
    cols = ["open", "high", "low", "close", "volume"]
    if not set(cols).issubset(df.columns):
        raise ValueError("All OHLCV columns are required")
    if df.index.has_duplicates:
        raise ValueError("Duplicate source candle timestamps")
    if any(ts.value % source_step.value != 0 for ts in df.index):
        raise ValueError(f"Source candles are not aligned to {source_tf}")
    df = df[cols]
    rule = _TF_MAP[tf]
    grouped = df.resample(rule, closed="left", label="left")
    result = grouped.agg(_AGG)
    counts = grouped.count().min(axis=1)
    times = pd.Series(df.index, index=df.index).resample(
        rule, closed="left", label="left",
    )
    first = times.first()
    last = times.last()
    complete = (
        (counts == expected)
        & (first == result.index)
        & (last + source_step == result.index + target_step)
    )
    return result.loc[complete].dropna()


def resample_to_all_tfs(df_15m: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Удобная обёртка: возвращает ресемплинги для всех поддерживаемых TF.

    Используется в backtest/engine.py при --source=local: один раз
    читается 15m-файл, затем строятся все нужные TF.
    """
    return {tf: resample_ohlcv(df_15m, tf) for tf in VALID_TARGET_TFS}


def is_monday_start(df_resampled: pd.DataFrame) -> bool:
    """Проверяет, что первый бар ресемплинга W-MON начинается с понедельника.

    Используется в тестах.
    """
    if len(df_resampled) == 0:
        return False
    first = df_resampled.index[0]
    return first.weekday() == 0  # Monday == 0