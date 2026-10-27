"""Белый список текстовых нормализаций (spec 6.1 + решение владельца, п. 9).

Только эти изменения текста разрешены; каждое пишется в журнал (id блока, было → стало).
Правка, затрагивающая результат поля Word или непрозрачный элемент, не выполняется.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.classify.features import (
    BULLET_RE,
    FIG_CAPTION_RE,
    LETTER_RE,
    ORDERED_RE,
    TABLE_CAPTION_RE,
)
from app.core.ir import Block, BlockType, DocumentIR, Run
from app.core.profile import Profile

T = BlockType
WS = " \t "

RULE_TRIM = "trim_whitespace"
RULE_SPACES = "collapse_spaces"
RULE_EMPTY = "remove_empty"
RULE_HEADING_NUMBER = "heading_number"
RULE_HEADING_DOT = "heading_trailing_dot"
RULE_CASE = "structural_case"
RULE_CAPTION = "caption_prefix"
RULE_TOC = "toc_replaced"
RULE_LIST_MARKER = "list_marker"
RULE_LABELS = {
    RULE_TRIM: "удалены пробелы и табуляции в начале и конце абзаца",
    RULE_SPACES: "схлопнуты двойные пробелы",
    RULE_EMPTY: "удалены пустые абзацы",
    RULE_HEADING_NUMBER: "номер заголовка приведён к виду «1.2 Название»",
    RULE_HEADING_DOT: "удалена точка в конце заголовка",
    RULE_CASE: "заголовки структурных элементов набраны прописными",
    RULE_CAPTION: "подписи приведены к виду профиля («Рисунок 3 – …»)",
    RULE_TOC: "старое оглавление заменено автоматическим",
    RULE_LIST_MARKER: "маркеры списков заменены на маркеры профиля",
}

TEXT_BLOCKS = {
    T.BODY,
    T.HEADING,
    T.STRUCTURAL_HEADING,
    T.APPENDIX_HEADING,
    T.LIST_ITEM,
    T.FIGURE_CAPTION,
    T.TABLE_CAPTION,
    T.TABLE_NOTE,
    T.BIBLIOGRAPHY_ITEM,
    T.FIGURE,
    T.FORMULA,
    T.UNKNOWN,
}
HEADING_NUM_RE = re.compile(r"^(?P<num>\d{1,2}(?:\.\d{1,2}){0,3})(?P<tail>[.)]?[ \t ]*)")


@dataclass
class JournalEntry:
    block_id: str
    rule: str
    before: str
    after: str


@dataclass
class Journal:
    entries: list[JournalEntry] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)  # правила без текста (пустые абзацы)

    def add(self, block: Block, rule: str, before: str) -> None:
        if before != block.text:
            self.entries.append(JournalEntry(block.id, rule, before, block.text))

    def count(self, rule: str, n: int = 1) -> None:
        self.summary[rule] = self.summary.get(rule, 0) + n

    def final_texts(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for e in self.entries:
            out[e.block_id] = e.after
        return out

    def stats(self) -> dict[str, int]:
        out = dict(self.summary)
        for e in self.entries:
            out[e.rule] = out.get(e.rule, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"entries": [e.__dict__ for e in self.entries], "summary": self.summary}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Journal:
        return cls([JournalEntry(**e) for e in d.get("entries", [])], dict(d.get("summary", {})))


# --- правка текста поверх run ----------------------------------------------------------


class RunText:
    """Правки сплошного текста абзаца с переносом на run."""

    def __init__(self, block: Block) -> None:
        self.block = block

    @property
    def text(self) -> str:
        return self.block.text

    def _locate(self) -> list[tuple[int, int]]:
        """Для каждого символа: (индекс run, смещение)."""
        pos: list[tuple[int, int]] = []
        for i, r in enumerate(self.block.runs):
            pos.extend((i, k) for k in range(len(r.text)))
        return pos

    def replace(self, start: int, end: int, new: str) -> bool:
        runs = self.block.runs
        pos = self._locate()
        if start < 0 or end > len(pos) or start > end:
            return False
        touched = {pos[k][0] for k in range(start, end)}
        if any(not runs[i].editable for i in touched):
            return False
        # Куда вставлять: в текстовый run, где начинается правка, иначе в ближайший.
        target: int | None = None
        if new:
            cands = [pos[start][0]] if start < len(pos) else []
            if start > 0:
                cands.append(pos[start - 1][0])
            for i in cands:
                if runs[i].kind == "text" and runs[i].editable:
                    target = i
                    break
            if target is None:
                return False
        # Удаление символов (с конца, чтобы смещения не сдвигались).
        insert_at: tuple[int, int] | None = None
        if new and target is not None:
            if start < len(pos) and pos[start][0] == target:
                insert_at = (target, pos[start][1])
            else:
                insert_at = (target, len(runs[target].text))
        for k in range(end - 1, start - 1, -1):
            i, off = pos[k]
            r = runs[i]
            r.text = r.text[:off] + r.text[off + 1 :]
            if insert_at and insert_at[0] == i and off < insert_at[1]:
                insert_at = (i, insert_at[1] - 1)
        if insert_at:
            i, off = insert_at
            runs[i].text = runs[i].text[:off] + new + runs[i].text[off:]
        self.block.runs = [r for r in runs if r.text or r.kind not in ("text", "tab")]
        return True


# --- отдельные нормализации -----------------------------------------------------------


def _trim(block: Block) -> None:
    rt = RunText(block)
    t = rt.text
    lead = len(t) - len(t.lstrip(WS))
    if lead:
        rt.replace(0, lead, "")
    t = rt.text
    trail = len(t) - len(t.rstrip(WS))
    if trail:
        rt.replace(len(t) - trail, len(t), "")


def _collapse_spaces(block: Block) -> None:
    rt = RunText(block)
    while True:
        done = True
        for m in re.finditer(r" {2,}", rt.text):
            if rt.replace(m.start(), m.end(), " "):
                done = False
                break
        if done:
            return


def _heading_number(block: Block) -> None:
    rt = RunText(block)
    num_label = (block.features.get("num") or {}).get("label")
    m = HEADING_NUM_RE.match(rt.text)
    if m and (m.group("tail") or rt.text[m.end() : m.end() + 1].isalpha()):
        if m.group("tail") != " ":
            rt.replace(m.end("num"), m.end(), " ")
        block.features["heading_number"] = m.group("num")
    elif num_label and (block.features.get("num") or {}).get("fmt") not in ("bullet", "none"):
        # Автонумерация Word: номер переносится в текст (нумерация Word снимается),
        # иначе видимый номер пропал бы из документа.
        num = str(block.features.get("number") or num_label.strip().rstrip("."))
        first_text = next((i for i, r in enumerate(block.runs) if r.editable), None)
        if first_text is not None:
            if block.runs[first_text].kind == "text" and first_text == 0:
                block.runs[0].text = f"{num} " + block.runs[0].text
            else:
                block.runs.insert(0, Run(text=f"{num} "))
            block.features["heading_number"] = num
            block.features["materialized_number"] = True


def _heading_dot(block: Block) -> None:
    rt = RunText(block)
    t = rt.text
    if t.endswith(".") and not t.endswith(".."):
        rt.replace(len(t) - 1, len(t), "")


def _upper(block: Block) -> None:
    t = block.text
    if t == t.upper():
        return
    if any(not r.editable and r.text for r in block.runs):
        return
    for r in block.runs:
        if r.kind == "text":
            r.text = r.text.upper()


def _caption(block: Block, profile: Profile) -> None:
    is_fig = block.type == T.FIGURE_CAPTION or block.type == T.FIGURE
    rx = FIG_CAPTION_RE if is_fig else TABLE_CAPTION_RE
    tpl = profile.figures.caption.template if is_fig else profile.tables.caption.template
    word = tpl.split("{num}")[0].strip()
    m_dash = re.search(r"\{num\}\s*(\S)\s*\{title\}", tpl)
    dash = m_dash.group(1) if m_dash else "–"
    rt = RunText(block)
    m = rx.match(rt.text)
    if not m or (block.type == T.TABLE_CAPTION and block.features.get("continuation")):
        return
    # Сначала разделитель (правее), потом слово — так смещения не сбиваются.
    title = m.group("title")
    if title:
        if m.group("sep") != f" {dash} ":
            rt.replace(m.start("sep"), m.end("sep"), f" {dash} ")
    m = rx.match(rt.text)
    if m and (m.group("word") != word or rt.text[m.end("word") : m.start("num")] != " "):
        rt.replace(m.start("word"), m.start("num"), f"{word} ")


# --- списки ------------------------------------------------------------------------------

_LETTERS_WORD = "абвгдежзиклмнопрстуфхцчшщэюя"  # последовательность Word russianLower


def _marker_ordinal(marker: str) -> tuple[str, int | None]:
    m = marker.rstrip(".)")
    if m.isdigit():
        return "ordered", int(m)
    if len(m) == 1 and m.lower() in _LETTERS_WORD:
        return "letter", _LETTERS_WORD.index(m.lower()) + 1
    return "bullet", None


def _list_instances(ir: DocumentIR) -> list[list[Block]]:
    groups: list[list[Block]] = []
    cur: list[Block] = []
    for b in ir.blocks:
        if b.type in (T.EMPTY,):
            continue
        if b.type in (T.LIST_ITEM, T.BIBLIOGRAPHY_ITEM):
            if cur and cur[-1].type != b.type:
                groups.append(cur)
                cur = []
            cur.append(b)
        elif cur:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def _plan_list(items: list[Block], journal: Journal, profile: Profile) -> None:
    """Решает, какие пункты получают нумерацию профиля, и снимает текстовые маркеры."""
    by_level: dict[int, list[tuple[Block, str, int | None, str]]] = {}
    for b in items:
        num = b.features.get("num") or {}
        text = b.text.lstrip(WS)
        source = "none"
        kind, ordinal = "plain", None
        if num:
            kind = (
                "bullet"
                if num.get("fmt") == "bullet"
                else (
                    "letter"
                    if str(num.get("fmt", "")).startswith(("russian", "lower", "upper"))
                    and not str(num.get("fmt")).endswith("Roman")
                    else "ordered"
                )
            )
            ordinal = num.get("ordinal")
            source = "word"
        else:
            m = BULLET_RE.match(text) or LETTER_RE.match(text) or ORDERED_RE.match(text)
            if m and b.type == T.LIST_ITEM:
                kind, ordinal = _marker_ordinal(m.group("marker"))
                source = "text"
        lvl = int(b.level or 0) if b.type == T.LIST_ITEM else 0
        by_level.setdefault(lvl, []).append((b, kind, ordinal, source))

    for lvl, entries in by_level.items():
        # Подряд идущие пункты одного вида на уровне — одна «серия» нумерации.
        series: list[list[tuple[Block, str, int | None, str]]] = []
        for e in entries:
            if series and series[-1][-1][1] == e[1] and series[-1][-1][3] == e[3]:
                series[-1].append(e)
            else:
                series.append([e])
        for s in series:
            kind, source = s[0][1], s[0][3]
            if source == "none" or kind == "plain":
                continue
            ords = [e[2] for e in s]
            if kind != "bullet":
                if any(o is None for o in ords):
                    continue
                start = int(ords[0] or 1)
                if ords != list(range(start, start + len(ords))):
                    for e in s:
                        e[0].notes.append("list_numbering_kept")
                    continue
            else:
                start = 1
            if s[0][0].type == T.BIBLIOGRAPHY_ITEM and source == "text":
                continue
            series_id = s[0][0].id
            for e in s:
                b = e[0]
                b.features["numbering"] = {
                    "kind": kind,
                    "start": start,
                    "level": lvl,
                    "series": series_id,
                }
                if source == "text":
                    before = b.text
                    rt = RunText(b)
                    t = rt.text
                    lead = len(t) - len(t.lstrip(WS))
                    m = (
                        BULLET_RE.match(t[lead:])
                        or LETTER_RE.match(t[lead:])
                        or ORDERED_RE.match(t[lead:])
                    )
                    if m and rt.replace(0, lead + m.end(), ""):
                        journal.add(b, RULE_LIST_MARKER, before)
                    else:
                        b.features.pop("numbering", None)
                elif source == "word" and kind != "bullet":
                    pass  # номера и так не в тексте


# --- точка входа ---------------------------------------------------------------------------


def normalize(ir: DocumentIR, profile: Profile) -> Journal:
    journal = Journal()
    empties = sum(1 for b in ir.blocks if b.type == T.EMPTY)
    if empties:
        journal.count(RULE_EMPTY, empties)
    if any(b.type == T.TOC for b in ir.blocks):
        journal.count(RULE_TOC)
    for b in ir.blocks:
        if b.type not in TEXT_BLOCKS:
            continue
        before = b.text
        _trim(b)
        journal.add(b, RULE_TRIM, before)
        before = b.text
        _collapse_spaces(b)
        journal.add(b, RULE_SPACES, before)
        if b.type == T.HEADING:
            before = b.text
            _heading_number(b)
            journal.add(b, RULE_HEADING_NUMBER, before)
        if b.type in (T.HEADING, T.STRUCTURAL_HEADING, T.APPENDIX_HEADING):
            before = b.text
            _heading_dot(b)
            journal.add(b, RULE_HEADING_DOT, before)
        upper_struct = profile.headings.structural.case == "upper"
        is_label = b.type == T.APPENDIX_HEADING and b.features.get("appendix_part") == "label"
        if (b.type == T.STRUCTURAL_HEADING and upper_struct) or (
            is_label and profile.appendix.label.case == "upper"
        ):
            before = b.text
            _upper(b)
            journal.add(b, RULE_CASE, before)
        if b.type in (T.FIGURE_CAPTION, T.TABLE_CAPTION) or (
            b.type == T.FIGURE and FIG_CAPTION_RE.match(b.text.strip())
        ):
            before = b.text
            _caption(b, profile)
            journal.add(b, RULE_CAPTION, before)
    for items in _list_instances(ir):
        _plan_list(items, journal, profile)
    return journal
