"""Объединение правил, ИИ и ответов пользователя (spec, разделы 5.2–5.4).

Пороги: ≥ accept — принять правило; [llm; accept) — в ИИ; < llm или противоречие
правил и ИИ — вопрос пользователю (не больше max_questions, по важности).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.classify.llm import Label
from app.core.ir import Block, BlockType, DocumentIR

T = BlockType
OPTION_LABELS: dict[tuple[str, int | None], str] = {
    ("heading", 1): "Заголовок 1 уровня",
    ("heading", 2): "Заголовок 2 уровня",
    ("heading", 3): "Заголовок 3 уровня",
    ("structural_heading", None): "Заголовок (ВВЕДЕНИЕ, ЗАКЛЮЧЕНИЕ…)",
    ("appendix_heading", None): "Заголовок приложения",
    ("body", None): "Обычный текст",
    ("list_item", None): "Элемент списка",
    ("figure_caption", None): "Подпись рисунка",
    ("table_caption", None): "Название таблицы",
    ("table_note", None): "Примечание к таблице",
    ("bibliography_item", None): "Источник в списке литературы",
    ("formula", None): "Формула",
}
IMPORTANCE = {
    T.HEADING: 0,
    T.STRUCTURAL_HEADING: 0,
    T.APPENDIX_HEADING: 0,
    T.FIGURE_CAPTION: 1,
    T.TABLE_CAPTION: 1,
    T.LIST_ITEM: 2,
}
SKIP = {T.TITLE_PAGE, T.TOC, T.EMPTY, T.PAGE_BREAK, T.SECTION_BREAK, T.TABLE, T.FIGURE}


@dataclass
class Question:
    block_id: str
    preview: str
    options: list[dict[str, Any]]  # [{"type":..., "level":..., "label":...}], первый — лучший


def _key(t: str, level: int | None) -> tuple[str, int | None]:
    return (t, level if t == "heading" else None)


def _set(b: Block, t: str, level: int | None, conf: float, source: str) -> None:
    b.type = BlockType(t)
    b.level = (level or 1) if t == "heading" else None
    b.confidence = round(conf, 3)
    b.source = source  # type: ignore[assignment]


def apply_llm(ir: DocumentIR, labels: dict[str, Label], accept: float) -> list[str]:
    """Применяет метки ИИ. Возвращает id блоков с противоречием (их спросим у пользователя)."""
    conflicts: list[str] = []
    for b in ir.blocks:
        lab = labels.get(b.id)
        if lab is None:
            continue
        level = lab.level or None
        b.features["llm"] = {"type": lab.type, "level": level, "confidence": lab.confidence}
        same = _key(lab.type, level) == _key(b.type.value, b.level)
        if same:
            _set(b, lab.type, level, max(b.confidence, min(lab.confidence, 0.99), accept), "llm")
        elif lab.confidence >= accept:
            # Правила и ИИ расходятся: берём вариант ИИ, но уточним у пользователя.
            prev = (b.type.value, b.level)
            _set(b, lab.type, level, lab.confidence * 0.8, "llm")
            b.features["rules_guess"] = {"type": prev[0], "level": prev[1]}
            conflicts.append(b.id)
        else:
            b.features["rules_guess"] = {"type": b.type.value, "level": b.level}
            conflicts.append(b.id)
    return conflicts


def _options(b: Block) -> list[dict[str, Any]]:
    seen: list[tuple[str, int | None]] = []

    def add(t: str, level: int | None) -> None:
        k = _key(t, level)
        if k in OPTION_LABELS and k not in seen:
            seen.append(k)

    add(b.type.value, b.level)
    llm = b.features.get("llm")
    if llm:
        add(llm["type"], llm.get("level"))
    rg = b.features.get("rules_guess")
    if rg:
        add(rg["type"], rg.get("level"))
    for t, lvl, _ in b.alternatives:
        add(t, lvl)
    for k in (("heading", 1), ("heading", 2), ("body", None)):
        add(*k)
    return [{"type": t, "level": lvl, "label": OPTION_LABELS[(t, lvl)]} for t, lvl in seen[:5]]


def pick_questions(ir: DocumentIR, low: float, conflicts: list[str], limit: int) -> list[Question]:
    cands = [
        b
        for b in ir.blocks
        if b.type not in SKIP and b.source != "user" and (b.confidence < low or b.id in conflicts)
    ]

    def importance(b: Block) -> int:
        kinds = [b.type.value] + [
            str(d["type"]) for d in (b.features.get("llm"), b.features.get("rules_guess")) if d
        ]
        return min(IMPORTANCE.get(BlockType(k), 3) for k in kinds)

    cands.sort(key=lambda b: (importance(b), b.confidence))
    out = []
    for b in cands[:limit]:
        text = " ".join(b.text.split())
        out.append(Question(b.id, text[:200], _options(b)))
    return out


def apply_answers(ir: DocumentIR, answers: dict[str, tuple[str, int | None]]) -> None:
    by_id = ir.by_id()
    for bid, (t, level) in answers.items():
        b = by_id.get(bid)
        if b is not None:
            _set(b, t, level, 1.0, "user")


def apply_defaults(ir: DocumentIR, questions: list[Question]) -> None:
    """Пользователь не ответил: берём лучший вариант, пометка попадёт в отчёт."""
    by_id = ir.by_id()
    for q in questions:
        b = by_id.get(q.block_id)
        if b is not None and b.source != "user":
            b.notes.append("no_user_answer")
