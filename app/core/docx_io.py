"""Чтение пакета .docx, безопасный XML, разрешение стилей и нумерации.

Здесь нет знаний о профилях и типах блоков: только «что написано в файле»
и «как Word это покажет» (эффективное форматирование с учётом наследования).
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any

from lxml import etree

# --- Пространства имён ---------------------------------------------------------------

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "v": "urn:schemas-microsoft-com:vml",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
}
W_NS = NS["w"]

REL_FOOTER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer"
REL_HEADER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header"
REL_NUMBERING = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering"
REL_ATTACHED_TEMPLATE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate"
)
CT_FOOTER = "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
CT_NUMBERING = "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"
CT_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"

MAIN_PART = "word/document.xml"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def qn(tag: str) -> str:
    """`w:p` → `{ns}p`."""
    prefix, local = tag.split(":")
    return f"{{{NS[prefix]}}}{local}"


def local(el: etree._Element) -> str:
    tag = el.tag
    return tag.split("}", 1)[1] if isinstance(tag, str) and "}" in tag else str(tag)


def wattr(el: etree._Element | None, name: str) -> str | None:
    if el is None:
        return None
    return el.get(qn(f"w:{name}"))


def children(el: etree._Element) -> list[etree._Element]:
    """Дочерние элементы без комментариев и processing instructions."""
    return [c for c in el if isinstance(c.tag, str)]


def safe_parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False, no_network=True, huge_tree=True, remove_blank_text=False
    )


def parse_xml(data: bytes) -> etree._Element:
    return etree.fromstring(data, safe_parser())


def to_xml(el: etree._Element) -> bytes:
    return etree.tostring(el, xml_declaration=True, encoding="UTF-8", standalone=True)


# --- Ошибки пакета -------------------------------------------------------------------


class PackageError(Exception):
    """Файл нельзя обрабатывать. `code` показывается пользователю через texts.py."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


# --- Пакет ---------------------------------------------------------------------------


@dataclass
class Rel:
    id: str
    type: str
    target: str
    external: bool


class Package:
    """Содержимое zip в памяти. Изменённые XML-части сериализуются при сохранении."""

    def __init__(self, parts: dict[str, bytes], order: list[str]) -> None:
        self._parts = parts
        self._order = order
        self._xml: dict[str, etree._Element] = {}

    # чтение
    def names(self) -> list[str]:
        return list(self._order)

    def has(self, name: str) -> bool:
        return name in self._parts or name in self._xml

    def raw(self, name: str) -> bytes:
        if name in self._xml:
            return to_xml(self._xml[name])
        return self._parts[name]

    def xml(self, name: str) -> etree._Element:
        if name not in self._xml:
            self._xml[name] = parse_xml(self._parts[name])
        return self._xml[name]

    def xml_or_none(self, name: str) -> etree._Element | None:
        return self.xml(name) if self.has(name) else None

    @property
    def document(self) -> etree._Element:
        return self.xml(MAIN_PART)

    @property
    def body(self) -> etree._Element:
        body = self.document.find(qn("w:body"))
        if body is None:
            raise PackageError("corrupted", "document.xml has no body")
        return body

    def rels(self, part: str = MAIN_PART) -> dict[str, Rel]:
        name = rels_name(part)
        if not self.has(name):
            return {}
        out: dict[str, Rel] = {}
        for r in self.xml(name):
            if not isinstance(r.tag, str):
                continue
            out[r.get("Id", "")] = Rel(
                r.get("Id", ""),
                r.get("Type", ""),
                r.get("Target", ""),
                r.get("TargetMode") == "External",
            )
        return out

    def part_for_rel(self, rel_id: str, part: str = MAIN_PART) -> str | None:
        rel = self.rels(part).get(rel_id)
        if rel is None or rel.external:
            return None
        return resolve_target(part, rel.target)

    # запись
    def set_xml(self, name: str, root: etree._Element) -> None:
        self._xml[name] = root
        if name not in self._order:
            self._order.append(name)

    def set_raw(self, name: str, data: bytes) -> None:
        self._xml.pop(name, None)
        self._parts[name] = data
        if name not in self._order:
            self._order.append(name)

    def remove(self, name: str) -> None:
        self._xml.pop(name, None)
        self._parts.pop(name, None)
        if name in self._order:
            self._order.remove(name)

    def copy(self) -> Package:
        pkg = Package(dict(self._parts), list(self._order))
        pkg._xml = {k: deepcopy(v) for k, v in self._xml.items()}
        return pkg

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name in self._order:
                z.writestr(name, self.raw(name))
        return buf.getvalue()

    # служебное
    def add_rel(self, part: str, rel_type: str, target: str) -> str:
        name = rels_name(part)
        if not self.has(name):
            root = etree.Element(qn("pr:Relationships"), nsmap={None: NS["pr"]})  # type: ignore[dict-item]
            self.set_xml(name, root)
        root = self.xml(name)
        used = {r.get("Id") for r in root}
        n = 1
        while f"rIdG{n}" in used:
            n += 1
        rid = f"rIdG{n}"
        etree.SubElement(root, qn("pr:Relationship"), Id=rid, Type=rel_type, Target=target)
        return rid

    def ensure_content_type(self, part_name: str, content_type: str) -> None:
        root = self.xml("[Content_Types].xml")
        pn = "/" + part_name
        for o in root.iter(qn("ct:Override")):
            if o.get("PartName") == pn:
                o.set("ContentType", content_type)
                return
        etree.SubElement(root, qn("ct:Override"), PartName=pn, ContentType=content_type)


