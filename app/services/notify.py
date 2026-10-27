"""Сообщения пользователю и владельцу (из worker и API тоже)."""

from __future__ import annotations

import logging

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup

from app.services.context import AppContext

log = logging.getLogger(__name__)


async def send(
    ctx: AppContext, chat_id: int, text: str, kb: InlineKeyboardMarkup | None = None
) -> int | None:
    if ctx.bot is None:
        log.info("bot disabled, message skipped", extra={"chat_id": chat_id})
        return None
    try:
        msg = await ctx.bot.send_message(
            chat_id, text, reply_markup=kb, disable_web_page_preview=True
        )
        return msg.message_id
    except TelegramAPIError as exc:
        log.warning("telegram send failed", extra={"chat_id": chat_id, "error": str(exc)[:200]})
        return None


async def send_file(
    ctx: AppContext,
    chat_id: int,
    data: bytes,
    filename: str,
    caption: str | None = None,
    kb: InlineKeyboardMarkup | None = None,
) -> bool:
    if ctx.bot is None:
        return False
    try:
        await ctx.bot.send_document(
            chat_id, BufferedInputFile(data, filename=filename), caption=caption, reply_markup=kb
        )
        return True
    except TelegramAPIError as exc:
        log.warning(
            "telegram file send failed", extra={"chat_id": chat_id, "error": str(exc)[:200]}
        )
        return False


async def owner(ctx: AppContext, text: str) -> None:
    for admin_id in ctx.settings.admin_ids:
        await send(ctx, admin_id, text)
