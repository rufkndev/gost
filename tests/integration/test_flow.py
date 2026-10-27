"""Полный путь заказа без Telegram: анализ → бета-оплата → вопросы → ответы → результат."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core import docx_io as dx
from app.db.models import Order, Question
from app.db.order_fsm import OrderStatus as S
from app.db.repo import get_or_create_user
from app.services import orders
from app.services.context import AppContext
from tests.integration.conftest import TEST_REDIS_URL
from tests.integration.fakes import make_ctx
from tests.synth import Doc, crooked_document, p, run, tp


@pytest.fixture
async def ctx(migrated_db: str, tmp_path: Path) -> AsyncIterator[AppContext]:
    c = await make_ctx(migrated_db, TEST_REDIS_URL, tmp_path, beta_mode=True)
    await c.redis.flushdb()
    yield c
    await c.close()


async def new_user(ctx: AppContext) -> int:
    uid = uuid.uuid4().int % 10**10
    async with ctx.sessionmaker.begin() as s:
        await get_or_create_user(s, uid, "u", "U")
    return uid


async def get(ctx: AppContext, oid: uuid.UUID) -> Order:
    async with ctx.sessionmaker() as s:
        o = await s.get(Order, oid)
        assert o is not None
        return o


async def test_full_flow_with_questions(ctx: AppContext) -> None:
    uid = await new_user(ctx)
    long = "Абзац основного текста с достаточным количеством слов для уверенности классификации."
    data = (
        Doc()
        .add(
            tp("Введение"),
            tp(long),
            tp(
                "Методика",
            ),
            tp(long),
            p(run("Короткая жирная строка", bold=True)),
            tp(long),
        )
        .build()
    )
    order = await orders.create_order(ctx, uid, "misis", "Работа.docx", data)
    await orders.run_analysis(ctx, order.id)
    o = await get(ctx, order.id)
    assert o.status == S.ANALYZED and o.analysis and o.ir
    assert await orders.mark_paid_beta(ctx, order.id)
    await orders.process(ctx, order.id)
    o = await get(ctx, order.id)
    async with ctx.sessionmaker() as s:
        qs = (await s.scalars(select(Question).where(Question.order_id == order.id))).all()
    if qs:
        assert o.status == S.AWAITING_USER_INPUT
        for q in qs:
            label = await orders.answer_question(ctx, q.id, 0, uid)
            assert label
        # чужой пользователь не может ответить
        assert await orders.answer_question(ctx, qs[0].id, 0, uid + 1) is None
    await orders.continue_order(ctx, order.id)
    o = await get(ctx, order.id)
    assert o.status == S.DONE and o.result_path and o.report
    result = ctx.storage.load(o.result_path)
    assert dx.read_package(result)


async def test_rejects_broken_file(ctx: AppContext) -> None:
    uid = await new_user(ctx)
    order = await orders.create_order(ctx, uid, "misis", "x.docx", b"not a docx")
    await orders.run_analysis(ctx, order.id)
    o = await get(ctx, order.id)
    assert o.status == S.REJECTED and o.error_code == "not_docx"
    assert not ctx.storage.exists(o.src_path)


async def test_duplicate_and_cleanup(ctx: AppContext) -> None:
    uid = await new_user(ctx)
    data = crooked_document()
    order = await orders.create_order(ctx, uid, "gost_7_32_2017", "a.docx", data)
    await orders.run_analysis(ctx, order.id)
    await orders.mark_paid_beta(ctx, order.id)
    await orders.process(ctx, order.id)
    await orders.continue_order(ctx, order.id)
    import hashlib

    dup = await orders.find_paid_duplicate(
        ctx, uid, "gost_7_32_2017", hashlib.sha256(data).hexdigest()
    )
    assert dup is not None and dup.id == order.id
    assert (
        await orders.find_paid_duplicate(ctx, uid, "misis", hashlib.sha256(data).hexdigest())
        is None
    )
    # Срок хранения: сдвигаем время заказа и чистим.
    from datetime import timedelta

    from sqlalchemy import update

    async with ctx.sessionmaker.begin() as s:
        await s.execute(
            update(Order)
            .where(Order.id == order.id)
            .values(updated_at=Order.updated_at - timedelta(hours=25))
        )
    deleted = await orders.cleanup_files(ctx)
    assert deleted["src"] >= 1 and deleted["result"] >= 1
    o = await get(ctx, order.id)
    assert o.src_path is None and o.result_path is None
