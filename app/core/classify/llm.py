"""ИИ-классификация серой зоны (spec, раздел 5.3).

- Один запрос на документ: все неоднозначные блоки сразу (не больше лимита).
- В запрос не попадает титульный лист; email, телефоны и ФИО маскируются.
- Ответ — только метки (JSON по схеме), проверяется pydantic; текст в ответе игнорируется.
- Невалидный ответ → один повтор на модели эскалации. Ошибка API → пустой результат,
  обработка документа продолжается (серая зона уходит пользователю и в отчёт).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from app.core.ir import Block, BlockType, DocumentIR

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "classifier_system.md"
T = BlockType
ALLOWED_TYPES = [
    "heading",
    "structural_heading",
    "appendix_heading",
    "body",
    "list_item",
    "figure_caption",
    "table_caption",
    "table_note",
    "bibliography_item",
    "formula",
]

# Цены OpenAI за 1M токенов (USD), постоянные цены GPT-6 (релиз 22.09.2026), сверено 2026-10.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-6-luna": (0.10, 0.50),
    "gpt-6-sol": (2.00, 10.00),
}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}")
_FIO = re.compile(
    r"\b[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.|"  # Иванов И. И.
    r"\b[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]+|"  # И. И. Иванов
    r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:вич|вна|ична|ович|евич|овна|евна)\b"
)


def mask(text: str) -> str:
    text = _EMAIL.sub("[EMAIL]", text)
    text = _PHONE.sub("[PHONE]", text)
    return _FIO.sub("[NAME]", text)


class Label(BaseModel):
    id: str
    type: str
    level: int = Field(ge=0, le=3)
    confidence: float = Field(ge=0, le=1)


class LabelList(BaseModel):
    labels: list[Label]


RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string", "enum": ALLOWED_TYPES},
                    "level": {"type": "integer"},
                    "confidence": {"type": "number"},
                },
                "required": ["id", "type", "level", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["labels"],
    "additionalProperties": False,
}


@dataclass
class CallLog:
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    ok: bool
    error: str | None = None


@dataclass
class LlmResult:
    labels: dict[str, Label] = field(default_factory=dict)
    calls: list[CallLog] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)  # id отправленных блоков
    overflow: list[str] = field(default_factory=list)  # не влезли в лимит/бюджет

    @property
    def cost_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.calls), 6)


class _Responses(Protocol):
    async def create(self, **kwargs: Any) -> Any: ...


class _Client(Protocol):
    @property
    def responses(self) -> _Responses: ...


def cost_of(model: str, input_tokens: int, output_tokens: int) -> float:
    # API может вернуть имя с датой («gpt-6-luna-2026-09-22») — ищем по префиксу.
    key = next((k for k in PRICES_PER_MTOK if model == k or model.startswith(k + "-")), None)
    pin, pout = PRICES_PER_MTOK[key] if key else (10.0, 50.0)  # неизвестная — с запасом
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


def gray_blocks(ir: DocumentIR, low: float, high: float) -> list[Block]:
    skip = {T.TITLE_PAGE, T.TOC, T.EMPTY, T.PAGE_BREAK, T.SECTION_BREAK, T.TABLE, T.FIGURE}
    return [
        b
        for b in ir.blocks
        if b.type not in skip and b.source == "rules" and low <= b.confidence < high
    ]


def _context(ir: DocumentIR, idx: int, step: int) -> dict[str, str] | None:
    j = idx + step
    while 0 <= j < len(ir.blocks):
        b = ir.blocks[j]
        if b.type not in (T.EMPTY, T.PAGE_BREAK, T.SECTION_BREAK):
            if b.type == T.TITLE_PAGE:
                return {"type": "title_page"}  # титульный лист не отправляется
            text = b.text or str(b.features.get("text_preview", ""))
            return {"type": b.label(), "text": mask(text.strip())[:80]}
        j += step
    return None


def build_payload(ir: DocumentIR, blocks: list[Block]) -> list[dict[str, Any]]:
    index = {b.id: i for i, b in enumerate(ir.blocks)}
    out = []
    for b in blocks:
        f = b.features
        i = index[b.id]
        out.append(
            {
                "id": b.id,
                "text": mask(b.text.strip())[:300],
                "features": {
                    "style": f.get("style_name"),
                    "bold": f.get("bold_ratio"),
                    "align": f.get("align"),
                    "size_pt": f.get("size_pt"),
                    "words": f.get("words"),
                    "word_numbering": (f.get("num") or {}).get("label"),
                    "ends_with": b.text.strip()[-1:] or None,
                },
                "rules_guess": [
                    {"type": t, "level": lvl, "score": s} for t, lvl, s in b.alternatives
                ],
                "prev": _context(ir, i, -1),
                "next": _context(ir, i, 1),
            }
        )
    return out


class LlmClassifier:
    def __init__(
        self,
        client: _Client,
        model: str,
        escalation_model: str,
        *,
        max_blocks: int = 60,
        max_cost_usd: float = 0.05,
        timeout_s: float = 60,
    ) -> None:
        self.client = client
        self.model = model
        self.escalation_model = escalation_model
        self.max_blocks = max_blocks
        self.max_cost_usd = max_cost_usd
        self.timeout_s = timeout_s
        self.system = PROMPT_PATH.read_text(encoding="utf-8")

    async def classify(self, ir: DocumentIR, low: float, high: float) -> LlmResult:
        res = LlmResult()
        gray = gray_blocks(ir, low, high)
        # Сначала самые важные для оформления: заголовки, подписи, списки.
        order = {
            T.HEADING: 0,
            T.STRUCTURAL_HEADING: 0,
            T.APPENDIX_HEADING: 0,
            T.FIGURE_CAPTION: 1,
            T.TABLE_CAPTION: 1,
            T.LIST_ITEM: 2,
        }
        gray.sort(key=lambda b: (order.get(b.type, 3), b.confidence))
        batch, res.overflow = gray[: self.max_blocks], [b.id for b in gray[self.max_blocks :]]
        if not batch:
            return res
        payload = build_payload(ir, batch)
        user = "Классифицируй абзацы. Входные данные (JSON):\n" + json.dumps(
            {"blocks": payload}, ensure_ascii=False
        )
        max_tokens = min(16000, 2000 + 120 * len(batch))
        est_in = (len(self.system) + len(user)) // 2  # грубо: ~2 символа кириллицы на токен
        if cost_of(self.model, est_in, max_tokens) > self.max_cost_usd:
            # Не влезаем в бюджет даже по верхней оценке — урезаем пакет.
            keep = max(
                1, int(len(batch) * self.max_cost_usd / cost_of(self.model, est_in, max_tokens))
            )
            res.overflow = [b.id for b in batch[keep:]] + res.overflow
            batch = batch[:keep]
            payload = payload[:keep]
            user = "Классифицируй абзацы. Входные данные (JSON):\n" + json.dumps(
                {"blocks": payload}, ensure_ascii=False
            )
        res.sent = [b.id for b in batch]
        ids = set(res.sent)

        labels = await self._call(self.model, user, max_tokens, ids, res)
        if labels is None and res.cost_usd < self.max_cost_usd:
            labels = await self._call(self.escalation_model, user, max_tokens, ids, res)
        if labels:
            res.labels = labels
        return res

    async def _call(
        self,
        model: str,
        user: str,
        max_tokens: int,
        ids: set[str],
        res: LlmResult,
    ) -> dict[str, Label] | None:
        t0 = time.perf_counter()
        try:
            resp = await self.client.responses.create(
                model=model,
                instructions=self.system,
                input=[{"role": "user", "content": user}],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "labels",
                        "schema": RESPONSE_SCHEMA,
                        "strict": True,
                    }
                },
                reasoning={"effort": "low"},
                max_output_tokens=max_tokens,
                store=False,  # не храним фрагменты работ на стороне OpenAI
                timeout=self.timeout_s,
            )
        except Exception as exc:  # сеть, лимиты, 5xx: обработка не падает
            latency = int((time.perf_counter() - t0) * 1000)
            res.calls.append(CallLog(model, 0, 0, 0.0, latency, False, type(exc).__name__))
            log.warning("llm call failed", extra={"model": model, "error": type(exc).__name__})
            return None
        latency = int((time.perf_counter() - t0) * 1000)
        usage = getattr(resp, "usage", None)
        tin = int(getattr(usage, "input_tokens", 0) or 0)
        tout = int(getattr(usage, "output_tokens", 0) or 0)
        served = str(getattr(resp, "model", model) or model)
        cost = cost_of(served, tin, tout)
        if getattr(resp, "status", "completed") == "incomplete":
            res.calls.append(CallLog(served, tin, tout, cost, latency, False, "incomplete"))
            return None
        refused = any(
            getattr(part, "type", None) == "refusal"
            for item in (getattr(resp, "output", None) or [])
            if getattr(item, "type", None) == "message"
            for part in (getattr(item, "content", None) or [])
        )
        if refused:
            res.calls.append(CallLog(served, tin, tout, cost, latency, False, "refusal"))
            return None
        try:
            parsed = LabelList.model_validate_json(getattr(resp, "output_text", "") or "")
        except ValidationError:
            res.calls.append(CallLog(served, tin, tout, cost, latency, False, "invalid_json"))
            return None
        labels = {
            lab.id: lab
            for lab in parsed.labels
            if lab.id in ids and lab.type in ALLOWED_TYPES  # неизвестные id и типы отбрасываем
        }
        res.calls.append(CallLog(served, tin, tout, cost, latency, True))
        return labels


def make_client(api_key: str, timeout_s: float) -> Any:
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=api_key, timeout=timeout_s, max_retries=1)
