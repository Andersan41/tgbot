"""
whale/detector.py — Big Trades Whale Detector

Детекция аномальных всплесков объёма (Z-Score) и классификация
по типу: Tier 1/2/3, INIT (инициатива) / ABS (поглощение).
"""
import math
from dataclasses import dataclass, field
from typing import List, Optional
import pandas as pd
import numpy as np
from loguru import logger


# ── Конфигурация порогов ────────────────────────────────
ZSCORE_LOOKBACK = 20          # Окно для расчёта SMA и STD объёма
TIER1_THRESHOLD = 2.0         # Z-Score >= 2.0 → Tier 1
TIER2_THRESHOLD = 3.0         # Z-Score >= 3.0 → Tier 2
TIER3_THRESHOLD = 4.0         # Z-Score >= 4.0 → Tier 3 (Whale)
PRICE_MOVE_THRESHOLD = 0.001  # 0.1% движение цены для классификации INIT/ABS
MAX_SIGNALS_PER_UPDATE = 10   # Максимум сигналов за раз


@dataclass
class WhaleSignal:
    """Один сигнал whale-активности."""
    tier: int                          # 1, 2, 3
    direction: str                     # 'buy' или 'sell'
    classification: str                # 'INIT' или 'ABS'
    z_score: float                     # Z-Score объёма
    volume: float                      # Текущий объём
    volume_sma: float                  # SMA объёма
    price: float                       # Цена закрытия
    bar_index: int                     # Индекс бара в DataFrame
    timestamp: Optional[int] = None    # Unix timestamp (ms)

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "direction": self.direction,
            "classification": self.classification,
            "z_score": round(self.z_score, 2),
            "volume": round(self.volume, 2),
            "volume_sma": round(self.volume_sma, 2),
            "price": round(self.price, 4),
            "bar_index": self.bar_index,
            "timestamp": self.timestamp,
        }


@dataclass
class WhaleState:
    """Результат анализа для дашборда."""
    z_score: float = 0.0
    z_score_abs: float = 0.0
    vwap: float = 0.0
    volume: float = 0.0
    volume_sma: float = 0.0
    signals: List[WhaleSignal] = field(default_factory=list)
    tier1_count: int = 0
    tier2_count: int = 0
    tier3_count: int = 0

    def to_dict(self) -> dict:
        return {
            "z_score": round(self.z_score, 2),
            "z_score_abs": round(self.z_score_abs, 2),
            "vwap": round(self.vwap, 4),
            "volume": round(self.volume, 2),
            "volume_sma": round(self.volume_sma, 2),
            "signals": [s.to_dict() for s in self.signals],
            "tier1_count": self.tier1_count,
            "tier2_count": self.tier2_count,
            "tier3_count": self.tier3_count,
        }


def _safe_float(val, default=0.0) -> float:
    if val is None:
        return default
    f = float(val)
    return default if math.isnan(f) or math.isinf(f) else f


def _compute_zscore(volume: float, volumes: pd.Series) -> float:
    """Z-Score текущего объёма относительно скользящего окна."""
    if len(volumes) < 2:
        return 0.0
    std = volumes.std()
    if std == 0 or math.isnan(std):
        return 0.0
    mean = volumes.mean()
    return (volume - mean) / std


