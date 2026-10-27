"""Построение элементов WordprocessingML с порядком дочерних узлов по схеме.

Word считает файл повреждённым, если порядок элементов в pPr/rPr/sectPr нарушен,
поэтому все вставки идут через `put()` с таблицами порядка из ECMA-376.
"""

from __future__ import annotations

from lxml import etree

from app.core import docx_io as dx
from app.core.docx_io import qn
from app.core.expect import ParaSpec

PPR_ORDER = [
    "pStyle",
    "keepNext",
    "keepLines",
    "pageBreakBefore",
    "framePr",
    "widowControl",
    "numPr",
    "suppressLineNumbers",
    "pBdr",
    "shd",
    "tabs",
    "suppressAutoHyphens",
    "kinsoku",
    "wordWrap",
    "overflowPunct",
    "topLinePunct",
    "autoSpaceDE",
    "autoSpaceDN",
    "bidi",
    "adjustRightInd",
    "snapToGrid",
    "spacing",
    "ind",
    "contextualSpacing",
    "mirrorIndents",
    "suppressOverlap",
    "jc",
    "textDirection",
    "textAlignment",
    "textboxTightWrap",
    "outlineLvl",
    "divId",
    "cnfStyle",
    "rPr",
    "sectPr",
    "pPrChange",
]
RPR_ORDER = [
    "rStyle",
    "rFonts",
    "b",
    "bCs",
    "i",
    "iCs",
    "caps",
    "smallCaps",
    "strike",
    "dstrike",
    "outline",
    "shadow",
    "emboss",
    "imprint",
    "noProof",
    "snapToGrid",
    "vanish",
    "webHidden",
    "color",
    "spacing",
    "w",
    "kern",
    "position",
    "sz",
    "szCs",
    "highlight",
    "u",
    "effect",
    "bdr",
    "shd",
    "fitText",
    "vertAlign",
    "rtl",
    "cs",
    "em",
    "lang",
    "eastAsianLayout",
    "specVanish",
    "oMath",
]
SECTPR_ORDER = [
    "headerReference",
    "footerReference",
    "footnotePr",
    "endnotePr",
    "type",
    "pgSz",
    "pgMar",
    "paperSrc",
    "pgBorders",
    "lnNumType",
    "pgNumType",
    "cols",
    "formProt",
    "vAlign",
    "noEndnote",
    "titlePg",
    "textDirection",
    "bidi",
    "rtlGutter",
    "docGrid",
    "printerSettings",
]
SETTINGS_ORDER = [
    "writeProtection",
    "view",
    "zoom",
    "removePersonalInformation",
    "removeDateAndTime",
    "doNotDisplayPageBoundaries",
    "displayBackgroundShape",
    "printPostScriptOverText",
    "printFractionalCharacterWidth",
    "printFormsData",
    "embedTrueTypeFonts",
    "embedSystemFonts",
    "saveSubsetFonts",
    "saveFormsData",
    "mirrorMargins",
    "alignBordersAndEdges",
    "bordersDoNotSurroundHeader",
    "bordersDoNotSurroundFooter",
    "gutterAtTop",
    "hideSpellingErrors",
    "hideGrammaticalErrors",
    "activeWritingStyle",
    "proofState",
    "formsDesign",
    "attachedTemplate",
    "linkStyles",
    "stylePaneFormatFilter",
    "stylePaneSortMethod",
    "documentType",
    "mailMerge",
    "revisionView",
    "trackRevisions",
    "doNotTrackMoves",
    "doNotTrackFormatting",
    "documentProtection",
    "autoFormatOverride",
    "styleLockTheme",
    "styleLockQFSet",
    "defaultTabStop",
    "autoHyphenation",
    "consecutiveHyphenLimit",
    "hyphenationZone",
    "doNotHyphenateCaps",
    "showEnvelope",
    "summaryLength",
    "clickAndTypeStyle",
    "defaultTableStyle",
    "evenAndOddHeaders",
    "bookFoldRevPrinting",
    "bookFoldPrinting",
    "bookFoldPrintingSheets",
    "drawingGridHorizontalSpacing",
    "drawingGridVerticalSpacing",
    "displayHorizontalDrawingGridEvery",
    "displayVerticalDrawingGridEvery",
    "doNotUseMarginsForDrawingGridOrigin",
    "drawingGridHorizontalOrigin",
    "drawingGridVerticalOrigin",
    "doNotShadeFormData",
    "noPunctuationKerning",
    "characterSpacingControl",
    "printTwoOnOne",
    "strictFirstAndLastChars",
    "noLineBreaksAfter",
    "noLineBreaksBefore",
    "savePreviewPicture",
    "doNotValidateAgainstSchema",
    "saveInvalidXml",
    "ignoreMixedContent",
    "alwaysShowPlaceholderText",
    "doNotDemarcateInvalidXml",
    "saveXmlDataOnly",
    "useXSLTWhenSaving",
    "saveThroughXslt",
    "showXMLTags",
    "alwaysMergeEmptyNamespace",
    "updateFields",
    "hdrShapeDefaults",
    "footnotePr",
    "endnotePr",
    "compat",
    "docVars",
    "rsids",
    "mathPr",
    "attachedSchema",
    "themeFontLang",
    "clrSchemeMapping",
    "doNotIncludeSubdocsInStats",
    "doNotAutoCompressPictures",
    "forceUpgrade",
    "captions",
    "readModeInkLockDown",
    "smartTagType",
    "schemaLibrary",
    "shapeDefaults",
    "doNotEmbedSmartTags",
    "decimalSymbol",
    "listSeparator",
]
LVL_ORDER = [
    "start",
    "numFmt",
    "lvlRestart",
    "pStyle",
    "isLgl",
    "suff",
    "lvlText",
    "lvlPicBulletId",
    "legacy",
    "lvlJc",
    "pPr",
    "rPr",
]
_ORDERS = {
    "pPr": PPR_ORDER,
    "rPr": RPR_ORDER,
    "sectPr": SECTPR_ORDER,
    "settings": SETTINGS_ORDER,
    "lvl": LVL_ORDER,
}
SEMANTIC_RPR = {"b", "bCs", "i", "iCs", "strike", "u", "vertAlign", "rStyle"}