def rels_name(part: str) -> str:
    folder, _, fname = part.rpartition("/")
    return f"{folder}/_rels/{fname}.rels" if folder else f"_rels/{fname}.rels"


def resolve_target(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    folder = source_part.rpartition("/")[0]
    parts = (folder.split("/") if folder else []) + target.split("/")
    out: list[str] = []
    for p in parts:
        if p == "..":
            if out:
                out.pop()
        elif p and p != ".":
            out.append(p)
    return "/".join(out)


def read_package(data: bytes, max_unzipped_bytes: int = 200 * 1024 * 1024) -> Package:
    """Проверяет и читает .docx. Отказ — `PackageError` с кодом для пользователя."""
    if data[:8] == OLE_MAGIC:
        # Старый .doc или зашифрованный .docx (шифрование упаковывает zip в OLE-контейнер).
        raise PackageError("ole_container", "legacy .doc or encrypted document")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise PackageError("not_docx", "not a zip archive")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
        total = sum(i.file_size for i in infos)
        if total > max_unzipped_bytes:
            raise PackageError("too_large_unzipped", f"unzipped size {total}")
        for i in infos:
            if i.compress_size and i.file_size / i.compress_size > 200 and i.file_size > 10**7:
                raise PackageError("too_large_unzipped", f"suspicious ratio in {i.filename}")
        names = [i.filename for i in infos if not i.is_dir()]
        parts = {n: zf.read(n) for n in names}
    except PackageError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError) as exc:
        raise PackageError("corrupted", str(exc)) from exc

    if "[Content_Types].xml" not in parts or MAIN_PART not in parts:
        raise PackageError("not_docx", "missing [Content_Types].xml or word/document.xml")
    pkg = Package(parts, names)
    try:
        ct = pkg.xml("[Content_Types].xml")
        doc = pkg.document
    except etree.XMLSyntaxError as exc:
        raise PackageError("corrupted", str(exc)) from exc

    for o in ct.iter(qn("ct:Override")):
        ctype = o.get("ContentType", "")
        if "macroEnabled" in ctype or "vbaProject" in ctype:
            raise PackageError("macros", ctype)
        if o.get("PartName") == "/word/document.xml" and ctype != CT_MAIN:
            raise PackageError("macros" if "macro" in ctype.lower() else "not_docx", ctype)
    if any(n.lower().endswith("vbaproject.bin") for n in names):
        raise PackageError("macros", "vbaProject.bin")

    for tag in ("w:ins", "w:del", "w:moveFrom", "w:moveTo"):
        if doc.find(f".//{qn(tag)}") is not None:
            raise PackageError("tracked_changes", tag)
    for tag in ("w:pPrChange", "w:rPrChange"):
        if doc.find(f".//{qn(tag)}") is not None:
            raise PackageError("tracked_changes", tag)
    if pkg.document.find(qn("w:body")) is None:
        raise PackageError("corrupted", "no body")
    return pkg


# --- Единицы -------------------------------------------------------------------------

TWIPS_PER_CM = 566.929
TWIPS_PER_MM = 56.6929
TWIPS_PER_PT = 20


