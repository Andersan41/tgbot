"""
indicators/engine.py — Расчёт технических индикаторов
"""
import math
from dataclasses import dataclass
from typing import Optional
import pandas as pd
import pandas_ta as ta
import numpy as np
from loguru import logger
from config.settings import config


def _safe_float(val, default=0.0) -> float:
    """Convert to float, replacing None/NaN with default."""
    if val is None:
        return default
    f = float(val)
    return default if math.isnan(f) or math.isinf(f) else f


@dataclass
class IndicatorValues:
    """Результат расчёта индикаторов на последней свече"""
    symbol: str
    timeframe: str

    # Цены
    close: float
    high: float
    low: float
    volume: float

    # EMA
    ema_fast: float
    ema_slow: float
    ema_trend: float
    ema_fast_prev: float    # EMA fast на предыдущей свече
    ema_slow_prev: float    # EMA slow на предыдущей свече

    # RSI
    rsi: float

    # MACD
    macd: float
    macd_signal: float
    macd_hist: float
    macd_hist_prev: float

    # ADX
    adx: float
    dmi_plus: float
    dmi_minus: float

    # ATR
    atr: float

    # Supertrend
    supertrend: float
    supertrend_direction: int  # 1 = up (bullish), -1 = down (bearish)

    # Volume
    volume_sma: float
    volume_delta_pct: Optional[float] = None  # +68% = покупки доминируют, -68% = продажи

    # Вычисляемые свойства
    @property
    def ema_bullish_cross(self) -> bool:
        """EMA fast пересекла EMA slow снизу вверх"""
        return self.ema_fast_prev <= self.ema_slow_prev and self.ema_fast > self.ema_slow

    @property
    def ema_bearish_cross(self) -> bool:
        """EMA fast пересекла EMA slow сверху вниз"""
        return self.ema_fast_prev >= self.ema_slow_prev and self.ema_fast < self.ema_slow

    @property
    def ema_bullish_alignment(self) -> bool:
        """EMA fast > EMA slow > EMA trend"""
        return self.ema_fast > self.ema_slow > self.ema_trend

    @property
    def ema_bearish_alignment(self) -> bool:
        """EMA fast < EMA slow < EMA trend"""
        return self.ema_fast < self.ema_slow < self.ema_trend

    @property
    def macd_bullish_cross(self) -> bool:
        """MACD гистограмма переходит от отрицательной к положительной"""
        return self.macd_hist_prev < 0 and self.macd_hist > 0

    @property
    def macd_bearish_cross(self) -> bool:
        """MACD гистограмма переходит от положительной к отрицательной"""
        return self.macd_hist_prev > 0 and self.macd_hist < 0

    @property
    def volume_above_avg(self) -> bool:
        return self.volume > self.volume_sma * config.trading.volume_factor

    @property
    def trend_is_strong(self) -> bool:
        return self.adx >= config.trading.adx_min

    @property
    def supertrend_bullish(self) -> bool:
        return self.supertrend_direction == 1

    @property
    def supertrend_bearish(self) -> bool:
        return self.supertrend_direction == -1


