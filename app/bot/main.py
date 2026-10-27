"""Процесс бота: aiogram, long polling, FSM в Redis. Документы здесь не обрабатываются."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage

from app.bot.handlers import admin, feedback, order, start, upload
from app.bot.middlewares import UserMiddleware
from app.config import get_settings
from app.logging_setup import setup_logging
from app.services.context import AppContext

log = logging.getLogger(__name__)


def build_dispatcher(storage: RedisStorage, app: AppContext) -> Dispatcher:
    dp = Dispatcher(storage=storage)
    dp["app"] = app
    mw = UserMiddleware()
    dp.message.outer_middleware(mw)
    dp.callback_query.outer_middleware(mw)
    for r in (admin.router, start.router, upload.router, order.router, feedback.router):
        dp.include_router(r)
    return dp


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.sentry_dsn, settings.environment)
    token = settings.bot_token.get_secret_value()
    if not token:
        # Для локального `docker compose up` без токена: сервис жив, но Telegram не опрашивает.
        log.warning("BOT_TOKEN is not set, bot is idle")
        await asyncio.Event().wait()
        return
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    app = await AppContext.create(settings, bot=bot)
    storage = RedisStorage.from_url(settings.redis_url)
    dp = build_dispatcher(storage, app)
    log.info("bot started")
    try:
        await dp.start_polling(bot)
    finally:
        await storage.close()
        await app.close()


if __name__ == "__main__":
    asyncio.run(main())
