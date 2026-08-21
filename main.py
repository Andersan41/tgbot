"""
main.py — Точка входа. Запускает бота и планировщик.
"""
import asyncio
import sys
import os
import atexit

# Добавляем корень проекта в PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Lock-файл для защиты от повторного запуска
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".trading_bot.lock")


def acquire_lock():
    if os.path.exists(LOCK_FILE):
        with open(LOCK_FILE) as f:
            old_pid = f.read().strip()
        if old_pid.isdigit():
            try:
                os.kill(int(old_pid), 0)  # сигнал 0 = проверка существования процесса
                print(f"Bot already running (PID {old_pid}). Exiting.")
                sys.exit(1)
            except (ProcessLookupError, OSError):
                pass  # процесс мёртв — можно удалить lock
        os.remove(LOCK_FILE)
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(release_lock)


def release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass

from loguru import logger
import config.logger  # noqa — инициализирует логгер

# T3.4 — Prometheus metrics HTTP-сервер (opt-in через METRICS_ENABLED=true)
from monitoring.metrics import _start_metrics_server
_start_metrics_server()

from telegram.ext import Application
from config.settings import config, refresh_runtime_symbols
from data.exchange_client import exchange_client
from storage.database import db
from bot.handlers import register_handlers
from bot.notifier import send_signal, send_error_alert
from scheduler.tasks import TaskScheduler
from context.fetcher import context_fetcher
from web.server import start_web_server
from web.signal_events import dual_notify


async def main():
    acquire_lock()

    logger.info("=" * 60)
    logger.info("  Trading Signal Bot starting...")
    logger.info("=" * 60)

    # Проверяем обязательные переменные
    if not config.telegram.token:
        logger.error("TELEGRAM_BOT_TOKEN is not set! Check .env file.")
        sys.exit(1)

    # Инициализируем БД
    await db.init()
    await refresh_runtime_symbols()
    from config.settings import reload_filter_toggles
    await reload_filter_toggles()

    # Подключаемся к бирже
    await exchange_client.connect()

    # Запускаем веб-сервер (дашборд)
    web_runner = None
    if config.web.enabled:
        try:
            web_runner = await start_web_server()
        except Exception as e:
            logger.warning(f"Web server failed to start: {e}")

    # Создаём Telegram Application с устойчивой к сетевым ошибкам конфигурацией
    from telegram.request import HTTPXRequest
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    app = Application.builder().token(config.telegram.token).request(request).build()

    # T3.3 — мониторинг ошибок хендлеров
    from telegram.error import NetworkError, TimedOut

    async def _handle_app_error(_, context):
        if isinstance(context.error, (NetworkError, TimedOut)):
            logger.warning(f"Telegram network issue: {context.error}")
            return
        logger.error(f"Unhandled app error: {context.error}")

    app.add_error_handler(_handle_app_error)
    register_handlers(app)

    # T3.2 — Telegram error sink для ERROR+ логов
    from config.logger import setup_error_sink
    setup_error_sink(app.bot)

    # Настраиваем планировщик
    scheduler = TaskScheduler(notify_callback=dual_notify)
    scheduler.setup()
    scheduler.start()

    logger.info(f"Bot configured:")
    logger.info(f"  Exchange: {config.exchange.name}")
    logger.info(f"  Symbols: {', '.join(config.trading.symbols)}")
    logger.info(f"  Timeframes: {', '.join(config.trading.primary_timeframes)}")
    logger.info(f"  Confirmation TF: {config.trading.confirm_timeframe}")
    logger.info(f"  Channel: {config.telegram.channel_id}")
    if config.web.enabled:
        logger.info(f"  Web Dashboard: http://{config.web.host}:{config.web.port}")

    # F1: запустить фоновый трекинг outcome'ов
    from scheduler.outcome_tracker import outcome_tracker_loop
    asyncio.create_task(outcome_tracker_loop())

    try:
        # Запускаем бота в режиме polling
        logger.info("Starting Telegram bot (polling mode)...")
        await app.initialize()
        await app.start()

        # Retry polling start to handle Telegram server lingering sessions
        for attempt in range(1, 6):
            try:
                await app.updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=["message", "callback_query"],
                )
                break
            except Exception as e:
                if "Conflict" in str(e) and attempt < 5:
                    logger.warning(f"Telegram conflict on attempt {attempt}, retrying in {attempt * 5}s...")
                    await app.updater.stop()
                    await asyncio.sleep(attempt * 5)
                else:
                    raise

        logger.info("✅ Bot is running. Press Ctrl+C to stop.")

        # Держим event loop живым
        while True:
            await asyncio.sleep(3600)

    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown signal received")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        await send_error_alert(str(e))
    finally:
        logger.info("Shutting down...")
        scheduler.stop()
        await exchange_client.close()
        await context_fetcher.close()
        if web_runner:
            await web_runner.cleanup()
        if app.updater and app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()
        release_lock()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    # FIX N3: Windows asyncio compatibility
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as e:
        logger.error(f"Fatal: {e}", exc_info=True)
        sys.exit(1)
