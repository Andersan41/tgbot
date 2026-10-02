"""
config/logger.py — Настройка логирования через loguru
"""
import sys
import os
from loguru import logger
from config.settings import config


def setup_logger():
    os.makedirs(os.path.dirname(config.log_file), exist_ok=True)

    # Создаём logs.txt для уведомлений о блокировке (простой файл, без rotation)
    # LOG_DIR позволяет тестам уводить синк во временную папку
    logs_dir = os.getenv("LOG_DIR", "logs")
    os.makedirs(logs_dir, exist_ok=True)

    logger.remove()

    # Консоль
    logger.add(
        sys.stdout,
        level=config.log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        colorize=True,
    )

    # Файл bot.log (технические логи)
    logger.add(
        config.log_file,
        level=config.log_level,
        rotation="10 MB",
        retention="14 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} - {message}",
    )

    # Файл logs.txt для уведомлений о блокировке сигналов
    logger.add(
        os.path.join(logs_dir, "logs.txt"),
        level="INFO",
        rotation="1 day",
        retention="7 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
        filter=lambda record: "BLOCKED" in record["extra"].get("tags", "") or
                              "signal_block" in record["extra"].get("tags", ""),
    )

    return logger


def setup_error_sink(bot):
    """Добавляет Telegram-sink для ERROR+ логов (T3.2).

    Вызывается из main.py после создания Application.
    """
    from config.settings import config as app_config

    if not app_config.telegram.error_channel_id:
        return

    async def telegram_sink(message):
        try:
            await bot.send_message(
                app_config.telegram.error_channel_id,
                text=f"\u26a0\ufe0f {str(message.record['message'])[:3500]}",
            )
        except Exception as e:
            # Avoid infinite loop if this fails
            logger.opt(depth=0).error(f"Telegram error sink failed: {e}")

    logger.add(telegram_sink, level="ERROR", enqueue=True, format="{message}")


setup_logger()
