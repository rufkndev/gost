"""Старт, согласие с условиями, меню, email."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InputMediaDocument, Message

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.states import Flow
from app.db.models import User
from app.services import legal_docs
from app.services.context import AppContext

router = Router(name="start")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[a-zA-Zа-яА-Я]{2,24}$")


async def send_legal_files(message: Message, app: AppContext) -> None:
    """Оферта, политика ПДн и правила — файлами .docx одной группой."""
    files = legal_docs.all_files(app.settings)
    await message.answer_media_group(
        [InputMediaDocument(media=BufferedInputFile(data, filename=name)) for name, data in files]
    )


async def show_menu(message: Message, app: AppContext) -> None:
    await message.answer(
        texts.MENU, reply_markup=kb.main_menu(app.profiles, app.settings.receipts_enabled)
    )


@router.message(CommandStart())
async def cmd_start(message: Message, app: AppContext, consent_ok: bool, state: FSMContext) -> None:
    await state.clear()
    if consent_ok:
        await show_menu(message, app)
        return
    async with app.sessionmaker() as s:
        user = await s.get(User, message.from_user.id) if message.from_user else None
        had_old = bool(user and user.terms_version)
    intro = texts.CONSENT_UPDATED if had_old else texts.WELCOME
    await message.answer(intro)
    await send_legal_files(message, app)
    await message.answer(texts.CONSENT, reply_markup=kb.consent())


@router.callback_query(F.data == "terms:accept")
async def accept_terms(call: CallbackQuery, app: AppContext) -> None:
    async with app.sessionmaker.begin() as s:
        user = await s.get(User, call.from_user.id)
        if user is not None:
            user.terms_version = app.settings.terms_version
            user.terms_accepted_at = datetime.now(UTC)
    await call.answer()
    if isinstance(call.message, Message):
        await call.message.answer(
            texts.CONSENT_THANKS,
            reply_markup=kb.main_menu(app.profiles, app.settings.receipts_enabled),
        )


@router.callback_query(F.data.startswith("menu:"))
async def menu(call: CallbackQuery, app: AppContext, state: FSMContext) -> None:
    await call.answer()
    if not isinstance(call.message, Message):
        return
    what = (call.data or "").split(":", 1)[1]
    if what == "how":
        await call.message.answer(texts.HOW_IT_WORKS.format(price=app.settings.price_rub))
    elif what == "docs":
        await send_legal_files(call.message, app)
    elif what == "support":
        await call.message.answer(texts.SUPPORT.format(contact=app.settings.support_contact or "—"))
    elif what == "email":
        await state.set_state(Flow.waiting_email)
        await state.update_data(short="")
        await call.message.answer(texts.ASK_EMAIL)


@router.message(Flow.waiting_email)
async def got_email(message: Message, app: AppContext, state: FSMContext) -> None:
    email = (message.text or "").strip()
    if not EMAIL_RE.match(email):
        await message.answer(texts.BAD_EMAIL)
        return
    async with app.sessionmaker.begin() as s:
        user = await s.get(User, message.from_user.id) if message.from_user else None
        if user is not None:
            user.email = email
    short = (await state.get_data()).get("short") or ""
    await state.clear()
    await message.answer(texts.EMAIL_SAVED.format(email=email))
    if short:
        from app.bot.handlers.order import start_payment

        await start_payment(message, app, short, message.from_user.id if message.from_user else 0)
