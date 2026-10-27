"""/admin — только для ADMIN_IDS."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import func, select

from app.bot import texts
from app.db.models import LlmCall, Order, Payment, User
from app.db.order_fsm import OrderStatus as S
from app.payments.service import PaymentService
from app.services import orders
from app.services.context import AppContext

router = Router(name="admin")


async def _stats(app: AppContext, days: int) -> str:
    since = datetime.now(UTC) - timedelta(days=days)
    async with app.sessionmaker() as s:
        by_status = dict(
            (
                await s.execute(
                    select(Order.status, func.count())
                    .where(Order.created_at >= since)
                    .group_by(Order.status)
                )
            ).all()
        )
        revenue = await s.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.status == "succeeded", Payment.created_at >= since
            )
        )
        llm = await s.scalar(
            select(func.coalesce(func.sum(LlmCall.cost_usd), 0)).where(LlmCall.created_at >= since)
        )
        users = await s.scalar(
            select(func.count()).select_from(User).where(User.created_at >= since)
        )
    total = sum(by_status.values())
    parts = ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items())) or "нет"
    return (
        f"<b>За {days} дн.</b>: заказов {total} ({parts}); выручка {revenue} ₽; "
        f"ИИ ${float(llm or 0):.4f}; новых пользователей {users}"
    )


@router.message(Command("admin"))
async def admin(message: Message, command: CommandObject, app: AppContext) -> None:
    if message.from_user is None or message.from_user.id not in app.settings.admin_ids:
        return
    args = (command.args or "").split()
    cmd = args[0] if args else ""
    if not cmd:
        beta = "вкл" if await app.beta_mode() else "выкл"
        await message.answer(
            "\n".join(
                [
                    await _stats(app, 1),
                    await _stats(app, 7),
                    f"Режим беты: {beta}",
                    "",
                    texts.ADMIN_HELP,
                ]
            )
        )
    elif cmd in ("orders", "problems"):
        async with app.sessionmaker() as s:
            q = select(Order).order_by(Order.created_at.desc()).limit(15)
            if cmd == "problems":
                q = q.where(Order.status.in_([S.FAILED, S.COMPLAINT]))
            rows = (await s.scalars(q)).all()
        lines = [
            f"{o.short_id} {o.status} {o.profile_id} {o.created_at:%d.%m %H:%M}"
            + (f" [{o.error_code}]" if o.error_code else "")
            for o in rows
        ]
        await message.answer("\n".join(lines) or "пусто")
    elif cmd == "refund" and len(args) > 1:
        order = await orders.order_by_short(app, args[1].upper())
        if order is None:
            await message.answer("заказ не найден")
            return
        status = await PaymentService(app).refund(order.id, reason="manual")
        await message.answer(f"Возврат {order.short_id}: {status}")
    elif cmd == "reprocess" and len(args) > 1:
        order = await orders.order_by_short(app, args[1].upper())
        if order is None:
            await message.answer("заказ не найден")
            return
        await app.enqueue("reprocess_order", str(order.id), job_id=f"reprocess:{order.id}")
        await message.answer("Повторная обработка поставлена в очередь")
    elif cmd == "beta" and len(args) > 1:
        await app.set_beta_mode(args[1] == "on")
        await message.answer(f"Режим беты: {'вкл' if args[1] == 'on' else 'выкл'}")
    elif cmd in ("ban", "unban", "betauser") and len(args) > 1 and args[1].isdigit():
        async with app.sessionmaker.begin() as s:
            user = await s.get(User, int(args[1]))
            if user is None:
                await message.answer("пользователь не найден")
                return
            if cmd == "betauser":
                user.is_beta = True
            else:
                user.is_banned = cmd == "ban"
        await message.answer("готово")
    else:
        await message.answer(texts.ADMIN_HELP)
