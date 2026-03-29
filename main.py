"""
main.py — точка входа KuCoin Futures Telegram Bot
"""

import asyncio
import sys
from loguru import logger
from telegram_bot import TradingBot
from console_input import ConsoleInput


def setup_logging():
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{line}</cyan> — <level>{message}</level>",
        level="INFO",
    )
    logger.add(
        "logs/bot.log",
        rotation="10 MB",
        retention="7 days",
        level="DEBUG",
    )


async def main():
    setup_logging()
    logger.info("Starting KuCoin Futures Bot…")

    bot = TradingBot()

    # Консольный ввод — передаём все зависимости
    console = ConsoleInput(
        alert_manager=bot.alert_manager,
        order_manager=bot.manager,
        client=bot.client,
        monitor=bot.monitor,
    )

    try:
        await asyncio.gather(
            bot.run(),
            console.start(),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    import os
    os.makedirs("logs", exist_ok=True)
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
