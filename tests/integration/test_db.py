import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import Event, Order
from app.db.order_fsm import InvalidTransition, OrderStatus
from app.db.repo import get_or_create_user, transition_order
from app.db.session import make_sessionmaker


async def _new_order(engine: AsyncEngine) -> uuid.UUID:
    sm = make_sessionmaker(engine)
    tg_id = uuid.uuid4().int % 10**12
    async with sm.begin() as s:
        await get_or_create_user(s, tg_id, "user", "Имя")
        await s.flush()
        order = Order(
            short_id=uuid.uuid4().hex[:8],
            user_id=tg_id,
            profile_id="misis",
            file_name="a.docx",
            file_hash="0" * 64,
            price_rub=300,
        )
        s.add(order)
    return order.id


async def test_transition_writes_event(engine: AsyncEngine) -> None:
    order_id = await _new_order(engine)
    sm = make_sessionmaker(engine)
    async with sm.begin() as s:
        await transition_order(s, order_id, OrderStatus.ANALYZING)
    async with sm() as s:
        order = await s.get(Order, order_id)
        assert order is not None and order.status == "analyzing"
        events = (await s.scalars(select(Event).where(Event.order_id == order_id))).all()
        assert [e.payload for e in events] == [{"from": "created", "to": "analyzing"}]


async def test_invalid_transition_rolls_back(engine: AsyncEngine) -> None:
    order_id = await _new_order(engine)
    sm = make_sessionmaker(engine)
    with pytest.raises(InvalidTransition):
        async with sm.begin() as s:
            await transition_order(s, order_id, OrderStatus.PAID)
    async with sm() as s:
        order = await s.get(Order, order_id)
        assert order is not None and order.status == "created"


async def test_concurrent_payment_confirmation_applies_once(engine: AsyncEngine) -> None:
    """Две проверки оплаты одновременно: переход в paid ровно один раз (FOR UPDATE)."""
    order_id = await _new_order(engine)
    sm = make_sessionmaker(engine)
    for status in (OrderStatus.ANALYZING, OrderStatus.ANALYZED, OrderStatus.AWAITING_PAYMENT):
        async with sm.begin() as s:
            await transition_order(s, order_id, status)

    async def confirm() -> bool:
        try:
            async with sm.begin() as s:
                await transition_order(s, order_id, OrderStatus.PAID)
            return True
        except InvalidTransition:
            return False

    results = await asyncio.gather(*(confirm() for _ in range(5)))
    assert results.count(True) == 1
