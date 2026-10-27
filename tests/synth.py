"""Генератор синтетических .docx для тестов (spec 11.4).

Собирает пакет из сырого XML: так тесты контролируют каждую деталь, включая сноски,
гиперссылки, OMML, рисунки, альбомные разделы и вложенные таблицы.
"""

from __future__ import annotations

import base64
import io
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
NS = f'xmlns:w="{W}" xmlns:r="{R}" xmlns:m="{M}" xmlns:wp="{WP}" xmlns:a="{A}" xmlns:pic="{PIC}"'

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles {NS}>
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/>
<w:sz w:val="22"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="259" w:lineRule="auto"/></w:pPr>
</w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
<w:pPr><w:keepNext/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="2"><w:name w:val="heading 2"/><w:basedOn w:val="1"/>
<w:pPr><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:sz w:val="28"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="TNR"><w:name w:val="Мой текст"/><w:basedOn w:val="Normal"/>
<w:pPr><w:ind w:firstLine="709"/><w:jc w:val="both"/></w:pPr>
<w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman"/><w:sz w:val="28"/></w:rPr></w:style>
<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont">
<w:name w:val="Default Paragraph Font"/></w:style>
<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/>
<w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4"/><w:bottom w:val="single" w:sz="4"/>
</w:tblBorders></w:tblPr></w:style>
</w:styles>"""

NUMBERING = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering {NS}>
<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/>
<w:numFmt w:val="bullet"/><w:lvlText w:val="•"/></w:lvl></w:abstractNum>
<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/>
<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
<w:num w:numId="3"><w:abstractNumId w:val="1"/>
<w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>
</w:numbering>"""

