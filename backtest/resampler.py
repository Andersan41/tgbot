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


def resample_ohlcv(df_15m: pd.DataFrame, target_tf: str) -> pd.DataFrame:
    """Resample a 15m OHLCV DataFrame to *target_tf*.

    Агрегация: open=first, high=max, low=min, close=last, volume=sum.
    Неполные бары (например, последний, не закончившийся на момент исходных
    данных) удаляются через dropna().

    Args:
        df_15m: DataFrame с DatetimeIndex (UTC) и колонками
                open/high/low/close/volume.
        target_tf: один из "1h", "2h", "4h", "1d", "1w".

    Returns:
        pd.DataFrame с тем же DatetimeIndex (UTC) и теми же колонками.
        Индекс — closed="left", label="left": бар 00:00 содержит данные
        с 00:00 (включительно) до 00:59 (исключительно).
    """
    tf = _normalize_target_tf(target_tf)
    pandas_rule = _TF_MAP[tf]

    if df_15m is None or len(df_15m) == 0:
        logger.warning(f"resample_ohlcv: empty input for {target_tf}")
        return df_15m

    df = _ensure_datetime_index(df_15m.copy())

    # Берём только OHLCV-колонки, игнорируем посторонние (taker_buy_volume и т.д.)
    cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    if not cols:
        raise ValueError(
            f"df_15m must contain at least one of open/high/low/close/volume, "
            f"got columns: {list(df.columns)}"
        )
    df = df[cols]

    # closed="left", label="left" — стандарт для OHLCV:
    # бар [00:00, 01:00) помечается меткой 00:00.
    resampled = df.resample(
        pandas_rule, closed="left", label="left",
    ).agg(_AGG)

    # dropna() убирает неполные бары (например, последний бар, в который
    # попали не все 4 x 15m свечи — если исходные данные обрываются посередине).
    resampled = resampled.dropna()

    logger.debug(
        f"resample_ohlcv: {len(df_15m)} x 15m -> {len(resampled)} x {target_tf} "
        f"({df.index[0]} -> {df.index[-1]})"
    )
    return resampled


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