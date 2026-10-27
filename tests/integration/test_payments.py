"""Оплата без webhook: подтверждение опросом API, повторная проверка, отмена, поздняя оплата,
поддельные данные (несуществующий платёж, неверная сумма), возвраты и их статус."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select, update

from app.db.models import Event, Order, Payment, Refund
from app.db.order_fsm import OrderStatus as S
from app.payments.service import PaymentService
from app.services import orders
from app.services.context import AppContext
from tests.integration.conftest import TEST_REDIS_URL
from tests.integration.fakes import FakeYooKassa, make_ctx
from tests.synth import crooked_document


@pytest.fixture
async def env(migrated_db: str, tmp_path: Path) -> AsyncIterator[tuple[AppContext, FakeYooKassa]]:
    fake = FakeYooKassa()
    ctx = await make_ctx(migrated_db, TEST_REDIS_URL, tmp_path, fake)
    await ctx.redis.flushdb()
    yield ctx, fake
    await ctx.close()


async def analyzed_order(ctx: AppContext) -> uuid.UUID:
    from app.db.repo import get_or_create_user

    uid = uuid.uuid4().int % 10**10
    async with ctx.sessionmaker.begin() as s:
        await get_or_create_user(s, uid, "u", "U")
    order = await orders.create_order(ctx, uid, "misis", "Курсовая.docx", crooked_document())
    await orders.run_analysis(ctx, order.id)
    return order.id


async def status(ctx: AppContext, order_id: uuid.UUID) -> str:
    async with ctx.sessionmaker() as s:
        o = await s.get(Order, order_id)
        assert o is not None
        return o.status


async def queued(ctx: AppContext) -> list[str]:
    assert ctx.arq is not None
    return [j.function for j in await ctx.arq.queued_jobs()]


async def test_success_and_repeated_check(env: tuple[AppContext, FakeYooKassa]) -> None:
    ctx, fake = env
    oid = await analyzed_order(ctx)
    assert await status(ctx, oid) == S.ANALYZED
    svc = PaymentService(ctx)
    url = await svc.start(oid)
    assert url.startswith("https://yoomoney.test/")
    assert await svc.start(oid) == url  # повторное нажатие — тот же платёж
    pid = next(iter(fake.payments))
    assert fake.payments[pid]["metadata"]["order_id"] == str(oid)
    fake.set_status(pid, "succeeded")
    assert await svc.sync(pid) == "paid"
    assert await svc.sync(pid) == "already"  # повторная проверка ничего не делает
    assert await status(ctx, oid) == S.PAID
    assert (await queued(ctx)).count("process_order") == 1


async def test_background_poller(env: tuple[AppContext, FakeYooKassa]) -> None:
    ctx, fake = env
    oid = await analyzed_order(ctx)
    svc = PaymentService(ctx)
    await svc.start(oid)
    pid = next(iter(fake.payments))
    fake.set_status(pid, "succeeded")
    assert await svc.poll_pending() == 1  # фоновая проверка нашла оплату
    assert await status(ctx, oid) == S.PAID
    assert await svc.check_order(oid) == "already"


async def test_cancel_and_retry(env: tuple[AppContext, FakeYooKassa]) -> None:
    ctx, fake = env
    oid = await analyzed_order(ctx)
    svc = PaymentService(ctx)
    await svc.start(oid)
    pid = next(iter(fake.payments))
    fake.set_status(pid, "canceled")
    assert await svc.sync(pid) == "canceled"
    assert await status(ctx, oid) == S.CANCELED
    url2 = await svc.start(oid)  # из canceled снова можно платить
    assert len(fake.payments) == 2 and url2
    assert await status(ctx, oid) == S.AWAITING_PAYMENT


async def test_expired_then_late_payment(env: tuple[AppContext, FakeYooKassa]) -> None:
    ctx, fake = env
    oid = await analyzed_order(ctx)
    svc = PaymentService(ctx)
    await svc.start(oid)
    async with ctx.sessionmaker.begin() as s:
        await s.execute(
            update(Order)
            .where(Order.id == oid)
            .values(updated_at=Order.updated_at - __import__("datetime").timedelta(hours=1))
        )
    assert await svc.expire_stale() == 1
    assert await status(ctx, oid) == S.EXPIRED
    pid = next(iter(fake.payments))
    fake.set_status(pid, "succeeded")  # оплатили позже — деньги засчитываем
    assert await svc.sync(pid) == "paid"
    assert await status(ctx, oid) == S.PAID


async def test_fake_payment_data(env: tuple[AppContext, FakeYooKassa]) -> None:
    ctx, fake = env
    oid = await analyzed_order(ctx)
    svc = PaymentService(ctx)
    await svc.start(oid)
    pid = next(iter(fake.payments))
    # Несуществующий платёж: API отвечает 404 — исключение, заказ не меняется.
    from app.payments.yookassa_client import YooKassaError

    with pytest.raises(YooKassaError):
        await svc.sync("pay-does-not-exist")
    # Неверная сумма в API — не засчитываем.
    fake.set_status(pid, "succeeded", amount="1.00")
    assert await svc.sync(pid) == "mismatch"
    assert await status(ctx, oid) == S.AWAITING_PAYMENT
    async with ctx.sessionmaker() as s:
        types = (await s.scalars(select(Event.type).where(Event.order_id == oid))).all()
    assert "payment_mismatch" in types


async def test_refund_after_failure(env: tuple[AppContext, FakeYooKassa]) -> None:
    ctx, fake = env
    oid = await analyzed_order(ctx)
    svc = PaymentService(ctx)
    await svc.start(oid)
    pid = next(iter(fake.payments))
    fake.set_status(pid, "succeeded")
    await svc.sync(pid)
    from app.db.repo import transition_order

    async with ctx.sessionmaker.begin() as s:
        await transition_order(s, oid, S.PROCESSING)
    await orders.fail_order(ctx, oid, "test_failure")
    assert await status(ctx, oid) == S.REFUNDED
    async with ctx.sessionmaker() as s:
        ref = await s.scalar(select(Refund))
        pay = await s.scalar(select(Payment).where(Payment.order_id == oid))
    assert ref is not None and ref.status == "succeeded" and pay is not None
    assert (await svc.refund(oid, "again")).startswith("уже оформлен")  # без дублей


async def test_refund_status_polling(env: tuple[AppContext, FakeYooKassa]) -> None:
    """Без webhook статус возврата проверяет фоновая задача."""
    ctx, fake = env
    fake.refund_status = "pending"
    oid = await analyzed_order(ctx)
    svc = PaymentService(ctx)
    await svc.start(oid)
    pid = next(iter(fake.payments))
    fake.set_status(pid, "succeeded")
    await svc.sync(pid)
    assert (await svc.refund(oid, "manual")) == "pending"
    assert await svc.poll_refunds() == 0  # ещё в обработке
    rid = next(iter(fake.refunds))
    fake.refunds[rid]["status"] = "succeeded"
    assert await svc.poll_refunds() == 1
    async with ctx.sessionmaker() as s:
        ref = await s.scalar(select(Refund).where(Refund.yookassa_refund_id == rid))
    assert ref is not None and ref.status == "succeeded"
    assert await svc.poll_refunds() == 0  # повторно не трогаем
