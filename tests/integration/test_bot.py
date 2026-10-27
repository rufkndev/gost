"""Сценарий бота с подменённым Telegram: согласие → меню → профиль → файл → анализ в очереди."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    GetFile,
    SendMediaGroup,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Document, File, Message, Update, User
from sqlalchemy import select

from app.bot.main import build_dispatcher
from app.db.models import Order
from app.db.models import User as DbUser
from app.services.context import AppContext
from tests.integration.conftest import TEST_REDIS_URL
from tests.integration.fakes import make_ctx
from tests.synth import crooked_document


class FakeSession(BaseSession):
    def __init__(self, file_bytes: bytes) -> None:
        super().__init__()
        self.sent: list[TelegramMethod[Any]] = []
        self.file_bytes = file_bytes

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 — сигнатура задана aiogram
    ) -> Any:
        self.sent.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.sent),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        if isinstance(method, SendMediaGroup):
            return [
                Message(
                    message_id=len(self.sent) + i,
                    date=datetime.now(UTC),
                    chat=Chat(id=int(method.chat_id), type="private"),
                )
                for i, _ in enumerate(method.media)
            ]
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u", file_path="docs/file.docx")
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 — сигнатура задана aiogram
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield self.file_bytes

    async def close(self) -> None:
        return None

    def texts(self) -> list[str]:
        return [str(m.text) for m in self.sent if isinstance(m, SendMessage)]


@pytest.fixture
async def setup(
    migrated_db: str, tmp_path: Path
) -> AsyncIterator[tuple[AppContext, Bot, FakeSession]]:
    session = FakeSession(crooked_document())
    bot = Bot("123456:TEST", session=session)
    ctx = await make_ctx(migrated_db, TEST_REDIS_URL, tmp_path)
    ctx.bot = bot
    await ctx.redis.flushdb()
    yield ctx, bot, session
    await ctx.close()


_DP: Any = None


def dispatcher(ctx: AppContext) -> Any:
    """Роутеры aiogram подключаются один раз — диспетчер общий, контекст подменяем."""
    global _DP
    if _DP is None:
        _DP = build_dispatcher(MemoryStorage(), ctx)  # type: ignore[arg-type]
    _DP["app"] = ctx
    return _DP


def tg_user(uid: int) -> User:
    return User(id=uid, is_bot=False, first_name="Студент", username="student")


def msg_update(uid: int, n: int, text: str | None = None, doc: Document | None = None) -> Update:
    return Update(
        update_id=n,
        message=Message(
            message_id=n,
            date=datetime.now(UTC),
            chat=Chat(id=uid, type="private"),
            from_user=tg_user(uid),
            text=text,
            document=doc,
        ),
    )


def cb_update(uid: int, n: int, data: str) -> Update:
    msg = Message(
        message_id=n,
        date=datetime.now(UTC),
        chat=Chat(id=uid, type="private"),
        from_user=tg_user(uid),
        text="…",
    )
    return Update(
        update_id=n,
        callback_query=CallbackQuery(
            id=str(n), from_user=tg_user(uid), chat_instance="ci", data=data, message=msg
        ),
    )


async def test_consent_menu_upload(setup: tuple[AppContext, Bot, FakeSession]) -> None:
    ctx, bot, session = setup
    dp = dispatcher(ctx)
    uid = uuid.uuid4().int % 10**9

    await dp.feed_update(bot, msg_update(uid, 1, "/start"))
    assert any("Принимаю условия" in t or "оферта" in t for t in session.texts())
    groups = [m for m in session.sent if isinstance(m, SendMediaGroup)]
    assert groups and len(groups[0].media) == 3  # оферта, политика ПДн, правила — файлами
    names = {str(m.media.filename) for m in groups[0].media}  # type: ignore[union-attr]
    assert all(n.endswith(".docx") for n in names) and "Публичная оферта.docx" in names
    # Без согласия файл не принимается.
    await dp.feed_update(bot, cb_update(uid, 2, "profile:misis"))
    assert any(isinstance(m, AnswerCallbackQuery) and m.show_alert for m in session.sent)

    await dp.feed_update(bot, cb_update(uid, 3, "terms:accept"))
    async with ctx.sessionmaker() as s:
        user = await s.get(DbUser, uid)
    assert user is not None and user.terms_version == ctx.settings.terms_version

    await dp.feed_update(bot, cb_update(uid, 4, "profile:misis"))
    assert any("Пришлите файл .docx" in t for t in session.texts())

    await dp.feed_update(
        bot,
        msg_update(
            uid,
            5,
            doc=Document(file_id="f1", file_unique_id="u1", file_name="Работа.doc", file_size=100),
        ),
    )
    assert any("Сохранить как" in t for t in session.texts())  # .doc — инструкция

    await dp.feed_update(
        bot,
        msg_update(
            uid,
            6,
            doc=Document(
                file_id="f2", file_unique_id="u2", file_name="Работа.docx", file_size=50_000
            ),
        ),
    )
    assert "Анализирую документ…" in session.texts()
    async with ctx.sessionmaker() as s:
        order = await s.scalar(select(Order).where(Order.user_id == uid))
    assert order is not None and order.profile_id == "misis"
    assert ctx.storage.exists(order.src_path)
    assert ctx.arq is not None
    jobs = [j.function for j in await ctx.arq.queued_jobs()]
    assert "analyze_order" in jobs


async def test_admin_only_for_admins(setup: tuple[AppContext, Bot, FakeSession]) -> None:
    ctx, bot, session = setup
    dp = dispatcher(ctx)
    await dp.feed_update(bot, msg_update(999, 1, "/admin"))
    assert not any("За 1 дн." in t for t in session.texts())
    await dp.feed_update(bot, msg_update(1, 2, "/admin"))  # ADMIN_IDS=[1] в make_ctx
    assert any("За 1 дн." in t for t in session.texts())


async def test_rate_limit_documents(setup: tuple[AppContext, Bot, FakeSession]) -> None:
    from app.services import orders

    ctx, _, _ = setup
    uid = uuid.uuid4().int % 10**9
    from app.db.repo import get_or_create_user

    async with ctx.sessionmaker.begin() as s:
        await get_or_create_user(s, uid, "u", "U")
    for _ in range(3):
        await orders.create_order(ctx, uid, "misis", "a.docx", b"x")
    assert await orders.recent_docs_count(ctx, uid) == 3