JC = {"justify": "both", "left": "left", "center": "center", "right": "right"}


def w(tag: str, **attrs: str | int | float) -> etree._Element:
    el = etree.Element(qn(f"w:{tag}"))
    for k, v in attrs.items():
        el.set(qn(f"w:{k}"), str(v))
    return el


def put(parent: etree._Element, child: etree._Element) -> etree._Element:
    """Вставляет child на место по схеме (заменяя существующий того же имени)."""
    order = _ORDERS.get(dx.local(parent))
    name = dx.local(child)
    existing = parent.find(child.tag)
    if existing is not None and name not in ("headerReference", "footerReference"):
        parent.replace(existing, child)
        return child
    if order is None or name not in order:
        parent.append(child)
        return child
    idx = order.index(name)
    for i, c in enumerate(parent):
        if (
            isinstance(c.tag, str)
            and c.tag.startswith(f"{{{dx.W_NS}}}")
            and dx.local(c) in order
            and order.index(dx.local(c)) > idx
        ):
            parent.insert(i, child)
            return child
    parent.append(child)
    return child


def ensure(parent: etree._Element, tag: str) -> etree._Element:
    el = parent.find(qn(f"w:{tag}"))
    if el is None:
        el = put(parent, w(tag))
    return el


def on(tag: str, value: bool) -> etree._Element:
    return w(tag) if value else w(tag, val=0)


def twips(cm: float) -> int:
    return dx.cm_to_twips(cm)


# --- стили -------------------------------------------------------------------------------


def ppr_for(spec: ParaSpec) -> etree._Element:
    ppr = w("pPr")
    put(ppr, on("keepNext", spec.keep_next))
    put(ppr, on("pageBreakBefore", spec.page_break_before))
    put(ppr, on("widowControl", spec.widow))
    if spec.tabs:
        tabs = w("tabs")
        for kind, pos in spec.tabs:
            if kind == "right_dot":
                tabs.append(w("tab", val="right", leader="dot", pos=twips(pos)))
            else:
                tabs.append(w("tab", val=kind, pos=twips(pos)))
        put(ppr, tabs)
    put(ppr, on("suppressAutoHyphens", spec.no_hyphen))
    put(
        ppr,
        w(
            "spacing",
            before=dx.pt_to_twips(spec.before_pt),
            after=dx.pt_to_twips(spec.after_pt),
            line=round(spec.line * 240),
            lineRule="auto",
        ),
    )
    ind = w("ind", left=twips(spec.left_cm), right=0)
    if spec.first_cm < 0:
        ind.set(qn("w:hanging"), str(twips(-spec.first_cm)))
    else:
        ind.set(qn("w:firstLine"), str(twips(spec.first_cm)))
    put(ppr, ind)
    put(ppr, w("contextualSpacing", val=0))
    put(ppr, w("jc", val=JC[spec.align]))
    put(ppr, w("outlineLvl", val=9 if spec.outline_lvl is None else spec.outline_lvl))
    return ppr


