"""Проверка неизменности текста (spec, раздел 6.2).

Ожидаемое: исходный текст из XML исходника + журнал нормализаций.
Фактическое: текст готового файла, извлечённый независимо от кода сборки.
В ошибки попадают только id блоков и позиции — без текста документа.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from app.core import docx_io as dx
from app.core.builder import BM_CONTENT, BM_TITLE_END
from app.core.docx_io import Package, qn, wattr
from app.core.ir import BlockType, DocumentIR
from app.core.normalize import RULE_LIST_MARKER, Journal

T = BlockType
SKIP = {T.TITLE_PAGE, T.TOC, T.EMPTY, T.PAGE_BREAK, T.SECTION_BREAK}
_ORDINAL_RE = re.compile(r"^[\s\t ]*(\d{1,3}|[а-яa-z])[.)]")


@dataclass
class _Para:
    text: str
    block: str | None = None
    ordinal: int | None = None  # ожидаемый номер пункта списка
    bullet: bool = False  # ожидается маркер списка
    numbered: bool = False  # фактически: есть нумерация Word
    label_ordinal: int | None = None


@dataclass
class IntegrityResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def _counts(elements: list[etree._Element]) -> dict[str, int]:
    out = {"drawings": 0, "tables": 0, "math": 0, "footnotes": 0, "hyperlinks": 0}
    for el in elements:
        for d in el.iter(qn("w:drawing"), qn("w:pict"), qn("w:object")):
            if not any(a.tag == qn("mc:Fallback") for a in d.iterancestors()):
                out["drawings"] += 1
        out["tables"] += sum(1 for _ in el.iter(qn("w:tbl")))
        out["math"] += sum(1 for _ in el.iter(qn("m:oMath")))
        out["footnotes"] += sum(
            1 for _ in el.iter(qn("w:footnoteReference"), qn("w:endnoteReference"))
        )
        out["hyperlinks"] += sum(1 for _ in el.iter(qn("w:hyperlink")))
    return out


def _paras_of(el: etree._Element) -> list[etree._Element]:
    if el.tag == qn("w:p"):
        return [el]
    return [
        p
        for p in el.iter(qn("w:p"))
        if not any(a.tag == qn("w:txbxContent") for a in p.iterancestors())
    ]


def _typed_ordinal(text: str) -> int | None:
    m = _ORDINAL_RE.match(text)
    if not m:
        return None
    v = m.group(1)
    if v.isdigit():
        return int(v)
    letters = "абвгдежзиклмнопрстуфхцчшщэюя"
    return letters.index(v) + 1 if v in letters else None


def _expected(
    src: Package, ir: DocumentIR, journal: Journal
) -> tuple[list[_Para], list[etree._Element], list[etree._Element]]:
    finals = journal.final_texts()
    marker_before = {e.block_id: e.before for e in journal.entries if e.rule == RULE_LIST_MARKER}
    body = src.body
    paras: list[_Para] = []
    content_els: list[etree._Element] = []
    title_els: list[etree._Element] = []
    for b in ir.blocks:
        if b.xml_ref is None:
            continue
        el = dx.resolve_path(body, b.xml_ref)
        if b.type == T.TITLE_PAGE:
            title_els.append(el)
            continue
        if b.type in SKIP:
            continue
        content_els.append(el)
        if el.tag == qn("w:tbl"):
            for p in _paras_of(el):
                t = dx.paragraph_text(p)
                if t.strip():
                    paras.append(_Para(t, b.id))
            continue
        t = finals.get(b.id, dx.paragraph_text(el))
        para = _Para(t, b.id)
        plan = b.features.get("numbering")
        if plan:
            if plan.get("kind") == "bullet":
                para.bullet = True
            elif b.id in marker_before:
                para.ordinal = _typed_ordinal(marker_before[b.id])
            else:
                para.ordinal = (b.features.get("num") or {}).get("ordinal")
        elif b.features.get("num") and "list_numbering_kept" in b.notes:
            para.ordinal = (b.features.get("num") or {}).get("ordinal")
        if t.strip():
            paras.append(para)
    return paras, content_els, title_els


def _actual(result: Package) -> tuple[list[_Para], list[etree._Element], list[etree._Element]]:
    body = result.body
    numbering = dx.Numbering(result.xml_or_none("word/numbering.xml"))
    counter = numbering.counter()
    styles = dx.StyleSheet.from_package(result)
    region = "title"
    title_els: list[etree._Element] = []
    content_els: list[etree._Element] = []
    paras: list[_Para] = []
    for el in dx.children(body):
        if el.tag == qn("w:bookmarkStart"):
            name = wattr(el, "name")
            if name == BM_TITLE_END:
                region = "toc"
            elif name == BM_CONTENT:
                region = "content"
            continue
        if el.tag not in (qn("w:p"), qn("w:tbl")):
            continue
        for p in _paras_of(el):
            props = styles.effective_ppr(p)
            label = counter.next(props.num_id, props.ilvl) if props.num_id else None
            if region != "content":
                continue
            t = dx.paragraph_text(p)
            if t.strip():
                para = _Para(t)
                if label is not None:
                    para.numbered = True
                    para.label_ordinal = label[1]
                paras.append(para)
        if region == "title":
            title_els.append(el)
        elif region == "content":
            content_els.append(el)
    return paras, content_els, title_els


def check(src: Package, ir: DocumentIR, journal: Journal, result_bytes: bytes) -> IntegrityResult:
    res = IntegrityResult(ok=True)
    result = dx.read_package(result_bytes)
    exp, exp_els, exp_title = _expected(src, ir, journal)
    act, act_els, act_title = _actual(result)

    # 1. Последовательность слов (как в spec) — для диагностики.
    exp_words = [w for p in exp for w in p.text.split()]
    act_words = [w for p in act for w in p.text.split()]
    res.stats["words"] = len(exp_words)
    if exp_words != act_words:
        res.ok = False
        sm = difflib.SequenceMatcher(a=exp_words, b=act_words, autojunk=False)
        ops = [op for op in sm.get_opcodes() if op[0] != "equal"][:5]
        res.errors.append(
            f"word_sequence: {len(ops)}+ diffs at word positions "
            + ", ".join(f"{o[0]}@{o[1]}" for o in ops)
        )

    # 2. Абзац к абзацу (строже: пробелы и табуляции тоже).
    if len(exp) != len(act):
        res.ok = False
        res.errors.append(f"paragraph_count: expected {len(exp)}, got {len(act)}")
    for i, (e, a) in enumerate(zip(exp, act, strict=False)):
        if e.text != a.text:
            res.ok = False
            res.errors.append(f"paragraph_text: index {i}, block {e.block}")
            break

    # 3. Нумерация списков: номера пунктов и наличие маркеров.
    if len(exp) == len(act):
        for i, (e, a) in enumerate(zip(exp, act, strict=True)):
            if e.bullet and not a.numbered:
                res.ok = False
                res.errors.append(f"list_marker_lost: block {e.block}")
            if e.ordinal is not None and a.label_ordinal != e.ordinal:
                res.ok = False
                res.errors.append(f"list_ordinal: block {e.block} index {i}")

    # 4. Титульный лист — текст побайтно.
    def title_text(els: list[etree._Element]) -> str:
        # Пустые абзацы — не текст: разрыв страницы в конце титула заменяется разрывом раздела.
        texts = (dx.paragraph_text(p) for el in els for p in _paras_of(el))
        return "\n".join(t for t in texts if t.strip())

    if title_text(exp_title) != title_text(act_title):
        res.ok = False
        res.errors.append("title_page_text_changed")

    # 5. Количество объектов.
    ce, ca = _counts(exp_els), _counts(act_els)
    te, ta = _counts(exp_title), _counts(act_title)
    res.stats["objects"] = ce
    for k in ce:
        if ce[k] != ca[k]:
            res.ok = False
            res.errors.append(f"object_count:{k}: expected {ce[k]}, got {ca[k]}")
        if te[k] != ta[k]:
            res.ok = False
            res.errors.append(f"title_object_count:{k}: expected {te[k]}, got {ta[k]}")
    return res