def _compute_vwap(df: pd.DataFrame) -> float:
    """VWAP: накопительный typical_price × volume / cumulative volume."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_tp_vol = (typical * df["volume"]).cumsum()
    cum_vol = df["volume"].cumsum()

    if cum_vol.iloc[-1] == 0:
        return float(df["close"].iloc[-1])

    return float(cum_tp_vol.iloc[-1] / cum_vol.iloc[-1])


def _classify_signal(
    current_close: float,
    prev_close: float,
    current_high: float,
    current_low: float,
    volume_direction: str,
    vwap: float,
) -> str:
    """Классифицирует сигнал как INIT (инициатива) или ABS (поглощение).

    INIT: цена движется в сторону объёма (подтверждение тренда)
    ABS:  цена не движется в сторону объёма или разворачивается (поглощение)
    """
    price_change = current_close - prev_close
    price_pct = abs(price_change) / prev_close if prev_close else 0

    if volume_direction == "buy":
        if price_change > 0 and price_pct >= PRICE_MOVE_THRESHOLD:
            return "INIT"
        elif price_change < 0 or price_pct < PRICE_MOVE_THRESHOLD:
            return "ABS"
    else:
        if price_change < 0 and price_pct >= PRICE_MOVE_THRESHOLD:
            return "INIT"
        elif price_change > 0 or price_pct < PRICE_MOVE_THRESHOLD:
            return "ABS"

    return "ABS"


def detect_whale_signals(df: pd.DataFrame) -> WhaleState:
    """Анализ OHLCV DataFrame на предмет китовых аномалий объёма.

    Args:
        df: DataFrame с колонками [open, high, low, close, volume] и опционально [taker_buy_volume]

    Returns:
        WhaleState с Z-Score, VWAP и списком обнаруженных сигналов
    """
    state = WhaleState()

    if df is None or len(df) < ZSCORE_LOOKBACK + 1:
        return state

    try:
        # ── Z-Score текущего объёма ──
        volumes = df["volume"].astype(float)
        current_volume = float(volumes.iloc[-1])
        sma_window = volumes.iloc[-ZSCORE_LOOKBACK:]
        current_sma = float(sma_window.mean())

        state.z_score = _compute_zscore(current_volume, sma_window)
        state.z_score_abs = abs(state.z_score)
        state.volume_sma = current_sma
        state.volume = current_volume

        # ── VWAP ──
        state.vwap = _compute_vwap(df)

        # ── Детекция аномалий за последние бары ──
        signals = []
        lookback = min(ZSCORE_LOOKBACK + 5, len(df))

        for i in range(-lookback, 0):
            idx = len(df) + i
            if idx < ZSCORE_LOOKBACK:
                continue

            vol_window = volumes.iloc[idx - ZSCORE_LOOKBACK:idx]
            vol = float(volumes.iloc[idx])

            if vol_window.std() == 0:
                continue

            z = _compute_zscore(vol, vol_window)
            z_abs = abs(z)

            # Определяем tier
            if z_abs >= TIER3_THRESHOLD:
                tier = 3
            elif z_abs >= TIER2_THRESHOLD:
                tier = 2
            elif z_abs >= TIER1_THRESHOLD:
                tier = 1
            else:
                continue

            # Направление: определяем по delta (если есть) или по close/open
            if "taker_buy_volume" in df.columns:
                buy_vol = _safe_float(df["taker_buy_volume"].iloc[idx])
                total_vol = vol
                if total_vol > 0:
                    buy_ratio = buy_vol / total_vol
                    direction = "buy" if buy_ratio > 0.55 else "sell" if buy_ratio < 0.45 else "buy"
                else:
                    direction = "buy"
            else:
                close_val = _safe_float(df["close"].iloc[idx])
                open_val = _safe_float(df["open"].iloc[idx])
                direction = "buy" if close_val >= open_val else "sell"

            # Классификация
            prev_close = _safe_float(df["close"].iloc[idx - 1]) if idx > 0 else _safe_float(df["open"].iloc[idx])
            cur_close = _safe_float(df["close"].iloc[idx])
            cur_high = _safe_float(df["high"].iloc[idx])
            cur_low = _safe_float(df["low"].iloc[idx])

            classification = _classify_signal(
                cur_close, prev_close, cur_high, cur_low,
                direction, state.vwap,
            )

            # Timestamp
            ts = None
            if hasattr(df.index[idx], "timestamp"):
                ts = int(df.index[idx].timestamp() * 1000)
            elif isinstance(df.index[idx], (int, float)):
                ts = int(df.index[idx])

            signals.append(WhaleSignal(
                tier=tier,
                direction=direction,
                classification=classification,
                z_score=z,
                volume=vol,
                volume_sma=current_sma,
                price=cur_close,
                bar_index=idx,
                timestamp=ts,
            ))

        # Сортируем по bar_index и берём последние N
        signals.sort(key=lambda s: s.bar_index)
        state.signals = signals[-MAX_SIGNALS_PER_UPDATE:]

        # Считчики
        state.tier1_count = sum(1 for s in signals if s.tier == 1)
        state.tier2_count = sum(1 for s in signals if s.tier == 2)
        state.tier3_count = sum(1 for s in signals if s.tier == 3)

    except Exception as e:
        logger.error(f"Whale detection error: {e}", exc_info=True)

    return state
