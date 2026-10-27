"""Промежуточное представление документа (spec, раздел 4.2).

IR сериализуется в JSON (хранится в orders.ir для продолжения после вопросов),
поэтому ссылки на XML — это пути (`xml_ref`), а не объекты lxml.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class BlockType(StrEnum):
    TITLE_PAGE = "title_page"
    TOC = "toc"
    STRUCTURAL_HEADING = "structural_heading"
    HEADING = "heading"
    APPENDIX_HEADING = "appendix_heading"
    BODY = "body"
    LIST_ITEM = "list_item"
    FIGURE = "figure"
    FIGURE_CAPTION = "figure_caption"
    TABLE = "table"
    TABLE_CAPTION = "table_caption"
    TABLE_NOTE = "table_note"
    FORMULA = "formula"
    BIBLIOGRAPHY_ITEM = "bibliography_item"
    PAGE_BREAK = "page_break"
    SECTION_BREAK = "section_break"
    EMPTY = "empty"
    UNKNOWN = "unknown"


TEXT_TYPES = {
    BlockType.STRUCTURAL_HEADING,
    BlockType.HEADING,
    BlockType.APPENDIX_HEADING,
    BlockType.BODY,
    BlockType.LIST_ITEM,
    BlockType.FIGURE_CAPTION,
    BlockType.TABLE_CAPTION,
    BlockType.TABLE_NOTE,
    BlockType.FORMULA,
    BlockType.BIBLIOGRAPHY_ITEM,
    BlockType.FIGURE,
    BlockType.UNKNOWN,
}

RunKind = Literal[
    "text", "tab", "break", "footnote_ref", "hyperlink", "field", "drawing", "math", "other"
]


class Run(BaseModel):
    text: str = ""
    bold: bool = False
    italic: bool = False
    underline: bool = False
    superscript: bool = False
    subscript: bool = False
    strike: bool = False
    kind: RunKind = "text"
    xml_ref: str | None = None  # ссылка на исходный элемент для непрозрачных run
    container: str | None = None  # путь к w:hyperlink / w:fldSimple, внутри которого run
    in_field: bool = False  # текст результата поля Word — править нельзя

    @property
    def editable(self) -> bool:
        return self.kind in ("text", "tab") and not self.in_field


class Block(BaseModel):
    id: str  # стабильный id, напр. "p0042"
    type: BlockType = BlockType.UNKNOWN
    level: int | None = None
    confidence: float = 0.0  # 0..1
    source: Literal["rules", "llm", "user", "default"] = "default"
    runs: list[Run] = Field(default_factory=list)
    xml_ref: str | None = None  # путь к элементу в w:body
    features: dict[str, Any] = Field(default_factory=dict)
    page_estimate: int | None = None
    notes: list[str] = Field(default_factory=list)
    alternatives: list[tuple[str, int | None, float]] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)

    def label(self) -> str:
        return f"{self.type.value}{self.level or ''}"


class Section(BaseModel):
    end_block: str | None  # id блока, которым раздел заканчивается (None — последний)
    orientation: Literal["portrait", "landscape"] = "portrait"
    width_mm: float | None = None
    height_mm: float | None = None
    margins_mm: dict[str, float] = Field(default_factory=dict)
    has_page_number: bool = False
    title_pg: bool = False


class DocumentIR(BaseModel):
    blocks: list[Block]
    sections: list[Section] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    def by_id(self) -> dict[str, Block]:
        return {b.id: b for b in self.blocks}
