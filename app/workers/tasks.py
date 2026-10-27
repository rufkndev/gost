"""Задачи arq. Анализ и оформление документов выполняются только здесь, не в процессе бота."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from app.bot import texts
from app.payments.service import PaymentService
from app.services import notify, orders
from app.services.context import AppContext

log = logging.getLogger(__name__)
Task = Callable[..., Awaitable[Any]]


def guarded(fn: Task) -> Task:
    """Ошибка задачи → лог и уведомление владельцу (без текста документа)."""

    @wraps(fn)
    async def wrapper(ctx: dict[str, Any], *args: Any) -> Any:
        try:
            return await fn(ctx, *args)
        except Exception as exc:
            log.exception("task failed", extra={"task": fn.__name__})
            app: AppContext = ctx["app"]
            await notify.owner(
                app, texts.OWNER_WORKER_ERROR.format(task=fn.__name__, error=type(exc).__name__)
            )
            raise

    return wrapper


async def ping(ctx: dict[str, Any]) -> str:
    return "pong"


@guarded
async def analyze_order(ctx: dict[str, Any], order_id: str) -> None:
    await orders.run_analysis(ctx["app"], uuid.UUID(order_id))


@guarded
async def process_order(ctx: dict[str, Any], order_id: str) -> None:
    app: AppContext = ctx["app"]
    try:
        await orders.process(app, uuid.UUID(order_id))
    except Exception:
        await orders.fail_order(app, uuid.UUID(order_id), "process_exception")
        raise


@guarded
async def continue_order(ctx: dict[str, Any], order_id: str) -> None:
    await orders.continue_order(ctx["app"], uuid.UUID(order_id))


@guarded
async def question_timeout(ctx: dict[str, Any], order_id: str) -> None:
    await orders.question_timeout(ctx["app"], uuid.UUID(order_id))


@guarded
async def reprocess_order(ctx: dict[str, Any], order_id: str) -> bool:
    return await orders.reprocess(ctx["app"], uuid.UUID(order_id))


# --- периодические задачи ------------------------------------------------------------------


async def cron_payments(ctx: dict[str, Any]) -> None:
    app: AppContext = ctx["app"]
    if app.yookassa is None:
        return
    svc = PaymentService(app)
    await svc.poll_pending()
    await svc.expire_stale()
    await svc.poll_refunds()


async def cron_cleanup(ctx: dict[str, Any]) -> None:
    app: AppContext = ctx["app"]
    deleted = await orders.cleanup_files(app)
    log.info("cleanup", extra=deleted)
    free_mb = app.storage.free_bytes() // (1024 * 1024)
    if free_mb < 1024 and await app.redis.set("alert:disk", "1", ex=3600, nx=True):
        await notify.owner(app, texts.OWNER_DISK.format(free_mb=free_mb))
