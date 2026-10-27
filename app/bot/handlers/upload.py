"""Выбор профиля и приём файла. Анализ ставится в очередь worker."""

from __future__ import annotations

import io

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import texts
from app.bot.states import Flow
from app.services import orders
from app.services.context import AppContext

router = Router(name="upload")


@router.callback_query(F.data.startswith("profile:"))
async def choose_profile(call: CallbackQuery, app: AppContext, state: FSMContext) -> None:
    pid = (call.data or "").split(":", 1)[1]
    await call.answer()
    if pid not in app.profiles or not isinstance(call.message, Message):
        return
    await state.set_state(Flow.waiting_file)
    await state.update_data(profile_id=pid)
    await call.message.answer(
        texts.SEND_FILE.format(profile=app.profiles[pid].title, mb=app.settings.max_upload_mb)
    )


@router.message(F.document)
async def got_document(message: Message, app: AppContext, state: FSMContext, bot: Bot) -> None:
    doc = message.document
    if doc is None or message.from_user is None:
        return
    data = await state.get_data()
    pid = data.get("profile_id")
    if not pid or pid not in app.profiles:
        await message.answer(texts.CHOOSE_PROFILE_FIRST)
        return
    name = doc.file_name or "document.docx"
    low = name.lower()
    if low.endswith(".doc"):
        await message.answer(texts.REJECT_DOC)
        return
    if low.endswith(".docm"):
        await message.answer(texts.REJECT_DOCM)
        return
    if not low.endswith(".docx"):
        await message.answer(texts.REJECT_EXT)
        return
    limit = app.settings.max_upload_mb * 1024 * 1024
    if (doc.file_size or 0) > limit:
        await message.answer(texts.REJECT_SIZE.format(mb=app.settings.max_upload_mb))
        return
    uid = message.from_user.id
    if await orders.recent_docs_count(app, uid) >= app.settings.rate_limit_docs_per_hour:
        await message.answer(texts.REJECT_RATE)
        return
    buf = io.BytesIO()
    await bot.download(doc, destination=buf)
    content = buf.getvalue()
    import hashlib

    dup = await orders.find_paid_duplicate(app, uid, pid, hashlib.sha256(content).hexdigest())
    if dup is not None:
        await message.answer(texts.DUPLICATE_PAID)
        if await orders.resend_result(app, dup):
            return
    order = await orders.create_order(app, uid, pid, name, content)
    await message.answer(texts.ANALYZING)
    await app.enqueue("analyze_order", str(order.id), job_id=f"analyze:{order.id}")
