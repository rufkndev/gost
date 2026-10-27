"""Схема и загрузка профилей оформления (`app/profiles/<id>/profile.yaml`).

Профиль — это данные: новый вуз добавляется папкой без изменения кода.
Профиль с ошибкой в схеме не загружается.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Align = Literal["left", "center", "right", "justify"]
Case = Literal["upper", "as_is"]
Numbering = Literal["continuous", "per_section"]
CheckMode = Literal["off", "check"]

PROFILE_FILE = "profile.yaml"
TEMPLATE_FILE = "template.docx"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Margins(_Strict):
    left: float = Field(gt=0)
    right: float = Field(gt=0)
    top: float = Field(gt=0)
    bottom: float = Field(gt=0)


class PageNumbering(_Strict):
    position: Literal["bottom_center", "bottom_right", "top_center", "top_right"]
    trailing_dot: bool = False
    start_from: int = Field(ge=1)
    show_on_title: bool
    font_pt: float = Field(gt=0)


class Page(_Strict):
    size: Literal["A4"]
    margins_mm: Margins
    landscape_margins_mm: Margins
    numbering: PageNumbering
    hyphenation: bool


class Body(_Strict):
    font: str = Field(min_length=1)
    size_pt: float = Field(gt=0)
    color: str = Field(pattern=r"^[0-9A-Fa-f]{6}$")
    line_spacing: float = Field(gt=0)
    first_line_indent_cm: float = Field(ge=0)
    align: Align
    space_before_pt: float = Field(ge=0)
    space_after_pt: float = Field(ge=0)
    widow_control: bool


class HeadingStyle(_Strict):
    case: Case
    align: Align
    bold: bool
    underline: bool
    indent: bool
    new_page: bool
    trailing_dot: bool
    size_pt: float = Field(gt=0)
    line_spacing: float = Field(gt=0)
    space_before_pt: float = Field(ge=0)
    space_after_pt: float = Field(ge=0)


class NumberedHeadingStyle(HeadingStyle):
    numbering: str = Field(pattern=r"^\{n\}(\.\{m\}(\.\{k\})?)?$")


class ConsecutiveHeadings(_Strict):
    space_between_pt: float = Field(ge=0)


class Headings(_Strict):
    structural_names: list[str] = Field(min_length=1)
    structural: HeadingStyle
    level1: NumberedHeadingStyle
    level2: NumberedHeadingStyle
    level3: NumberedHeadingStyle
    consecutive_headings: ConsecutiveHeadings | None = None
    keep_with_next: bool

    @model_validator(mode="after")
    def _check_numbering_depth(self) -> Self:
        expected = {"level1": "{n}", "level2": "{n}.{m}", "level3": "{n}.{m}.{k}"}
        for level, prefix in expected.items():
            if getattr(self, level).numbering != prefix:
                raise ValueError(f"headings.{level}.numbering must be {prefix!r}")
        return self


def _check_caption_template(template: str, word: str) -> None:
    if not template.startswith(word) or "{num}" not in template or "{title}" not in template:
        raise ValueError(f"caption template must look like '{word} {{num}} – {{title}}'")


class FigureCaption(_Strict):
    position: Literal["below", "above"]
    align: Align
    indent: bool
    template: str
    part_suffix: str | None = None
    trailing_dot: bool
    line_spacing: float = Field(gt=0)
    numbering: Numbering
    appendix_number: str

    @model_validator(mode="after")
    def _check_template(self) -> Self:
        _check_caption_template(self.template, "Рисунок")
        return self


class Figures(_Strict):
    align: Align
    indent: bool
    blank_lines_around: bool = False
    caption: FigureCaption


class TableCaption(_Strict):
    position: Literal["above", "below"]
    align: Align
    indent: bool
    template: str
    trailing_dot: bool
    size_pt: float | None = Field(default=None, gt=0)
    line_spacing: float = Field(gt=0)
    numbering: Numbering
    appendix_number: str

    @model_validator(mode="after")
    def _check_template(self) -> Self:
        _check_caption_template(self.template, "Таблица")
        return self


class TableText(_Strict):
    size_pt: float = Field(gt=0)
    size_pt_min: float | None = Field(default=None, gt=0)
    line_spacing: float = Field(gt=0)
    first_line_indent_cm: float = Field(ge=0)


class TableHeaderRow(_Strict):
    align: Align
    bold: bool | None = None  # None — оставить как в исходнике


class Tables(_Strict):
    caption: TableCaption
    text: TableText
    header_row: TableHeaderRow
    width: Literal["fit_text_area", "autofit_window"]
    continuation_label: str = Field(pattern=r"\{num\}")
    column_numbers_row: CheckMode = "off"
    space_after_table_pt: float = Field(ge=0)


class Notes(_Strict):
    indent: bool
    single_template: str


class FormulaNumber(_Strict):
    position: Literal["right"]
    template: str = Field(pattern=r"\{num\}")
    numbering: Numbering
    appendix_number: str


class Formulas(_Strict):
    align: Align
    number: FormulaNumber
    blank_line_around: bool
    where_without_colon: CheckMode


class Lists(_Strict):
    layout: Literal["first_line", "hanging"]
    marker_indent_cm: float = Field(ge=0)
    text_indent_cm: float | None = Field(default=None, ge=0)
    bullet_char: str = Field(min_length=1, max_length=1)
    ordered_template: str = Field(pattern=r"\{n\}")
    lettered_template: str = Field(pattern=r"\{a\}")
    letters: str = Field(min_length=1)
    uniform: bool = False

    @model_validator(mode="after")
    def _check_hanging(self) -> Self:
        if self.layout == "hanging" and self.text_indent_cm is None:
            raise ValueError("lists.text_indent_cm is required for hanging layout")
        return self


class Bibliography(_Strict):
    heading: str
    numbering: str = Field(pattern=r"\{n\}")
    indent: bool
    reformat_entries: bool


class AppendixLine(_Strict):
    align: Align
    bold: bool
    case: Case
    trailing_dot: bool = False
    size_pt: float | None = Field(default=None, gt=0)


class Appendix(_Strict):
    heading_template: str = Field(pattern=r"\{letter\}")
    letters: str = Field(min_length=1)
    new_page: bool
    label: AppendixLine
    title: AppendixLine


class Toc(_Strict):
    generate: Literal["always", "never", "auto"]
    title: str
    levels: int = Field(ge=1, le=3)
    entry_align: Align
    dot_leader: bool
    optional_below_pages: int = Field(ge=0)


class Checks(_Strict):
    figures_tables_referenced: bool = False
    appendices_referenced: bool = False
    section_ends_with_table_or_figure: bool = False
    max_blank_page_fraction: float | None = Field(default=None, gt=0, le=1)


class Profile(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    title: str = Field(min_length=1, max_length=40)  # текст кнопки в Telegram
    schema_version: Literal[1]
    page: Page
    body: Body
    headings: Headings
    figures: Figures
    tables: Tables
    notes: Notes
    formulas: Formulas
    lists: Lists
    bibliography: Bibliography
    appendix: Appendix
    toc: Toc
    checks: Checks = Checks()


class ProfileError(Exception):
    pass


def load_profile(profile_dir: Path) -> Profile:
    path = profile_dir / PROFILE_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        profile = Profile.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ProfileError(f"invalid profile {profile_dir.name}: {exc}") from exc
    if profile.id != profile_dir.name:
        raise ProfileError(f"profile id {profile.id!r} does not match folder {profile_dir.name!r}")
    return profile


def load_profiles(profiles_dir: Path) -> dict[str, Profile]:
    """Все профили из папки. ГОСТ всегда первым, остальные — по названию."""
    profiles = [
        load_profile(d) for d in sorted(profiles_dir.iterdir()) if (d / PROFILE_FILE).is_file()
    ]
    profiles.sort(key=lambda p: (not p.id.startswith("gost_"), p.title))
    return {p.id: p for p in profiles}


def template_path(profiles_dir: Path, profile_id: str) -> Path:
    return profiles_dir / profile_id / TEMPLATE_FILE
