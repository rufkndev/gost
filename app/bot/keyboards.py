"""Клавиатуры. Используются и ботом, и worker (отчёт, вопросы, результат)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import texts
from app.core.profile import Profile


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def b(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def consent() -> InlineKeyboardMarkup:
    return _kb([[b(texts.BTN_ACCEPT, "terms:accept")]])


def main_menu(profiles: dict[str, Profile], receipts: bool) -> InlineKeyboardMarkup:
    rows = [[b(p.title, f"profile:{p.id}")] for p in profiles.values()]
    rows.append([b(texts.BTN_HOW, "menu:how"), b(texts.BTN_DOCS, "menu:docs")])
    last = [b(texts.BTN_SUPPORT, "menu:support")]
    if receipts:
        last.append(b(texts.BTN_EMAIL, "menu:email"))
    rows.append(last)
    return _kb(rows)


def profiles_only(profiles: dict[str, Profile], short: str, exclude: str) -> InlineKeyboardMarkup:
    return _kb(
        [[b(p.title, f"reprofile:{short}:{p.id}")] for p in profiles.values() if p.id != exclude]
    )


def preliminary(short: str, price: int, beta: bool) -> InlineKeyboardMarkup:
    pay = texts.BTN_PAY_BETA if beta else texts.BTN_PAY.format(price=price)
    return _kb(
        [
            [b(pay, f"pay:{short}")],
            [b(texts.BTN_OTHER_PROFILE, f"other:{short}"), b(texts.BTN_CANCEL, f"cancel:{short}")],
        ]
    )


def payment(url: str, short: str) -> InlineKeyboardMarkup:
    return _kb(
        [
            [InlineKeyboardButton(text=texts.BTN_GO_PAY, url=url)],
            [b(texts.BTN_CHECK_PAY, f"check:{short}")],
        ]
    )


def retry_pay(short: str, price: int) -> InlineKeyboardMarkup:
    return _kb([[b(texts.BTN_PAY.format(price=price), f"pay:{short}")]])


def question(qid: str, options: list[dict[str, object]]) -> InlineKeyboardMarkup:
    return _kb([[b(str(o["label"]), f"q:{qid}:{i}")] for i, o in enumerate(options)])


def feedback(short: str) -> InlineKeyboardMarkup:
    return _kb([[b(texts.BTN_OK, f"fb:ok:{short}"), b(texts.BTN_PROBLEM, f"fb:bad:{short}")]])


def keep_file(short: str) -> InlineKeyboardMarkup:
    return _kb(
        [[b(texts.BTN_KEEP_YES, f"keep:yes:{short}"), b(texts.BTN_KEEP_NO, f"keep:no:{short}")]]
    )
