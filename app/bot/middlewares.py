"""Пользователь в БД, бан, согласие с условиями, лимит сообщений."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot import texts
from app.db.repo import get_or_create_user
from app.services.context import AppContext


class UserMiddleware(BaseMiddleware):
    """AppContext приходит из workflow_data диспетчера (dp["app"])."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = getattr(event, "from_user", None)
        if tg_user is None:
            return await handler(event, data)
        app: AppContext = data["app"]
        minute = int(time.time() // 60)
        key = f"rl:msg:{tg_user.id}:{minute}"
        count = await app.redis.incr(key)
        if count == 1:
            await app.redis.expire(key, 70)
        if count > app.settings.rate_limit_msgs_per_minute:
            return None
        async with app.sessionmaker.begin() as s:
            user = await get_or_create_user(s, tg_user.id, tg_user.username, tg_user.first_name)
            banned = user.is_banned
            consent_ok = user.terms_version == app.settings.terms_version
        if banned:
            return None
        data["app"] = app
        data["consent_ok"] = consent_ok
        is_start = isinstance(event, Message) and (event.text or "").startswith("/start")
        is_accept = isinstance(event, CallbackQuery) and event.data == "terms:accept"
        is_admin = tg_user.id in app.settings.admin_ids
        if not consent_ok and not (is_start or is_accept or is_admin):
            if isinstance(event, Message):
                await event.answer(texts.NEED_CONSENT)
            elif isinstance(event, CallbackQuery):
                await event.answer(texts.NEED_CONSENT, show_alert=True)
            return None
        return await handler(event, data)
