"""Ответы на вопросы, отзыв и жалоба."""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.states import Flow
from app.db.models import Feedback
from app.db.order_fsm import OrderStatus as S
from app.services import orders
from app.services.context import AppContext

router = Router(name="feedback")


@router.callback_query(F.data.startswith("q:"))
async def answer(call: CallbackQuery, app: AppContext) -> None:
    _, qid, idx = (call.data or "q::").split(":", 2)
    try:
        label = await orders.answer_question(app, uuid.UUID(qid), int(idx), call.from_user.id)
    except ValueError:
        label = None
    await call.answer()
    if label and isinstance(call.message, Message):
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer(texts.ANSWER_SAVED.format(label=label))


@router.callback_query(F.data.startswith("fb:"))
async def feedback(call: CallbackQuery, app: AppContext, state: FSMContext) -> None:
    _, kind, short = (call.data or "fb::").split(":", 2)
    await call.answer()
    order = await orders.order_by_short(app, short)
    if order is None or order.user_id != call.from_user.id or not isinstance(call.message, Message):
        return
    await call.message.edit_reply_markup(reply_markup=None)
    if kind == "ok":
        async with app.sessionmaker.begin() as s:
            s.add(Feedback(order_id=order.id, rating="ok"))
        await call.message.answer(texts.THANKS)
        await call.message.answer(
            texts.MENU, reply_markup=kb.main_menu(app.profiles, app.settings.receipts_enabled)
        )
        return
    if order.status != S.DONE:
        return
    await state.set_state(Flow.complaint_text)
    await state.update_data(short=short)
    await call.message.answer(texts.DESCRIBE_PROBLEM)


@router.message(Flow.complaint_text)
async def complaint_text(message: Message, state: FSMContext, app: AppContext) -> None:
    data = await state.get_data()
    await state.update_data(comment=(message.text or "")[:4000])
    await message.answer(
        texts.ASK_KEEP_FILE.format(days=app.settings.retention_complaint_days),
        reply_markup=kb.keep_file(data.get("short", "")),
    )


@router.callback_query(F.data.startswith("keep:"))
async def keep(call: CallbackQuery, app: AppContext, state: FSMContext) -> None:
    _, yes, short = (call.data or "keep::").split(":", 2)
    await call.answer()
    data = await state.get_data()
    await state.clear()
    order = await orders.order_by_short(app, short)
    if order is None or order.user_id != call.from_user.id or order.status != S.DONE:
        return
    await orders.complaint(app, order, str(data.get("comment", "")), keep=yes == "yes")
    if isinstance(call.message, Message):
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer(texts.COMPLAINT_SAVED)
