"""Генерация template.docx из profile.yaml (`cli build-template <profile>`).

Шаблон содержит полный набор стилей Gost*, колонтитулы (с номером страницы и пустой)
и образец каждого стиля в теле — чтобы владелец мог открыть его в Word и посмотреть.
"""

from __future__ import annotations

from lxml import etree

from app.core import docx_io as dx
from app.core import expect as E
from app.core import ooxml as X
from app.core.docx_io import Package, qn
from app.core.profile import Profile

FOOTER_NUM = "word/footerGostNum.xml"
FOOTER_EMPTY = "word/footerGostEmpty.xml"

_W = dx.NS["w"]
_R = dx.NS["r"]

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="{main}"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
<Override PartName="/{fnum}" ContentType="{footer}"/>
<Override PartName="/{fempty}" ContentType="{footer}"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>
<Relationship Id="rId3" Type="{footer}" Target="{fnum}"/>
<Relationship Id="rId4" Type="{footer}" Target="{fempty}"/>
</Relationships>"""


def styles_xml(profile: Profile) -> bytes:
    from lxml import etree

    b = profile.body
    root = etree.Element(qn("w:styles"), nsmap={"w": _W})
    dd = X.w("docDefaults")
    rd = X.w("rPrDefault")
    rpr = X.rpr_font(b.font, b.size_pt)
    X.put(rpr, X.w("lang", val="ru-RU", eastAsia="ru-RU", bidi="ar-SA"))
    rd.append(rpr)
    pd = X.w("pPrDefault")
    ppr = X.w("pPr")
    X.put(ppr, X.w("spacing", before=0, after=0, line=240, lineRule="auto"))
    pd.append(ppr)
    dd.extend([rd, pd])
    root.append(dd)

    normal = X.w("style", type="paragraph", default=1, styleId="Normal")
    normal.append(X.w("name", val="Normal"))
    normal.append(X.w("qFormat"))
    root.append(normal)
    dpf = X.w("style", type="character", default=1, styleId="DefaultParagraphFont")
    dpf.append(X.w("name", val="Default Paragraph Font"))
    dpf.append(X.w("uiPriority", val=1))
    dpf.append(X.w("semiHidden"))
    root.append(dpf)
    tn = X.w("style", type="table", default=1, styleId="TableNormal")
    tn.append(X.w("name", val="Normal Table"))
    tn.append(X.w("semiHidden"))
    tblpr = X.w("tblPr")
    tblpr.append(X.w("tblInd", w=0, type="dxa"))
    mar = X.w("tblCellMar")
    for side, v in (("top", 0), ("left", 108), ("bottom", 0), ("right", 108)):
        mar.append(X.w(side, w=v, type="dxa"))
    tblpr.append(mar)
    tn.append(tblpr)
    root.append(tn)
    grid = X.w("style", type="table", styleId="GostTableGrid")
    grid.append(X.w("name", val="GostTableGrid"))
    grid.append(X.w("basedOn", val="TableNormal"))
    gp = X.w("tblPr")
    borders = X.w("tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        borders.append(X.w(side, val="single", sz=4, space=0, color="000000"))
    gp.append(borders)
    grid.append(gp)
    root.append(grid)

    for spec in E.para_specs(profile).values():
        root.append(X.paragraph_style(spec))

    fref = X.w("style", type="character", styleId=E.FOOTNOTE_REF)
    fref.append(X.w("name", val="footnote reference"))
    frpr = X.w("rPr")
    X.put(frpr, X.w("vertAlign", val="superscript"))
    fref.append(frpr)
    root.append(fref)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def footer_xml(with_number: bool) -> bytes:
    from lxml import etree

    root = etree.Element(qn("w:ftr"), nsmap={"w": _W, "r": _R})
    p = X.paragraph(E.FOOTER)
    if with_number:
        for r in X.field_runs("PAGE", "1"):
            p.append(r)
    root.append(p)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def settings_xml(profile: Profile) -> bytes:
    hyph = "1" if profile.page.hyphenation else "0"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:settings xmlns:w="{_W}"><w:autoHyphenation w:val="{hyph}"/>'
        '<w:defaultTabStop w:val="709"/><w:characterSpacingControl w:val="doNotCompress"/>'
        '<w:compat><w:compatSetting w:name="compatibilityMode" '
        'w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat>'
        "</w:settings>"
    ).encode()


def section_xml(
    profile: Profile,
    landscape: bool = False,
    footer_rid: str | None = "rId3",
    first_section: bool = True,
) -> bytes:
    from lxml import etree

    return etree.tostring(sect_pr(profile, landscape, footer_rid, first_section))


def sect_pr(
    profile: Profile, landscape: bool, footer_rid: str | None, restart_numbering: bool
) -> etree._Element:
    m = profile.page.landscape_margins_mm if landscape else profile.page.margins_mm
    sp = X.w("sectPr")
    if footer_rid:
        ref = X.w("footerReference", type="default")
        ref.set(qn("r:id"), footer_rid)
        sp.append(ref)
    X.put(sp, X.w("type", val="nextPage"))
    wdt, hgt = (16838, 11906) if landscape else (11906, 16838)
    pg = X.w("pgSz", w=wdt, h=hgt)
    if landscape:
        pg.set(qn("w:orient"), "landscape")
    X.put(sp, pg)
    X.put(
        sp,
        X.w(
            "pgMar",
            top=dx.mm_to_twips(m.top),
            right=dx.mm_to_twips(m.right),
            bottom=dx.mm_to_twips(m.bottom),
            left=dx.mm_to_twips(m.left),
            header=709,
            footer=709,
            gutter=0,
        ),
    )
    if restart_numbering:
        X.put(sp, X.w("pgNumType", start=profile.page.numbering.start_from))
    X.put(sp, X.w("cols", space=708))
    return sp


def document_xml(profile: Profile) -> bytes:
    from lxml import etree

    root = etree.Element(qn("w:document"), nsmap={"w": _W, "r": _R})
    body = etree.SubElement(root, qn("w:body"))
    samples = [
        (E.STRUCT, "ВВЕДЕНИЕ"),
        (E.BODY, "Образец основного текста. " * 6),
        (E.HEADING[1], "1 Заголовок раздела"),
        (E.HEADING[2], "1.1 Заголовок подраздела"),
        (E.HEADING[3], "1.1.1 Заголовок пункта"),
        (E.BODY, "Текст подраздела. " * 8),
        (E.LIST_BULLET, "элемент перечисления"),
        (
            E.FIGURE_CAPTION,
            profile.figures.caption.template.format(num=1, title="Название рисунка"),
        ),
        (E.TABLE_CAPTION, profile.tables.caption.template.format(num=1, title="Название таблицы")),
        (E.TABLE_TEXT, "Текст в таблице"),
        (E.BIB_ITEM, "1. Иванов И. И. Образец источника."),
        (E.APPENDIX, profile.appendix.heading_template.format(letter="А")),
        (E.APPENDIX_TITLE, "Название приложения"),
    ]
    for sid, text in samples:
        p = X.paragraph(sid)
        p.append(X.run(text.strip()))
        body.append(p)
    body.append(sect_pr(profile, False, "rId3", True))
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def build_template(profile: Profile) -> bytes:
    footer_ct = dx.CT_FOOTER
    fnum, fempty = FOOTER_NUM.removeprefix("word/"), FOOTER_EMPTY.removeprefix("word/")
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES.format(
            main=dx.CT_MAIN, footer=footer_ct, fnum=FOOTER_NUM, fempty=FOOTER_EMPTY
        ).encode(),
        "_rels/.rels": _ROOT_RELS.encode(),
        "word/_rels/document.xml.rels": _DOC_RELS.format(
            footer=dx.REL_FOOTER, fnum=fnum, fempty=fempty
        ).encode(),
        "word/document.xml": document_xml(profile),
        "word/styles.xml": styles_xml(profile),
        "word/settings.xml": settings_xml(profile),
        FOOTER_NUM: footer_xml(True),
        FOOTER_EMPTY: footer_xml(False),
    }
    return Package(parts, list(parts)).to_bytes()
