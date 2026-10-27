"""Классификация блоков правилами (spec, раздел 5.2).

Каждое правило голосует за пару (тип, уровень) с весом. Уверенность — нормализованная
разница между лучшим и вторым вариантом. Структурные области (титульный лист,
оглавление, список литературы, приложения) определяются отдельными проходами.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from app.core.classify.features import (
    BIBLIOGRAPHY_KEYS,
    STRUCTURAL,
    TITLE_MARKERS,
    TOC_TITLES,
    Derived,
    derive,
    is_content,
)
from app.core.ir import Block, BlockType, DocumentIR

T = BlockType
Key = tuple[BlockType, int | None]

TITLE_SCAN_LIMIT = 60
SHORT_DOC_PAGES = 15


@dataclass
class _Ctx:
    blocks: list[Block]
    d: dict[str, Derived]
    median_size: float | None

    def prev_content(self, i: int) -> Block | None:
        for j in range(i - 1, -1, -1):
            if is_content(self.blocks[j]):
                return self.blocks[j]
        return None

    def next_content(self, i: int) -> Block | None:
        for j in range(i + 1, len(self.blocks)):
            if is_content(self.blocks[j]):
                return self.blocks[j]
        return None


def _is_figure_block(b: Block | None) -> bool:
    return b is not None and b.type != T.TABLE and int(b.features.get("drawings", 0)) > 0


def _is_table(b: Block | None) -> bool:
    return b is not None and b.type == T.TABLE


# --- проходы по областям ------------------------------------------------------------


def _mark_empty(ctx: _Ctx) -> None:
    for b in ctx.blocks:
        if b.type in (T.TABLE, T.TOC):
            continue
        f = b.features
        if is_content(b) or any(r.kind in ("other", "footnote_ref") and r.text for r in b.runs):
            continue
        if f.get("section_end"):
            b.type = T.SECTION_BREAK
        elif f.get("page_break_runs"):
            b.type = T.PAGE_BREAK
        else:
            b.type = T.EMPTY
        b.confidence, b.source = 1.0, "rules"


def _title_end(ctx: _Ctx) -> int:
    """Индекс первого блока после титульного листа (0 — титульного листа нет)."""
    blocks = ctx.blocks
    limit = min(len(blocks), TITLE_SCAN_LIMIT)
    first_struct = next(
        (i for i in range(limit) if blocks[i].id in ctx.d and ctx.d[blocks[i].id].struct),
        limit,
    )
    boundary = None
    for i in range(min(first_struct, limit)):
        b = blocks[i]
        f = b.features
        if i > 0 and f.get("page_break_before") and is_content(b):
            boundary = i
            break
        if i > 0 and f.get("rendered_break"):
            boundary = i
            break
        if f.get("page_break_runs") or f.get("section_end"):
            boundary = i + 1
            break
    end = boundary if boundary is not None else first_struct
    if end >= limit and boundary is None:
        # Ни разрыва, ни структурного заголовка: ищем строку «Город 20xx».
        end = 0
        for i in range(min(40, len(blocks))):
            t = blocks[i].text.strip()
            if 0 < len(t) < 40 and any(ch.isdigit() for ch in t) and "20" in t:
                end = i + 1
    text = " ".join(b.text for b in blocks[:end]) + " ".join(
        str(b.features.get("text_preview", "")) for b in blocks[:end] if b.type == T.TABLE
    )
    markers = len(set(m.group(0).lower()[:6] for m in TITLE_MARKERS.finditer(text)))
    return end if markers >= 3 else 0


def _assignment_end(ctx: _Ctx, start: int) -> int:
    """Лист задания сразу после титульного тоже не форматируется (переносится как есть)."""
    blocks = ctx.blocks
    for i in range(start, min(len(blocks), start + 60)):
        b = blocks[i]
        dv = ctx.d.get(b.id)
        if dv is not None and dv.struct:
            return start
        if i > start and (b.features.get("page_break_before") or b.features.get("rendered_break")):
            return start
        if dv is not None and re.match(r"^(техническое\s+)?задание\b", dv.text, re.IGNORECASE):
            for j in range(i, min(len(blocks), i + 80)):
                bj = blocks[j]
                dj = ctx.d.get(bj.id)
                if dj is not None and dj.struct:
                    return j
                if bj.features.get("page_break_runs") or bj.features.get("section_end"):
                    return j + 1
                if j > i and (
                    bj.features.get("page_break_before") or bj.features.get("rendered_break")
                ):
                    return j
            return start
    return start


def _mark_title(ctx: _Ctx) -> None:
    end = _title_end(ctx)
    if end:
        end = _assignment_end(ctx, end)
    for b in ctx.blocks[:end]:
        b.type, b.confidence, b.source = T.TITLE_PAGE, 0.95, "rules"


def _mark_toc(ctx: _Ctx) -> None:
    blocks = ctx.blocks
    i = 0
    while i < len(blocks):
        b = blocks[i]
        dv = ctx.d.get(b.id)
        if b.type == T.TITLE_PAGE or dv is None:
            i += 1
            continue
        if dv.toc_style or b.features.get("in_toc_field"):
            b.type, b.confidence, b.source = T.TOC, 1.0, "rules"
        elif dv.struct in TOC_TITLES:
            b.type, b.confidence, b.source = T.TOC, 1.0, "rules"
            b.features["toc_title"] = True
            j = i + 1
            while j < len(blocks):
                nb = blocks[j]
                nd = ctx.d.get(nb.id)
                if nb.type in (T.EMPTY, T.PAGE_BREAK, T.SECTION_BREAK):
                    j += 1
                    continue
                if nb.type == T.TOC:
                    j += 1
                    continue
                if nd is not None and (
                    nd.toc_like or nd.toc_style or nb.features.get("in_toc_field")
                ):
                    nb.type, nb.confidence, nb.source = T.TOC, 0.95, "rules"
                    j += 1
                    continue
                break
            i = j
            continue
        i += 1


# --- голосование ---------------------------------------------------------------------


def _votes(ctx: _Ctx, i: int, b: Block, dv: Derived) -> dict[Key, float]:
    f = b.features
    v: dict[Key, float] = defaultdict(float)
    words = int(f.get("words", 0))
    chars = int(f.get("chars", 0))
    bold = float(f.get("bold_ratio", 0))
    size = f.get("size_pt")
    big = bool(size and ctx.median_size and size >= ctx.median_size + 1.5)
    centered = f.get("align") == "center"
    prev, nxt = ctx.prev_content(i), ctx.next_content(i)
    num = f.get("num") or {}
    numbered_heading_like = False

    # Рисунки и формулы — по содержимому.
    if dv.has_drawing:
        if words <= 8 or dv.fig_caption:
            v[(T.FIGURE, None)] += 1.2
        else:
            v[(T.BODY, None)] += 0.6
            v[(T.FIGURE, None)] += 0.3
    if dv.has_math:
        if dv.formula_only:
            v[(T.FORMULA, None)] += 1.2
        else:
            v[(T.BODY, None)] += 0.8

    # Структурные заголовки и приложения.
    if dv.struct and not dv.number:
        v[(T.STRUCTURAL_HEADING, None)] += 1.2
    if dv.appendix and chars <= 150:
        v[(T.APPENDIX_HEADING, None)] += 1.2

    # Подписи.
    if dv.continuation:
        v[(T.TABLE_CAPTION, None)] += 1.2
    if dv.fig_caption and not dv.has_drawing:
        w = 0.55
        if _is_figure_block(prev) or _is_figure_block(nxt):
            w += 0.45
        if dv.caption_style:
            w += 0.2
        if chars > 250:
            w -= 0.4
        v[(T.FIGURE_CAPTION, None)] += w
        if w < 0.8:
            v[(T.BODY, None)] += 0.3
    if dv.table_caption and not dv.continuation:
        w = 0.55
        if _is_table(nxt):
            w += 0.45
        elif _is_table(prev):
            w += 0.25
        if dv.caption_style:
            w += 0.2
        if chars > 250:
            w -= 0.4
        v[(T.TABLE_CAPTION, None)] += w
        if w < 0.8:
            v[(T.BODY, None)] += 0.3
    if dv.note:
        if _is_table(prev):
            v[(T.TABLE_NOTE, None)] += 1.0
        else:
            v[(T.BODY, None)] += 0.6

    # Заголовки по стилю Word.
    if dv.heading_style_level is not None and not dv.struct and not dv.appendix:
        lvl = min(dv.number_depth or dv.heading_style_level, 3)
        w = 0.6
        if chars <= 150 and dv.end_char not in ".;,:":
            w += 0.25
        if bold >= 0.8:
            w += 0.1
        if words > 25:
            w -= 0.5
        v[(T.HEADING, lvl)] += w

    # Заголовки с номером.
    if dv.number and not dv.struct and not dv.fig_caption and not dv.table_caption:
        first = int(dv.number.split(".")[0])
        label_ok = not dv.number_from_label or bold >= 0.8 or dv.heading_style_level
        if (
            first <= 30
            and chars <= 150
            and dv.rest_starts_upper
            and dv.end_char not in ";,"
            and label_ok
        ):
            lvl = min(dv.number_depth, 3)
            w = 0.45
            if dv.end_char not in ".:":
                w += 0.25
            if bold >= 0.8:
                w += 0.15
            if dv.number_from_label and num.get("fmt") not in (None, "bullet"):
                w -= 0.1
            if nxt is not None and nxt.type != T.TABLE and int(nxt.features.get("words", 0)) > 12:
                w += 0.1
            v[(T.HEADING, lvl)] += w
            numbered_heading_like = bold >= 0.8 and dv.end_char not in ".:;,"
        if dv.end_char in ";," or words > 20:
            v[(T.LIST_ITEM, None)] += 0.4

    # Списки.
    if num and dv.heading_style_level is None:
        v[(T.LIST_ITEM, None)] += 1.1 if num.get("fmt") == "bullet" else 0.9
    strong_marker = False
    if dv.text_marker:
        marker = dv.text.split(maxsplit=1)[0] if dv.text else ""
        strong_marker = dv.text_marker in ("bullet", "letter") or marker.endswith(")")
        v[(T.LIST_ITEM, None)] += 1.1 if strong_marker else 0.5

    # Заголовки без номера (жирная/крупная короткая строка).
    if (
        not dv.number
        and not dv.struct
        and not dv.appendix
        and dv.heading_style_level is None
        and not dv.fig_caption
        and not dv.table_caption
        and not dv.text_marker
        and not num
        and 0 < words <= 15
        and chars <= 120
        and dv.end_char not in ".:;,!?"
        and dv.text[:1].isupper()
    ):
        w = 0.0
        if bold >= 0.8:
            w += 0.45
        if big:
            w += 0.2
        if centered and w:
            w += 0.1
        if float(f.get("upper_ratio", 0)) > 0.9 and chars > 3 and w:
            w += 0.1
        if w and nxt is not None and nxt.type != T.TABLE and int(nxt.features.get("words", 0)) > 12:
            w += 0.1
        if w:
            v[(T.HEADING, 1 if (centered or big) else 2)] += w

    # Обычный текст.
    body = 0.35 if words else 0.0
    if words >= 8 and dv.end_char in ".!?…»)":
        body += 0.6
    elif words >= 3 and dv.end_char in ".!?…»)":
        body += 0.45 if bold < 0.8 else 0.2
    if chars >= 80:
        body += 0.3
    if words >= 20:
        body += 0.25
    if dv.end_char == ":":
        body += 0.4
    if dv.end_char in ";,":
        body += 0.3
    if bold >= 0.8 and words < 15:
        body = max(0.0, body - 0.15)
    caption_near = (
        (dv.fig_caption and (_is_figure_block(prev) or _is_figure_block(nxt)))
        or (dv.table_caption and (_is_table(nxt) or _is_table(prev)))
        or dv.continuation
    )
    if num and dv.heading_style_level is None:
        body = 0.1  # нумерация Word — явная структура
    elif dv.struct or dv.appendix or caption_near or dv.formula_only:
        body = min(body, 0.1)
    elif strong_marker:
        body *= 0.4
    elif numbered_heading_like and words <= 15:
        body *= 0.25
    elif dv.heading_style_level is not None and words <= 15 and dv.end_char != ".":
        body *= 0.4
    v[(T.BODY, None)] += body
    return v


def _decide(b: Block, votes: dict[Key, float]) -> None:
    ranked = sorted(votes.items(), key=lambda kv: kv[1], reverse=True)
    (t, lvl), s1 = ranked[0]
    s2 = ranked[1][1] if len(ranked) > 1 else 0.0
    # Доминирование над вторым вариантом × сила самого голоса.
    conf = max(0.0, min(1.0, (1 - s2 / (s1 + 0.3)) * min(1.0, s1 / 0.8)))
    if s1 < 0.3:
        t, lvl, conf = T.UNKNOWN, None, conf
    b.type, b.level, b.confidence, b.source = t, lvl, round(conf, 3), "rules"
    b.alternatives = [(k[0].value, k[1], round(s, 3)) for k, s in ranked[:3]]


# --- постпроходы ---------------------------------------------------------------------


def _bibliography_and_appendix(ctx: _Ctx) -> None:
    in_bib = False
    appendix = None
    for i, b in enumerate(ctx.blocks):
        dv = ctx.d.get(b.id)
        if b.type == T.STRUCTURAL_HEADING:
            in_bib = bool(dv and dv.struct in BIBLIOGRAPHY_KEYS)
            b.features["canonical"] = STRUCTURAL.get(dv.struct or "", "") if dv else ""
            continue
        if b.type == T.APPENDIX_HEADING and dv and dv.appendix:
            in_bib = False
            appendix = dv.appendix.upper()
            b.features["appendix_part"] = "label"
            b.features["appendix_letter"] = appendix
            nxt_i = next(
                (j for j in range(i + 1, len(ctx.blocks)) if is_content(ctx.blocks[j])), None
            )
            if nxt_i is not None:
                nb = ctx.blocks[nxt_i]
                nd = ctx.d.get(nb.id)
                if (
                    nd is not None
                    and nb.type not in (T.TABLE, T.FIGURE, T.APPENDIX_HEADING)
                    and int(nb.features.get("chars", 0)) <= 200
                    and nd.end_char not in ".;,"
                    and not nd.fig_caption
                    and not nd.table_caption
                ):
                    strong = (
                        nb.features.get("align") == "center"
                        or float(nb.features.get("bold_ratio", 0)) >= 0.8
                    )
                    nb.type, nb.level = T.APPENDIX_HEADING, None
                    nb.confidence, nb.source = (0.92 if strong else 0.75), "rules"
                    nb.features["appendix_part"] = "title"
            continue
        if b.type == T.HEADING and b.level == 1:
            in_bib = False
        if appendix:
            b.features["appendix"] = appendix
        if in_bib and b.type in (T.BODY, T.LIST_ITEM, T.UNKNOWN, T.HEADING):
            b.type, b.level, b.confidence, b.source = T.BIBLIOGRAPHY_ITEM, None, 0.95, "rules"


def _lists(ctx: _Ctx) -> None:
    for b in ctx.blocks:
        if b.type not in (T.LIST_ITEM, T.BIBLIOGRAPHY_ITEM):
            continue
        dv = ctx.d[b.id]
        num = b.features.get("num") or {}
        if num:
            kind = "bullet" if num.get("fmt") == "bullet" else "ordered"
            level = int(b.features.get("ilvl") or 0)
        elif dv.text_marker:
            kind = "bullet" if dv.text_marker == "bullet" else "ordered"
            level = 0
        elif dv.number:
            kind, level = "ordered", 0
        else:
            kind, level = "plain", 0
        b.features["list_kind"] = kind
        if dv.text_marker == "letter":
            b.features["list_kind"] = "letter"
        if b.type == T.LIST_ITEM:
            b.level = min(level, 2)


def _short_doc_headings(ctx: _Ctx, pages: int) -> None:
    """Короткие работы без нумерованных разделов: заголовки без номера — 2 уровня.

    Иначе «Цель работы», «Ход работы», «Вывод» в практической начинались бы с новой
    страницы каждый (решение описано в docs/GUIDE.md).
    """
    if pages > SHORT_DOC_PAGES:
        return
    if any(b.type == T.HEADING and ctx.d[b.id].number for b in ctx.blocks if b.id in ctx.d):
        return
    for b in ctx.blocks:
        if b.type == T.HEADING and b.level == 1:
            b.level = 2
            b.notes.append("short_doc_unnumbered_heading")


def classify(ir: DocumentIR) -> DocumentIR:
    blocks = ir.blocks
    d = {b.id: derive(b) for b in blocks if b.type != T.TABLE and b.type != T.TOC}
    ctx = _Ctx(blocks, d, ir.meta.get("median_size_pt"))
    _mark_empty(ctx)
    _mark_title(ctx)
    _mark_toc(ctx)
    for i, b in enumerate(blocks):
        if b.source == "rules" or b.id not in d:
            continue
        _decide(b, _votes(ctx, i, b, d[b.id]))
    _bibliography_and_appendix(ctx)
    _lists(ctx)
    _short_doc_headings(ctx, int(ir.meta.get("pages_estimate") or 1))
    for b in blocks:
        if b.type in (T.HEADING, T.STRUCTURAL_HEADING, T.APPENDIX_HEADING):
            b.features["number"] = d[b.id].number if b.id in d else None
    return ir
