"""
tests/test_outcome_tracker.py — Unit-тесты на закрытие сигналов по TP/SL.
"""
import sys
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.database import db, Base, Signal, SignalOutcome
from scheduler.outcome_tracker import check_open_outcomes


@pytest.fixture(autouse=True)
async def setup_db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test_outcome.db"
    db._engine = create_async_engine(db_url, echo=False)
    db._session_factory = sessionmaker(
        db._engine, class_=AsyncSession, expire_on_commit=False
    )
    async with db._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await db._engine.dispose()


@pytest.fixture
async def buy_signal():
    """Создаёт BUY-сигнал: entry=100, SL=95, TP=110."""
    # Set entry_candle_open to 2 hours ago to avoid same-candle skip
    entry_candle = datetime.now(timezone.utc) - timedelta(hours=2)
    sig = await db.save_signal(
        symbol="BTC/USDT",
        timeframe="1h",
        signal_type="BUY",
        close_price=100.0,
        sl=95.0,
        tp=110.0,
        score=5,
        reasons=["test"],
        entry_candle_open=entry_candle,
    )
    await db.create_outcome(sig.id)
    return sig


@pytest.fixture
async def sell_signal():
    """Создаёт SELL-сигнал: entry=100, SL=105, TP=90."""
    # Set entry_candle_open to 2 hours ago to avoid same-candle skip
    entry_candle = datetime.now(timezone.utc) - timedelta(hours=2)
    sig = await db.save_signal(
        symbol="ETH/USDT",
        timeframe="1h",
        signal_type="SELL",
        close_price=100.0,
        sl=105.0,
        tp=90.0,
        score=5,
        reasons=["test"],
        entry_candle_open=entry_candle,
    )
    await db.create_outcome(sig.id)
    return sig


@pytest.fixture(autouse=True)
def mock_notification():
    """Mock Telegram notifications to prevent real messages during tests."""
    with patch(
        "scheduler.outcome_tracker._send_close_notification",
        new_callable=AsyncMock,
    ):
        yield


class TestHitTP:
    """Цена пробивает TP → HIT_TP."""

    @pytest.mark.asyncio
    async def test_buy_hit_tp(self, buy_signal, setup_db):
        with patch(
            "scheduler.outcome_tracker.exchange_client.fetch_ticker_price",
            new_callable=AsyncMock, return_value=112.0,
        ):
            await check_open_outcomes()

        stats = await db.get_outcome_stats()
        assert stats["closed"] == 1
        assert stats["wins"] == 1
        assert stats["avg_pnl"] > 0

    @pytest.mark.asyncio
    async def test_sell_hit_tp(self, sell_signal, setup_db):
        with patch(
            "scheduler.outcome_tracker.exchange_client.fetch_ticker_price",
            new_callable=AsyncMock, return_value=88.0,
        ):
            await check_open_outcomes()

        stats = await db.get_outcome_stats()
        assert stats["closed"] == 1
        assert stats["wins"] == 1


class TestHitSL:
    """Цена пробивает SL → HIT_SL."""

    @pytest.mark.asyncio
    async def test_buy_hit_sl(self, buy_signal, setup_db):
        with patch(
            "scheduler.outcome_tracker.exchange_client.fetch_ticker_price",
            new_callable=AsyncMock, return_value=93.0,
        ):
            await check_open_outcomes()

        stats = await db.get_outcome_stats()
        assert stats["closed"] == 1
        assert stats["wins"] == 0  # SL — не win
        assert stats["avg_pnl"] < 0

    @pytest.mark.asyncio
    async def test_sell_hit_sl(self, sell_signal, setup_db):
        with patch(
            "scheduler.outcome_tracker.exchange_client.fetch_ticker_price",
            new_callable=AsyncMock, return_value=107.0,
        ):
            await check_open_outcomes()

        stats = await db.get_outcome_stats()
        assert stats["closed"] == 1
        assert stats["wins"] == 0


class TestNoHit:
    """Цена в коридоре → outcome остаётся OPEN."""

    @pytest.mark.asyncio
    async def test_price_in_corridor(self, buy_signal, setup_db):
        with patch(
            "scheduler.outcome_tracker.exchange_client.fetch_ticker_price",
            new_callable=AsyncMock, return_value=105.0,
        ):
            await check_open_outcomes()

        open_outcomes = await db.get_open_outcomes()
        assert len(open_outcomes) == 1
        stats = await db.get_outcome_stats()
        assert stats["open"] == 1
        assert stats["closed"] == 0


