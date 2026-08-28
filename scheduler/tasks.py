"""
scheduler/tasks.py — APScheduler задачи

Параметры расписания берутся из config.scheduler (hot-reload safe).
"""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from config.settings import config
from scheduler.scanner import run_scan_cycle


class TaskScheduler:
    def __init__(self, notify_callback):
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._notify_callback = notify_callback
        self._history_failures = 0

    def setup(self):
        """Настраиваем расписание.

        1. Сканирование каждые 15 минут
        2. Ежедневный отчёт в 00:05 UTC
        3. Обновление кэша 15m-истории каждые 15 минут
        """
        sc = config.scheduler
        self._scheduler.add_job(
            self._scan_job,
            CronTrigger(minute=sc.scan_minutes),
            id="scan_all_tfs",
            name="Scan all primary timeframes",
            max_instances=1,
            coalesce=True,
        )

        # Daily report at 00:05 UTC
        self._scheduler.add_job(
            self._daily_report_job,
            CronTrigger(hour=0, minute=5),
            id="daily_report",
            name="Daily trading report",
            max_instances=1,
            coalesce=True,
        )

        # Keep the 15m history cache current (used by --source=local backtests)
        self._scheduler.add_job(
            self._update_history_job,
            CronTrigger(minute="*/15"),
            id="update_history_cache",
            name="Update 15m OHLCV history cache",
            max_instances=1,
            coalesce=True,
        )

        # Full offline-cache sync for backtests (every 6 hours, idempotent)
        self._scheduler.add_job(
            self._update_ohlcv_cache_job,
            CronTrigger(hour="*/6"),
            id="update_ohlcv_cache",
            name="Sync offline 15m OHLCV cache",
            max_instances=1,
            coalesce=True,
        )

        logger.info(
            "Scheduler configured: scanning every 15 min, daily report at 00:05 UTC, "
            "history cache update every 15 min, offline cache sync every 6h"
        )

    async def _scan_job(self, timeframes: list[str] | None = None):
        logger.info(f"Scheduler triggered: starting scan (tfs={timeframes})")
        try:
            from bot.notifier import send_signal_blocked
            await run_scan_cycle(
                self._notify_callback,
                blocked_callback=send_signal_blocked,
                timeframes=timeframes,
            )
        except Exception as e:
            logger.error(f"Scan job error: {e}", exc_info=True)

    async def _daily_report_job(self):
        logger.info("Scheduler triggered: generating daily report")
        try:
            from analytics.daily_report import generate_and_save, generate_summary
            from bot.notifier import get_bot
            from telegram.constants import ParseMode

            filepath = await generate_and_save()
            logger.info(f"Daily report saved: {filepath}")

            # Send summary to Telegram
            summary = await generate_summary()
            bot = get_bot()
            if bot and config.telegram.channel_id:
                await bot.send_message(
                    chat_id=config.telegram.channel_id,
                    text=summary,
                    parse_mode=ParseMode.HTML,
                )
                logger.info("Daily report summary sent to Telegram")
        except Exception as e:
            logger.error(f"Daily report job error: {e}", exc_info=True)

    async def _update_history_job(self):
        """Incrementally update the 15m OHLCV history cache for all symbols.

        Не роняет scheduler при ошибках; после 3 сбоев подряд шлёт ERROR
        в Telegram через bot/notifier.send_error_alert.
        """
        from backtest.cache_ohlcv import BASE_TIMEFRAME, update_history
        from bot.notifier import send_error_alert
        from config.settings import get_active_symbols

        symbols = get_active_symbols()
        if not symbols:
            return
        logger.info(f"Scheduler: updating 15m OHLCV history for {len(symbols)} symbols")
        for symbol in symbols:
            try:
                added = await update_history(
                    symbol,
                    timeframe=BASE_TIMEFRAME,
                    market_type=config.exchange.market_type,
                )
                self._history_failures = 0
                logger.info(f"History update {symbol}: +{added} candles")
            except Exception as e:
                self._history_failures += 1
                logger.error(f"History update failed for {symbol}: {e}", exc_info=True)
                if self._history_failures >= 3:
                    try:
                        await send_error_alert(
                            f"OHLCV history update failed 3x in a row: {e}"
                        )
                    except Exception:
                        logger.exception("Failed to send history-update error alert")
                    self._history_failures = 0

    async def _update_ohlcv_cache_job(self):
        """Sync the offline 15m OHLCV cache for all symbols (every 6 hours).

        Идемпотентно (update_history сам определяет недостающие свечи),
        поэтому не дублирует запросы к бирже с outcome tracker. try/except —
        не роняет scheduler.
        """
        from backtest.cache_ohlcv import BASE_TIMEFRAME, update_history
        from config.settings import get_active_symbols

        symbols = get_active_symbols()
        if not symbols:
            return
        logger.info(f"Scheduler: syncing offline 15m OHLCV cache for {len(symbols)} symbols")
        for symbol in symbols:
            try:
                added = await update_history(
                    symbol,
                    timeframe=BASE_TIMEFRAME,
                    market_type=config.exchange.market_type,
                )
                logger.info(f"Offline cache sync {symbol}: +{added} candles")
            except Exception as e:
                logger.error(f"Offline cache sync failed for {symbol}: {e}", exc_info=True)

    def start(self):
        self._scheduler.start()
        logger.info("Scheduler started")

    def stop(self):
        self._scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
