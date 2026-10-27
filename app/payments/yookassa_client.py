"""Клиент API ЮKassa v3 (httpx). Контракт сверен с yookassa.ru/developers (2026-10).

Ключ идемпотентности хранится в БД и переиспользуется при повторе (ЮKassa держит его 24 ч).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

API_URL = "https://api.yookassa.ru/v3"


class YooKassaError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"yookassa http {status}")
        self.status = status
        self.body = body


def money(amount: Decimal | int) -> dict[str, str]:
    return {"value": f"{Decimal(amount):.2f}", "currency": "RUB"}


class YooKassaClient:
    def __init__(
        self,
        shop_id: str,
        secret_key: str,
        *,
        base_url: str = API_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url, auth=(shop_id, secret_key), timeout=timeout, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        idempotence_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Idempotence-Key": idempotence_key} if idempotence_key else {}
        resp = await self._http.request(method, path, json=json, headers=headers)
        if resp.status_code >= 400:
            raise YooKassaError(resp.status_code, resp.text[:500])
        data: dict[str, Any] = resp.json()
        return data

    async def create_payment(
        self,
        *,
        amount: Decimal | int,
        description: str,
        return_url: str,
        metadata: dict[str, str],
        idempotence_key: str,
        receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "amount": money(amount),
            "payment_method_data": {"type": "sbp"},
            "confirmation": {"type": "redirect", "return_url": return_url},
            "capture": True,
            "description": description[:128],
            "metadata": metadata,
        }
        if receipt is not None:
            body["receipt"] = receipt
        return await self._request("POST", "/payments", json=body, idempotence_key=idempotence_key)

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/payments/{payment_id}")

    async def get_refund(self, refund_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/refunds/{refund_id}")

    async def create_refund(
        self, *, payment_id: str, amount: Decimal | int, idempotence_key: str, description: str = ""
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"payment_id": payment_id, "amount": money(amount)}
        if description:
            body["description"] = description[:250]
        return await self._request("POST", "/refunds", json=body, idempotence_key=idempotence_key)


def build_receipt(email: str, amount: Decimal | int, vat_code: int) -> dict[str, Any]:
    return {
        "customer": {"email": email},
        "items": [
            {
                "description": "Услуга по оформлению документа",
                "quantity": "1.00",
                "amount": money(amount),
                "vat_code": vat_code,
                "payment_subject": "service",
                "payment_mode": "full_payment",
            }
        ],
    }
