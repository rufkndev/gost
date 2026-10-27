"""Сборка, целостность, линтер, шаблон (spec 4.4, 6.2, 6.3)."""

import io
import zipfile

import pytest

from app.config import ROOT_DIR
from app.core import docx_io as dx
from app.core import expect as E
from app.core.builder import build
from app.core.classify.rules import classify
from app.core.integrity import check
from app.core.linter import lint
from app.core.normalize import normalize
from app.core.ooxml import SEMANTIC_RPR
from app.core.parser import parse
from app.core.pipeline import format_document, load_template
from app.core.profile import Profile, load_profiles
from app.core.template import build_template
from tests.synth import Doc, crooked_document, drawing, p, run, sect, title_page, tp

PROFILES = ROOT_DIR / "app" / "profiles"


@pytest.fixture(scope="module")
def profiles() -> dict[str, Profile]:
    return load_profiles(PROFILES)


@pytest.fixture(scope="module", params=["misis", "gost_7_32_2017"])
def formatted(request: pytest.FixtureRequest, profiles: dict[str, Profile]):  # type: ignore[no-untyped-def]
    prof = profiles[request.param]
    res = format_document(crooked_document(), prof, load_template(PROFILES, prof), use_render=False)
    return prof, res


def _rezip(data: bytes, name: str, fn) -> bytes:  # type: ignore[no-untyped-def]
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in src.namelist():
            c = src.read(n)
            z.writestr(n, fn(c) if n == name else c)
    return buf.getvalue()


def test_crooked_document_passes(formatted) -> None:  # type: ignore[no-untyped-def]
    _, res = formatted
    assert res.integrity.ok, res.integrity.errors
    assert not [i for i in res.lint if i.severity == "error"], res.lint
    assert res.ok and res.data


def test_objects_transferred(formatted) -> None:  # type: ignore[no-untyped-def]
    _, res = formatted
    objs = res.integrity.stats["objects"]
    assert objs["drawings"] == 1 and objs["math"] == 1
    assert objs["footnotes"] == 1 and objs["hyperlinks"] == 1 and objs["tables"] == 3
    pkg = dx.read_package(res.data)
    assert pkg.has("word/media/img.png")
    blip = next(pkg.body.iter(dx.qn("a:blip")))
    rid = blip.get(dx.qn("r:embed"))
    assert pkg.part_for_rel(rid or "") == "word/media/img.png"
    link = next(pkg.body.iter(dx.qn("w:hyperlink")))
    assert pkg.rels()[link.get(dx.qn("r:id")) or ""].external


def test_landscape_section_kept(formatted) -> None:  # type: ignore[no-untyped-def]
    prof, res = formatted
    pkg = dx.read_package(res.data)
    sects = list(pkg.body.iter(dx.qn("w:sectPr")))
    orients = [dx.wattr(s.find(dx.qn("w:pgSz")), "orient") for s in sects]
    assert "landscape" in orients
    land = sects[orients.index("landscape")]
    left = dx._int(dx.wattr(land.find(dx.qn("w:pgMar")), "left"))
    assert left == dx.mm_to_twips(prof.page.landscape_margins_mm.left)


def test_title_section_without_number(formatted) -> None:  # type: ignore[no-untyped-def]
    _, res = formatted
    assert not [i for i in res.lint if i.rule.startswith("page_number")]


def test_only_semantic_run_formatting_after_title(formatted) -> None:  # type: ignore[no-untyped-def]
    _, res = formatted
    pkg = dx.read_package(res.data)
    in_content = False
    for el in dx.children(pkg.body):
        if el.tag == dx.qn("w:bookmarkStart") and dx.wattr(el, "name") == "_GostContent":
            in_content = True
        if not in_content:
            continue
        for rpr in el.iter(dx.qn("w:rPr")):
            if any(
                a.tag in (dx.qn("w:drawing"), dx.qn("w:pPr"), dx.qn("w:lvl"))
                for a in rpr.iterancestors()
            ):
                continue
            names = {dx.local(c) for c in rpr}
            assert names <= SEMANTIC_RPR, names


def test_integrity_detects_changed_word(profiles: dict[str, Profile]) -> None:
    prof = profiles["misis"]
    src = crooked_document()
    pkg = dx.read_package(src)
    ir = classify(parse(pkg))
    journal = normalize(ir, prof)
    out = build(pkg, ir, prof, load_template(PROFILES, prof)).data
    assert check(pkg, ir, journal, out).ok
    tampered = _rezip(
        out, "word/document.xml", lambda c: c.replace("заключения".encode(), "вывода".encode())
    )
    res = check(pkg, ir, journal, tampered)
    assert not res.ok and any("word_sequence" in e for e in res.errors)
    # В ошибках нет текста документа.
    assert not any("вывод" in e for e in res.errors)


