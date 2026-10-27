"""Производные признаки абзаца для классификации (spec, раздел 5.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.ir import Block, BlockType

# Номер раздела в начале строки: «1», «1.2», «1.2.3.», за ним пробел/таб/конец.
NUMBER_RE = re.compile(r"^(?P<num>\d{1,2}(?:\.\d{1,2}){0,3})(?P<dot>\.)?(?=[\s\t]|$)")
NUMBER_GLUED_RE = re.compile(r"^(?P<num>\d{1,2}(?:\.\d{1,2}){0,3})(?P<dot>\.)?(?=[А-ЯЁA-Z])")
FIG_CAPTION_RE = re.compile(
    r"^(?P<word>рисунок|рис\.?)\s*(?P<num>(?:[А-ЯЁA-Z]\.)?\d{1,3}(?:\.\d{1,3})?)"
    r"(?P<sep>\s*[.:]?\s*[-–—]?\s*)(?P<title>.*)$",
    re.IGNORECASE | re.DOTALL,
)
TABLE_CAPTION_RE = re.compile(
    r"^(?P<word>таблица|табл\.?)\s*(?P<num>(?:[А-ЯЁA-Z]\.)?\d{1,3}(?:\.\d{1,3})?)"
    r"(?P<sep>\s*[.:]?\s*[-–—]?\s*)(?P<title>.*)$",
    re.IGNORECASE | re.DOTALL,
)
CONTINUATION_RE = re.compile(r"^(продолжение|окончание)\s+таблицы", re.IGNORECASE)
APPENDIX_RE = re.compile(r"^приложени[ея]\s+(?P<letter>[А-ЯЁA-Z]|\d{1,2})\b", re.IGNORECASE)
NOTE_RE = re.compile(r"^примечани[ея]", re.IGNORECASE)
BULLET_RE = re.compile(r"^(?P<marker>[-–—•·*▪●◦○■□➢✓►])[\s\t]+")
ORDERED_RE = re.compile(r"^(?P<marker>\d{1,2}[).])[\s\t]+")
LETTER_RE = re.compile(r"^(?P<marker>[а-яa-z]\))[\s\t]+")
TOC_ENTRY_RE = re.compile(r"(\t|\.{3,}|…+|\s{2,})\s*\d{1,3}\s*$")
FORMULA_NUM_RE = re.compile(r"^[\s\t]*(\((?:[А-ЯЁA-Z]\.)?\d{1,3}(?:\.\d{1,3})?\))?[\s\t]*$")
HEADING_STYLE_RE = re.compile(r"^(heading|заголовок)\s*(\d)$", re.IGNORECASE)
TOC_STYLE_RE = re.compile(r"^(toc|оглавление)\s*(\d)$", re.IGNORECASE)

TITLE_MARKERS = re.compile(
    r"министерств|университет|институт|кафедр|выполнил|проверил|руководител|отч[её]т|"
    r"курсов|практическ|лабораторн|работ[аы]|по дисциплине|студент|групп[аы]|москва|"
    r"\b20\d\d\b|федеральное|образован",
    re.IGNORECASE,
)

STRUCTURAL: dict[str, str] = {
    "содержание": "СОДЕРЖАНИЕ",
    "оглавление": "СОДЕРЖАНИЕ",
    "введение": "ВВЕДЕНИЕ",
    "заключение": "ЗАКЛЮЧЕНИЕ",
    "реферат": "РЕФЕРАТ",
    "аннотация": "АННОТАЦИЯ",
    "список исполнителей": "СПИСОК ИСПОЛНИТЕЛЕЙ",
    "термины и определения": "ТЕРМИНЫ И ОПРЕДЕЛЕНИЯ",
    "определения": "ОПРЕДЕЛЕНИЯ",
    "перечень сокращений и обозначений": "ПЕРЕЧЕНЬ СОКРАЩЕНИЙ И ОБОЗНАЧЕНИЙ",
    "перечень сокращений": "ПЕРЕЧЕНЬ СОКРАЩЕНИЙ",
    "список сокращений": "СПИСОК СОКРАЩЕНИЙ",
    "обозначения и сокращения": "ОБОЗНАЧЕНИЯ И СОКРАЩЕНИЯ",
    "определения, обозначения и сокращения": "ОПРЕДЕЛЕНИЯ, ОБОЗНАЧЕНИЯ И СОКРАЩЕНИЯ",
    "список использованных источников": "СПИСОК ИСПОЛЬЗОВАННЫХ ИСТОЧНИКОВ",
    "список использованной литературы": "СПИСОК ИСПОЛЬЗОВАННОЙ ЛИТЕРАТУРЫ",
    "список литературы": "СПИСОК ЛИТЕРАТУРЫ",
    "список источников": "СПИСОК ИСТОЧНИКОВ",
    "использованные источники": "ИСПОЛЬЗОВАННЫЕ ИСТОЧНИКИ",
    "библиографический список": "БИБЛИОГРАФИЧЕСКИЙ СПИСОК",
    "литература": "ЛИТЕРАТУРА",
    "список использованных источников и литературы": "СПИСОК ИСПОЛЬЗОВАННЫХ ИСТОЧНИКОВ",
    "приложения": "ПРИЛОЖЕНИЯ",
}
TOC_TITLES = {"содержание", "оглавление"}
BIBLIOGRAPHY_KEYS = {
    k for k in STRUCTURAL if "литератур" in k or "источник" in k or k == "библиографический список"
}


def norm_key(text: str) -> str:
    t = text.strip().lower().replace("ё", "е")
    t = re.sub(r"[\s ]+", " ", t)
    return t.rstrip(" .:")


def structural_key(text: str) -> str | None:
    if len(text.strip()) > 60:
        return None
    key = norm_key(text)
    return key if key in STRUCTURAL else None


@dataclass
class Derived:
    text: str
    number: str | None
    number_depth: int
    number_from_label: bool
    rest_starts_upper: bool
    end_char: str
    struct: str | None
    appendix: str | None
    fig_caption: bool
    table_caption: bool
    continuation: bool
    note: bool
    text_marker: str | None  # bullet/ordered/letter
    toc_like: bool
    heading_style_level: int | None
    toc_style: bool
    caption_style: bool
    has_drawing: bool
    has_math: bool
    formula_only: bool


def derive(block: Block) -> Derived:
    f = block.features
    text = block.text.strip()
    num_label = (f.get("num") or {}).get("label") if f.get("num") else None
    number = None
    depth = 0
    from_label = False
    rest = text
    m = NUMBER_RE.match(text) or NUMBER_GLUED_RE.match(text)
    if m:
        number = m.group("num")
        rest = text[m.end() :].strip()
    elif num_label and (f.get("num") or {}).get("fmt") not in (None, "bullet"):
        lab = num_label.strip().rstrip(".")
        if ")" not in lab and re.fullmatch(r"\d{1,2}(\.\d{1,2}){0,3}", lab):
            number = lab
            from_label = True
    if number:
        depth = number.count(".") + 1
    style_name = (f.get("style_name") or "").strip()
    hs = HEADING_STYLE_RE.match(style_name)
    heading_level = int(hs.group(2)) if hs else None
    if heading_level is None and f.get("outline_lvl") is not None and f["outline_lvl"] < 9:
        heading_level = int(f["outline_lvl"]) + 1
    non_math_text = text
    bullet = BULLET_RE.match(text)
    ordered = ORDERED_RE.match(text)
    letter = LETTER_RE.match(text)
    marker = "bullet" if bullet else "ordered" if ordered else "letter" if letter else None
    appendix = APPENDIX_RE.match(text)
    return Derived(
        text=text,
        number=number,
        number_depth=depth,
        number_from_label=from_label,
        rest_starts_upper=bool(rest[:1]) and rest[:1].isupper(),
        end_char=text[-1] if text else "\0",  # "\0": пустая строка входит в любую строку
        struct=structural_key(text),
        appendix=appendix.group("letter") if appendix else None,
        fig_caption=bool(FIG_CAPTION_RE.match(text)),
        table_caption=bool(TABLE_CAPTION_RE.match(text)),
        continuation=bool(CONTINUATION_RE.match(text)),
        note=bool(NOTE_RE.match(text)),
        text_marker=marker,
        toc_like=bool(TOC_ENTRY_RE.search(block.text.rstrip())) and len(text) < 200,
        heading_style_level=heading_level,
        toc_style=bool(TOC_STYLE_RE.match(style_name)),
        caption_style=style_name.lower() in ("caption", "название объекта"),
        has_drawing=int(f.get("drawings", 0)) > 0,
        has_math=int(f.get("math", 0)) > 0,
        formula_only=int(f.get("math", 0)) > 0 and bool(FORMULA_NUM_RE.match(non_math_text)),
    )


def is_content(block: Block) -> bool:
    """Абзац с видимым содержимым (текст, рисунок, формула) или таблица."""
    if block.type == BlockType.TABLE:
        return True
    f = block.features
    return bool(block.text.strip()) or int(f.get("drawings", 0)) > 0 or int(f.get("math", 0)) > 0
