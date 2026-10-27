"""Линтер готового документа (spec, раздел 6.3).

Открывает результат заново и проверяет эффективное форматирование против профиля.
`error` блокирует выдачу файла, `warning` попадает в отчёт.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lxml import etree

from app.core import docx_io as dx
from app.core import expect as E
from app.core.builder import BM_CONTENT, BM_TITLE_END
from app.core.classify.features import FIG_CAPTION_RE, TABLE_CAPTION_RE
from app.core.docx_io import Package, qn, wattr
from app.core.ir import BlockType
from app.core.ooxml import SEMANTIC_RPR
from app.core.profile import Profile
from app.core.report import caption_matches

ALLOWED_PPR = {"pStyle", "numPr", "sectPr"}
HEADING_STYLES = (
    set(E.HEADING.values())
    | set(E.HEADING_TIGHT.values())
    | set(E.HEADING_FOLLOW.values())
    | set(E.HEADING_TIGHT_FOLLOW.values())
)
NEW_PAGE_CHECK = {E.STRUCT, E.APPENDIX, *E.HEADING.values()}
LIST_STYLES = {E.LIST_BULLET, E.LIST_NUMBER, E.BIB_ITEM}


@dataclass
class LintIssue:
    rule: str
    ref: str  # позиция: «p12» — 12-й абзац основной части, «t3» — таблица, «s1» — раздел
    severity: str  # error / warning
    message: str
    preview: str = ""  # первые слова абзаца — только для отчёта пользователю, не для логов


def _preview(p: etree._Element, words: int = 10) -> str:
    parts = dx.paragraph_text(p).split()
    return " ".join(parts[:words]) + (" …" if len(parts) > words else "")


class _Linter:
    def __init__(self, data: bytes, profile: Profile, expect_toc: bool) -> None:
        self.pkg: Package = dx.read_package(data)
        self.profile = profile
        self.expect_toc = expect_toc
        self.styles = dx.StyleSheet.from_package(self.pkg)
        self.specs = E.para_specs(profile)
        self.issues: list[LintIssue] = []

    def add(self, rule: str, ref: str, severity: str, message: str, preview: str = "") -> None:
        self.issues.append(LintIssue(rule, ref, severity, message, preview))

    # --- регионы ---------------------------------------------------------------------------

    def regions(self) -> tuple[list[etree._Element], list[etree._Element], list[etree._Element]]:
        title: list[etree._Element] = []
        toc: list[etree._Element] = []
        content: list[etree._Element] = []
        region = "title"
        for el in dx.children(self.pkg.body):
            if el.tag == qn("w:bookmarkStart") and wattr(el, "name") == BM_TITLE_END:
                region = "toc"
                continue
            if el.tag == qn("w:bookmarkStart") and wattr(el, "name") == BM_CONTENT:
                region = "content"
                continue
            if el.tag not in (qn("w:p"), qn("w:tbl")):
                continue
            {"title": title, "toc": toc, "content": content}[region].append(el)
        return title, toc, content

    # --- абзацы ------------------------------------------------------------------------------

    def check_paragraph(self, p: etree._Element, ref: str, in_table: bool = False) -> None:
        ppr = p.find(qn("w:pPr"))
        style = None
        if ppr is not None:
            ps = ppr.find(qn("w:pStyle"))
            style = wattr(ps, "val") if ps is not None else None
            extra = [
                dx.local(c)
                for c in ppr
                if isinstance(c.tag, str)
                and dx.local(c) not in ALLOWED_PPR | ({"jc"} if in_table else set())
            ]
            if extra:
                self.add(
                    "direct_paragraph_formatting",
                    ref,
                    "error",
                    f"прямое форматирование абзаца: {', '.join(sorted(set(extra)))}",
                    _preview(p),
                )
        text = dx.paragraph_text(p)
        has_obj = dx.has_descendant(p, "w:drawing", "w:pict", "w:object", "m:oMath")
        has_break = any(wattr(br, "type") == "page" for br in p.iter(qn("w:br")))
        carrier = ppr is not None and ppr.find(qn("w:sectPr")) is not None
        if not text.strip() and not has_obj:
            if not (has_break or carrier or in_table):
                self.add("empty_paragraph", ref, "error", "пустой абзац")
            return
        if style not in self.specs:
            self.add("foreign_style", ref, "error", f"стиль не из профиля: {style}", _preview(p))
            return
        self.check_runs(p, ref)
        self.check_effective(p, ref, style)
        if not in_table and "  " in text:
            self.add("double_spaces", ref, "warning", "двойные пробелы", _preview(p))

    def check_runs(self, p: etree._Element, ref: str) -> None:
        for r in p.iter(qn("w:r")):
            if any(
                a.tag in (qn("w:drawing"), qn("w:pict"), qn("w:txbxContent"), qn("m:oMath"))
                for a in r.iterancestors()
            ):
                continue
            rpr = r.find(qn("w:rPr"))
            if rpr is None:
                continue
            bad = [
                dx.local(c)
                for c in rpr
                if isinstance(c.tag, str) and dx.local(c) not in SEMANTIC_RPR
            ]
            rs = rpr.find(qn("w:rStyle"))
            if rs is not None and wattr(rs, "val") != E.FOOTNOTE_REF:
                bad.append("rStyle")
            if bad:
                self.add(
                    "direct_run_formatting",
                    ref,
                    "error",
                    f"прямое форматирование текста: {', '.join(sorted(set(bad)))}",
                    _preview(p),
                )
                return

    def check_effective(self, p: etree._Element, ref: str, style: str) -> None:
        spec = self.specs[style]
        pp = self.styles.effective_ppr(p)
        prob: list[str] = []
        if (pp.align or "left") != spec.align and style not in (E.TABLE_TEXT,):
            prob.append("выравнивание")
        if pp.line is None or abs(pp.line - spec.line) > 0.01:
            prob.append("межстрочный интервал")
        if (
            abs(pp.space_before / 20 - spec.before_pt) > 0.5
            or abs(pp.space_after / 20 - spec.after_pt) > 0.5
        ):
            prob.append("интервал до/после")
        if style not in LIST_STYLES or not pp.num_id:
            if abs(pp.ind_first - dx.cm_to_twips(spec.first_cm)) > 20:
                prob.append("абзацный отступ")
        if spec.page_break_before != pp.page_break_before:
            prob.append("начало с новой страницы")
        first_run = next(
            (
                r
                for r in p.iter(qn("w:r"))
                if r.find(qn("w:t")) is not None
                and not any(
                    a.tag in (qn("w:drawing"), qn("w:pict"), qn("mc:AlternateContent"))
                    for a in r.iterancestors()
                )
            ),
            None,
        )
        if first_run is not None:
            rp = self.styles.effective_rpr(p, first_run)
            if (rp.font or "").lower() != spec.font.lower():
                prob.append("шрифт")
            if rp.size_pt is None or abs(rp.size_pt - spec.size_pt) > 0.25:
                prob.append("кегль")
            if spec.bold and not rp.bold:
                prob.append("полужирный")
        if prob:
            self.add(
                "style_mismatch",
                ref,
                "error",
                "не соответствует профилю: " + ", ".join(prob),
                _preview(p),
            )
        text = dx.paragraph_text(p).strip()
        if style in HEADING_STYLES | {E.STRUCT, E.APPENDIX, E.APPENDIX_TITLE}:
            if text.endswith(".") and not text.endswith(".."):
                self.add(
                    "heading_trailing_dot", ref, "error", "точка в конце заголовка", _preview(p)
                )
        if (
            style == E.STRUCT
            and self.profile.headings.structural.case == "upper"
            and text != text.upper()
        ):
            self.add(
                "structural_case",
                ref,
                "warning",
                "заголовок структурного элемента не прописными",
                _preview(p),
            )

    # --- разделы, колонтитулы, оглавление --------------------------------------------------

    def check_sections(self) -> None:
        sects = list(self.pkg.body.iter(qn("w:sectPr")))
        for k, s in enumerate(sects):
            ref = f"s{k + 1}"
            pg = s.find(qn("w:pgSz"))
            w = dx._int(wattr(pg, "w")) or 0
            h = dx._int(wattr(pg, "h")) or 0
            landscape = w > h
            if {w, h} != {11906, 16838}:
                self.add("page_size", ref, "error", "формат страницы не A4")
            m = (
                self.profile.page.landscape_margins_mm
                if landscape
                else self.profile.page.margins_mm
            )
            mar = s.find(qn("w:pgMar"))
            for side in ("left", "right", "top", "bottom"):
                v = dx._int(wattr(mar, side)) or 0
                if abs(v - dx.mm_to_twips(getattr(m, side))) > 30:
                    self.add("page_margins", ref, "error", f"поле {side} не по профилю")

    def _footer_has_page(self, sect: etree._Element) -> bool | None:
        for ref in sect.findall(qn("w:footerReference")):
            if wattr(ref, "type") not in (None, "default"):
                continue
            part = self.pkg.part_for_rel(ref.get(qn("r:id"), ""))
            if part and self.pkg.has(part):
                root = self.pkg.xml(part)
                instr = " ".join((t.text or "") for t in root.iter(qn("w:instrText")))
                instr += " ".join(wattr(f, "instr") or "" for f in root.iter(qn("w:fldSimple")))
                return "PAGE" in instr.upper()
        return None

    def check_page_numbers(self, has_title: bool) -> None:
        sects = list(self.pkg.body.iter(qn("w:sectPr")))
        if not sects:
            self.add("page_numbers_missing", "s1", "error", "нет параметров страницы")
            return
        content_sects = sects[1:] if has_title else sects
        if has_title and self._footer_has_page(sects[0]):
            self.add("page_number_on_title", "s1", "error", "номер страницы на титульном листе")
        if not content_sects or not self._footer_has_page(content_sects[0]):
            self.add(
                "page_numbers_missing",
                "s2" if has_title else "s1",
                "error",
                "нет номера страницы в нижнем колонтитуле",
            )

    def check_toc(self, toc: list[etree._Element], content: list[etree._Element]) -> None:
        if not self.expect_toc:
            return
        instr = " ".join((t.text or "") for el in toc for t in el.iter(qn("w:instrText")))
        if "TOC" not in instr:
            self.add("toc_missing", "toc", "error", "нет оглавления")
            return
        entries = sum(
            1
            for el in toc
            for ps in el.iter(qn("w:pStyle"))
            if (wattr(ps, "val") or "").startswith("GostTOC")
        )
        levels = self.profile.toc.levels
        headings = 0
        for el in content:
            if el.tag != qn("w:p"):
                continue
            pp = self.styles.effective_ppr(el)
            if (
                pp.outline_lvl is not None
                and pp.outline_lvl < levels
                and dx.paragraph_text(el).strip()
            ):
                headings += 1
        if entries != headings:
            self.add(
                "toc_incomplete",
                "toc",
                "warning",
                f"в оглавлении {entries} строк, заголовков {headings}",
            )

    # --- содержательные предупреждения (методичка) -----------------------------------------

    def check_content(self, content: list[etree._Element]) -> None:
        checks = self.profile.checks
        styled = [(el, self._style(el)) for el in content]
        body_text = " ".join(
            dx.paragraph_text(el)
            for el, s in styled
            if el.tag == qn("w:p") and s not in (E.FIGURE_CAPTION, E.TABLE_CAPTION)
        )
        fig_nums: list[str] = []
        tab_nums: list[str] = []
        for k, (el, style) in enumerate(styled):
            ref = f"p{k + 1}"
            if style == E.FIGURE_CAPTION or style == E.TABLE_CAPTION:
                is_fig = style == E.FIGURE_CAPTION
                text = dx.paragraph_text(el).strip()
                btype = BlockType.FIGURE_CAPTION if is_fig else BlockType.TABLE_CAPTION
                if not caption_matches(text, btype, self.profile):
                    self.add(
                        "caption_format",
                        ref,
                        "warning",
                        "подпись не по шаблону профиля",
                        _preview(el),
                    )
                m = (FIG_CAPTION_RE if is_fig else TABLE_CAPTION_RE).match(text)
                if m:
                    (fig_nums if is_fig else tab_nums).append(m.group("num"))
                neighbor = (
                    styled[k - 1]
                    if is_fig and k > 0
                    else (styled[k + 1] if not is_fig and k + 1 < len(styled) else None)
                )
                if is_fig and (neighbor is None or neighbor[1] != E.FIGURE):
                    self.add(
                        "caption_position", ref, "warning", "подпись не под рисунком", _preview(el)
                    )
                if (
                    not is_fig
                    and not re.match(r"^(продолжение|окончание)", text, re.I)
                    and (neighbor is None or neighbor[0].tag != qn("w:tbl"))
                ):
                    self.add(
                        "caption_position", ref, "warning", "подпись не над таблицей", _preview(el)
                    )
            if el.tag == qn("w:tbl") and self.profile.tables.column_numbers_row == "check":
                rows = el.findall(qn("w:tr"))
                cells = rows[1].findall(qn("w:tc")) if len(rows) > 1 else []
                vals = [
                    "".join(dx.paragraph_text(p) for p in c.iter(qn("w:p"))).strip() for c in cells
                ]
                if vals != [str(i + 1) for i in range(len(vals))] or not vals:
                    self.add(
                        "table_column_numbers",
                        f"t{k + 1}",
                        "warning",
                        "нет строки с номерами столбцов после шапки",
                    )
        for nums, word in ((fig_nums, "рис"), (tab_nums, "табл")):
            plain = [n for n in nums if re.fullmatch(r"\d+", n)]
            if plain and plain != [str(i + 1) for i in range(len(plain))]:
                self.add(
                    "caption_numbering",
                    "doc",
                    "warning",
                    f"нумерация ({word}.) непоследовательная: {', '.join(plain[:15])}",
                )
            if checks.figures_tables_referenced:
                for n in nums:
                    if not re.search(
                        rf"{word}\w*\.?\s*(№\s*)?{re.escape(n)}(?!\d)", body_text, re.IGNORECASE
                    ):
                        self.add(
                            "not_referenced",
                            "doc",
                            "warning",
                            f"нет ссылки в тексте: {'рисунок' if word == 'рис' else 'таблица'} {n}",
                        )
        if checks.section_ends_with_table_or_figure:
            starts = {
                E.STRUCT,
                E.APPENDIX,
                *E.HEADING.values(),
                *E.HEADING_TIGHT.values(),
                *E.HEADING_FOLLOW.values(),
                *E.HEADING_TIGHT_FOLLOW.values(),
            }
            for k, (el, style) in enumerate(styled):
                nxt = styled[k + 1][1] if k + 1 < len(styled) else None
                if nxt in starts or nxt is None:
                    if el.tag == qn("w:tbl") or style in (E.FIGURE, E.FIGURE_CAPTION):
                        self.add(
                            "section_ends_with_object",
                            f"p{k + 1}",
                            "warning",
                            "раздел заканчивается таблицей или рисунком",
                        )

    def _style(self, el: etree._Element) -> str | None:
        if el.tag != qn("w:p"):
            return None
        ps = el.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
        return wattr(ps, "val") if ps is not None else None

    def run(self) -> list[LintIssue]:
        title, toc, content = self.regions()
        for k, el in enumerate(content):
            if el.tag == qn("w:p"):
                self.check_paragraph(el, f"p{k + 1}")
            else:
                for j, p in enumerate(el.iter(qn("w:p"))):
                    if any(a.tag == qn("w:txbxContent") for a in p.iterancestors()):
                        continue
                    self.check_paragraph(p, f"t{k + 1}.{j + 1}", in_table=True)
        self.check_sections()
        self.check_page_numbers(bool(title))
        self.check_toc(toc, content)
        self.check_content(content)
        return self.issues


def lint(data: bytes, profile: Profile, expect_toc: bool = False) -> list[LintIssue]:
    return _Linter(data, profile, expect_toc).run()