def cm_to_twips(cm: float) -> int:
    return round(cm * TWIPS_PER_CM)


def mm_to_twips(mm: float) -> int:
    return round(mm * TWIPS_PER_MM)


def pt_to_twips(pt: float) -> int:
    return round(pt * TWIPS_PER_PT)


def _int(v: str | None) -> int | None:
    if v is None:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def _on(el: etree._Element | None) -> bool | None:
    """Булево свойство OOXML: <w:b/>, <w:b w:val="0"/>, <w:b w:val="false"/>."""
    if el is None:
        return None
    val = wattr(el, "val")
    return val not in ("0", "false", "off", "none")


# --- Эффективное форматирование ------------------------------------------------------


@dataclass(frozen=True)
class ParaProps:
    style_id: str | None = None
    style_name: str | None = None
    align: str | None = None  # left/center/right/justify
    ind_left: int = 0  # twips
    ind_right: int = 0
    ind_first: int = 0  # twips; отрицательное — выступ (hanging)
    space_before: int = 0  # twips
    space_after: int = 0
    line: float | None = None  # множитель (lineRule=auto)
    line_exact_twips: int | None = None  # exact/atLeast
    outline_lvl: int | None = None
    keep_next: bool = False
    page_break_before: bool = False
    widow_control: bool = True
    num_id: str | None = None
    ilvl: int = 0


@dataclass(frozen=True)
class RunProps:
    font: str | None = None
    size_pt: float | None = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    caps: bool = False
    vert_align: str | None = None  # superscript/subscript
    color: str | None = None
    highlight: str | None = None


_ALIGN = {
    "both": "justify",
    "distribute": "justify",
    "justify": "justify",
    "left": "left",
    "start": "left",
    "center": "center",
    "right": "right",
    "end": "right",
}


@dataclass
class _Style:
    id: str
    type: str
    name: str
    based_on: str | None
    ppr: etree._Element | None
    rpr: etree._Element | None
    is_default: bool


@dataclass
class StyleSheet:
    styles: dict[str, _Style] = field(default_factory=dict)
    default_ppr: etree._Element | None = None
    default_rpr: etree._Element | None = None
    default_para_style: str | None = None
    default_char_style: str | None = None
    theme_fonts: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_package(cls, pkg: Package) -> StyleSheet:
        sheet = cls()
        theme_part = None
        for rel in pkg.rels().values():
            if rel.type.endswith("/theme") and not rel.external:
                theme_part = resolve_target(MAIN_PART, rel.target)
        if theme_part and pkg.has(theme_part):
            sheet.theme_fonts = _theme_fonts(pkg.xml(theme_part))
        root = pkg.xml_or_none("word/styles.xml")
        if root is None:
            return sheet
        dd = root.find(qn("w:docDefaults"))
        if dd is not None:
            sheet.default_ppr = dd.find(f"{qn('w:pPrDefault')}/{qn('w:pPr')}")
            sheet.default_rpr = dd.find(f"{qn('w:rPrDefault')}/{qn('w:rPr')}")
        for st in root.iter(qn("w:style")):
            sid = wattr(st, "styleId") or ""
            stype = wattr(st, "type") or "paragraph"
            name_el = st.find(qn("w:name"))
            based = st.find(qn("w:basedOn"))
            s = _Style(
                id=sid,
                type=stype,
                name=(wattr(name_el, "val") or sid) if name_el is not None else sid,
                based_on=wattr(based, "val") if based is not None else None,
                ppr=st.find(qn("w:pPr")),
                rpr=st.find(qn("w:rPr")),
                is_default=wattr(st, "default") in ("1", "true"),
            )
            sheet.styles[sid] = s
            if s.is_default and stype == "paragraph":
                sheet.default_para_style = sid
            if s.is_default and stype == "character":
                sheet.default_char_style = sid
        return sheet

    def chain(self, style_id: str | None) -> list[_Style]:
        """Цепочка basedOn от корня к стилю."""
        out: list[_Style] = []
        seen: set[str] = set()
        sid = style_id
        while sid and sid in self.styles and sid not in seen:
            seen.add(sid)
            out.append(self.styles[sid])
            sid = self.styles[sid].based_on
        return list(reversed(out))

    def style_name(self, style_id: str | None) -> str | None:
        if style_id and style_id in self.styles:
            return self.styles[style_id].name
        return None

    def para_style_id(self, p: etree._Element) -> str | None:
        ps = p.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
        sid = wattr(ps, "val") if ps is not None else None
        return sid if sid in self.styles else self.default_para_style

    def effective_ppr(self, p: etree._Element) -> ParaProps:
        sid = self.para_style_id(p)
        layers: list[etree._Element | None] = [self.default_ppr]
        layers += [s.ppr for s in self.chain(sid)]
        layers.append(p.find(qn("w:pPr")))
        props = _merge_ppr(layers)
        return replace(props, style_id=sid, style_name=self.style_name(sid))

    def effective_rpr(self, p: etree._Element, r: etree._Element) -> RunProps:
        layers: list[etree._Element | None] = [self.default_rpr]
        layers += [s.rpr for s in self.chain(self.para_style_id(p))]
        rs = r.find(f"{qn('w:rPr')}/{qn('w:rStyle')}")
        layers += [s.rpr for s in self.chain(wattr(rs, "val") if rs is not None else None)]
        layers.append(r.find(qn("w:rPr")))
        return _merge_rpr(layers, self.theme_fonts)

    def semantic_rpr(self, r: etree._Element) -> RunProps:
        """Только прямое форматирование run и его символьный стиль (без стиля абзаца)."""
        layers: list[etree._Element | None] = []
        rs = r.find(f"{qn('w:rPr')}/{qn('w:rStyle')}")
        layers += [s.rpr for s in self.chain(wattr(rs, "val") if rs is not None else None)]
        layers.append(r.find(qn("w:rPr")))
        return _merge_rpr(layers, self.theme_fonts)


