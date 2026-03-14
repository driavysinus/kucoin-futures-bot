"""
main.py — точка входа KuCoin Futures Telegram Bot
"""

import asyncio
import sys
from loguru import logger
from telegram_bot import TradingBot


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
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    import os
    os.makedirs("logs", exist_ok=True)
    asyncio.run(main())
