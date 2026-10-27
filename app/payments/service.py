"""Оплата через ЮKassa (spec, раздел 7).

- Без webhook: статус платежа проверяется запросом GET /v3/payments/{id} — фоновой задачей
  каждые 30 секунд и по кнопке «Я оплатил — проверить».
- Переход в paid — под блокировкой строки заказа, ровно один раз; задача оформления
  ставится с job_id = process:<order_id>, дублей не будет.
- Сверяются сумма, валюта и metadata.order_id.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.bot import texts
from app.db.models import Order, Payment, Refund, User
from app.db.order_fsm import OrderStatus as S
from app.db.repo import add_event, lock_order, transition_order
from app.payments.yookassa_client import YooKassaError, build_receipt
from app.services import notify
from app.services.context import AppContext

log = logging.getLogger(__name__)
PAYABLE = {S.ANALYZED, S.EXPIRED, S.CANCELED, S.AWAITING_PAYMENT}


class PaymentUnavailable(Exception):
    pass


class PaymentService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    @property
    def api(self) -> Any:
        if self.ctx.yookassa is None:
            raise PaymentUnavailable("yookassa is not configured")
        return self.ctx.yookassa

    # --- создание ---------------------------------------------------------------------------

    async def start(self, order_id: uuid.UUID) -> str:
        """Создаёт платёж и возвращает confirmation_url. Повторный вызов — тот же платёж."""
        st = self.ctx.settings
        async with self.ctx.sessionmaker.begin() as s:
            order = await lock_order(s, order_id)
            if order.status not in PAYABLE:
                raise PaymentUnavailable(f"order status {order.status}")
            fresh = await s.scalar(
                select(Payment)
                .where(
                    Payment.order_id == order_id,
                    Payment.status == "pending",
                    Payment.created_at
                    >= datetime.now(UTC) - timedelta(minutes=st.payment_ttl_minutes),
                )
                .order_by(Payment.created_at.desc())
            )
            if fresh is not None and fresh.confirmation_url:
                return fresh.confirmation_url
            if order.status != S.AWAITING_PAYMENT:
                await transition_order(s, order_id, S.AWAITING_PAYMENT)
            user = await s.get(User, order.user_id)
            pay = Payment(
                order_id=order_id,
                idempotence_key=str(uuid.uuid4()),
                status="creating",
                amount=Decimal(order.price_rub),
            )
            s.add(pay)
            await s.flush()
            pay_id, key = pay.id, pay.idempotence_key
            amount, short, user_id = order.price_rub, order.short_id, order.user_id
            email = user.email if user else None
        receipt = None
        if st.receipts_enabled:
            if not email:
                raise PaymentUnavailable("email required for receipt")
            receipt = build_receipt(email, amount, st.receipt_vat_code)
        return_url = f"https://t.me/{st.bot_username}" if st.bot_username else "https://t.me"
        try:
            data = await self.api.create_payment(
                amount=amount,
                description=f"Оформление документа по ГОСТ, заказ {short}",
                return_url=return_url,
                metadata={"order_id": str(order_id), "tg_user_id": str(user_id)},
                idempotence_key=key,
                receipt=receipt,
            )
        except (YooKassaError, OSError) as exc:
            await notify.owner(
                self.ctx, texts.OWNER_SERVICE.format(service="ЮKassa", error=str(exc)[:200])
            )
            raise PaymentUnavailable(str(exc)) from exc
        async with self.ctx.sessionmaker.begin() as s:
            row = await s.get(Payment, pay_id)
            if row is None:
                raise PaymentUnavailable("payment row vanished")
            row.yookassa_payment_id = data["id"]
            row.status = data.get("status", "pending")
            row.confirmation_url = (data.get("confirmation") or {}).get("confirmation_url")
            row.raw_last = _safe_raw(data)
            await add_event(s, order_id, "payment_created", {"payment": data["id"]})
            url = row.confirmation_url or ""
        return url

    # --- подтверждение ----------------------------------------------------------------------

    async def sync(self, yk_payment_id: str) -> str:
        """Сверяет платёж с API и применяет результат. Идемпотентно."""
        data = await self.api.get_payment(yk_payment_id)
        status = str(data.get("status"))
        async with self.ctx.sessionmaker.begin() as s:
            pay = await s.scalar(
                select(Payment)
                .where(Payment.yookassa_payment_id == yk_payment_id)
                .with_for_update()
            )
            if pay is None:
                await add_event(s, None, "payment_unknown", {"payment": yk_payment_id})
                return "unknown"
            order = await lock_order(s, pay.order_id)
            meta_order = str((data.get("metadata") or {}).get("order_id", ""))
            amount = Decimal(str((data.get("amount") or {}).get("value", "0")))
            currency = (data.get("amount") or {}).get("currency")
            if (
                meta_order != str(order.id)
                or amount != Decimal(order.price_rub)
                or currency != "RUB"
            ):
                await add_event(
                    s, order.id, "payment_mismatch", {"payment": yk_payment_id, "status": status}
                )
                return "mismatch"
            pay.status = status
            pay.raw_last = _safe_raw(data)
            outcome = status
            if status == "succeeded":
                if order.status in (S.EXPIRED, S.CANCELED):
                    # Оплатили после истечения ссылки — деньги получены, заказ выполняем.
                    await transition_order(
                        s, order.id, S.AWAITING_PAYMENT, event_type="late_payment"
                    )
                    order.status = S.AWAITING_PAYMENT
                if order.status == S.AWAITING_PAYMENT:
                    await transition_order(s, order.id, S.PAID, payload={"payment": yk_payment_id})
                    outcome = "paid"
                else:
                    outcome = "already"
            elif status == "canceled" and order.status == S.AWAITING_PAYMENT:
                latest = await s.scalar(
                    select(Payment)
                    .where(Payment.order_id == order.id)
                    .order_by(Payment.created_at.desc())
                )
                if latest is not None and latest.id == pay.id:
                    await transition_order(
                        s,
                        order.id,
                        S.CANCELED,
                        payload={"reason": (data.get("cancellation_details") or {}).get("reason")},
                    )
                    outcome = "canceled"
            order_id, user_id = order.id, order.user_id
        if outcome == "paid":
            await self.ctx.enqueue("process_order", str(order_id), job_id=f"process:{order_id}")
            await notify.send(self.ctx, user_id, texts.PAID)
        elif outcome == "canceled":
            await notify.send(self.ctx, user_id, texts.PAY_CANCELED)
        return outcome

    async def check_order(self, order_id: uuid.UUID) -> str:
        """Кнопка «Я оплатил — проверить»."""
        async with self.ctx.sessionmaker() as s:
            order = await s.get(Order, order_id)
            pay = await s.scalar(
                select(Payment)
                .where(Payment.order_id == order_id, Payment.yookassa_payment_id.is_not(None))
                .order_by(Payment.created_at.desc())
            )
            status = order.status if order else None
        if status in (S.PAID, S.PROCESSING, S.AWAITING_USER_INPUT, S.DONE):
            return "already"
        if pay is None or pay.yookassa_payment_id is None:
            return "none"
        return await self.sync(pay.yookassa_payment_id)

    async def poll_pending(self) -> int:
        """Основной способ подтверждения: pending-платежи младше 2 часов."""
        since = datetime.now(UTC) - timedelta(hours=2)
        async with self.ctx.sessionmaker() as s:
            ids = (
                await s.scalars(
                    select(Payment.yookassa_payment_id).where(
                        Payment.status == "pending",
                        Payment.created_at >= since,
                        Payment.yookassa_payment_id.is_not(None),
                    )
                )
            ).all()
        n = 0
        for pid in ids:
            try:
                await self.sync(str(pid))
                n += 1
            except (YooKassaError, OSError) as exc:
                log.warning("poll failed", extra={"payment": pid, "error": str(exc)[:100]})
        return n

    async def expire_stale(self) -> int:
        """Заказы, где ссылка на оплату прожила дольше PAYMENT_TTL_MINUTES, — в expired."""
        ttl = datetime.now(UTC) - timedelta(minutes=self.ctx.settings.payment_ttl_minutes)
        n = 0
        async with self.ctx.sessionmaker.begin() as s:
            orders = (
                await s.scalars(
                    select(Order).where(Order.status == S.AWAITING_PAYMENT, Order.updated_at < ttl)
                )
            ).all()
            for o in orders:
                await transition_order(s, o.id, S.EXPIRED)
                n += 1
        return n

    # --- возвраты ----------------------------------------------------------------------------

    async def succeeded_payment(self, order_id: uuid.UUID) -> Payment | None:
        async with self.ctx.sessionmaker() as s:
            return await s.scalar(
                select(Payment)
                .where(Payment.order_id == order_id, Payment.status == "succeeded")
                .order_by(Payment.created_at.desc())
            )

    async def refund(self, order_id: uuid.UUID, reason: str) -> str:
        pay = await self.succeeded_payment(order_id)
        if pay is None or pay.yookassa_payment_id is None:
            return "нет оплаченного платежа"
        async with self.ctx.sessionmaker.begin() as s:
            existing = await s.scalar(select(Refund).where(Refund.payment_id == pay.id))
            if existing is not None and existing.status in ("pending", "succeeded"):
                return f"уже оформлен ({existing.status})"
            ref = existing or Refund(
                payment_id=pay.id,
                idempotence_key=str(uuid.uuid4()),
                amount=pay.amount,
                reason=reason[:64],
                status="creating",
            )
            s.add(ref)
            await s.flush()
            ref_id, key = ref.id, ref.idempotence_key
        try:
            data = await self.api.create_refund(
                payment_id=pay.yookassa_payment_id,
                amount=pay.amount,
                idempotence_key=key,
                description=f"Возврат: {reason}",
            )
        except (YooKassaError, OSError, PaymentUnavailable) as exc:
            async with self.ctx.sessionmaker.begin() as s:
                failed = await s.get(Refund, ref_id)
                if failed is not None:
                    failed.status = "error"
                await add_event(s, order_id, "refund_error", {"error": str(exc)[:200]})
            return "ошибка, нужен ручной возврат"
        async with self.ctx.sessionmaker.begin() as s:
            saved = await s.get(Refund, ref_id)
            if saved is None:
                return "ошибка учёта возврата"
            saved.yookassa_refund_id = data.get("id")
            saved.status = str(data.get("status", "pending"))
            refund_status = saved.status
            order = await lock_order(s, order_id)
            if order.status in (S.FAILED, S.COMPLAINT):
                await transition_order(s, order_id, S.REFUNDED, payload={"reason": reason})
            await add_event(
                s, order_id, "refund_created", {"refund": data.get("id"), "status": refund_status}
            )
            short = order.short_id
        await notify.owner(self.ctx, texts.OWNER_REFUND.format(short=short, status=refund_status))
        return refund_status

    async def poll_refunds(self) -> int:
        """Статус возвратов в обработке (pending) — проверяется запросом к API."""
        async with self.ctx.sessionmaker() as s:
            refs = (
                await s.scalars(
                    select(Refund).where(
                        Refund.status == "pending", Refund.yookassa_refund_id.is_not(None)
                    )
                )
            ).all()
            pending = [(r.id, str(r.yookassa_refund_id)) for r in refs]
        changed = 0
        for ref_id, yk_id in pending:
            try:
                data = await self.api.get_refund(yk_id)
            except (YooKassaError, OSError) as exc:
                log.warning("refund poll failed", extra={"refund": yk_id, "error": str(exc)[:100]})
                continue
            status = str(data.get("status", "pending"))
            if status == "pending":
                continue
            async with self.ctx.sessionmaker.begin() as s:
                ref = await s.get(Refund, ref_id)
                if ref is None:
                    continue
                ref.status = status
                pay = await s.get(Payment, ref.payment_id)
                order = await s.get(Order, pay.order_id) if pay else None
                await add_event(
                    s, order.id if order else None, f"refund_{status}", {"refund": yk_id}
                )
                short = order.short_id if order else "?"
            changed += 1
            if status != "succeeded":
                await notify.owner(
                    self.ctx,
                    texts.OWNER_REFUND.format(
                        short=short, status=f"{status} — нужен ручной возврат"
                    ),
                )
        return changed


def _safe_raw(data: dict[str, Any]) -> dict[str, Any]:
    """Ответ API без лишних персональных данных."""
    keep = (
        "id",
        "status",
        "paid",
        "amount",
        "created_at",
        "captured_at",
        "test",
        "refundable",
        "cancellation_details",
        "metadata",
    )
    return {k: data[k] for k in keep if k in data}
