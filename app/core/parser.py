"""docx → IR: обход w:body по порядку, эффективное форматирование, run с путями к XML.

Инвариант: `Block.text` абзаца совпадает с `docx_io.paragraph_text` его XML-элемента.
На этом держится проверка целостности: журнал нормализаций оперирует тем же текстом.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from lxml import etree

from app.core import docx_io as dx
from app.core.docx_io import Package, StyleSheet, children, qn, wattr
from app.core.ir import Block, BlockType, DocumentIR, Run, Section

CHARS_PER_PAGE = 1900  # грубая оценка для 12–14 пт, полуторный интервал

_CONTAINERS = {qn("w:hyperlink"), qn("w:fldSimple")}
_UNWRAP = {qn("w:smartTag"), qn("w:customXml")}
_DROP = {
    qn("w:pPr"),
    qn("w:proofErr"),
    qn("w:lastRenderedPageBreak"),
    qn("w:softHyphen"),
    qn("w:smartTagPr"),
    qn("w:customXmlPr"),
}


@dataclass
class _FieldState:
    stack: list[dict[str, Any]] = field(default_factory=list)

    @property
    def in_result(self) -> bool:
        return any(f["phase"] == "result" for f in self.stack)

    @property
    def in_toc(self) -> bool:
        return any(f["instr"].strip().upper().startswith("TOC") for f in self.stack)


class _Parser:
    def __init__(self, pkg: Package) -> None:
        self.pkg = pkg
        self.styles = StyleSheet.from_package(pkg)
        self.numbering = dx.Numbering(pkg.xml_or_none("word/numbering.xml"))
        self.counter = self.numbering.counter()
        self.fields = _FieldState()
        self.blocks: list[Block] = []
        self.sections: list[Section] = []
        self.rendered_breaks = 0
        self.counts = {"p": 0, "t": 0, "s": 0}

    # --- верхний уровень -----------------------------------------------------------

    def run(self) -> DocumentIR:
        body = self.pkg.body
        self._walk(body, "")
        final = body.find(qn("w:sectPr"))
        self.sections.append(self._section(final, None))
        self._estimate_pages()
        meta = {
            "pages_estimate": self._pages_estimate(),
            "rendered_page_breaks": self.rendered_breaks,
            "median_size_pt": self._median_size(),
            "has_toc_field": any(b.features.get("in_toc_field") for b in self.blocks),
            "footer_texts": self._footer_texts(),
            "pdf_like": self._pdf_like(),
        }
        return DocumentIR(blocks=self.blocks, sections=self.sections, meta=meta)

    def _walk(self, parent: etree._Element, prefix: str) -> None:
        for i, el in enumerate(children(parent)):
            path = f"{prefix}{i}"
            tag = el.tag
            if tag == qn("w:p"):
                self._paragraph(el, path)
            elif tag == qn("w:tbl"):
                self._table(el, path)
            elif tag == qn("w:sdt"):
                gallery = el.find(f".//{qn('w:docPartGallery')}")
                content = el.find(qn("w:sdtContent"))
                if gallery is not None and "Table of Contents" in (wattr(gallery, "val") or ""):
                    self._opaque_toc(el, path)
                elif content is not None:
                    idx = children(el).index(content)
                    self._walk(content, f"{path}/{idx}/")
            elif tag == qn("w:customXml"):
                self._walk(el, f"{path}/")

    def _new_id(self, kind: str) -> str:
        self.counts[kind] += 1
        return f"{kind}{self.counts[kind]:04d}"

    # --- абзац -----------------------------------------------------------------------

    def _paragraph(self, p: etree._Element, path: str) -> None:
        ppr = self.styles.effective_ppr(p)
        in_toc_before = self.fields.in_toc
        runs: list[Run] = []
        info: dict[str, Any] = {
            "fields": [],
            "page_break_runs": 0,
            "drawings": 0,
            "math": 0,
            "footnotes": 0,
            "rendered_break": False,
        }
        char_props: list[tuple[int, dx.RunProps]] = []
        self._collect(p, p, path, runs, info, char_props, container=None)
        runs = _merge_text_runs(runs)
        text = "".join(r.text for r in runs)

        label = None
        if ppr.num_id:
            nxt = self.counter.next(ppr.num_id, ppr.ilvl)
            if nxt is not None:
                lab, ordinal, lvl = nxt
                label = {"label": lab, "ordinal": ordinal, "fmt": lvl.fmt}

        sect = p.find(f"{qn('w:pPr')}/{qn('w:sectPr')}")
        f = self._para_features(p, ppr, char_props, text, info, label)
        f["in_toc_field"] = in_toc_before or self.fields.in_toc
        f["section_end"] = sect is not None
        block = Block(id=self._new_id("p"), runs=runs, xml_ref=path, features=f)
        self.blocks.append(block)
        if sect is not None:
            self.sections.append(self._section(sect, block.id))

    def _collect(
        self,
        p: etree._Element,
        parent: etree._Element,
        path: str,
        runs: list[Run],
        info: dict[str, Any],
        char_props: list[tuple[int, dx.RunProps]],
        container: str | None,
    ) -> None:
        for i, el in enumerate(children(parent)):
            cpath = f"{path}/{i}"
            tag = el.tag
            if tag in _DROP:
                if tag == qn("w:lastRenderedPageBreak"):
                    info["rendered_break"] = True
                continue
            if tag == qn("w:r"):
                self._run(p, el, cpath, runs, info, char_props, container)
            elif tag in _CONTAINERS:
                if container is not None:
                    # Вложенные контейнеры переносим целиком как непрозрачный узел.
                    runs.append(Run(kind="other", xml_ref=cpath, text=dx.paragraph_text(el)))
                    continue
                if tag == qn("w:fldSimple"):
                    info["fields"].append((wattr(el, "instr") or "").strip())
                    self.fields.stack.append({"instr": wattr(el, "instr") or "", "phase": "result"})
                    self._collect(p, el, cpath, runs, info, char_props, container=cpath)
                    self.fields.stack.pop()
                else:
                    self._collect(p, el, cpath, runs, info, char_props, container=cpath)
            elif tag in _UNWRAP:
                self._collect(p, el, cpath, runs, info, char_props, container)
            elif tag in (qn("m:oMath"), qn("m:oMathPara")):
                info["math"] += 1
                runs.append(Run(kind="math", xml_ref=cpath, container=container))
            else:
                # bookmarkStart/End, commentRange*, inline sdt и прочее — копируются как есть.
                runs.append(
                    Run(
                        kind="other",
                        xml_ref=cpath,
                        container=container,
                        text=dx.paragraph_text(el) if tag == qn("w:sdt") else "",
                    )
                )

    def _run(
        self,
        p: etree._Element,
        r: etree._Element,
        path: str,
        runs: list[Run],
        info: dict[str, Any],
        char_props: list[tuple[int, dx.RunProps]],
        container: str | None,
    ) -> None:
        sem = self.styles.semantic_rpr(r)
        eff = self.styles.effective_rpr(p, r)
        flags = {
            "bold": sem.bold,
            "italic": sem.italic,
            "underline": sem.underline,
            "strike": sem.strike,
            "superscript": sem.vert_align == "superscript",
            "subscript": sem.vert_align == "subscript",
        }
        for i, el in enumerate(children(r)):
            cpath = f"{path}/{i}"
            tag = el.tag
            in_res = self.fields.in_result
            if tag == qn("w:rPr"):
                continue
            if tag == qn("w:t"):
                t = el.text or ""
                runs.append(Run(text=t, container=container, in_field=in_res, **flags))
                char_props.append((len(t.strip()), eff))
            elif tag == qn("w:tab"):
                runs.append(
                    Run(kind="tab", text="\t", container=container, in_field=in_res, **flags)
                )
            elif tag in (qn("w:br"), qn("w:cr")):
                txt = dx.node_text(el)
                if not txt:
                    info["page_break_runs"] += 1
                runs.append(
                    Run(kind="break", text=txt, xml_ref=cpath, container=container, in_field=in_res)
                )
            elif tag == qn("w:noBreakHyphen"):
                runs.append(Run(text="‑", container=container, in_field=in_res, **flags))
            elif tag == qn("w:fldChar"):
                ftype = wattr(el, "fldCharType")
                if ftype == "begin":
                    self.fields.stack.append({"instr": "", "phase": "instr"})
                elif ftype == "separate" and self.fields.stack:
                    self.fields.stack[-1]["phase"] = "result"
                elif ftype == "end" and self.fields.stack:
                    done = self.fields.stack.pop()
                    info["fields"].append(done["instr"].strip())
                runs.append(Run(kind="field", xml_ref=cpath, container=container, **flags))
            elif tag == qn("w:instrText"):
                if self.fields.stack:
                    self.fields.stack[-1]["instr"] += el.text or ""
                runs.append(Run(kind="field", xml_ref=cpath, container=container, **flags))
            elif tag in (qn("w:footnoteReference"), qn("w:endnoteReference")):
                info["footnotes"] += 1
                runs.append(Run(kind="footnote_ref", xml_ref=cpath, container=container, **flags))
            elif tag in (qn("w:drawing"), qn("w:pict"), qn("w:object"), qn("mc:AlternateContent")):
                info["drawings"] += 1
                runs.append(Run(kind="drawing", xml_ref=cpath, container=container))
            elif tag in (qn("w:lastRenderedPageBreak"),):
                info["rendered_break"] = True
            elif tag in (qn("w:softHyphen"),):
                continue
            else:
                runs.append(
                    Run(
                        kind="other",
                        xml_ref=cpath,
                        container=container,
                        in_field=in_res,
                        text=dx.paragraph_text(el),
                    )
                )

    def _para_features(
        self,
        p: etree._Element,
        ppr: dx.ParaProps,
        char_props: list[tuple[int, dx.RunProps]],
        text: str,
        info: dict[str, Any],
        label: dict[str, Any] | None,
    ) -> dict[str, Any]:
        total = sum(n for n, _ in char_props) or 0
        fonts: Counter[str] = Counter()
        sizes: Counter[float] = Counter()
        bold = italic = caps = 0
        for n, rp in char_props:
            if rp.font:
                fonts[rp.font] += n
            if rp.size_pt:
                sizes[rp.size_pt] += n
            bold += n if rp.bold else 0
            italic += n if rp.italic else 0
            caps += n if rp.caps else 0
        if info["rendered_break"]:
            self.rendered_breaks += 1
        stripped = text.strip()
        letters = [c for c in stripped if c.isalpha()]
        upper_ratio = (sum(c.isupper() for c in letters) / len(letters)) if letters else 0.0
        return {
            "style_id": ppr.style_id,
            "style_name": ppr.style_name,
            "outline_lvl": ppr.outline_lvl,
            "num": label,
            "num_id": ppr.num_id,
            "ilvl": ppr.ilvl,
            "align": ppr.align or "left",
            "ind_first_cm": round(ppr.ind_first / dx.TWIPS_PER_CM, 2),
            "ind_left_cm": round(ppr.ind_left / dx.TWIPS_PER_CM, 2),
            "line": ppr.line,
            "line_exact_pt": (ppr.line_exact_twips / 20) if ppr.line_exact_twips else None,
            "space_before_pt": ppr.space_before / 20,
            "space_after_pt": ppr.space_after / 20,
            "page_break_before": ppr.page_break_before,
            "keep_next": ppr.keep_next,
            "font": fonts.most_common(1)[0][0] if fonts else None,
            "fonts": dict(fonts),
            "size_pt": sizes.most_common(1)[0][0] if sizes else None,
            "sizes": {str(k): v for k, v in sizes.items()},
            "bold_ratio": round(bold / total, 3) if total else 0.0,
            "italic_ratio": round(italic / total, 3) if total else 0.0,
            "caps_ratio": round(caps / total, 3) if total else 0.0,
            "upper_ratio": round(upper_ratio, 3),
            "chars": len(stripped),
            "words": len(stripped.split()),
            "drawings": info["drawings"],
            "math": info["math"],
            "footnotes": info["footnotes"],
            "fields": [f for f in info["fields"] if f],
            "page_break_runs": info["page_break_runs"],
            "rendered_break": info["rendered_break"],
            "leading_ws": len(text) - len(text.lstrip(" \t ")),
            "double_spaces": len(re.findall(r" {2,}", text)),
        }

    # --- таблица и оглавление-контейнер -------------------------------------------------

    def _table(self, tbl: etree._Element, path: str) -> None:
        rows = tbl.findall(qn("w:tr"))
        cols = max((len(r.findall(qn("w:tc"))) for r in rows), default=0)
        texts = [dx.paragraph_text(p) for p in tbl.iter(qn("w:p"))]
        # Нумерация внутри таблиц тоже двигает счётчики Word.
        for p in tbl.iter(qn("w:p")):
            pp = self.styles.effective_ppr(p)
            if pp.num_id:
                self.counter.next(pp.num_id, pp.ilvl)
        first_row = []
        if rows:
            first_row = [dx.paragraph_text(p) for p in rows[0].iter(qn("w:p"))]
        second_row = []
        if len(rows) > 1:
            second_row = [
                "".join(dx.paragraph_text(p) for p in tc.iter(qn("w:p"))).strip()
                for tc in rows[1].findall(qn("w:tc"))
            ]
        f = {
            "rows": len(rows),
            "cols": cols,
            "nested": tbl.find(f".//{qn('w:tc')}//{qn('w:tbl')}") is not None,
            "chars": sum(len(t) for t in texts),
            "drawings": len(tbl.findall(f".//{qn('w:drawing')}")),
            "first_row": " | ".join(t for t in first_row if t.strip())[:200],
            "second_row": second_row,
            "text_preview": " ".join(t for t in texts if t.strip())[:300],
        }
        self.blocks.append(
            Block(
                id=self._new_id("t"),
                type=BlockType.TABLE,
                xml_ref=path,
                features=f,
                confidence=1.0,
                source="rules",
            )
        )

    def _opaque_toc(self, sdt: etree._Element, path: str) -> None:
        text = "\n".join(dx.paragraph_text(p) for p in sdt.iter(qn("w:p")))
        self.blocks.append(
            Block(
                id=self._new_id("s"),
                type=BlockType.TOC,
                xml_ref=path,
                confidence=1.0,
                source="rules",
                features={"sdt_toc": True, "chars": len(text), "text_preview": text[:300]},
            )
        )

    # --- разделы и оценки -----------------------------------------------------------------

    def _section(self, sect: etree._Element | None, end_block: str | None) -> Section:
        if sect is None:
            return Section(end_block=end_block)
        pg = sect.find(qn("w:pgSz"))
        mar = sect.find(qn("w:pgMar"))
        w = dx._int(wattr(pg, "w")) if pg is not None else None
        h = dx._int(wattr(pg, "h")) if pg is not None else None
        orient = wattr(pg, "orient") if pg is not None else None
        landscape = orient == "landscape" or (w is not None and h is not None and w > h)
        margins: dict[str, float] = {}
        if mar is not None:
            for side in ("left", "right", "top", "bottom"):
                v = dx._int(wattr(mar, side))
                if v is not None:
                    margins[side] = round(abs(v) / dx.TWIPS_PER_MM, 1)
        has_num = False
        for ref in sect.findall(qn("w:footerReference")) + sect.findall(qn("w:headerReference")):
            part = self.pkg.part_for_rel(ref.get(qn("r:id"), ""))
            if part and self.pkg.has(part):
                xml = self.pkg.raw(part)
                if b"PAGE" in xml:
                    has_num = True
        return Section(
            end_block=end_block,
            orientation="landscape" if landscape else "portrait",
            width_mm=round(w / dx.TWIPS_PER_MM, 1) if w else None,
            height_mm=round(h / dx.TWIPS_PER_MM, 1) if h else None,
            margins_mm=margins,
            has_page_number=has_num,
            title_pg=sect.find(qn("w:titlePg")) is not None,
        )

    def _pdf_like(self) -> bool:
        """Похоже на конвертацию из PDF: раздел на каждую страницу или слова через табуляцию."""
        paras = [b for b in self.blocks if b.id.startswith("p") and b.features.get("words", 0) > 3]
        tabbed = sum(1 for b in paras if b.text.count("\t") >= 3 and "\t\t" not in b.text)
        many_sections = len(self.sections) >= 5 and len(self.sections) >= len(self.blocks) / 15
        return many_sections or (bool(paras) and tabbed / len(paras) > 0.05)

    def _footer_texts(self) -> list[str]:
        out = []
        for rel in self.pkg.rels().values():
            if rel.type in (dx.REL_FOOTER, dx.REL_HEADER) and not rel.external:
                part = dx.resolve_target(dx.MAIN_PART, rel.target)
                if self.pkg.has(part):
                    root = self.pkg.xml(part)
                    txt = " ".join(dx.paragraph_text(p) for p in root.iter(qn("w:p"))).strip()
                    if txt and not txt.isdigit():
                        out.append(txt[:100])
        return out

    def _estimate_pages(self) -> None:
        use_rendered = self.rendered_breaks >= 2
        page, chars = 1, 0
        for b in self.blocks:
            f = b.features
            if f.get("page_break_before") and chars > 0:
                page += 1
                chars = 0
            if use_rendered and f.get("rendered_break"):
                page += 1
                chars = 0
            b.page_estimate = page
            chars += int(f.get("chars", 0)) + 300 * int(f.get("drawings", 0))
            if b.type == BlockType.TABLE:
                chars += 80 * int(f.get("rows", 0))
            if not use_rendered and chars > CHARS_PER_PAGE:
                page += chars // CHARS_PER_PAGE
                chars %= CHARS_PER_PAGE
            if f.get("page_break_runs") or f.get("section_end"):
                page += 1
                chars = 0

    def _pages_estimate(self) -> int:
        app = self.pkg.xml_or_none("docProps/app.xml")
        if app is not None:
            for el in app.iter():
                if isinstance(el.tag, str) and el.tag.endswith("}Pages") and el.text:
                    try:
                        n = int(el.text)
                    except ValueError:
                        break
                    if n > 0:
                        return n
        return max((b.page_estimate or 1 for b in self.blocks), default=1)

    def _median_size(self) -> float | None:
        sizes: list[float] = []
        for b in self.blocks:
            for k, v in b.features.get("sizes", {}).items():
                sizes.extend([float(k)] * min(int(v), 2000))
        return median(sizes) if sizes else None


def _merge_text_runs(runs: list[Run]) -> list[Run]:
    out: list[Run] = []
    for r in runs:
        prev = out[-1] if out else None
        if (
            prev is not None
            and r.kind == "text"
            and prev.kind == "text"
            and r.xml_ref is None
            and prev.xml_ref is None
            and (
                r.bold,
                r.italic,
                r.underline,
                r.strike,
                r.superscript,
                r.subscript,
                r.container,
                r.in_field,
            )
            == (
                prev.bold,
                prev.italic,
                prev.underline,
                prev.strike,
                prev.superscript,
                prev.subscript,
                prev.container,
                prev.in_field,
            )
        ):
            prev.text += r.text
        else:
            out.append(r)
    return out


def parse(pkg: Package) -> DocumentIR:
    return _Parser(pkg).run()
