"""Кнопки отчёта: оплата, проверка оплаты, другой профиль, отмена."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.states import Flow
from app.db.models import User
from app.db.order_fsm import OrderStatus as S
from app.db.repo import transition_order
from app.payments.service import PaymentService, PaymentUnavailable
from app.services import orders
from app.services.context import AppContext

router = Router(name="order")


async def _own_order(app: AppContext, short: str, user_id: int):  # type: ignore[no-untyped-def]
    order = await orders.order_by_short(app, short)
    if order is None or order.user_id != user_id:
        return None
    return order


async def start_payment(message: Message, app: AppContext, short: str, user_id: int) -> None:
    order = await _own_order(app, short, user_id)
    if order is None:
        return
    if await orders.is_beta_user(app, user_id):
        if await orders.mark_paid_beta(app, order.id):
            await message.answer(texts.BETA_FREE)
        return
    try:
        url = await PaymentService(app).start(order.id)
    except PaymentUnavailable:
        await message.answer(texts.PAY_ERROR)
        return
    await message.answer(
        texts.PAY_LINK.format(price=order.price_rub, minutes=app.settings.payment_ttl_minutes),
        reply_markup=kb.payment(url, short),
    )


@router.callback_query(F.data.startswith("pay:"))
async def pay(call: CallbackQuery, app: AppContext, state: FSMContext) -> None:
    short = (call.data or "").split(":", 1)[1]
    await call.answer()
    if not isinstance(call.message, Message):
        return
    beta = await orders.is_beta_user(app, call.from_user.id)
    if app.settings.receipts_enabled and not beta:
        async with app.sessionmaker() as s:
            user = await s.get(User, call.from_user.id)
        if user is None or not user.email:
            await state.set_state(Flow.waiting_email)
            await state.update_data(short=short)
            await call.message.answer(texts.ASK_EMAIL)
            return
    await start_payment(call.message, app, short, call.from_user.id)


@router.callback_query(F.data.startswith("check:"))
async def check(call: CallbackQuery, app: AppContext) -> None:
    short = (call.data or "").split(":", 1)[1]
    order = await _own_order(app, short, call.from_user.id)
    if order is None:
        await call.answer()
        return
    try:
        result = await PaymentService(app).check_order(order.id)
    except PaymentUnavailable:
        await call.answer(texts.PAY_ERROR, show_alert=True)
        return
    if result in ("paid", "already"):
        await call.answer()
    elif result == "canceled":
        await call.answer(texts.PAY_CANCELED, show_alert=True)
    else:
        refreshed = await orders.order_by_short(app, short)
        msg = (
            texts.PAY_EXPIRED if refreshed and refreshed.status == S.EXPIRED else texts.PAY_NOT_YET
        )
        await call.answer(msg, show_alert=True)


@router.callback_query(F.data.startswith("other:"))
async def other_profile(call: CallbackQuery, app: AppContext) -> None:
    short = (call.data or "").split(":", 1)[1]
    await call.answer()
    order = await _own_order(app, short, call.from_user.id)
    if order is None or not isinstance(call.message, Message):
        return
    await call.message.answer(
        texts.MENU, reply_markup=kb.profiles_only(app.profiles, short, order.profile_id)
    )


@router.callback_query(F.data.startswith("reprofile:"))
async def reprofile(call: CallbackQuery, app: AppContext) -> None:
    _, short, pid = (call.data or "").split(":", 2)
    await call.answer()
    order = await _own_order(app, short, call.from_user.id)
    if order is None or pid not in app.profiles or not isinstance(call.message, Message):
        return
    new = await orders.clone_for_profile(app, order, pid)
    if new is None:
        await call.message.answer(texts.CHOOSE_PROFILE_FIRST)
        return
    await call.message.answer(texts.ANALYZING)
    await app.enqueue("analyze_order", str(new.id), job_id=f"analyze:{new.id}")


@router.callback_query(F.data.startswith("cancel:"))
async def cancel(call: CallbackQuery, app: AppContext) -> None:
    short = (call.data or "").split(":", 1)[1]
    await call.answer()
    order = await _own_order(app, short, call.from_user.id)
    if order is None or not isinstance(call.message, Message):
        return
    if order.status == S.AWAITING_PAYMENT:
        async with app.sessionmaker.begin() as s:
            await transition_order(s, order.id, S.CANCELED, event_type="user_cancel")
    await call.message.answer(texts.CANCELLED)