class TestExpired:
    """Просроченный сигнал → EXPIRED."""

    @pytest.mark.asyncio
    async def test_expired_signal(self, setup_db):
        old_ts = datetime.now(timezone.utc) - timedelta(days=8)
        sig = await db.save_signal(
            symbol="SOL/USDT",
            timeframe="1h",
            signal_type="BUY",
            close_price=50.0,
            sl=48.0,
            tp=55.0,
            score=5,
            reasons=["test"],
            entry_candle_open=old_ts - timedelta(hours=1),
        )
        # Обновляем created_at в БД на 8 дней назад
        async with db._session_factory() as session:
            result = await session.execute(
                select(Signal).where(Signal.id == sig.id)
            )
            row = result.scalar_one()
            row.created_at = old_ts
            await session.commit()
        await db.create_outcome(sig.id)

        with patch(
            "scheduler.outcome_tracker.exchange_client.fetch_ticker_price",
            new_callable=AsyncMock, return_value=51.0,
        ), patch(
            "scheduler.outcome_tracker.load_outcome_window",
            new_callable=AsyncMock, return_value=[],
        ):
            await check_open_outcomes()

        stats = await db.get_outcome_stats()
        assert stats["closed"] == 1
        open_outcomes = await db.get_open_outcomes()
        assert len(open_outcomes) == 0


class TestGetOutcomeStats:
    """Проверка get_outcome_stats."""

    @pytest.mark.asyncio
    async def test_empty_stats(self, setup_db):
        stats = await db.get_outcome_stats()
        assert stats["closed"] == 0
        assert stats["open"] == 0
        assert stats["wins"] == 0
        assert stats["avg_pnl"] == 0.0

    @pytest.mark.asyncio
    async def test_stats_with_mixed_results(self, setup_db):
        entry_candle = datetime.now(timezone.utc) - timedelta(hours=2)
        sig1 = await db.save_signal(
            symbol="BTC/USDT", timeframe="1h", signal_type="BUY",
            close_price=100.0, sl=95.0, tp=110.0, score=5, reasons=["a"],
            entry_candle_open=entry_candle,
        )
        sig2 = await db.save_signal(
            symbol="ETH/USDT", timeframe="1h", signal_type="BUY",
            close_price=200.0, sl=190.0, tp=220.0, score=5, reasons=["b"],
            entry_candle_open=entry_candle,
        )
        await db.create_outcome(sig1.id)
        await db.create_outcome(sig2.id)

        # Первый сигнал → HIT_TP (цена 115)
        with patch(
            "scheduler.outcome_tracker.exchange_client.fetch_ticker_price",
            new_callable=AsyncMock, return_value=115.0,
        ):
            await check_open_outcomes()

        # Второй сигнал → HIT_SL (цена 185)
        with patch(
            "scheduler.outcome_tracker.exchange_client.fetch_ticker_price",
            new_callable=AsyncMock, return_value=185.0,
        ):
            await check_open_outcomes()

        stats = await db.get_outcome_stats()
        assert stats["closed"] == 2
        assert stats["wins"] == 1
        assert stats["open"] == 0
        assert stats["avg_pnl"] != 0.0
        assert stats["best_pnl"] > 0
        assert stats["worst_pnl"] < 0


class TestFetchError:
    """fetch_ticker_price возвращает None → скип, outcome остаётся OPEN."""

    @pytest.mark.asyncio
    async def test_fetch_returns_none(self, buy_signal, setup_db):
        with patch(
            "scheduler.outcome_tracker.exchange_client.fetch_ticker_price",
            new_callable=AsyncMock, return_value=None,
        ):
            await check_open_outcomes()

        open_outcomes = await db.get_open_outcomes()
        assert len(open_outcomes) == 1


class TestTickerSLBreachWhileCandleInside:
    """Регрессионный тест: ticker пробивает SL, но закрытая свеча внутри диапазона.

    Баг: outcome_tracker использовал close последней ЗАКРЫТОЙ свечи (iloc[:-1]).
    Если SL пробит на ТЕКУЩЕЙ незакрытой свече — трекер не видел пробоя.
    Исправление: используем fetch_ticker_price (real-time).
    """

    @pytest.mark.asyncio
    async def test_sell_sl_breach_ticker_only(self, sell_signal, setup_db):
        """SELL entry=100, SL=105. Candle close=102 (внутри), ticker=106 (SL пробит)."""
        with patch(
            "scheduler.outcome_tracker.exchange_client.fetch_ticker_price",
            new_callable=AsyncMock, return_value=106.0,
        ):
            await check_open_outcomes()

        stats = await db.get_outcome_stats()
        assert stats["closed"] == 1
        assert stats["wins"] == 0  # SL

    @pytest.mark.asyncio
    async def test_buy_sl_breach_ticker_only(self, buy_signal, setup_db):
        """BUY entry=100, SL=95. Candle close=97 (внутри), ticker=94 (SL пробит)."""
        with patch(
            "scheduler.outcome_tracker.exchange_client.fetch_ticker_price",
            new_callable=AsyncMock, return_value=94.0,
        ):
            await check_open_outcomes()

        stats = await db.get_outcome_stats()
        assert stats["closed"] == 1
        assert stats["wins"] == 0  # SL

    @pytest.mark.asyncio
    async def test_sell_tp_breach_ticker_only(self, sell_signal, setup_db):
        """SELL entry=100, TP=90. Candle close=92 (внутри), ticker=89 (TP пробит)."""
        with patch(
            "scheduler.outcome_tracker.exchange_client.fetch_ticker_price",
            new_callable=AsyncMock, return_value=89.0,
        ):
            await check_open_outcomes()

        stats = await db.get_outcome_stats()
        assert stats["closed"] == 1
        assert stats["wins"] == 1  # TP
