"""Тестовые заглушки: API ЮKassa (httpx.MockTransport) и контекст приложения."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.payments.yookassa_client import YooKassaClient
from app.services.context import AppContext


class FakeYooKassa:
    """Имитация API: хранит платежи и возвраты, позволяет менять статус «со стороны банка»."""

    def __init__(self) -> None:
        self.payments: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        self.idem: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self.refund_status = "succeeded"  # что API отвечает на создание возврата

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        path = request.url.path.removeprefix("/v3")
        key = request.headers.get("Idempotence-Key")
        if request.method == "POST" and key in self.idem:
            return httpx.Response(200, json=self.idem[key])
        if request.method == "POST" and path == "/payments":
            body = json.loads(request.content)
            pid = f"pay-{uuid.uuid4().hex[:8]}"
            obj = {
                "id": pid,
                "status": "pending",
                "paid": False,
                "amount": body["amount"],
                "metadata": body.get("metadata", {}),
                "test": True,
                "confirmation": {
                    "type": "redirect",
                    "confirmation_url": f"https://yoomoney.test/{pid}",
                },
            }
            self.payments[pid] = obj
            self.idem[key or pid] = obj
            return httpx.Response(200, json=obj)
        if request.method == "GET" and path.startswith("/payments/"):
            pid = path.split("/")[-1]
            if pid not in self.payments:
                return httpx.Response(404, json={"type": "error", "code": "not_found"})
            return httpx.Response(200, json=self.payments[pid])
        if request.method == "POST" and path == "/refunds":
            body = json.loads(request.content)
            rid = f"ref-{uuid.uuid4().hex[:8]}"
            obj = {
                "id": rid,
                "status": self.refund_status,
                "payment_id": body["payment_id"],
                "amount": body["amount"],
            }
            self.refunds[rid] = obj
            self.idem[key or rid] = obj
            return httpx.Response(200, json=obj)
        if request.method == "GET" and path.startswith("/refunds/"):
            rid = path.split("/")[-1]
            return httpx.Response(200, json=self.refunds[rid])
        return httpx.Response(404)

    def set_status(self, pid: str, status: str, amount: str | None = None) -> None:
        self.payments[pid]["status"] = status
        self.payments[pid]["paid"] = status == "succeeded"
        if amount is not None:
            self.payments[pid]["amount"] = {"value": amount, "currency": "RUB"}


async def make_ctx(
    db_url: str, redis_url: str, storage: Path, fake: FakeYooKassa | None = None, **overrides: Any
) -> AppContext:
    settings = Settings(
        _env_file=None,
        database_url=db_url,
        redis_url=redis_url,
        storage_dir=storage,
        yookassa_shop_id="test",
        yookassa_secret_key="test",
        bot_username="gost_test_bot",
        admin_ids=[1],
        **overrides,
    )
    ctx = await AppContext.create(settings, with_bot=False)
    if fake is not None:
        assert ctx.yookassa is not None
        await ctx.yookassa.aclose()
        ctx.yookassa = YooKassaClient("test", "test", transport=httpx.MockTransport(fake.handler))
    return ctx