def _theme_fonts(theme: etree._Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for kind in ("major", "minor"):
        el = theme.find(f".//{qn('a:' + kind + 'Font')}/{qn('a:latin')}")
        if el is not None and el.get("typeface"):
            out[kind] = el.get("typeface", "")
    return out


def _merge_ppr(layers: list[etree._Element | None]) -> ParaProps:
    v: dict[str, Any] = {}
    for ppr in layers:
        if ppr is None:
            continue
        jc = ppr.find(qn("w:jc"))
        if jc is not None:
            v["align"] = _ALIGN.get(wattr(jc, "val") or "", "left")
        ind = ppr.find(qn("w:ind"))
        if ind is not None:
            left = _int(wattr(ind, "left") or wattr(ind, "start"))
            if left is not None:
                v["ind_left"] = left
            right = _int(wattr(ind, "right") or wattr(ind, "end"))
            if right is not None:
                v["ind_right"] = right
            first = _int(wattr(ind, "firstLine"))
            hanging = _int(wattr(ind, "hanging"))
            if hanging is not None:
                v["ind_first"] = -hanging
            elif first is not None:
                v["ind_first"] = first
        sp = ppr.find(qn("w:spacing"))
        if sp is not None:
            before = _int(wattr(sp, "before"))
            if before is not None:
                v["space_before"] = before
            after = _int(wattr(sp, "after"))
            if after is not None:
                v["space_after"] = after
            line = _int(wattr(sp, "line"))
            if line is not None:
                rule = wattr(sp, "lineRule") or "auto"
                if rule == "auto":
                    v["line"] = line / 240
                    v["line_exact_twips"] = None
                else:
                    v["line"] = None
                    v["line_exact_twips"] = line
        ol = ppr.find(qn("w:outlineLvl"))
        if ol is not None:
            v["outline_lvl"] = _int(wattr(ol, "val"))
        for tag, key in (
            ("w:keepNext", "keep_next"),
            ("w:pageBreakBefore", "page_break_before"),
            ("w:widowControl", "widow_control"),
        ):
            el = ppr.find(qn(tag))
            if el is not None:
                v[key] = bool(_on(el))
        num = ppr.find(qn("w:numPr"))
        if num is not None:
            nid = num.find(qn("w:numId"))
            lvl = num.find(qn("w:ilvl"))
            if nid is not None:
                v["num_id"] = wattr(nid, "val")
            if lvl is not None:
                v["ilvl"] = _int(wattr(lvl, "val")) or 0
    if v.get("num_id") == "0":
        v["num_id"] = None  # numId=0 отменяет нумерацию
    return ParaProps(**v)


def _merge_rpr(layers: list[etree._Element | None], theme: dict[str, str]) -> RunProps:
    v: dict[str, Any] = {}
    for rpr in layers:
        if rpr is None:
            continue
        rf = rpr.find(qn("w:rFonts"))
        if rf is not None:
            font = wattr(rf, "ascii") or wattr(rf, "hAnsi")
            theme_ref = wattr(rf, "asciiTheme") or wattr(rf, "hAnsiTheme")
            if theme_ref:
                font = theme.get("major" if theme_ref.startswith("major") else "minor", font)
            if font:
                v["font"] = font
        sz = rpr.find(qn("w:sz"))
        if sz is not None and _int(wattr(sz, "val")) is not None:
            v["size_pt"] = (_int(wattr(sz, "val")) or 0) / 2
        for tag, key in (
            ("w:b", "bold"),
            ("w:i", "italic"),
            ("w:strike", "strike"),
            ("w:caps", "caps"),
        ):
            el = rpr.find(qn(tag))
            if el is not None:
                v[key] = bool(_on(el))
        u = rpr.find(qn("w:u"))
        if u is not None:
            v["underline"] = (wattr(u, "val") or "single") != "none"
        va = rpr.find(qn("w:vertAlign"))
        if va is not None:
            val = wattr(va, "val")
            v["vert_align"] = val if val in ("superscript", "subscript") else None
        c = rpr.find(qn("w:color"))
        if c is not None:
            v["color"] = wattr(c, "val")
        h = rpr.find(qn("w:highlight"))
        if h is not None:
            v["highlight"] = wattr(h, "val")
    return RunProps(**v)


# --- Нумерация Word ------------------------------------------------------------------


@dataclass(frozen=True)
class LevelDef:
    fmt: str  # decimal, bullet, lowerLetter, russianLower, ...
    text: str  # lvlText, напр. "%1." или "•"
    start: int = 1


class Numbering:
    """Определения numbering.xml и счётчики для вычисления видимых меток списков."""

    def __init__(self, root: etree._Element | None) -> None:
        self.abstract: dict[str, dict[int, LevelDef]] = {}
        self.num_to_abstract: dict[str, str] = {}
        self.overrides: dict[str, dict[int, int]] = {}
        if root is None:
            return
        for an in root.iter(qn("w:abstractNum")):
            aid = wattr(an, "abstractNumId") or ""
            levels: dict[int, LevelDef] = {}
            for lvl in an.iter(qn("w:lvl")):
                ilvl = _int(wattr(lvl, "ilvl")) or 0
                fmt = wattr(lvl.find(qn("w:numFmt")), "val") or "decimal"
                text = wattr(lvl.find(qn("w:lvlText")), "val") or ""
                start = _int(wattr(lvl.find(qn("w:start")), "val"))
                levels[ilvl] = LevelDef(fmt, text, 1 if start is None else start)
            self.abstract[aid] = levels
        for num in root.iter(qn("w:num")):
            nid = wattr(num, "numId") or ""
            an_el = num.find(qn("w:abstractNumId"))
            self.num_to_abstract[nid] = wattr(an_el, "val") or ""
            ov: dict[int, int] = {}
            for lo in num.iter(qn("w:lvlOverride")):
                so = lo.find(qn("w:startOverride"))
                if so is not None:
                    ov[_int(wattr(lo, "ilvl")) or 0] = _int(wattr(so, "val")) or 1
            self.overrides[nid] = ov

    def level(self, num_id: str | None, ilvl: int) -> LevelDef | None:
        if not num_id:
            return None
        aid = self.num_to_abstract.get(num_id)
        if aid is None:
            return None
        return self.abstract.get(aid, {}).get(ilvl)

    def counter(self) -> NumberingCounter:
        return NumberingCounter(self)


_RU_LETTERS = "абвгдежзиклмнопрстуфхцчшщэюя"


def format_number(n: int, fmt: str) -> str:
    if fmt in ("decimal", "decimalZero"):
        return f"{n:02d}" if fmt == "decimalZero" and n < 10 else str(n)
    if fmt in ("lowerLetter", "upperLetter"):
        s = ""
        k = n
        while k > 0:
            k, r = divmod(k - 1, 26)
            s = chr(ord("a") + r) + s
        return s.upper() if fmt == "upperLetter" else s
    if fmt in ("russianLower", "russianUpper"):
        s = _RU_LETTERS[(n - 1) % len(_RU_LETTERS)] * ((n - 1) // len(_RU_LETTERS) + 1)
        return s.upper() if fmt == "russianUpper" else s
    if fmt in ("lowerRoman", "upperRoman"):
        vals = [
            (1000, "m"),
            (900, "cm"),
            (500, "d"),
            (400, "cd"),
            (100, "c"),
            (90, "xc"),
            (50, "l"),
            (40, "xl"),
            (10, "x"),
            (9, "ix"),
            (5, "v"),
            (4, "iv"),
            (1, "i"),
        ]
        s, k = "", n
        for v, sym in vals:
            while k >= v:
                s += sym
                k -= v
        return s.upper() if fmt == "upperRoman" else s
    if fmt == "none":
        return ""
    return str(n)


class NumberingCounter:
    """Имитирует счётчики Word: общие для abstractNum, startOverride — при первом num."""

    def __init__(self, numbering: Numbering) -> None:
        self.n = numbering
        self.counts: dict[str, dict[int, int]] = {}
        self.seen_nums: set[str] = set()

    def next(self, num_id: str | None, ilvl: int) -> tuple[str, int | None, LevelDef] | None:
        """Возвращает (видимая метка, порядковый номер или None для маркеров, уровень)."""
        lvl = self.n.level(num_id, ilvl)
        if lvl is None or num_id is None:
            return None
        aid = self.n.num_to_abstract[num_id]
        counts = self.counts.setdefault(aid, {})
        if num_id not in self.seen_nums:
            self.seen_nums.add(num_id)
            for ov_lvl, start in self.n.overrides.get(num_id, {}).items():
                counts[ov_lvl] = start - 1
                for deeper in [k for k in counts if k > ov_lvl]:
                    del counts[deeper]
        current = counts.get(ilvl, lvl.start - 1) + 1
        counts[ilvl] = current
        for deeper in [k for k in counts if k > ilvl]:
            del counts[deeper]
        if lvl.fmt == "bullet":
            return lvl.text, None, lvl

        def repl(m: re.Match[str]) -> str:
            k = int(m.group(1)) - 1
            lk = self.n.level(num_id, k)
            value = counts.get(k, (lk.start if lk else 1))
            return format_number(value, lk.fmt if lk else "decimal")

        label = re.sub(r"%(\d)", repl, lvl.text)
        return label, current, lvl


# --- Обход элементов -----------------------------------------------------------------


def resolve_path(body: etree._Element, path: str) -> etree._Element:
    """Путь `3/1/0` — индексы дочерних элементов начиная с w:body."""
    el = body
    for part in path.split("/"):
        el = children(el)[int(part)]
    return el


def iter_text_nodes(p: etree._Element) -> Iterator[etree._Element]:
    """Видимые текстовые узлы абзаца, без вложенных объектов и Fallback."""
    skip = {
        qn("w:drawing"),
        qn("w:pict"),
        qn("w:object"),
        qn("mc:AlternateContent"),
        qn("w:instrText"),
        qn("w:delText"),
        qn("w:footnoteReference"),
        qn("w:endnoteReference"),
        qn("w:pPr"),
        qn("w:rPr"),
        qn("w:sdtPr"),
        qn("w:sdtEndPr"),
        qn("m:oMath"),
        qn("m:oMathPara"),
    }
    stack = list(reversed(children(p)))
    while stack:
        el = stack.pop()
        if el.tag in skip:
            continue
        if el.tag in (qn("w:t"), qn("w:tab"), qn("w:br"), qn("w:cr"), qn("w:noBreakHyphen")):
            yield el
            continue
        stack.extend(reversed(children(el)))


def node_text(el: etree._Element) -> str:
    tag = el.tag
    if tag == qn("w:t"):
        return el.text or ""
    if tag == qn("w:tab"):
        return "\t"
    if tag in (qn("w:br"), qn("w:cr")):
        return "" if wattr(el, "type") in ("page", "column") else "\n"
    if tag == qn("w:noBreakHyphen"):
        return "‑"
    return ""


def paragraph_text(p: etree._Element) -> str:
    return "".join(node_text(n) for n in iter_text_nodes(p))


def has_descendant(el: etree._Element, *tags: str) -> bool:
    return any(el.find(f".//{qn(t)}") is not None for t in tags)
