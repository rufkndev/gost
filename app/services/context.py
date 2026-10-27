"""Общие зависимости бота, worker и API: БД, Redis, хранилище, профили, бот, очередь, ЮKassa."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import docx_io as dx
from app.core.pipeline import load_template
from app.core.profile import Profile, load_profiles
from app.db.session import make_engine, make_sessionmaker
from app.payments.yookassa_client import YooKassaClient
from app.storage import Storage

log = logging.getLogger(__name__)
BETA_KEY = "cfg:beta_mode"


@dataclass
class AppContext:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    storage: Storage
    profiles: dict[str, Profile]
    bot: Bot | None = None
    arq: ArqRedis | None = None
    yookassa: YooKassaClient | None = None
    _templates: dict[str, dx.Package] = field(default_factory=dict)

    @classmethod
    async def create(
        cls,
        settings: Settings,
        *,
        with_bot: bool = True,
        with_arq: bool = True,
        bot: Bot | None = None,
    ) -> AppContext:
        engine = make_engine(settings.database_url)
        token = settings.bot_token.get_secret_value()
        if bot is None and with_bot and token:
            bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        ctx = cls(
            settings=settings,
            engine=engine,
            sessionmaker=make_sessionmaker(engine),
            redis=Redis.from_url(settings.redis_url),
            storage=Storage.from_settings(settings),
            profiles=load_profiles(settings.profiles_dir),
            bot=bot,
        )
        if with_arq:
            ctx.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        if settings.yookassa_shop_id and settings.yookassa_secret_key.get_secret_value():
            ctx.yookassa = YooKassaClient(
                settings.yookassa_shop_id, settings.yookassa_secret_key.get_secret_value()
            )
        return ctx

    async def close(self) -> None:
        if self.arq is not None:
            await self.arq.aclose()
        if self.yookassa is not None:
            await self.yookassa.aclose()
        await self.redis.aclose()
        await self.engine.dispose()
        if self.bot is not None:
            await self.bot.session.close()

    def template(self, profile_id: str) -> dx.Package:
        if profile_id not in self._templates:
            self._templates[profile_id] = load_template(
                self.settings.profiles_dir, self.profiles[profile_id]
            )
        return self._templates[profile_id].copy()

    async def enqueue(
        self, func: str, *args: Any, job_id: str | None = None, defer_s: int | None = None
    ) -> None:
        if self.arq is None:
            raise RuntimeError("arq pool is not configured")
        kwargs: dict[str, Any] = {"_job_id": job_id}
        if defer_s:
            kwargs["_defer_by"] = defer_s
        await self.arq.enqueue_job(func, *args, **kwargs)

    async def beta_mode(self) -> bool:
        val = await self.redis.get(BETA_KEY)
        if val is None:
            return self.settings.beta_mode
        return bool(val == b"1")

    async def set_beta_mode(self, on: bool) -> None:
        await self.redis.set(BETA_KEY, "1" if on else "0")
