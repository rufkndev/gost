"""IR + профиль → новый .docx (spec, раздел 4.4).

Результат собирается в копии исходного пакета: картинки, сноски, гиперссылки и формулы
сохраняют свои связи (r:id). Оформление строится заново из IR и стилей профиля,
прямое форматирование исходника выбрасывается. Титульный лист переносится как есть:
его эффективное форматирование «запекается» в прямое, чтобы не зависеть от смены стилей.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from app.core import docx_io as dx
from app.core import expect as E
from app.core import ooxml as X
from app.core.docx_io import Package, StyleSheet, qn, wattr
from app.core.ir import Block, BlockType, DocumentIR, Run
from app.core.profile import Profile
from app.core.template import FOOTER_EMPTY, FOOTER_NUM, sect_pr

T = BlockType
BM_TITLE_END = "_GostTitleEnd"
BM_CONTENT = "_GostContent"
TOC_INSTR = 'TOC \\o "1-{levels}" \\h \\z \\u'
HEADING_TYPES = {T.HEADING, T.STRUCTURAL_HEADING, T.APPENDIX_HEADING}


def _detach(el: etree._Element) -> None:
    parent = el.getparent()
    if parent is not None:
        parent.remove(el)


@dataclass
class TocEntry:
    bookmark: str
    level: int
    text: str


@dataclass
class BuildResult:
    data: bytes
    toc: list[TocEntry] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def toc_needed(ir: DocumentIR, profile: Profile) -> bool:
    mode = profile.toc.generate
    if mode == "never":
        return False
    if mode == "always":
        return True
    had_toc = any(b.type == T.TOC for b in ir.blocks)
    headings = sum(1 for b in ir.blocks if b.type in (T.HEADING, T.STRUCTURAL_HEADING))
    pages = int(ir.meta.get("pages_estimate") or 1)
    return had_toc or (pages > profile.toc.optional_below_pages and headings >= 2)


class _Builder:
    def __init__(
        self,
        src: Package,
        ir: DocumentIR,
        profile: Profile,
        template: Package,
        toc_pages: dict[str, int] | None,
    ) -> None:
        self.src = src
        self.ir = ir
        self.profile = profile
        self.template = template
        self.toc_pages = toc_pages or {}
        self.pkg = src.copy()
        self.sbody = src.body
        self.src_styles = StyleSheet.from_package(src)
        self.specs = E.para_specs(profile)
        self.blocks = [b for b in ir.blocks]
        self.index = {b.id: i for i, b in enumerate(self.blocks)}
        self.style_renames: dict[str, str] = {}
        self.new_styles = deepcopy(template.xml("word/styles.xml"))
        self.toc: list[TocEntry] = []
        self.bm_id = self._max_bookmark_id() + 1000
        self.numbering_root: etree._Element | None = None
        self.abstract_ids: dict[str, str] = {}
        self.series_num: dict[str, str] = {}
        self.footer_rids: dict[str, str] = {}
        self.meta: dict[str, Any] = {
            "dropped_page_breaks": 0,
            "kept_page_breaks": 0,
            "sections_in": len(ir.sections),
        }

    # --- подготовка пакета ---------------------------------------------------------------

    def _max_bookmark_id(self) -> int:
        ids = [dx._int(wattr(b, "id")) or 0 for b in self.src.document.iter(qn("w:bookmarkStart"))]
        return max(ids, default=0)

    def _copy_src_style(self, sid: str) -> str:
        """Копирует исходный стиль (таблицы, нумерации) с префиксом Src и цепочкой basedOn."""
        if sid in self.style_renames:
            return self.style_renames[sid]
        st = self.src_styles.styles.get(sid)
        if st is None:
            return ""
        new_id = f"Src{sid}"
        self.style_renames[sid] = new_id
        src_root = self.src.xml("word/styles.xml")
        el = next((s for s in src_root.iter(qn("w:style")) if wattr(s, "styleId") == sid), None)
        if el is None:
            return ""
        copy = deepcopy(el)
        copy.set(qn("w:styleId"), new_id)
        if qn("w:default") in copy.attrib:
            del copy.attrib[qn("w:default")]
        name = copy.find(qn("w:name"))
        if name is not None:
            name.set(qn("w:val"), f"Src {wattr(name, 'val')}")
        for ref_tag in ("w:basedOn", "w:next", "w:link"):
            ref = copy.find(qn(ref_tag))
            if ref is None:
                continue
            target = wattr(ref, "val") or ""
            if ref_tag == "w:basedOn" and target in self.src_styles.styles:
                ref.set(qn("w:val"), self._copy_src_style(target) or "TableNormal")
            else:
                copy.remove(ref)
        self.new_styles.append(copy)
        return new_id

    def _install_footers(self) -> None:
        for part in (FOOTER_NUM, FOOTER_EMPTY):
            self.pkg.set_raw(part, self.template.raw(part))
            self.pkg.ensure_content_type(part, dx.CT_FOOTER)
            rid = self.pkg.add_rel(dx.MAIN_PART, dx.REL_FOOTER, part.removeprefix("word/"))
            self.footer_rids[part] = rid

    def _ensure_numbering(self) -> etree._Element:
        if self.numbering_root is not None:
            return self.numbering_root
        name = "word/numbering.xml"
        if not self.pkg.has(name):
            root = etree.Element(qn("w:numbering"), nsmap={"w": dx.W_NS})
            self.pkg.set_xml(name, root)
            self.pkg.ensure_content_type(name, dx.CT_NUMBERING)
            self.pkg.add_rel(dx.MAIN_PART, dx.REL_NUMBERING, "numbering.xml")
        root = self.pkg.xml(name)
        # Ссылки на стили нумерации исходника — на их копии.
        for tag in ("w:styleLink", "w:numStyleLink", "w:pStyle"):
            for el in root.iter(qn(tag)):
                sid = wattr(el, "val") or ""
                if sid in self.src_styles.styles:
                    new = self._copy_src_style(sid)
                    if new:
                        el.set(qn("w:val"), new)
        self.numbering_root = root
        return root

    def _abstract(self, kind: str) -> str:
        if kind in self.abstract_ids:
            return self.abstract_ids[kind]
        root = self._ensure_numbering()
        used = {dx._int(wattr(a, "abstractNumId")) or 0 for a in root.iter(qn("w:abstractNum"))}
        aid = str(max(used, default=0) + 1 + len(self.abstract_ids) + 100)
        lst = self.profile.lists
        an = X.w("abstractNum", abstractNumId=aid)
        an.append(X.w("multiLevelType", val="multilevel"))
        for k in range(3):
            lvl = X.w("lvl", ilvl=k)
            X.put(lvl, X.w("start", val=1))
            if kind == "bullet":
                X.put(lvl, X.w("numFmt", val="bullet"))
                X.put(lvl, X.w("lvlText", val=lst.bullet_char))
            elif kind == "letter":
                X.put(lvl, X.w("numFmt", val="russianLower"))
                X.put(lvl, X.w("lvlText", val=lst.lettered_template.replace("{a}", f"%{k + 1}")))
            elif kind == "bib":
                X.put(lvl, X.w("numFmt", val="decimal"))
                X.put(
                    lvl,
                    X.w(
                        "lvlText",
                        val=self.profile.bibliography.numbering.replace("{n}", f"%{k + 1}"),
                    ),
                )
            else:
                X.put(lvl, X.w("numFmt", val="decimal"))
                X.put(lvl, X.w("lvlText", val=lst.ordered_template.replace("{n}", f"%{k + 1}")))
            ppr = X.w("pPr")
            if kind == "bib":
                X.put(lvl, X.w("suff", val="space"))
                first = (
                    self.profile.body.first_line_indent_cm
                    if self.profile.bibliography.indent
                    else 0
                )
                X.put(ppr, X.w("ind", left=0, firstLine=X.twips(first)))
            elif lst.layout == "hanging":
                text = lst.text_indent_cm or 1.25
                left = X.twips(text * (k + 1))
                hang = X.twips(text - lst.marker_indent_cm)
                X.put(lvl, X.w("suff", val="tab"))
                tabs = X.w("tabs")
                tabs.append(X.w("tab", val="num", pos=left))
                X.put(ppr, tabs)
                X.put(ppr, X.w("ind", left=left, hanging=hang))
            else:
                X.put(lvl, X.w("suff", val="space"))
                X.put(
                    ppr,
                    X.w(
                        "ind",
                        left=X.twips(lst.marker_indent_cm * k),
                        firstLine=X.twips(lst.marker_indent_cm),
                    ),
                )
            X.put(lvl, X.w("lvlJc", val="left"))
            X.put(lvl, ppr)
            rpr = X.w("rPr")
            X.put(rpr, X.w("rFonts", ascii=self.profile.body.font, hAnsi=self.profile.body.font))
            X.put(lvl, rpr)
            an.append(lvl)
        # abstractNum должны идти перед num.
        first_num = root.find(qn("w:num"))
        if first_num is not None:
            first_num.addprevious(an)
        else:
            root.append(an)
        self.abstract_ids[kind] = aid
        return aid

    def _num_for(self, plan: dict[str, Any], is_bib: bool) -> str:
        key = str(plan["series"])
        if key in self.series_num:
            return self.series_num[key]
        kind = "bib" if is_bib else str(plan["kind"])
        aid = self._abstract(kind)
        root = self._ensure_numbering()
        used = {dx._int(wattr(n, "numId")) or 0 for n in root.iter(qn("w:num"))}
        nid = str(max(used, default=0) + 1)
        num = X.w("num", numId=nid)
        num.append(X.w("abstractNumId", val=aid))
        lo = X.w("lvlOverride", ilvl=int(plan.get("level") or 0))
        lo.append(X.w("startOverride", val=int(plan.get("start") or 1)))
        num.append(lo)
        root.append(num)
        self.series_num[key] = nid
        return nid

    # --- запекание форматирования (титульный лист, надписи) ------------------------------

    def _bake_paragraph(self, p: etree._Element) -> None:
        props = self.src_styles.effective_ppr(p)
        old = p.find(qn("w:pPr"))
        ppr = X.w("pPr")
        if old is not None:
            for keep in ("w:numPr", "w:framePr", "w:pBdr", "w:shd", "w:textDirection"):
                el = old.find(qn(keep))
                if el is not None:
                    X.put(ppr, deepcopy(el))
        X.put(ppr, X.on("keepNext", props.keep_next))
        X.put(ppr, X.on("widowControl", props.widow_control))
        tabs = self._effective_tabs(p)
        if tabs is not None:
            X.put(ppr, tabs)
        sp = X.w("spacing", before=props.space_before, after=props.space_after)
        if props.line is not None:
            sp.set(qn("w:line"), str(round(props.line * 240)))
            sp.set(qn("w:lineRule"), "auto")
        elif props.line_exact_twips is not None:
            sp.set(qn("w:line"), str(props.line_exact_twips))
            sp.set(qn("w:lineRule"), "exact")
        X.put(ppr, sp)
        ind = X.w("ind", left=props.ind_left, right=props.ind_right)
        if props.ind_first < 0:
            ind.set(qn("w:hanging"), str(-props.ind_first))
        else:
            ind.set(qn("w:firstLine"), str(props.ind_first))
        X.put(ppr, ind)
        X.put(ppr, X.w("contextualSpacing", val=0))
        X.put(ppr, X.w("jc", val=X.JC.get(props.align or "left", "left")))
        mark = self._mark_rpr(p)
        X.put(ppr, mark)
        if old is not None:
            p.replace(old, ppr)
        else:
            p.insert(0, ppr)
        for r in self._runs_of(p):
            self._bake_run(p, r)

    def _effective_tabs(self, p: etree._Element) -> etree._Element | None:
        stops: dict[int, etree._Element] = {}
        layers = [s.ppr for s in self.src_styles.chain(self.src_styles.para_style_id(p))]
        layers.append(p.find(qn("w:pPr")))
        for ppr in layers:
            if ppr is None:
                continue
            tabs = ppr.find(qn("w:tabs"))
            if tabs is None:
                continue
            for t in tabs.findall(qn("w:tab")):
                pos = dx._int(wattr(t, "pos")) or 0
                if wattr(t, "val") == "clear":
                    stops.pop(pos, None)
                else:
                    stops[pos] = deepcopy(t)
        if not stops:
            return None
        out = X.w("tabs")
        for pos in sorted(stops):
            out.append(stops[pos])
        return out

    def _mark_rpr(self, p: etree._Element) -> etree._Element:
        fake = X.w("r")
        mark = p.find(f"{qn('w:pPr')}/{qn('w:rPr')}")
        if mark is not None:
            fake.append(deepcopy(mark))
        return self._baked_rpr(self.src_styles.effective_rpr(p, fake))

    def _baked_rpr(self, rp: dx.RunProps) -> etree._Element:
        rpr = X.w("rPr")
        font = rp.font or self.profile.body.font
        X.put(rpr, X.w("rFonts", ascii=font, hAnsi=font, eastAsia=font, cs=font))
        X.put(rpr, X.on("b", rp.bold))
        X.put(rpr, X.on("bCs", rp.bold))
        X.put(rpr, X.on("i", rp.italic))
        X.put(rpr, X.on("iCs", rp.italic))
        X.put(rpr, X.on("caps", rp.caps))
        X.put(rpr, X.on("strike", rp.strike))
        X.put(rpr, X.w("color", val=rp.color or "000000"))
        size = rp.size_pt or self.profile.body.size_pt
        X.put(rpr, X.w("sz", val=round(size * 2)))
        X.put(rpr, X.w("szCs", val=round(size * 2)))
        if rp.highlight:
            X.put(rpr, X.w("highlight", val=rp.highlight))
        X.put(rpr, X.w("u", val="single" if rp.underline else "none"))
        if rp.vert_align:
            X.put(rpr, X.w("vertAlign", val=rp.vert_align))
        return rpr

    def _bake_run(self, p: etree._Element, r: etree._Element) -> None:
        rpr = self._baked_rpr(self.src_styles.effective_rpr(p, r))
        old = r.find(qn("w:rPr"))
        if old is not None:
            r.replace(old, rpr)
        else:
            r.insert(0, rpr)

    @staticmethod
    def _runs_of(p: etree._Element) -> Iterable[etree._Element]:
        """Run абзаца, без run внутри вложенных объектов (у надписей свои абзацы)."""
        stack = list(p)
        while stack:
            el = stack.pop(0)
            if not isinstance(el.tag, str):
                continue
            if el.tag == qn("w:r"):
                yield el
            elif el.tag in (
                qn("w:hyperlink"),
                qn("w:fldSimple"),
                qn("w:smartTag"),
                qn("w:sdt"),
                qn("w:sdtContent"),
                qn("w:customXml"),
            ):
                stack = list(el) + stack

    def _bake_nested(self, el: etree._Element) -> None:
        """Все абзацы внутри элемента (надписи, ячейки) — с запечённым форматированием."""
        for p in list(el.iter(qn("w:p"))):
            if p is el:
                continue
            self._bake_paragraph(p)

    def _rename_table_styles(self, el: etree._Element) -> None:
        for ts in el.iter(qn("w:tblStyle")):
            sid = wattr(ts, "val") or ""
            new = self._copy_src_style(sid)
            if new:
                ts.set(qn("w:val"), new)
            else:
                _detach(ts)

    # --- блоки ---------------------------------------------------------------------------

    def _src(self, path: str) -> etree._Element:
        return dx.resolve_path(self.sbody, path)

    def _next_content(self, i: int) -> Block | None:
        for j in range(i + 1, len(self.blocks)):
            b = self.blocks[j]
            if b.type not in (T.EMPTY, T.PAGE_BREAK, T.SECTION_BREAK, T.TOC, T.TITLE_PAGE):
                return b
        return None

    def _prev_content(self, i: int) -> Block | None:
        for j in range(i - 1, -1, -1):
            b = self.blocks[j]
            if b.type not in (T.EMPTY, T.PAGE_BREAK, T.SECTION_BREAK, T.TOC, T.TITLE_PAGE):
                return b
        return None

    def _starts_new_page(self, b: Block | None) -> bool:
        if b is None:
            return False
        style = self._style_for(b, self.index[b.id])
        spec = self.specs.get(style)
        return bool(spec and spec.page_break_before)

    def _style_for(self, b: Block, i: int) -> str:
        t = b.type
        if t in (T.BODY, T.UNKNOWN):
            prev = self._prev_content(i)
            if (
                prev is not None
                and prev.type == T.TABLE
                and self.profile.tables.space_after_table_pt > 0
            ):
                return E.BODY_AFTER_TABLE
            return E.BODY
        if t == T.HEADING:
            lvl = max(1, min(3, b.level or 1))
            if self.profile.headings.consecutive_headings is None:
                return E.HEADING[lvl]
            prev, nxt = self._prev_content(i), self._next_content(i)
            follow = prev is not None and prev.type == T.HEADING
            tight = nxt is not None and nxt.type == T.HEADING
            if follow and tight:
                return E.HEADING_TIGHT_FOLLOW[lvl]
            if tight:
                return E.HEADING_TIGHT[lvl]
            if follow:
                return E.HEADING_FOLLOW[lvl]
            return E.HEADING[lvl]
        if t == T.STRUCTURAL_HEADING:
            return E.STRUCT
        if t == T.APPENDIX_HEADING:
            return E.APPENDIX_TITLE if b.features.get("appendix_part") == "title" else E.APPENDIX
        if t == T.LIST_ITEM:
            plan = b.features.get("numbering") or {}
            kind = plan.get("kind") or b.features.get("list_kind")
            return E.LIST_NUMBER if kind in ("ordered", "letter") else E.LIST_BULLET
        if t == T.BIBLIOGRAPHY_ITEM:
            return E.BIB_ITEM
        if t == T.FIGURE:
            return E.FIGURE
        if t == T.FIGURE_CAPTION:
            return E.FIGURE_CAPTION
        if t == T.TABLE_CAPTION:
            return E.TABLE_CAPTION
        if t == T.TABLE_NOTE:
            return E.TABLE_NOTE
        if t == T.FORMULA:
            numbered = bool(re.search(r"\(\S+\)\s*$", b.text))
            return E.FORMULA_NUMBERED if numbered and b.text.startswith("\t") else E.FORMULA
        return E.BODY

    def _paragraph(self, b: Block, i: int) -> etree._Element:
        style = self._style_for(b, i)
        p = X.paragraph(style)
        ppr = X.p_ppr(p)
        plan = b.features.get("numbering")
        if plan and b.type in (T.LIST_ITEM, T.BIBLIOGRAPHY_ITEM):
            nid = self._num_for(plan, b.type == T.BIBLIOGRAPHY_ITEM)
            numpr = X.w("numPr")
            numpr.append(X.w("ilvl", val=int(plan.get("level") or 0)))
            numpr.append(X.w("numId", val=nid))
            X.put(ppr, numpr)
        elif (
            b.type in (T.LIST_ITEM, T.BIBLIOGRAPHY_ITEM)
            and b.features.get("num_id")
            and "list_numbering_kept" in b.notes
        ):
            numpr = X.w("numPr")
            numpr.append(X.w("ilvl", val=int(b.features.get("ilvl") or 0)))
            numpr.append(X.w("numId", val=str(b.features["num_id"])))
            X.put(ppr, numpr)
        drop_breaks = self._starts_new_page(self._next_content(i))
        strip_emphasis = b.type in HEADING_TYPES or b.type in (T.FIGURE_CAPTION, T.TABLE_CAPTION)
        content = self._render_runs(b.runs, drop_breaks, strip_emphasis)
        bm = self._toc_bookmark(b, style)
        if bm:
            p.append(X.w("bookmarkStart", id=self.bm_id, name=bm))
        for el in content:
            p.append(el)
        if bm:
            p.append(X.w("bookmarkEnd", id=self.bm_id))
            self.bm_id += 1
        return p

    def _toc_bookmark(self, b: Block, style: str) -> str | None:
        if not self.want_toc:
            return None
        spec = self.specs.get(style)
        if spec is None or spec.outline_lvl is None:
            return None
        level = spec.outline_lvl + 1
        if level > self.profile.toc.levels:
            return None
        name = f"_TocGost{len(self.toc) + 1:04d}"
        self.toc.append(TocEntry(name, level, " ".join(b.text.split())))
        return name

    def _render_runs(
        self, runs: list[Run], drop_page_breaks: bool, strip_emphasis: bool
    ) -> list[etree._Element]:
        out: list[etree._Element] = []
        wrapper: etree._Element | None = None
        wrapper_path: str | None = None
        for r in runs:
            if r.container != wrapper_path:
                wrapper = None
                wrapper_path = r.container
                if r.container is not None:
                    src = self._src(r.container)
                    wrapper = etree.Element(src.tag)
                    for k, v in src.attrib.items():
                        wrapper.set(k, v)
                    out.append(wrapper)
            el = self._render_run(
                r,
                drop_page_breaks,
                strip_emphasis,
                in_link=wrapper is not None and wrapper.tag == qn("w:hyperlink"),
            )
            if el is None:
                continue
            if wrapper is not None:
                wrapper.append(el)
            else:
                out.append(el)
        return out

    def _rpr(
        self, r: Run, strip_emphasis: bool, in_link: bool, rstyle: str | None = None
    ) -> etree._Element | None:
        return X.semantic_rpr(
            bold=r.bold and not strip_emphasis,
            italic=r.italic,
            underline=r.underline and not strip_emphasis and not in_link,
            strike=r.strike,
            superscript=r.superscript and rstyle is None,
            subscript=r.subscript,
            rstyle=rstyle,
        )

    def _render_run(
        self, r: Run, drop_page_breaks: bool, strip_emphasis: bool, in_link: bool
    ) -> etree._Element | None:
        if r.kind == "text":
            if not r.text:
                return None
            return X.run(r.text, self._rpr(r, strip_emphasis, in_link))
        if r.kind == "tab":
            rr = X.run(None, self._rpr(r, strip_emphasis, in_link))
            rr.append(X.w("tab"))
            return rr
        if r.xml_ref is None:
            return None
        src = self._src(r.xml_ref)
        if r.kind == "break":
            if not r.text and drop_page_breaks:
                self.meta["dropped_page_breaks"] += 1
                return None
            rr = X.run(None, None)
            rr.append(deepcopy(src))
            return rr
        parent = src.getparent()
        in_run = parent is not None and parent.tag == qn("w:r")
        copy = deepcopy(src)
        self._bake_nested(copy)
        if r.kind == "math" or not in_run:
            return copy
        rstyle = E.FOOTNOTE_REF if r.kind == "footnote_ref" else None
        rpr = None if r.kind == "drawing" else self._rpr(r, strip_emphasis, in_link, rstyle)
        rr = X.run(None, rpr)
        rr.append(copy)
        return rr

    def _table(self, b: Block) -> etree._Element:
        tbl = deepcopy(self._src(b.xml_ref or ""))
        self._rename_table_styles(tbl)
        width = self.profile.tables.width
        for t in [tbl, *tbl.iter(qn("w:tbl"))]:
            tblpr = t.find(qn("w:tblPr"))
            if tblpr is None:
                continue
            ind = tblpr.find(qn("w:tblInd"))
            if ind is not None:
                tblpr.remove(ind)
            if t is tbl:
                tw = tblpr.find(qn("w:tblW"))
                too_wide = (
                    tw is not None
                    and wattr(tw, "type") == "dxa"
                    and (dx._int(wattr(tw, "w")) or 0) > X.twips(E.text_width_cm(self.profile))
                )
                if width == "autofit_window" or too_wide or tw is None:
                    new = X.w("tblW", w=5000, type="pct")
                    if tw is not None:
                        tblpr.replace(tw, new)
                    else:
                        tblpr.insert(0, new)
                    layout = tblpr.find(qn("w:tblLayout"))
                    if layout is not None:
                        tblpr.remove(layout)
        rows = tbl.findall(qn("w:tr"))
        header_rows = [
            r for r in rows if r.find(f"{qn('w:trPr')}/{qn('w:tblHeader')}") is not None
        ] or rows[:1]
        header_paras = {id(p) for r in header_rows for p in r.iter(qn("w:p"))}
        bold_header = self.profile.tables.header_row.bold
        for p in tbl.iter(qn("w:p")):
            is_header = id(p) in header_paras
            old = p.find(qn("w:pPr"))
            ppr = X.w("pPr")
            X.put(ppr, X.w("pStyle", val=E.TABLE_HEADER if is_header else E.TABLE_TEXT))
            if old is not None:
                num = old.find(qn("w:numPr"))
                if num is not None:
                    X.put(ppr, deepcopy(num))
                jc = old.find(qn("w:jc"))
                if jc is not None and not is_header:
                    X.put(ppr, deepcopy(jc))
                p.replace(old, ppr)
            else:
                p.insert(0, ppr)
            for r in self._runs_of(p):
                rpr = r.find(qn("w:rPr"))
                if rpr is None:
                    continue
                X.strip_rpr_to_semantic(rpr)
                if is_header and bold_header is not None:
                    for tag in ("w:b", "w:bCs"):
                        el = rpr.find(qn(tag))
                        if el is not None:
                            rpr.remove(el)
                if not len(rpr):
                    r.remove(rpr)
            for el in p.iter(qn("w:lastRenderedPageBreak")):
                _detach(el)
            for draw in p.iter(qn("w:txbxContent")):
                self._bake_nested(draw)
        return tbl

    def _title_element(self, b: Block) -> etree._Element:
        el = deepcopy(self._src(b.xml_ref or ""))
        if el.tag == qn("w:p"):
            ppr = el.find(qn("w:pPr"))
            if ppr is not None:
                sect = ppr.find(qn("w:sectPr"))
                if sect is not None:
                    ppr.remove(sect)
            self._bake_paragraph(el)
            for lr in list(el.iter(qn("w:lastRenderedPageBreak"))):
                _detach(lr)
            for txbx in el.iter(qn("w:txbxContent")):
                self._bake_nested(txbx)
        else:
            self._rename_table_styles(el)
            self._bake_nested(el)
        return el

    @staticmethod
    def _strip_trailing_page_breaks(elements: list[etree._Element]) -> None:
        """Разрыв страницы в конце титульного листа заменяется разрывом раздела."""
        while elements:
            last = elements[-1]
            if last.tag != qn("w:p"):
                return
            brs = [br for br in last.iter(qn("w:br")) if wattr(br, "type") == "page"]
            text = dx.paragraph_text(last).strip()
            has_obj = dx.has_descendant(last, "w:drawing", "w:pict", "w:object")
            for br in brs:
                _detach(br)
            if not text and not has_obj and not brs:
                return
            if not text and not has_obj:
                elements.pop()
                continue
            return

    # --- оглавление ----------------------------------------------------------------------

    def _toc_paragraphs(self) -> list[etree._Element]:
        if not self.toc:
            return []
        title = X.paragraph(E.TOC_TITLE)
        title.append(X.run(self.profile.toc.title))
        out = [title]
        instr = TOC_INSTR.format(levels=self.profile.toc.levels)
        for k, entry in enumerate(self.toc):
            p = X.paragraph(E.TOC[min(entry.level, 3)])
            if k == 0:
                begin = X.field_runs(instr, "")[:3]
                p.extend(begin)
            link = X.w("hyperlink", anchor=entry.bookmark, history=1)
            link.append(X.run(entry.text))
            tab = X.run()
            tab.append(X.w("tab"))
            link.append(tab)
            page = self.toc_pages.get(entry.bookmark)
            link.extend(X.field_runs(f"PAGEREF {entry.bookmark} \\h", str(page) if page else ""))
            p.append(link)
            if k == len(self.toc) - 1:
                end = X.run()
                end.append(X.w("fldChar", fldCharType="end"))
                p.append(end)
            out.append(p)
        return out

    # --- сборка тела -------------------------------------------------------------------------

    def _orientations(self) -> dict[str, str]:
        out: dict[str, str] = {}
        sec_iter = iter(self.ir.sections)
        current = next(sec_iter, None)
        for b in self.blocks:
            out[b.id] = current.orientation if current else "portrait"
            if current is not None and current.end_block == b.id:
                current = next(sec_iter, current)
        return out

    def _sect(self, landscape: bool, footer: str, first: bool) -> etree._Element:
        return sect_pr(self.profile, landscape, self.footer_rids[footer], first)

    def build(self) -> BuildResult:
        self._install_footers()
        self.want_toc = toc_needed(self.ir, self.profile)
        orient = self._orientations()

        title = [b for b in self.blocks if b.type == T.TITLE_PAGE]
        title_els = [self._title_element(b) for b in title]
        self._strip_trailing_page_breaks(title_els)

        content: list[tuple[etree._Element, str]] = []  # (элемент, ориентация)
        for i, b in enumerate(self.blocks):
            t = b.type
            if t in (T.TITLE_PAGE, T.EMPTY, T.SECTION_BREAK, T.TOC):
                continue
            o = orient.get(b.id, "portrait")
            if t == T.PAGE_BREAK:
                nxt = self._next_content(i)
                prev_el = content[-1][0] if content else None
                prev_is_break = (
                    prev_el is not None
                    and prev_el.find(f".//{qn('w:br')}[@{qn('w:type')}='page']") is not None
                    and not dx.paragraph_text(prev_el).strip()
                )
                if nxt is None or self._starts_new_page(nxt) or not content or prev_is_break:
                    self.meta["dropped_page_breaks"] += 1
                    continue
                p = X.paragraph(E.BODY)
                br = X.run()
                br.append(X.w("br", type="page"))
                p.append(br)
                content.append((p, o))
                self.meta["kept_page_breaks"] += 1
                continue
            if t == T.TABLE:
                content.append((self._table(b), o))
                continue
            content.append((self._paragraph(b, i), o))

        body = etree.Element(qn("w:body"))
        first_section = True
        if title_els:
            body.extend(title_els)
            carrier = title_els[-1] if title_els and title_els[-1].tag == qn("w:p") else None
            if carrier is None:
                carrier = X.paragraph()
                body.append(carrier)
            title_landscape = orient.get(title[0].id) == "landscape"
            X.p_ppr(carrier).append(self._sect(title_landscape, FOOTER_EMPTY, True))
            first_section = False
        body.append(X.w("bookmarkStart", id=self.bm_id, name=BM_TITLE_END))
        body.append(X.w("bookmarkEnd", id=self.bm_id))
        self.bm_id += 1
        toc_ps = self._toc_paragraphs()
        body.extend(toc_ps)
        body.append(X.w("bookmarkStart", id=self.bm_id, name=BM_CONTENT))
        body.append(X.w("bookmarkEnd", id=self.bm_id))
        self.bm_id += 1

        sections_out = 1 if title_els else 0
        for k, (el, o) in enumerate(content):
            body.append(el)
            nxt_o = content[k + 1][1] if k + 1 < len(content) else None
            if nxt_o is not None and nxt_o != o:
                carrier = el if el.tag == qn("w:p") else None
                if carrier is None:
                    carrier = X.paragraph()
                    body.append(carrier)
                X.p_ppr(carrier).append(self._sect(o == "landscape", FOOTER_NUM, first_section))
                first_section = False
                sections_out += 1
        last_o = content[-1][1] if content else "portrait"
        body.append(self._sect(last_o == "landscape", FOOTER_NUM, first_section))
        sections_out += 1
        self.meta["sections_out"] = sections_out

        old_body = self.pkg.body
        self.pkg.document.replace(old_body, body)
        self.pkg.set_xml("word/styles.xml", self.new_styles)
        self._restyle_notes()
        self._settings()
        self.meta["toc_entries"] = len(self.toc)
        self.meta["toc_generated"] = bool(self.toc)
        return BuildResult(self.pkg.to_bytes(), list(self.toc), self.meta)

    def _restyle_notes(self) -> None:
        for name in ("word/footnotes.xml", "word/endnotes.xml"):
            if not self.pkg.has(name):
                continue
            root = self.pkg.xml(name)
            for note in list(root):
                if not isinstance(note.tag, str) or wattr(note, "type") in (
                    "separator",
                    "continuationSeparator",
                    "continuationNotice",
                ):
                    continue
                for p in note.iter(qn("w:p")):
                    old = p.find(qn("w:pPr"))
                    ppr = X.w("pPr")
                    X.put(ppr, X.w("pStyle", val=E.FOOTNOTE_TEXT))
                    if old is not None:
                        p.replace(old, ppr)
                    else:
                        p.insert(0, ppr)
                    for r in self._runs_of(p):
                        is_ref = (
                            r.find(qn("w:footnoteRef")) is not None
                            or r.find(qn("w:endnoteRef")) is not None
                        )
                        rpr = r.find(qn("w:rPr"))
                        if rpr is not None:
                            X.strip_rpr_to_semantic(rpr)
                        if is_ref:
                            if rpr is None:
                                rpr = X.w("rPr")
                                r.insert(0, rpr)
                            X.put(rpr, X.w("rStyle", val=E.FOOTNOTE_REF))
                            va = rpr.find(qn("w:vertAlign"))
                            if va is not None:
                                rpr.remove(va)
                        if rpr is not None and not len(rpr):
                            r.remove(rpr)

    def _settings(self) -> None:
        name = "word/settings.xml"
        if not self.pkg.has(name):
            return
        root = self.pkg.xml(name)
        for el in list(root):
            if not isinstance(el.tag, str):
                continue
            if el.tag == qn("w:attachedTemplate"):
                rid = el.get(qn("r:id"))
                root.remove(el)
                if rid:
                    self._remove_settings_rel(rid)
            elif el.tag in (qn("w:autoHyphenation"), qn("w:updateFields")):
                root.remove(el)
        X.put(root, X.w("autoHyphenation", val=1 if self.profile.page.hyphenation else 0))
        if self.toc:
            X.put(root, X.w("updateFields", val="true"))

    def _remove_settings_rel(self, rid: str) -> None:
        name = dx.rels_name("word/settings.xml")
        if not self.pkg.has(name):
            return
        rels = self.pkg.xml(name)
        for rel in list(rels):
            if rel.get("Id") == rid:
                rels.remove(rel)


def build(
    src: Package,
    ir: DocumentIR,
    profile: Profile,
    template: Package,
    toc_pages: dict[str, int] | None = None,
) -> BuildResult:
    b = _Builder(src, ir, profile, template, toc_pages)
    b.want_toc = False
    return b.build()