FOOTNOTES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:footnotes {NS}>
<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>
<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r>
</w:p></w:footnote>
<w:footnote w:id="1"><w:p><w:pPr><w:pStyle w:val="Normal"/></w:pPr><w:r><w:rPr>
<w:vertAlign w:val="superscript"/></w:rPr><w:footnoteRef/></w:r><w:r>
<w:t xml:space="preserve"> Текст сноски.</w:t></w:r></w:p></w:footnote>
</w:footnotes>"""


def t(text: str) -> str:
    return f'<w:t xml:space="preserve">{escape(text)}</w:t>'


def run(
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    size: int | None = None,
    font: str | None = None,
    color: str | None = None,
) -> str:
    props = ""
    if font:
        props += f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}"/>'
    if bold:
        props += "<w:b/>"
    if italic:
        props += "<w:i/>"
    if color:
        props += f'<w:color w:val="{color}"/>'
    if size:
        props += f'<w:sz w:val="{size * 2}"/>'
    rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
    return f"<w:r>{rpr}{t(text)}</w:r>"


def p(
    *runs: str,
    style: str | None = None,
    jc: str | None = None,
    num: tuple[int, int] | None = None,
    page_break_before: bool = False,
    sect: str = "",
    ind_first: int | None = None,
) -> str:
    ppr = ""
    if style:
        ppr += f'<w:pStyle w:val="{style}"/>'
    if page_break_before:
        ppr += "<w:pageBreakBefore/>"
    if num:
        ppr += f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>'
    if ind_first is not None:
        ppr += f'<w:ind w:firstLine="{ind_first}"/>'
    if jc:
        ppr += f'<w:jc w:val="{jc}"/>'
    ppr += sect
    pp = f"<w:pPr>{ppr}</w:pPr>" if ppr else ""
    return f"<w:p>{pp}{''.join(runs)}</w:p>"


def tp(text: str, **kw: object) -> str:
    """Абзац из одного run с текстом."""
    style = kw.pop("style", None)
    jc = kw.pop("jc", None)
    return p(run(text, **kw), style=style, jc=jc)  # type: ignore[arg-type]


def page_break() -> str:
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def sect(
    landscape: bool = False, margins: tuple[int, int, int, int] = (1134, 850, 1134, 1701)
) -> str:
    w, h = (16838, 11906) if landscape else (11906, 16838)
    orient = ' w:orient="landscape"' if landscape else ""
    top, right, bottom, left = margins
    return (
        f'<w:sectPr><w:pgSz w:w="{w}" w:h="{h}"{orient}/><w:pgMar w:top="{top}" '
        f'w:right="{right}" w:bottom="{bottom}" w:left="{left}" w:header="708" '
        f'w:footer="708" w:gutter="0"/></w:sectPr>'
    )


def drawing(rid: str = "rIdImg1") -> str:
    return (
        '<w:r><w:drawing><wp:inline><wp:extent cx="914400" cy="914400"/>'
        '<wp:docPr id="1" name="Рисунок 1"/><a:graphic><a:graphicData '
        'uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic>'
        '<pic:nvPicPr><pic:cNvPr id="1" name="img.png"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch>'
        '</pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="914400" '
        'cy="914400"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>"
    )


def omml(text: str = "x=1") -> str:
    return f"<m:oMath><m:r><m:t>{escape(text)}</m:t></m:r></m:oMath>"


def hyperlink(text: str, rid: str = "rIdLink1") -> str:
    return f'<w:hyperlink r:id="{rid}">{run(text)}</w:hyperlink>'


def footnote_ref(fid: int = 1) -> str:
    return (
        f'<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
        f'<w:footnoteReference w:id="{fid}"/></w:r>'
    )


def word_field(instr: str, result: str) -> str:
    return (
        f'<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve">'
        f' {escape(instr)} </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/>'
        f'</w:r>{run(result)}<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )


def table(rows: list[list[str]], style: str | None = "TableGrid", nested: str = "") -> str:
    tblpr = (
        f'<w:tblPr><w:tblStyle w:val="{style}"/><w:tblW w:w="9000" w:type="dxa"/>'
        f'<w:tblInd w:w="300" w:type="dxa"/></w:tblPr>'
        if style
        else "<w:tblPr/>"
    )
    cols = max(len(r) for r in rows)
    grid = "<w:tblGrid>" + "".join('<w:gridCol w:w="2000"/>' for _ in range(cols)) + "</w:tblGrid>"
    body = ""
    for i, r in enumerate(rows):
        cells = ""
        for j, c in enumerate(r):
            inner = nested if (nested and i == len(rows) - 1 and j == 0) else ""
            cells += (
                f'<w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr>'
                f"{inner}{p(run(c, font='Arial', size=9))}</w:tc>"
            )
        body += f"<w:tr>{cells}</w:tr>"
    return f"<w:tbl>{tblpr}{grid}{body}</w:tbl>"


@dataclass
class Doc:
    body: list[str] = field(default_factory=list)
    final_sect: str = field(default_factory=sect)
    footnotes: bool = False
    image: bool = False
    link: bool = False

    def add(self, *xml: str) -> Doc:
        self.body.extend(xml)
        return self

    def build(self) -> bytes:
        rels = [
            '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/styles" Target="styles.xml"/>',
            '<Relationship Id="rIdNum" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/numbering" Target="numbering.xml"/>',
            '<Relationship Id="rIdSettings" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/settings" Target="settings.xml"/>',
        ]
        overrides = [
            (
                "/word/document.xml",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
            ),
            (
                "/word/styles.xml",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml",
            ),
            (
                "/word/numbering.xml",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml",
            ),
            (
                "/word/settings.xml",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml",
            ),
        ]
        parts: dict[str, bytes] = {}
        if self.footnotes:
            rels.append(
                '<Relationship Id="rIdFn" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>'
            )
            overrides.append(
                (
                    "/word/footnotes.xml",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
                )
            )
            parts["word/footnotes.xml"] = FOOTNOTES.encode()
        if self.image:
            rels.append(
                '<Relationship Id="rIdImg1" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/image" Target="media/img.png"/>'
            )
            parts["word/media/img.png"] = PNG_1PX
        if self.link:
            rels.append(
                '<Relationship Id="rIdLink1" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/hyperlink" '
                'Target="https://example.org" TargetMode="External"/>'
            )
        ct = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://'
            'schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" '
            'ContentType="image/png"/>'
            + "".join(f'<Override PartName="{n}" ContentType="{c}"/>' for n, c in overrides)
            + "</Types>"
        )
        doc = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {NS}>'
            f"<w:body>{''.join(self.body)}{self.final_sect}</w:body></w:document>"
        )
        parts.update(
            {
                "[Content_Types].xml": ct.encode(),
                "_rels/.rels": (
                    b'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://'
                    b'schemas.openxmlformats.org/package/2006/relationships"><Relationship '
                    b'Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006'
                    b'/relationships/officeDocument" Target="word/document.xml"/>'
                    b"</Relationships>"
                ),
                "word/_rels/document.xml.rels": (
                    '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.'
                    'openxmlformats.org/package/2006/relationships">'
                    + "".join(rels)
                    + "</Relationships>"
                ).encode(),
                "word/document.xml": doc.encode(),
                "word/styles.xml": STYLES.encode(),
                "word/numbering.xml": NUMBERING.encode(),
                "word/settings.xml": (
                    f'<?xml version="1.0" encoding="UTF-8"?><w:settings {NS}>'
                    '<w:zoom w:percent="100"/><w:defaultTabStop w:val="708"/>'
                    "<w:compat/></w:settings>"
                ).encode(),
            }
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in parts.items():
                z.writestr(name, data)
        return buf.getvalue()


def title_page() -> list[str]:
    return [
        tp("МИНИСТЕРСТВО НАУКИ И ВЫСШЕГО ОБРАЗОВАНИЯ РОССИЙСКОЙ ФЕДЕРАЦИИ", jc="center"),
        tp("Национальный исследовательский технологический университет «МИСИС»", jc="center"),
        tp("Кафедра бизнес-информатики", jc="center"),
        tp("КУРСОВАЯ РАБОТА", jc="center", bold=True),
        tp("Выполнил: студент группы ББИ-23-4 Иванов И. И.", jc="right"),
        tp("Москва 2025", jc="center"),
        page_break(),
    ]


def crooked_document() -> bytes:
    """«Кривой» документ: все типичные ошибки студентов разом."""
    d = Doc(footnotes=True, image=True, link=True)
    d.add(*title_page())
    d.add(
        tp("Введение", style="1", jc="center"),
        p(
            run(
                "    Текст введения  с двойными  пробелами и отступом пробелами.",
                font="Arial",
                size=11,
            )
        ),
        tp(
            "",
        ),
        tp(""),
        p(run("1.  Теоретическая часть.", bold=True, size=16)),
        p(
            run("Обычный абзац со сноской"),
            footnote_ref(1),
            run(" и ссылкой "),
            hyperlink("на сайт"),
            run("."),
        ),
        p(run("1.1. Подраздел первый", bold=True)),
        p(run("Первый пункт списка;"), num=(1, 0)),
        p(run("Второй пункт списка."), num=(1, 0)),
        tp("- текстовый маркер первый,"),
        tp("- текстовый маркер второй."),
        p(drawing(), jc="center"),
        tp("Рис. 1. Схема процесса"),
        p(run("На рисунке 1 показана схема, в таблице 1 — данные, формула (1) ниже."), style="TNR"),
        tp("Таблица 1. Исходные данные"),
        table([["Показатель", "Значение"], ["1", "2"], ["Выручка", "100"]]),
        p(run("\t"), omml("E=mc^2"), run("\t(1)")),
        p(run("где E – энергия."), style="TNR"),
        p(run("Поле: "), word_field("PAGE", "7"), run(" страница.")),
        p(run("Абзац перед альбомным разделом."), sect=sect()),
        p(run("Широкая таблица на альбомном листе:")),
        table(
            [["A", "B", "C"], ["1", "2", "3"]], nested=table([["вложенная", "таблица"]], style=None)
        ),
        p(run("Конец альбомного раздела."), sect=sect(landscape=True)),
        tp("Заключение", bold=True, jc="center"),
        tp("Текст заключения без отступа и с лишними   пробелами."),
        tp("Список литературы.", bold=True),
        p(run("Иванов И. И. Книга. — М., 2020."), num=(2, 0)),
        p(run("Петров П. П. Статья // Журнал. — 2021."), num=(2, 0)),
        tp("Приложение А", bold=True, jc="center"),
        tp("Код программы", jc="center", bold=True),
        tp("print('hello')"),
    )
    return d.build()
