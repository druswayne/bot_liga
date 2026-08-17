import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

import config


def _setup_logging() -> None:
    """Пишем логи в файл bot.log и сразу в stdout."""
    try:
        sys.stdout.reconfigure(line_buffering=True, write_through=True)
        sys.stderr.reconfigure(line_buffering=True, write_through=True)
    except (AttributeError, OSError):
        pass

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.setLevel(logging.INFO)

    log_file = RotatingFileHandler(
        config.LOG_PATH,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    log_file.setFormatter(formatter)
    log_file.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(log_file)


_setup_logging()

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

import db
from handlers_auth import router as auth_router
from handlers_emails import router as emails_router
from handlers_ig import router as ig_router
from handlers_reply import router as reply_router
from ig_client import ig_loop
from mail_imap import mail_loop
from middleware import AccessMiddleware

logger = logging.getLogger(__name__)


async def main() -> None:
    if not config.BOT_TOKEN:
        logger.error("Укажите BOT_TOKEN в файле .env")
        sys.exit(1)
    if not config.ADMIN_PASSWORD:
        logger.warning("ADMIN_PASSWORD в .env пустой — вход в бота будет недоступен")
    if not config.MAIL_PASSWORD:
        logger.warning("MAIL_PASSWORD в .env пустой — проверка почты не запустится")
    if not config.IG_USERNAME or not config.IG_PASSWORD:
        logger.warning("IG_USERNAME или IG_PASSWORD в .env пустые — проверка Instagram не запустится")

    config.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    await db.init_db()

    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())
    dp.include_router(auth_router)
    dp.include_router(reply_router)
    dp.include_router(emails_router)
    dp.include_router(ig_router)

    asyncio.create_task(mail_loop(bot))
    asyncio.create_task(ig_loop(bot))
    logger.info("Бот запущен, логи пишутся в %s", config.LOG_PATH)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