class IndicatorEngine:
    _CORE_COLUMNS = ["ema_fast", "ema_slow", "rsi", "adx", "atr"]

    def _compute_columns(self, df: pd.DataFrame, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        """Add indicator columns to df in place (mutates df). Returns df or None if too short.

        Pure causal computation: the value at row i equals the value computed on any
        window ending at i, so the backtest precomputes columns once (O(n)) instead of
        re-running every indicator per candle (O(n²)).
        """
        try:
            if len(df) < config.trading.candles_limit // 2:
                logger.warning(f"Not enough candles for {symbol} {timeframe}: {len(df)}")
                return None

            cfg = config.trading

            # EMA
            df["ema_fast"] = ta.ema(df["close"], length=cfg.ema_fast)
            df["ema_slow"] = ta.ema(df["close"], length=cfg.ema_slow)
            df["ema_trend"] = ta.ema(df["close"], length=cfg.ema_trend)

            # RSI
            df["rsi"] = ta.rsi(df["close"], length=cfg.rsi_period)

            # MACD
            macd_df = ta.macd(df["close"], fast=cfg.macd_fast, slow=cfg.macd_slow, signal=cfg.macd_signal)
            if macd_df is not None:
                macd_col = [c for c in macd_df.columns if c.startswith("MACD_")][0]
                hist_col = [c for c in macd_df.columns if c.startswith("MACDh_")][0]
                signal_col = [c for c in macd_df.columns if c.startswith("MACDs_")][0]
                df["macd"] = macd_df[macd_col]
                df["macd_signal"] = macd_df[signal_col]
                df["macd_hist"] = macd_df[hist_col]
            else:
                df["macd"] = np.nan
                df["macd_signal"] = np.nan
                df["macd_hist"] = np.nan

            # ADX + DMI
            adx_df = ta.adx(df["high"], df["low"], df["close"], length=cfg.adx_period)
            if adx_df is not None:
                adx_cols = list(adx_df.columns)
                adx_col = [c for c in adx_cols if c.startswith("ADX_") and "R" not in c]
                dmp_col = [c for c in adx_cols if c.startswith("DMP_")]
                dmn_col = [c for c in adx_cols if c.startswith("DMN_")]
                df["adx"] = adx_df[adx_col[0]] if adx_col else np.nan
                df["dmi_plus"] = adx_df[dmp_col[0]] if dmp_col else np.nan
                df["dmi_minus"] = adx_df[dmn_col[0]] if dmn_col else np.nan
            else:
                df["adx"] = np.nan
                df["dmi_plus"] = np.nan
                df["dmi_minus"] = np.nan

            # ATR
            df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=cfg.atr_period)

            # Volume SMA
            df["volume_sma"] = ta.sma(df["volume"], length=cfg.volume_sma_period)

            # Volume Delta (for futures with taker buy volume)
            if "taker_buy_volume" in df.columns:
                buy_vol = df["taker_buy_volume"]
                sell_vol = df["volume"] - buy_vol
                df["volume_delta_pct"] = ((buy_vol - sell_vol) / df["volume"]) * 100
            else:
                df["volume_delta_pct"] = None

            # Supertrend
            st_df = ta.supertrend(
                df["high"], df["low"], df["close"],
                length=cfg.supertrend_period,
                multiplier=cfg.supertrend_multiplier,
            )
            if st_df is not None:
                # pandas_ta возвращает несколько колонок; нужна SUPERT_... и SUPERTd_...
                st_cols = [c for c in st_df.columns]
                st_val_col = [c for c in st_cols if c.startswith("SUPERT_") and "d" not in c and "l" not in c and "s" not in c]
                st_dir_col = [c for c in st_cols if "SUPERTd_" in c]
                if st_val_col and st_dir_col:
                    df["supertrend"] = st_df[st_val_col[0]]
                    df["supertrend_dir"] = st_df[st_dir_col[0]]
                else:
                    df["supertrend"] = np.nan
                    df["supertrend_dir"] = 0
            else:
                df["supertrend"] = np.nan
                df["supertrend_dir"] = 0

            # Удаляем строки с NaN — перенесено в _values_from_clean / calculate.
            return df

        except Exception as e:
            logger.error(f"Indicator calculation error for {symbol} {timeframe}: {e}", exc_info=True)
            return None

    def _values_from_clean(self, df_clean: pd.DataFrame, symbol: str, timeframe: str) -> IndicatorValues:
        """Build IndicatorValues from a NaN-free DataFrame using its last two rows."""
        last = df_clean.iloc[-1]
        prev = df_clean.iloc[-2]

        return IndicatorValues(
            symbol=symbol,
            timeframe=timeframe,
            close=_safe_float(last["close"]),
            high=_safe_float(last["high"]),
            low=_safe_float(last["low"]),
            volume=_safe_float(last["volume"]),
            ema_fast=_safe_float(last["ema_fast"]),
            ema_slow=_safe_float(last["ema_slow"]),
            ema_trend=_safe_float(last["ema_trend"]),
            ema_fast_prev=_safe_float(prev["ema_fast"]),
            ema_slow_prev=_safe_float(prev["ema_slow"]),
            rsi=_safe_float(last["rsi"]),
            macd=_safe_float(last.get("macd")),
            macd_signal=_safe_float(last.get("macd_signal")),
            macd_hist=_safe_float(last.get("macd_hist")),
            macd_hist_prev=_safe_float(prev.get("macd_hist")),
            adx=_safe_float(last["adx"]),
            dmi_plus=_safe_float(last.get("dmi_plus")),
            dmi_minus=_safe_float(last.get("dmi_minus")),
            atr=_safe_float(last["atr"]),
            supertrend=_safe_float(last.get("supertrend"), _safe_float(last["close"])),
            supertrend_direction=int(_safe_float(last.get("supertrend_dir"), 0)),
            volume_sma=_safe_float(last.get("volume_sma"), _safe_float(last["volume"])),
            volume_delta_pct=_safe_float(last.get("volume_delta_pct"), None) if last.get("volume_delta_pct") is not None else None,
        )

    def calculate(self, df: pd.DataFrame, symbol: str, timeframe: str) -> Optional[IndicatorValues]:
        """Compute indicators on the provided DataFrame (live path, per-window)."""
        df = self._compute_columns(df, symbol, timeframe)
        if df is None:
            return None
        df_clean = df.dropna(subset=self._CORE_COLUMNS)
        if len(df_clean) < 2:
            logger.warning(f"Not enough clean data for {symbol} {timeframe}")
            return None
        return self._values_from_clean(df_clean, symbol, timeframe)

    def precompute(self, df: pd.DataFrame, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        """Backtest path: add indicator columns once on the full series (O(n))."""
        return self._compute_columns(df, symbol, timeframe)

    def values_at(self, df: pd.DataFrame, symbol: str, timeframe: str, i: int) -> Optional[IndicatorValues]:
        """Backtest path: IndicatorValues for bar i from precomputed columns.

        Post-warmup guarantee (core periods < warmup): core columns are non-NaN at
        rows i and i-1, so this equals calculate(df.iloc[:i+1]) without recompute.
        """
        if i < 1 or i >= len(df):
            return None
        if any(pd.isna(df.iloc[i][c]) for c in self._CORE_COLUMNS):
            return None
        if any(pd.isna(df.iloc[i - 1][c]) for c in self._CORE_COLUMNS):
            return None
        return self._values_from_clean(df.iloc[i - 1:i + 1], symbol, timeframe)


indicator_engine = IndicatorEngine()