def rpr_font(
    font: str, size_pt: float, bold: bool = False, caps: bool = False, italic: bool = False
) -> etree._Element:
    rpr = w("rPr")
    put(rpr, w("rFonts", ascii=font, hAnsi=font, eastAsia=font, cs=font))
    put(rpr, on("b", bold))
    put(rpr, on("bCs", bold))
    put(rpr, on("i", italic))
    put(rpr, on("iCs", italic))
    put(rpr, on("caps", caps))
    put(rpr, w("color", val="000000"))
    put(rpr, w("sz", val=round(size_pt * 2)))
    put(rpr, w("szCs", val=round(size_pt * 2)))
    return rpr


def paragraph_style(spec: ParaSpec, based_on: str = "Normal") -> etree._Element:
    st = w("style", type="paragraph", styleId=spec.style_id)
    st.set(qn("w:customStyle"), "1")
    st.append(w("name", val=spec.name))
    st.append(w("basedOn", val=based_on))
    st.append(w("qFormat"))
    st.append(ppr_for(spec))
    st.append(rpr_font(spec.font, spec.size_pt, spec.bold, spec.caps))
    return st


# --- абзацы и run ----------------------------------------------------------------------------


def paragraph(style_id: str | None = None) -> etree._Element:
    p = w("p")
    if style_id:
        ppr = w("pPr")
        put(ppr, w("pStyle", val=style_id))
        p.append(ppr)
    return p


def p_ppr(p: etree._Element) -> etree._Element:
    ppr = p.find(qn("w:pPr"))
    if ppr is None:
        ppr = w("pPr")
        p.insert(0, ppr)
    return ppr


def semantic_rpr(
    *,
    bold: bool = False,
    italic: bool = False,
    underline: bool = False,
    strike: bool = False,
    superscript: bool = False,
    subscript: bool = False,
    rstyle: str | None = None,
) -> etree._Element | None:
    rpr = w("rPr")
    if rstyle:
        put(rpr, w("rStyle", val=rstyle))
    if bold:
        put(rpr, w("b"))
        put(rpr, w("bCs"))
    if italic:
        put(rpr, w("i"))
        put(rpr, w("iCs"))
    if strike:
        put(rpr, w("strike"))
    if underline:
        put(rpr, w("u", val="single"))
    if superscript:
        put(rpr, w("vertAlign", val="superscript"))
    elif subscript:
        put(rpr, w("vertAlign", val="subscript"))
    return rpr if len(rpr) else None


def run(text: str | None = None, rpr: etree._Element | None = None) -> etree._Element:
    r = w("r")
    if rpr is not None:
        r.append(rpr)
    if text is not None:
        r.append(text_el(text))
    return r


def text_el(text: str) -> etree._Element:
    t = w("t")
    t.text = text
    if text != text.strip() or "  " in text:
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return t


def field_runs(instr: str, result: str, rpr: etree._Element | None = None) -> list[etree._Element]:
    from copy import deepcopy

    def r_(child: etree._Element) -> etree._Element:
        rr = w("r")
        if rpr is not None:
            rr.append(deepcopy(rpr))
        rr.append(child)
        return rr

    instr_el = w("instrText")
    instr_el.text = f" {instr} "
    instr_el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return [
        r_(w("fldChar", fldCharType="begin")),
        r_(instr_el),
        r_(w("fldChar", fldCharType="separate")),
        r_(text_el(result)),
        r_(w("fldChar", fldCharType="end")),
    ]


def strip_rpr_to_semantic(rpr: etree._Element, keep_style: set[str] | None = None) -> None:
    """Оставляет в rPr только семантику (жирный, курсив, подчёркивание, индексы)."""
    for c in list(rpr):
        if not isinstance(c.tag, str):
            continue
        name = dx.local(c)
        if name not in SEMANTIC_RPR or (
            name == "rStyle" and (keep_style is None or dx.wattr(c, "val") not in keep_style)
        ):
            rpr.remove(c)
