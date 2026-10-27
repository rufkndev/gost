"""Сценарий заказа: анализ → оплата → (ИИ, вопросы) → оформление → выдача.

Вызывается из worker (тяжёлые шаги) и бота (лёгкие). Текст документов в логи не пишется.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.bot import keyboards as kb
from app.bot import texts
from app.core import docx_io as dx
from app.core.classify.llm import LlmClassifier, make_client
from app.core.classify.resolve import (
    apply_answers,
    apply_defaults,
    apply_llm,
    pick_questions,
)
from app.core.ir import DocumentIR
from app.core.normalize import RULE_LABELS
from app.core.pipeline import analyze, format_document
from app.core.report import TELEGRAM_LIMIT, render_final_text
from app.db.models import LlmCall, Order, Question, User
from app.db.order_fsm import OrderStatus as S
from app.db.repo import add_event, lock_order, transition_order
from app.services import notify
from app.services.context import AppContext

log = logging.getLogger(__name__)


def short_id() -> str:
    return secrets.token_hex(4).upper()


def now() -> datetime:
    return datetime.now(UTC)


async def order_by_short(ctx: AppContext, short: str) -> Order | None:
    async with ctx.sessionmaker() as s:
        return await s.scalar(select(Order).where(Order.short_id == short))


# --- создание и анализ ---------------------------------------------------------------------


async def recent_docs_count(ctx: AppContext, user_id: int) -> int:
    async with ctx.sessionmaker() as s:
        since = now() - timedelta(hours=1)
        return int(
            await s.scalar(
                select(func.count())
                .select_from(Order)
                .where(Order.user_id == user_id, Order.created_at >= since)
            )
            or 0
        )


async def find_paid_duplicate(
    ctx: AppContext, user_id: int, profile_id: str, file_hash: str
) -> Order | None:
    since = now() - timedelta(hours=24)
    async with ctx.sessionmaker() as s:
        return await s.scalar(
            select(Order)
            .where(
                Order.user_id == user_id,
                Order.profile_id == profile_id,
                Order.file_hash == file_hash,
                Order.status == S.DONE,
                Order.updated_at >= since,
                Order.result_path.is_not(None),
            )
            .order_by(Order.created_at.desc())
        )


async def create_order(
    ctx: AppContext, user_id: int, profile_id: str, file_name: str, data: bytes
) -> Order:
    key = ctx.storage.save(data)
    async with ctx.sessionmaker.begin() as s:
        order = Order(
            short_id=short_id(),
            user_id=user_id,
            profile_id=profile_id,
            file_name=file_name[:255],
            file_hash=hashlib.sha256(data).hexdigest(),
            src_path=key,
            price_rub=ctx.settings.price_rub,
        )
        s.add(order)
        await s.flush()
        await add_event(s, order.id, "order_created", {"profile": profile_id, "bytes": len(data)})
    return order


async def clone_for_profile(ctx: AppContext, order: Order, profile_id: str) -> Order | None:
    """«Другой профиль» — новый заказ из того же файла (пока файл не удалён)."""
    if not ctx.storage.exists(order.src_path):
        return None
    data = ctx.storage.load(order.src_path or "")
    return await create_order(ctx, order.user_id, profile_id, order.file_name, data)


async def run_analysis(ctx: AppContext, order_id: uuid.UUID) -> None:
    async with ctx.sessionmaker.begin() as s:
        order = await transition_order(s, order_id, S.ANALYZING)
        src, profile_id, user_id, short = (
            order.src_path,
            order.profile_id,
            order.user_id,
            order.short_id,
        )
    profile = ctx.profiles[profile_id]
    try:
        data = ctx.storage.load(src or "")
        result = await asyncio.to_thread(
            analyze,
            data,
            profile,
            ctx.settings.price_rub,
            ctx.settings.classify_accept_threshold,
            ctx.settings.max_unzipped_mb * 1024 * 1024,
        )
    except dx.PackageError as exc:
        async with ctx.sessionmaker.begin() as s:
            order = await transition_order(s, order_id, S.REJECTED, payload={"code": exc.code})
            order.error_code = exc.code
        ctx.storage.delete(src)
        await notify.send(
            ctx, user_id, texts.REJECT_CODES.get(exc.code, texts.REJECT_CODES["corrupted"])
        )
        return
    except Exception:
        log.exception("analysis failed", extra={"order": str(order_id)})
        async with ctx.sessionmaker.begin() as s:
            order = await transition_order(s, order_id, S.REJECTED, payload={"code": "internal"})
            order.error_code = "internal"
        await notify.send(ctx, user_id, texts.REJECT_CODES["internal"])
        return
    async with ctx.sessionmaker.begin() as s:
        order = await transition_order(
            s, order_id, S.ANALYZED, payload={"uncertain": result.report.uncertain}
        )
        order.analysis = result.report.to_dict()
        order.ir = result.ir.model_dump(mode="json")
        order.pages_estimate = result.report.pages
    beta = await is_beta_user(ctx, user_id)
    await notify.send(
        ctx,
        user_id,
        texts.preliminary(result.report),
        kb.preliminary(short, ctx.settings.price_rub, beta),
    )


async def is_beta_user(ctx: AppContext, user_id: int) -> bool:
    if not await ctx.beta_mode():
        return False
    if user_id in ctx.settings.beta_user_ids:
        return True
    async with ctx.sessionmaker() as s:
        user = await s.get(User, user_id)
        return bool(user and user.is_beta)


async def mark_paid_beta(ctx: AppContext, order_id: uuid.UUID) -> bool:
    async with ctx.sessionmaker.begin() as s:
        order = await lock_order(s, order_id)
        if order.status != S.ANALYZED:
            return False
        await transition_order(s, order_id, S.PAID, event_type="beta_free")
    await ctx.enqueue("process_order", str(order_id), job_id=f"process:{order_id}")
    return True


# --- обработка после оплаты -----------------------------------------------------------------


def _ir_of(order: Order) -> DocumentIR:
    return DocumentIR.model_validate(order.ir)


async def process(ctx: AppContext, order_id: uuid.UUID) -> None:
    """paid → processing: ИИ по серой зоне, затем вопросы пользователю или сразу сборка."""
    async with ctx.sessionmaker.begin() as s:
        order = await lock_order(s, order_id)
        if order.status != S.PAID:
            return  # повторная постановка задачи — ничего не делаем
        await transition_order(s, order_id, S.PROCESSING)
        ir = _ir_of(order)
        user_id = order.user_id
    st = ctx.settings
    conflicts: list[str] = []
    if st.llm_enabled and st.openai_api_key.get_secret_value():
        clf = LlmClassifier(
            make_client(st.openai_api_key.get_secret_value(), st.llm_timeout_s),
            st.llm_model,
            st.llm_escalation_model,
            max_blocks=st.llm_max_blocks,
            max_cost_usd=st.llm_max_cost_per_doc_usd,
            timeout_s=st.llm_timeout_s,
        )
        res = await clf.classify(ir, st.classify_llm_threshold, st.classify_accept_threshold)
        conflicts = apply_llm(ir, res.labels, st.classify_accept_threshold)
        async with ctx.sessionmaker.begin() as s:
            for c in res.calls:
                s.add(
                    LlmCall(
                        order_id=order_id,
                        model=c.model,
                        input_tokens=c.input_tokens,
                        output_tokens=c.output_tokens,
                        cost_usd=c.cost_usd,
                        latency_ms=c.latency_ms,
                        ok=c.ok,
                    )
                )
            await add_event(
                s,
                order_id,
                "llm",
                {
                    "sent": len(res.sent),
                    "labels": len(res.labels),
                    "cost_usd": res.cost_usd,
                    "errors": [c.error for c in res.calls if c.error],
                },
            )
            if res.calls and not any(c.ok for c in res.calls):
                await add_event(s, order_id, "llm_unavailable")
        if res.calls and not any(c.ok for c in res.calls):
            await notify.owner(
                ctx, texts.OWNER_SERVICE.format(service="ИИ", error=res.calls[-1].error)
            )
    questions = pick_questions(ir, st.classify_llm_threshold, conflicts, st.max_user_questions)
    async with ctx.sessionmaker.begin() as s:
        order = await lock_order(s, order_id)
        order.ir = ir.model_dump(mode="json")
        if questions:
            await transition_order(
                s, order_id, S.AWAITING_USER_INPUT, payload={"questions": len(questions)}
            )
            rows = []
            for q in questions:
                row = Question(
                    order_id=order_id,
                    block_id=q.block_id,
                    options={"options": q.options, "preview": q.preview},
                )
                s.add(row)
                rows.append(row)
            await s.flush()
            qids = [(str(r.id), q) for r, q in zip(rows, questions, strict=True)]
    if not questions:
        await ctx.enqueue("continue_order", str(order_id), job_id=f"continue:{order_id}")
        return
    await notify.send(ctx, user_id, texts.QUESTION_INTRO.format(n=len(qids)))
    for qid, q in qids:
        await notify.send(
            ctx,
            user_id,
            texts.QUESTION.format(preview=_esc(q.preview)),
            kb.question(qid, q.options),
        )
    await ctx.enqueue(
        "question_timeout",
        str(order_id),
        job_id=f"qtimeout:{order_id}",
        defer_s=st.question_timeout_minutes * 60,
    )


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def answer_question(ctx: AppContext, qid: uuid.UUID, idx: int, user_id: int) -> str | None:
    """Сохраняет ответ. Возвращает подпись выбранного варианта или None."""
    async with ctx.sessionmaker.begin() as s:
        q = await s.get(Question, qid)
        if q is None or q.answered_at is not None:
            return None
        order = await lock_order(s, q.order_id)
        if order.user_id != user_id or order.status != S.AWAITING_USER_INPUT:
            return None
        options = q.options["options"]
        if not 0 <= idx < len(options):
            return None
        q.answer = f"{options[idx]['type']}:{options[idx].get('level') or ''}"
        q.answered_at = now()
        await s.flush()
        left = await s.scalar(
            select(func.count())
            .select_from(Question)
            .where(Question.order_id == q.order_id, Question.answered_at.is_(None))
        )
        order_id = q.order_id
        label = str(options[idx]["label"])
    if not left:
        await ctx.enqueue("continue_order", str(order_id), job_id=f"continue:{order_id}")
    return label


async def question_timeout(ctx: AppContext, order_id: uuid.UUID) -> None:
    async with ctx.sessionmaker() as s:
        order = await s.get(Order, order_id)
        if order is None or order.status != S.AWAITING_USER_INPUT:
            return
        user_id = order.user_id
    await notify.send(ctx, user_id, texts.QUESTION_TIMEOUT_NOTE)
    await ctx.enqueue("continue_order", str(order_id), job_id=f"continue:{order_id}")


async def continue_order(ctx: AppContext, order_id: uuid.UUID) -> None:
    """Сборка, проверки, выдача. Любая ошибка → failed + возврат + уведомление владельца."""
    async with ctx.sessionmaker.begin() as s:
        order = await lock_order(s, order_id)
        if order.status == S.AWAITING_USER_INPUT:
            await transition_order(s, order_id, S.PROCESSING, event_type="resume")
        elif order.status != S.PROCESSING:
            return
        ir = _ir_of(order)
        qs = (await s.scalars(select(Question).where(Question.order_id == order_id))).all()
        answers: dict[str, tuple[str, int | None]] = {}
        for q in qs:
            if q.answer:
                t, _, lvl = q.answer.partition(":")
                answers[q.block_id] = (t, int(lvl) if lvl else None)
        unanswered = [q for q in qs if not q.answer]
        src, profile_id, user_id = order.src_path, order.profile_id, order.user_id
        file_name, short = order.file_name, order.short_id
    apply_answers(ir, answers)
    if unanswered:
        from app.core.classify.resolve import Question as Q

        apply_defaults(ir, [Q(q.block_id, "", []) for q in unanswered])
    try:
        data = ctx.storage.load(src or "")
        outcome = await asyncio.wait_for(
            asyncio.to_thread(
                format_document,
                data,
                ctx.profiles[profile_id],
                ctx.template(profile_id),
                accept_threshold=ctx.settings.classify_accept_threshold,
                ir=ir,
                timeout_s=max(30, ctx.settings.process_timeout_s - 20),
            ),
            timeout=ctx.settings.process_timeout_s,
        )
    except Exception as exc:
        log.exception("format failed", extra={"order": str(order_id)})
        await fail_order(ctx, order_id, f"exception:{type(exc).__name__}")
        return
    if not outcome.ok or outcome.data is None:
        async with ctx.sessionmaker.begin() as s:
            await add_event(
                s,
                order_id,
                "checks_failed",
                {
                    "integrity": outcome.integrity.errors[:10],
                    "lint": [f"{i.rule}:{i.ref}" for i in outcome.lint if i.severity == "error"][
                        :20
                    ],
                },
            )
        await fail_order(ctx, order_id, outcome.error_code or "checks")
        return
    result_key = ctx.storage.save(outcome.data)
    report_text = render_final_text(outcome.report, RULE_LABELS)
    async with ctx.sessionmaker.begin() as s:
        order = await transition_order(
            s,
            order_id,
            S.DONE,
            payload={
                "lint_warnings": sum(1 for i in outcome.lint if i.severity == "warning"),
                "manual": len(outcome.report.manual),
            },
        )
        order.result_path = result_key
        order.report = outcome.report.to_dict()
    await deliver(ctx, user_id, short, file_name, outcome.data, report_text)


def result_name(file_name: str) -> str:
    stem = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    return f"{stem}_ГОСТ.docx"


async def deliver(
    ctx: AppContext, user_id: int, short: str, file_name: str, data: bytes, report_text: str
) -> None:
    await notify.send_file(ctx, user_id, data, result_name(file_name), texts.RESULT_CAPTION)
    if len(report_text) <= TELEGRAM_LIMIT - 100:
        await notify.send(ctx, user_id, _esc(report_text), kb.feedback(short))
    else:
        await notify.send_file(
            ctx, user_id, report_text.encode("utf-8"), "Отчёт.txt", texts.REPORT_TOO_LONG
        )
        await notify.send(ctx, user_id, _esc(report_text[:1500]) + " …", kb.feedback(short))


async def resend_result(ctx: AppContext, order: Order) -> bool:
    if not ctx.storage.exists(order.result_path):
        return False
    data = ctx.storage.load(order.result_path or "")
    rep = order.report or {}
    from app.core.report import FinalReport, ManualItem

    final = FinalReport(
        rep.get("fixed", {}),
        rep.get("normalizations", {}),
        rep.get("warnings", {}),
        [ManualItem(**m) for m in rep.get("manual", [])],
        int(rep.get("answers", 0)),
        rep.get("meta", {}),
    )
    await deliver(
        ctx,
        order.user_id,
        order.short_id,
        order.file_name,
        data,
        render_final_text(final, RULE_LABELS),
    )
    return True


async def fail_order(ctx: AppContext, order_id: uuid.UUID, code: str) -> None:
    from app.payments.service import PaymentService

    async with ctx.sessionmaker.begin() as s:
        order = await transition_order(s, order_id, S.FAILED, payload={"code": code})
        order.error_code = code[:64]
        user_id, short = order.user_id, order.short_id
        keep_until = now() + timedelta(days=ctx.settings.retention_complaint_days)
        order.keep_for_debug_until = keep_until  # исходник нужен для разбора ошибки
    refund_status = "не требуется (бета)"
    paid = await PaymentService(ctx).succeeded_payment(order_id)
    if paid is not None:
        refund_status = await PaymentService(ctx).refund(order_id, reason="auto_failed")
        await notify.send(ctx, user_id, texts.FAILED)
    else:
        await notify.send(ctx, user_id, texts.FAILED_BETA)
    await notify.owner(ctx, texts.OWNER_FAILED.format(short=short, code=code, refund=refund_status))


# --- отзывы и жалобы --------------------------------------------------------------------------


async def complaint(ctx: AppContext, order: Order, comment: str, keep: bool) -> None:
    from app.db.models import Feedback

    async with ctx.sessionmaker.begin() as s:
        o = await transition_order(s, order.id, S.COMPLAINT)
        if keep:
            o.keep_for_debug_until = now() + timedelta(days=ctx.settings.retention_complaint_days)
        s.add(Feedback(order_id=order.id, rating="problem", comment=comment[:4000]))
    await notify.owner(
        ctx, texts.OWNER_COMPLAINT.format(short=order.short_id, text=_esc(comment[:500]))
    )


async def reprocess(ctx: AppContext, order_id: uuid.UUID) -> bool:
    """Бесплатная повторная обработка по жалобе (запускает владелец из /admin)."""
    async with ctx.sessionmaker() as s:
        order = await s.get(Order, order_id)
        if order is None or order.status != S.COMPLAINT or not ctx.storage.exists(order.src_path):
            return False
        ir = _ir_of(order)
        data = ctx.storage.load(order.src_path or "")
        profile_id, user_id, short, name = (
            order.profile_id,
            order.user_id,
            order.short_id,
            order.file_name,
        )
    outcome = await asyncio.to_thread(
        format_document,
        data,
        ctx.profiles[profile_id],
        ctx.template(profile_id),
        ir=ir,
        accept_threshold=ctx.settings.classify_accept_threshold,
    )
    if not outcome.ok or outcome.data is None:
        return False
    key = ctx.storage.save(outcome.data)
    async with ctx.sessionmaker.begin() as s:
        o = await transition_order(s, order_id, S.DONE, event_type="reprocessed")
        old = o.result_path
        o.result_path = key
        o.report = outcome.report.to_dict()
    ctx.storage.delete(old)
    await deliver(
        ctx, user_id, short, name, outcome.data, render_final_text(outcome.report, RULE_LABELS)
    )
    return True


# --- сроки хранения ----------------------------------------------------------------------------


async def cleanup_files(ctx: AppContext) -> dict[str, int]:
    st = ctx.settings
    t = now()
    unpaid = {
        S.CREATED,
        S.ANALYZING,
        S.ANALYZED,
        S.REJECTED,
        S.AWAITING_PAYMENT,
        S.EXPIRED,
        S.CANCELED,
    }
    finished = {S.DONE, S.FAILED, S.REFUNDED, S.COMPLAINT}
    deleted = {"src": 0, "result": 0}
    async with ctx.sessionmaker.begin() as s:
        rows = (
            await s.scalars(
                select(Order).where(
                    (Order.src_path.is_not(None)) | (Order.result_path.is_not(None))
                )
            )
        ).all()
        for o in rows:
            keep = o.keep_for_debug_until is not None and o.keep_for_debug_until > t
            if keep:
                continue
            expired = False
            if o.status in unpaid and o.created_at < t - timedelta(hours=st.retention_unpaid_hours):
                expired = True
            if o.status in finished and o.updated_at < t - timedelta(hours=st.retention_paid_hours):
                expired = True
            if not expired:
                continue
            payload: dict[str, Any] = {}
            if o.src_path and ctx.storage.delete(o.src_path):
                deleted["src"] += 1
                payload["src"] = True
            if o.result_path and ctx.storage.delete(o.result_path):
                deleted["result"] += 1
                payload["result"] = True
            o.src_path = None
            o.result_path = None
            if o.status in unpaid:
                o.ir = None
            await add_event(s, o.id, "files_deleted", payload)
    return deleted