def test_integrity_detects_lost_drawing(profiles: dict[str, Profile]) -> None:
    prof = profiles["misis"]
    pkg = dx.read_package(crooked_document())
    ir = classify(parse(pkg))
    journal = normalize(ir, prof)
    out = build(pkg, ir, prof, load_template(PROFILES, prof)).data
    tampered = _rezip(
        out,
        "word/document.xml",
        lambda c: c.replace(b"<w:drawing>", b"<w:drawingX>").replace(
            b"</w:drawing>", b"</w:drawingX>"
        ),
    )
    res = check(pkg, ir, journal, tampered)
    assert any("drawings" in e for e in res.errors)


def test_linter_catches_direct_formatting_and_margins(formatted) -> None:  # type: ignore[no-untyped-def]
    prof, res = formatted
    bad = _rezip(
        res.data,
        "word/document.xml",
        lambda c: c.replace(
            b'<w:pStyle w:val="GostBody"/>', b'<w:pStyle w:val="GostBody"/><w:jc w:val="left"/>', 1
        ).replace(b'w:left="1701"', b'w:left="1000"'),
    )
    rules = {i.rule for i in lint(bad, prof) if i.severity == "error"}
    assert "direct_paragraph_formatting" in rules
    assert "page_margins" in rules


def test_linter_catches_empty_and_dot(profiles: dict[str, Profile]) -> None:
    prof = profiles["gost_7_32_2017"]
    res = format_document(crooked_document(), prof, load_template(PROFILES, prof), use_render=False)
    assert res.data is not None
    bad = _rezip(
        res.data,
        "word/document.xml",
        lambda c: c.replace("ЗАКЛЮЧЕНИЕ</w:t>".encode(), "ЗАКЛЮЧЕНИЕ.</w:t>".encode()).replace(
            b"<w:bookmarkStart w:id=",
            b'<w:p><w:pPr><w:pStyle w:val="GostBody"/></w:pPr></w:p><w:bookmarkStart w:id=',
            1,
        ),
    )
    rules = {i.rule for i in lint(bad, prof) if i.severity == "error"}
    assert "heading_trailing_dot" in rules


def test_template_has_all_styles(profiles: dict[str, Profile]) -> None:
    for prof in profiles.values():
        pkg = dx.read_package(build_template(prof))
        ids = {dx.wattr(s, "styleId") for s in pkg.xml("word/styles.xml").iter(dx.qn("w:style"))}
        assert set(E.para_specs(prof)) <= ids
        assert {"GostBody", "GostHeading1Tight", "GostBodyAfterTable"} <= ids


def test_new_page_headings_and_toc(profiles: dict[str, Profile]) -> None:
    prof = profiles["misis"]
    long = "Длинный абзац текста для объёма документа. " * 60
    body = [*title_page(), tp("Введение"), tp(long)]
    for n in range(1, 5):
        body += [p(run(f"{n} Раздел номер {n}", bold=True)), tp(long), tp(long)]
    data = Doc().add(*body).build()
    res = format_document(data, prof, load_template(PROFILES, prof), use_render=False)
    assert res.ok, (res.integrity.errors, [i for i in res.lint if i.severity == "error"])
    assert res.build_meta["toc_generated"] and res.build_meta["toc_entries"] == 5


def test_heading_after_heading_uses_tight_styles(profiles: dict[str, Profile]) -> None:
    prof = profiles["misis"]
    long = "Абзац основного текста с достаточным количеством слов для уверенности классификации."
    data = (
        Doc()
        .add(
            p(run("1 Анализ", bold=True)),
            p(run("1.1 Данные", bold=True)),
            tp(long),
            p(run("1.2 Методы", bold=True)),
            tp(long),
        )
        .build()
    )
    res = format_document(data, prof, load_template(PROFILES, prof), use_render=False)
    assert res.data is not None
    styles = [dx.wattr(ps, "val") for ps in dx.read_package(res.data).body.iter(dx.qn("w:pStyle"))]
    assert styles[:3] == ["GostHeading1Tight", "GostHeading2Follow", "GostBody"]
    assert "GostHeading2" in styles


def test_page_breaks_before_new_page_headings_dropped(profiles: dict[str, Profile]) -> None:
    prof = profiles["misis"]
    long = "Абзац основного текста с достаточным количеством слов для уверенности классификации."
    data = (
        Doc()
        .add(
            tp(long),
            '<w:p><w:r><w:br w:type="page"/></w:r></w:p>',
            tp("Заключение"),
            tp(long),
            p(drawing()),
        )
        .build()
    )
    res = format_document(data, prof, load_template(PROFILES, prof), use_render=False)
    assert res.build_meta["dropped_page_breaks"] == 1


def test_sections_collapse_pdf_like(profiles: dict[str, Profile]) -> None:
    prof = profiles["misis"]
    long = "Абзац основного текста с достаточным количеством слов для уверенности классификации."
    body = []
    for _ in range(6):
        body += [tp(long), p(sect=sect())]
    res = format_document(
        Doc().add(*body).build(), prof, load_template(PROFILES, prof), use_render=False
    )
    assert res.ok and res.build_meta["sections_out"] == 1
