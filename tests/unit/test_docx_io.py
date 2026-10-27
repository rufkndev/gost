import io
import zipfile

import pytest

from app.core import docx_io as dx
from tests.synth import Doc, p, run, sect, tp


def test_rejects_non_zip() -> None:
    with pytest.raises(dx.PackageError) as e:
        dx.read_package(b"hello")
    assert e.value.code == "not_docx"


def test_rejects_ole_doc() -> None:
    with pytest.raises(dx.PackageError) as e:
        dx.read_package(dx.OLE_MAGIC + b"\x00" * 100)
    assert e.value.code == "ole_container"


def test_rejects_zip_without_document() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "x")
    with pytest.raises(dx.PackageError) as e:
        dx.read_package(buf.getvalue())
    assert e.value.code == "not_docx"


def _patch(data: bytes, name: str, fn: object) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in src.namelist():
            content = src.read(n)
            if n == name:
                content = fn(content)  # type: ignore[operator]
            z.writestr(n, content)
    return buf.getvalue()


def test_rejects_tracked_changes() -> None:
    data = Doc().add(tp("текст")).build()
    data = _patch(
        data,
        "word/document.xml",
        lambda c: c.replace(
            b"<w:body>",
            b'<w:body><w:p><w:ins w:id="1" w:author="a"><w:r><w:t>x</w:t></w:r></w:ins></w:p>',
        ),
    )
    with pytest.raises(dx.PackageError) as e:
        dx.read_package(data)
    assert e.value.code == "tracked_changes"


def test_rejects_macros() -> None:
    data = Doc().add(tp("текст")).build()
    data = _patch(
        data,
        "[Content_Types].xml",
        lambda c: c.replace(b"document.main+xml", b"document.macroEnabled.main+xml"),
    )
    with pytest.raises(dx.PackageError) as e:
        dx.read_package(data)
    assert e.value.code == "macros"


def test_rejects_zip_bomb_limit() -> None:
    data = Doc().add(tp("x" * 5000)).build()
    with pytest.raises(dx.PackageError) as e:
        dx.read_package(data, max_unzipped_bytes=1000)
    assert e.value.code == "too_large_unzipped"


def test_xml_entities_not_resolved() -> None:
    evil = b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY e SYSTEM "file:///etc/passwd">]><d>&e;</d>'
    root = dx.parse_xml(evil)
    assert "root:" not in (root.text or "")


def test_style_inheritance_chain() -> None:
    # docDefaults: Calibri 11, after=160 → «1» (heading 1): bold 16 → «2» (heading 2): 14 → прямое
    data = (
        Doc()
        .add(
            p(run("Заголовок"), style="2"),
            p(run("Прямое", size=20, font="Arial"), style="2", jc="center"),
            tp("Обычный"),
        )
        .build()
    )
    pkg = dx.read_package(data)
    st = dx.StyleSheet.from_package(pkg)
    paras = list(pkg.body.iter(dx.qn("w:p")))
    r0 = paras[0].find(dx.qn("w:r"))
    r1 = paras[1].find(dx.qn("w:r"))
    r2 = paras[2].find(dx.qn("w:r"))
    assert r0 is not None and r1 is not None and r2 is not None
    pp0 = st.effective_ppr(paras[0])
    rp0 = st.effective_rpr(paras[0], r0)
    assert pp0.style_name == "heading 2"
    assert pp0.outline_lvl == 1 and pp0.keep_next and pp0.space_after == 160
    assert rp0.bold and rp0.size_pt == 14 and rp0.font == "Calibri"
    rp1 = st.effective_rpr(paras[1], r1)
    assert rp1.size_pt == 20 and rp1.font == "Arial" and rp1.bold
    assert st.effective_ppr(paras[1]).align == "center"
    rp2 = st.effective_rpr(paras[2], r2)
    assert not rp2.bold and rp2.size_pt == 11
    sem = st.semantic_rpr(r0)
    assert not sem.bold  # жирный из стиля абзаца — не семантика run


def test_numbering_labels_and_overrides() -> None:
    data = (
        Doc()
        .add(
            p(run("a"), num=(2, 0)),
            p(run("b"), num=(2, 0)),
            p(run("c"), num=(3, 0)),
            p(run("•"), num=(1, 0)),
        )
        .build()
    )
    pkg = dx.read_package(data)
    n = dx.Numbering(pkg.xml("word/numbering.xml")).counter()
    labels = [n.next("2", 0), n.next("2", 0), n.next("3", 0), n.next("1", 0)]
    assert [x[0] for x in labels if x] == ["1.", "2.", "1.", "•"]
    assert labels[2] is not None and labels[2][1] == 1  # startOverride перезапускает


def test_format_number() -> None:
    assert dx.format_number(3, "russianLower") == "в"
    assert dx.format_number(4, "upperRoman") == "IV"
    assert dx.format_number(28, "lowerLetter") == "ab"


def test_package_roundtrip_and_sections() -> None:
    data = Doc().add(p(run("x"), sect=sect(landscape=True)), tp("y")).build()
    pkg = dx.read_package(data)
    again = dx.read_package(pkg.to_bytes())
    assert len(list(again.body.iter(dx.qn("w:sectPr")))) == 2
