"""Доступ к данным. Смена статуса заказа — только через `transition_order`."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, Order, User
from app.db.order_fsm import OrderStatus, check_transition


class OrderNotFound(Exception):
    pass


async def add_event(
    session: AsyncSession,
    order_id: uuid.UUID | None,
    type_: str,
    payload: dict[str, Any] | None = None,
) -> Event:
    event = Event(order_id=order_id, type=type_, payload=payload or {})
    session.add(event)
    return event


async def lock_order(session: AsyncSession, order_id: uuid.UUID) -> Order:
    """SELECT … FOR UPDATE: вызывать внутри транзакции."""
    order = await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
    if order is None:
        raise OrderNotFound(str(order_id))
    return order


async def transition_order(
    session: AsyncSession,
    order_id: uuid.UUID,
    target: OrderStatus,
    *,
    event_type: str | None = None,
    payload: dict[str, Any] | None = None,
) -> Order:
    """Переводит заказ в новый статус под блокировкой строки и пишет событие аудита.

    Транзакцией управляет вызывающий код. Недопустимый переход — `InvalidTransition`.
    """
    order = await lock_order(session, order_id)
    current = OrderStatus(order.status)
    check_transition(current, target)
    order.status = target
    await add_event(
        session,
        order.id,
        event_type or "status_changed",
        {"from": current.value, "to": target.value, **(payload or {})},
    )
    return order


async def get_or_create_user(
    session: AsyncSession, tg_id: int, username: str | None, first_name: str | None
) -> User:
    user = await session.get(User, tg_id)
    if user is None:
        user = User(tg_id=tg_id, username=username, first_name=first_name)
        session.add(user)
    else:
        user.username = username
        user.first_name = first_name
    return user
