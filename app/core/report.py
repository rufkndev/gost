"""Предварительный (до оплаты, без ИИ) и итоговый отчёты (spec, раздел 6.4).

Модуль возвращает данные; человекочитаемый текст собирается в render_* по шаблонам
из app/bot/texts.py, чтобы формулировки правились в одном месте.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.core import expect as E
from app.core.classify.features import FIG_CAPTION_RE, TABLE_CAPTION_RE
from app.core.ir import Block, BlockType, DocumentIR
from app.core.profile import Profile

T = BlockType
CATEGORIES = (
    "font",
    "spacing",
    "indents",
    "margins",
    "captions",
    "headings",
    "page_numbers",
    "empty_lines",
)
CHECKABLE = {
    T.BODY,
    T.LIST_ITEM,
    T.BIBLIOGRAPHY_ITEM,
    T.HEADING,
    T.STRUCTURAL_HEADING,
    T.APPENDIX_HEADING,
    T.FIGURE_CAPTION,
    T.TABLE_CAPTION,
}


@dataclass
class PreliminaryReport:
    profile_id: str
    profile_title: str
    pages: int
    counts: dict[str, Any]
    violations: dict[str, int]
    uncertain: int
    price_rub: int
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def expected_style(block: Block, profile: Profile) -> E.ParaSpec | None:
    specs = E.para_specs(profile)
    t = block.type
    if t in (T.BODY, T.UNKNOWN):
        return specs[E.BODY]
    if t == T.HEADING:
        return specs[E.heading_text_style(block.level or 1)]
    if t == T.STRUCTURAL_HEADING:
        return specs[E.STRUCT]
    if t == T.APPENDIX_HEADING:
        part = block.features.get("appendix_part")
        return specs[E.APPENDIX_TITLE if part == "title" else E.APPENDIX]
    if t == T.LIST_ITEM:
        return specs[E.LIST_BULLET]
    if t == T.BIBLIOGRAPHY_ITEM:
        return specs[E.BIB_ITEM]
    if t == T.FIGURE_CAPTION:
        return specs[E.FIGURE_CAPTION]
    if t == T.TABLE_CAPTION:
        return specs[E.TABLE_CAPTION]
    if t == T.FIGURE:
        return specs[E.FIGURE]
    return None


def _close(a: float | None, b: float, tol: float) -> bool:
    return a is not None and abs(a - b) <= tol


def block_violations(block: Block, profile: Profile) -> set[str]:
    """Категории нарушений исходного блока относительно профиля."""
    out: set[str] = set()
    spec = expected_style(block, profile)
    f = block.features
    if spec is None or block.type not in CHECKABLE:
        return out
    if f.get("font") and f["font"].lower() != spec.font.lower():
        out.add("font")
    if f.get("size_pt") and not _close(f["size_pt"], spec.size_pt, 0.25):
        out.add("font")
    line = f.get("line")
    if line is None or not _close(line, spec.line, 0.05):
        if not (line is None and f.get("line_exact_pt") is None and spec.line == 1.0):
            out.add("spacing")
    if block.type in (T.BODY, T.LIST_ITEM, T.BIBLIOGRAPHY_ITEM):
        if not _close(f.get("space_before_pt"), spec.before_pt, 1) or not _close(
            f.get("space_after_pt"), spec.after_pt, 1
        ):
            out.add("spacing")
    if block.type in (T.BODY, T.BIBLIOGRAPHY_ITEM):
        if not _close(f.get("ind_first_cm"), spec.first_cm, 0.1) or f.get("leading_ws"):
            out.add("indents")
    if f.get("double_spaces"):
        out.add("empty_lines")
    text = block.text.strip()
    if block.type in (T.HEADING, T.STRUCTURAL_HEADING, T.APPENDIX_HEADING):
        if text.endswith(".") and not text.endswith("..."):
            out.add("headings")
        if f.get("align") != spec.align:
            out.add("headings")
        if spec.bold and float(f.get("bold_ratio", 0)) < 0.8:
            out.add("headings")
        if block.type == T.STRUCTURAL_HEADING and text != text.upper():
            out.add("headings")
        if block.type == T.HEADING and re.match(r"^\d+(\.\d+)*\.\s", text):
            out.add("headings")
    if block.type in (T.FIGURE_CAPTION, T.TABLE_CAPTION):
        if not caption_matches(text, block.type, profile) or f.get("align") != spec.align:
            out.add("captions")
    return out


def caption_matches(text: str, btype: BlockType, profile: Profile) -> bool:
    """Подпись уже в формате профиля: «Рисунок 3 – Название» без точки в конце."""
    if btype == T.TABLE_CAPTION and re.match(r"^(продолжение|окончание)\s+таблицы", text, re.I):
        return True
    tpl = (
        profile.figures.caption.template
        if btype == T.FIGURE_CAPTION
        else profile.tables.caption.template
    )
    word, dash = tpl.split("{num}")[0].strip(), _template_dash(tpl)
    rx = FIG_CAPTION_RE if btype == T.FIGURE_CAPTION else TABLE_CAPTION_RE
    m = rx.match(text)
    if not m or m.group("word") != word:
        return False
    if m.group("title") and m.group("sep") != f" {dash} ":
        return False
    return not text.endswith(".")


def _template_dash(template: str) -> str:
    m = re.search(r"\{num\}\s*(\S)\s*\{title\}", template)
    return m.group(1) if m else "–"


def _section_violations(ir: DocumentIR, profile: Profile) -> dict[str, int]:
    out = {"margins": 0, "page_numbers": 0}
    for s in ir.sections:
        m = (
            profile.page.landscape_margins_mm
            if s.orientation == "landscape"
            else profile.page.margins_mm
        )
        exp = {"left": m.left, "right": m.right, "top": m.top, "bottom": m.bottom}
        if any(not _close(s.margins_mm.get(k), v, 1.0) for k, v in exp.items()):
            out["margins"] += 1
    has_title = any(b.type == T.TITLE_PAGE for b in ir.blocks)
    if not any(s.has_page_number for s in ir.sections) or (
        has_title and ir.sections and ir.sections[0].has_page_number and not ir.sections[0].title_pg
    ):
        out["page_numbers"] = 1
    return out


def count_elements(ir: DocumentIR) -> dict[str, Any]:
    c: Counter[str] = Counter()
    headings: Counter[int] = Counter()
    for b in ir.blocks:
        c[b.type.value] += 1
        if b.type == T.HEADING:
            headings[b.level or 1] += 1
    appendices = sum(
        1
        for b in ir.blocks
        if b.type == T.APPENDIX_HEADING and b.features.get("appendix_part") == "label"
    )
    return {
        "headings": {str(k): v for k, v in sorted(headings.items())},
        "structural": c[T.STRUCTURAL_HEADING.value],
        "tables": c[T.TABLE.value],
        "figures": c[T.FIGURE.value],
        "formulas": c[T.FORMULA.value],
        "bibliography": c[T.BIBLIOGRAPHY_ITEM.value],
        "appendices": appendices,
        "lists": c[T.LIST_ITEM.value],
    }


def uncertain_blocks(ir: DocumentIR, threshold: float) -> list[Block]:
    skip = {T.EMPTY, T.TITLE_PAGE, T.TOC, T.PAGE_BREAK, T.SECTION_BREAK, T.TABLE}
    return [b for b in ir.blocks if b.type not in skip and b.confidence < threshold]


def preliminary(
    ir: DocumentIR, profile: Profile, price_rub: int, accept_threshold: float = 0.9
) -> PreliminaryReport:
    viol: Counter[str] = Counter({k: 0 for k in CATEGORIES})
    for b in ir.blocks:
        for cat in block_violations(b, profile):
            viol[cat] += 1
    for k, v in _section_violations(ir, profile).items():
        viol[k] += v
    empties = sum(1 for b in ir.blocks if b.type == T.EMPTY)
    viol["empty_lines"] += empties
    notes = []
    if ir.meta.get("footer_texts"):
        notes.append("footer_text")
    if ir.meta.get("pdf_like"):
        notes.append("pdf_like")
    return PreliminaryReport(
        profile_id=profile.id,
        profile_title=profile.title,
        pages=int(ir.meta.get("pages_estimate") or 1),
        counts=count_elements(ir),
        violations=dict(viol),
        uncertain=len(uncertain_blocks(ir, accept_threshold)),
        price_rub=price_rub,
        notes=notes,
    )


CATEGORY_LABELS = {
    "font": "шрифт и кегль",
    "spacing": "межстрочные интервалы и отступы между абзацами",
    "indents": "абзацные отступы",
    "margins": "поля страницы",
    "captions": "подписи рисунков и таблиц",
    "headings": "заголовки",
    "page_numbers": "нумерация страниц",
    "empty_lines": "пустые строки и лишние пробелы",
}


def render_preliminary_text(r: PreliminaryReport) -> str:
    c = r.counts
    heads = ", ".join(f"{lvl} ур. — {n}" for lvl, n in c["headings"].items()) or "нет"
    lines = [
        f"Профиль: {r.profile_title}",
        f"Объём: около {r.pages} стр.",
        "",
        "Найдено:",
        f"• заголовков: {heads}; структурных — {c['structural']}",
        f"• таблиц: {c['tables']}, рисунков: {c['figures']}, формул: {c['formulas']}",
        f"• источников в списке литературы: {c['bibliography']}, приложений: {c['appendices']}",
        "",
        "Нарушения оформления:",
    ]
    total = 0
    for key in CATEGORIES:
        n = r.violations.get(key, 0)
        total += n
        if n:
            lines.append(f"• {CATEGORY_LABELS[key]}: {n}")
    if not total:
        lines.append("• не найдено")
    if r.uncertain:
        lines += ["", f"Требуют уточнения: {r.uncertain} элементов — уточним после оплаты."]
    for note in r.notes:
        lines += ["", NOTE_LABELS.get(note, note)]
    return "\n".join(lines)


NOTE_LABELS = {
    "pdf_like": "Документ похож на конвертированный из PDF: строки могут быть разбиты на "
    "отдельные абзацы, а слова разделены табуляциями. Такой текст мы не меняем — "
    "результат будет аккуратнее, если вставить текст в Word заново.",
    "footer_text": "В колонтитулах был текст — колонтитулы заменяются номером страницы.",
}


# --- итоговый отчёт ------------------------------------------------------------------------

TYPE_LABELS = {
    "heading": "заголовок",
    "structural_heading": "заголовок структурного элемента",
    "appendix_heading": "заголовок приложения",
    "body": "обычный текст",
    "list_item": "элемент списка",
    "figure": "рисунок",
    "figure_caption": "подпись рисунка",
    "table_caption": "подпись таблицы",
    "table_note": "примечание к таблице",
    "formula": "формула",
    "bibliography_item": "источник",
    "unknown": "не определено",
}
TELEGRAM_LIMIT = 4096


@dataclass
class ManualItem:
    section: str
    type_label: str
    preview: str
    reason: str


@dataclass
class FinalReport:
    fixed: dict[str, int]
    normalizations: dict[str, int]
    warnings: dict[str, list[str]]
    manual: list[ManualItem]
    answers: int
    meta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixed": self.fixed,
            "normalizations": self.normalizations,
            "warnings": self.warnings,
            "answers": self.answers,
            "meta": self.meta,
            "manual": [m.__dict__ for m in self.manual],
        }


def _words(text: str, n: int = 10) -> str:
    parts = text.split()
    return " ".join(parts[:n]) + (" …" if len(parts) > n else "")


def final_report(
    ir: DocumentIR,
    journal: Any,
    issues: list[Any],
    profile: Profile,
    accept_threshold: float,
    meta: dict[str, Any],
) -> FinalReport:
    viol: Counter[str] = Counter()
    for b in ir.blocks:
        for cat in block_violations(b, profile):
            viol[cat] += 1
    for k, v in _section_violations(ir, profile).items():
        viol[k] += v
    viol["empty_lines"] += sum(1 for b in ir.blocks if b.type == T.EMPTY)

    section = "начало документа"
    manual: list[ManualItem] = []
    for b in ir.blocks:
        if (
            b.type in (T.HEADING, T.STRUCTURAL_HEADING, T.APPENDIX_HEADING)
            and b.text.strip()
            and b.features.get("appendix_part") != "title"
        ):
            section = _words(b.text, 6)
        skip = {T.EMPTY, T.TITLE_PAGE, T.TOC, T.PAGE_BREAK, T.SECTION_BREAK, T.TABLE}
        if b.type in skip or b.source == "user":
            continue
        reason = ""
        if b.confidence < accept_threshold:
            reason = "система не уверена в типе"
        elif b.type == T.UNKNOWN:
            reason = "тип не определён"
        elif "list_numbering_kept" in b.notes:
            reason = "нумерация списка непоследовательная — оставлена как была"
        if reason:
            manual.append(
                ManualItem(
                    section,
                    TYPE_LABELS.get(b.type.value, b.type.value),
                    _words(b.text or str(b.features.get("text_preview", ""))),
                    reason,
                )
            )
    warnings: dict[str, list[str]] = {}
    for i in issues:
        if i.severity != "warning":
            continue
        warnings.setdefault(i.rule, []).append(i.preview or i.message)
    answers = sum(1 for b in ir.blocks if b.source == "user")
    meta = {
        **meta,
        "notes": [
            n
            for n, on in (
                ("pdf_like", ir.meta.get("pdf_like")),
                ("footer_text", ir.meta.get("footer_texts")),
            )
            if on
        ],
    }
    return FinalReport(dict(viol), journal.stats(), warnings, manual, answers, meta)


WARNING_LABELS = {
    "caption_format": "подписи не удалось привести к шаблону",
    "caption_position": "подпись стоит не на своём месте",
    "caption_numbering": "нумерация рисунков/таблиц непоследовательная (номера не меняли)",
    "not_referenced": "нет ссылки в тексте",
    "table_column_numbers": "нет строки с номерами столбцов после шапки таблицы",
    "section_ends_with_object": "раздел заканчивается таблицей или рисунком",
    "double_spaces": "двойные пробелы внутри полей Word",
    "structural_case": "заголовок не удалось набрать прописными",
    "toc_incomplete": "оглавление стоит обновить в Word (F9)",
}


def render_final_text(rep: FinalReport, normalization_labels: dict[str, str]) -> str:
    lines = ["Что исправлено:"]
    for key in CATEGORIES:
        if rep.fixed.get(key):
            lines.append(f"• {CATEGORY_LABELS[key]}: {rep.fixed[key]}")
    if len(lines) == 1:
        lines.append("• оформление уже было в порядке")
    if rep.normalizations:
        lines += ["", "Изменения текста (только оформительские):"]
        for k, n in rep.normalizations.items():
            lines.append(f"• {normalization_labels.get(k, k)}: {n}")
    if rep.meta.get("toc_generated"):
        lines += [
            "",
            "Оглавление создано автоматически. Если номера страниц разошлись — "
            "в Word: правый клик по оглавлению → «Обновить поле».",
        ]
    if rep.answers:
        lines += ["", f"Учтены ваши ответы: {rep.answers}"]
    if rep.warnings:
        lines += ["", "Обратите внимание:"]
        for k, items in rep.warnings.items():
            lines.append(f"• {WARNING_LABELS.get(k, k)}: {len(items)}")
            for it in items[:3]:
                lines.append(f"   – {it}")
    for note in rep.meta.get("notes", []):
        lines += ["", NOTE_LABELS.get(note, note)]
    if rep.manual:
        lines += ["", "Проверьте вручную:"]
        for m in rep.manual:
            lines.append(f"• [{m.section}] {m.type_label}: «{m.preview}» — {m.reason}")
    return "\n".join(lines)
