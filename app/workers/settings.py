from __future__ import annotations

from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.logging_setup import setup_logging
from app.services.context import AppContext
from app.workers import tasks

_settings = get_settings()


async def startup(ctx: dict[str, Any]) -> None:
    setup_logging(_settings.log_level, _settings.sentry_dsn, _settings.environment)
    ctx["app"] = await AppContext.create(_settings)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["app"].close()


class WorkerSettings:
    functions: ClassVar[list[Any]] = [
        tasks.ping,
        tasks.analyze_order,
        tasks.process_order,
        tasks.continue_order,
        tasks.question_timeout,
        tasks.reprocess_order,
    ]
    cron_jobs: ClassVar[list[Any]] = [
        cron(tasks.cron_payments, second={5, 35}, run_at_startup=False),  # каждые 30 с
        cron(tasks.cron_cleanup, minute=set(range(0, 60, 10)), second=40),  # каждые 10 минут
    ]
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    on_startup = startup
    on_shutdown = shutdown
    job_timeout = _settings.process_timeout_s + 60
    max_jobs = 2  # LibreOffice и разбор docx тяжёлые; на 2 vCPU больше не нужно
    keep_result = 3600
