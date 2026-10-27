"""Профиль → ожидаемое оформление каждого стиля.

Единая таблица «стиль → параметры» для генерации template.docx, линтера
и подсчёта нарушений в предварительном отчёте.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.profile import HeadingStyle, Profile

# Фиксированный набор стилей (spec 4.3 + решение владельца, вопрос 9 этапа 0).
BODY = "GostBody"
BODY_AFTER_TABLE = "GostBodyAfterTable"
HEADING = {1: "GostHeading1", 2: "GostHeading2", 3: "GostHeading3"}
# Заголовки подряд (методичка вуза: «между ними нет отступа»). Интервалы Word складываются,
# поэтому нужны варианты без интервала после (Tight), до (Follow) и без обоих.
HEADING_TIGHT = {1: "GostHeading1Tight", 2: "GostHeading2Tight", 3: "GostHeading3Tight"}
HEADING_FOLLOW = {1: "GostHeading1Follow", 2: "GostHeading2Follow", 3: "GostHeading3Follow"}
HEADING_TIGHT_FOLLOW = {
    1: "GostHeading1TightFollow",
    2: "GostHeading2TightFollow",
    3: "GostHeading3TightFollow",
}
STRUCT = "GostStructHeading"
TOC_TITLE = "GostTOCTitle"
APPENDIX = "GostAppendixHeading"
APPENDIX_TITLE = "GostAppendixTitle"
LIST_BULLET = "GostListBullet"
LIST_NUMBER = "GostListNumber"
FIGURE = "GostFigure"
FIGURE_CAPTION = "GostFigureCaption"
TABLE_CAPTION = "GostTableCaption"
TABLE_TEXT = "GostTableText"
TABLE_HEADER = "GostTableHeader"
TABLE_NOTE = "GostTableNote"
FORMULA = "GostFormula"
FORMULA_NUMBERED = "GostFormulaNumbered"
BIB_ITEM = "GostBibItem"
TOC = {1: "GostTOC1", 2: "GostTOC2", 3: "GostTOC3"}
FOOTNOTE_TEXT = "GostFootnoteText"
FOOTNOTE_REF = "GostFootnoteReference"
FOOTER = "GostFooter"


@dataclass(frozen=True)
class ParaSpec:
    style_id: str
    name: str
    font: str
    size_pt: float
    align: str
    line: float
    first_cm: float = 0.0
    left_cm: float = 0.0
    before_pt: float = 0.0
    after_pt: float = 0.0
    bold: bool = False
    caps: bool = False
    keep_next: bool = False
    page_break_before: bool = False
    outline_lvl: int | None = None
    widow: bool = True
    no_hyphen: bool = False
    tabs: tuple[tuple[str, float], ...] = ()  # (тип, позиция см)


def text_width_cm(profile: Profile, landscape: bool = False) -> float:
    m = profile.page.landscape_margins_mm if landscape else profile.page.margins_mm
    width = 297.0 if landscape else 210.0
    return (width - m.left - m.right) / 10


def _heading(
    profile: Profile,
    sid: str,
    name: str,
    h: HeadingStyle,
    lvl: int | None,
    tight: bool = False,
    follow: bool = False,
) -> ParaSpec:
    body = profile.body
    between = (
        profile.headings.consecutive_headings.space_between_pt
        if profile.headings.consecutive_headings
        else None
    )
    return ParaSpec(
        style_id=sid,
        name=name,
        font=body.font,
        size_pt=h.size_pt,
        align=h.align,
        line=h.line_spacing,
        first_cm=body.first_line_indent_cm if h.indent else 0.0,
        before_pt=between if follow and between is not None else h.space_before_pt,
        after_pt=between if tight and between is not None else h.space_after_pt,
        bold=h.bold,
        caps=False,
        keep_next=profile.headings.keep_with_next,
        page_break_before=h.new_page,
        outline_lvl=lvl,
        no_hyphen=True,
    )


def para_specs(profile: Profile) -> dict[str, ParaSpec]:
    b = profile.body
    hd = profile.headings
    t = profile.tables
    fig = profile.figures
    specs: list[ParaSpec] = []

    specs.append(
        ParaSpec(
            BODY,
            BODY,
            align=b.align,
            first_cm=b.first_line_indent_cm,
            before_pt=b.space_before_pt,
            after_pt=b.space_after_pt,
            widow=b.widow_control,
            font=b.font,
            size_pt=b.size_pt,
            line=b.line_spacing,
        )
    )
    specs.append(
        ParaSpec(
            BODY_AFTER_TABLE,
            BODY_AFTER_TABLE,
            align=b.align,
            first_cm=b.first_line_indent_cm,
            before_pt=t.space_after_table_pt or b.space_before_pt,
            after_pt=b.space_after_pt,
            widow=b.widow_control,
            font=b.font,
            size_pt=b.size_pt,
            line=b.line_spacing,
        )
    )
    for lvl in (1, 2, 3):
        h = getattr(hd, f"level{lvl}")
        specs.append(_heading(profile, HEADING[lvl], HEADING[lvl], h, lvl - 1))
        specs.append(
            _heading(profile, HEADING_TIGHT[lvl], HEADING_TIGHT[lvl], h, lvl - 1, tight=True)
        )
        specs.append(
            _heading(profile, HEADING_FOLLOW[lvl], HEADING_FOLLOW[lvl], h, lvl - 1, follow=True)
        )
        specs.append(
            _heading(
                profile,
                HEADING_TIGHT_FOLLOW[lvl],
                HEADING_TIGHT_FOLLOW[lvl],
                h,
                lvl - 1,
                tight=True,
                follow=True,
            )
        )
    specs.append(_heading(profile, STRUCT, STRUCT, hd.structural, 0))
    specs.append(_heading(profile, TOC_TITLE, TOC_TITLE, hd.structural, None))
    ap = profile.appendix
    specs.append(
        ParaSpec(
            APPENDIX,
            APPENDIX,
            font=b.font,
            size_pt=ap.label.size_pt or hd.structural.size_pt,
            align=ap.label.align,
            line=hd.structural.line_spacing,
            bold=ap.label.bold,
            keep_next=True,
            page_break_before=ap.new_page,
            outline_lvl=0,
            no_hyphen=True,
        )
    )
    specs.append(
        ParaSpec(
            APPENDIX_TITLE,
            APPENDIX_TITLE,
            font=b.font,
            size_pt=ap.title.size_pt or hd.structural.size_pt,
            align=ap.title.align,
            line=hd.structural.line_spacing,
            bold=ap.title.bold,
            after_pt=hd.structural.space_after_pt,
            keep_next=True,
            no_hyphen=True,
        )
    )
    lst = profile.lists
    if lst.layout == "hanging":
        list_first, list_left = (
            -(lst.text_indent_cm or 0) + lst.marker_indent_cm,
            (lst.text_indent_cm or 0),
        )
    else:
        list_first, list_left = lst.marker_indent_cm, 0.0
    for sid in (LIST_BULLET, LIST_NUMBER):
        specs.append(
            ParaSpec(
                sid,
                sid,
                align=b.align,
                first_cm=list_first,
                left_cm=list_left,
                widow=b.widow_control,
                font=b.font,
                size_pt=b.size_pt,
                line=b.line_spacing,
            )
        )
    specs.append(
        ParaSpec(
            FIGURE,
            FIGURE,
            align=fig.align,
            first_cm=b.first_line_indent_cm if fig.indent else 0.0,
            keep_next=fig.caption.position == "below",
            font=b.font,
            size_pt=b.size_pt,
            line=b.line_spacing,
        )
    )
    cap = fig.caption
    specs.append(
        ParaSpec(
            FIGURE_CAPTION,
            FIGURE_CAPTION,
            font=b.font,
            size_pt=b.size_pt,
            align=cap.align,
            line=cap.line_spacing,
            first_cm=b.first_line_indent_cm if cap.indent else 0.0,
            no_hyphen=True,
        )
    )
    tc = t.caption
    specs.append(
        ParaSpec(
            TABLE_CAPTION,
            TABLE_CAPTION,
            font=b.font,
            size_pt=tc.size_pt or b.size_pt,
            align=tc.align,
            line=tc.line_spacing,
            first_cm=b.first_line_indent_cm if tc.indent else 0.0,
            keep_next=True,
            no_hyphen=True,
        )
    )
    specs.append(
        ParaSpec(
            TABLE_TEXT,
            TABLE_TEXT,
            font=b.font,
            size_pt=t.text.size_pt,
            align="left",
            line=t.text.line_spacing,
            first_cm=t.text.first_line_indent_cm,
        )
    )
    specs.append(
        ParaSpec(
            TABLE_HEADER,
            TABLE_HEADER,
            font=b.font,
            size_pt=t.text.size_pt,
            align=t.header_row.align,
            line=t.text.line_spacing,
            bold=bool(t.header_row.bold),
        )
    )
    specs.append(
        ParaSpec(
            TABLE_NOTE,
            TABLE_NOTE,
            align=b.align,
            first_cm=b.first_line_indent_cm if profile.notes.indent else 0.0,
            font=b.font,
            size_pt=b.size_pt,
            line=b.line_spacing,
        )
    )
    one_line = b.size_pt * b.line_spacing if profile.formulas.blank_line_around else 0.0
    width = text_width_cm(profile)
    specs.append(
        ParaSpec(
            FORMULA,
            FORMULA,
            align=profile.formulas.align,
            before_pt=one_line,
            after_pt=one_line,
            font=b.font,
            size_pt=b.size_pt,
            line=b.line_spacing,
        )
    )
    specs.append(
        ParaSpec(
            FORMULA_NUMBERED,
            FORMULA_NUMBERED,
            align="left",
            before_pt=one_line,
            after_pt=one_line,
            tabs=(("center", round(width / 2, 2)), ("right", round(width, 2))),
            font=b.font,
            size_pt=b.size_pt,
            line=b.line_spacing,
        )
    )
    bib = profile.bibliography
    specs.append(
        ParaSpec(
            BIB_ITEM,
            BIB_ITEM,
            align=b.align,
            first_cm=b.first_line_indent_cm if bib.indent else 0.0,
            font=b.font,
            size_pt=b.size_pt,
            line=b.line_spacing,
        )
    )
    for lvl in (1, 2, 3):
        specs.append(
            ParaSpec(
                TOC[lvl],
                f"toc {lvl}",
                font=b.font,
                size_pt=b.size_pt,
                align="left",
                line=b.line_spacing,
                left_cm=round(0.5 * (lvl - 1), 2),
                tabs=(("right_dot" if profile.toc.dot_leader else "right", round(width, 2)),),
            )
        )
    specs.append(
        ParaSpec(
            FOOTNOTE_TEXT,
            "footnote text",
            font=b.font,
            size_pt=max(10.0, b.size_pt - 2),
            align="justify",
            line=1.0,
            first_cm=b.first_line_indent_cm,
        )
    )
    specs.append(
        ParaSpec(
            FOOTER,
            "footer",
            font=b.font,
            size_pt=profile.page.numbering.font_pt,
            align="center" if profile.page.numbering.position.endswith("center") else "right",
            line=1.0,
        )
    )
    return {s.style_id: s for s in specs}


def heading_text_style(level: int) -> str:
    return HEADING[max(1, min(3, level))]
