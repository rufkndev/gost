"""Белый список нормализаций: по тесту на каждое правило (spec 6.1)."""

from pathlib import Path

import pytest

from app.config import ROOT_DIR
from app.core import docx_io as dx
from app.core.classify.rules import classify
from app.core.ir import Block, BlockType, DocumentIR, Run
from app.core.normalize import (
    RULE_CAPTION,
    RULE_CASE,
    RULE_EMPTY,
    RULE_HEADING_DOT,
    RULE_HEADING_NUMBER,
    RULE_LIST_MARKER,
    RULE_SPACES,
    RULE_TOC,
    RULE_TRIM,
    RunText,
    normalize,
)
from app.core.parser import parse
from app.core.profile import Profile, load_profiles
from tests.synth import Doc, drawing, p, run, table, tp, word_field

T = BlockType
LONG = "Это обычный абзац основного текста, в котором достаточно много слов для уверенности."


@pytest.fixture(scope="module")
def profiles() -> dict[str, Profile]:
    return load_profiles(ROOT_DIR / "app" / "profiles")


def norm(profile: Profile, *body: str) -> tuple[DocumentIR, dict[str, int]]:
    ir = classify(parse(dx.read_package(Doc(image=True).add(*body).build())))
    journal = normalize(ir, profile)
    return ir, journal.stats()


def test_runtext_replace_across_runs() -> None:
    b = Block(id="p1", runs=[Run(text="Рис"), Run(text=". 3", bold=True), Run(text=". Схема")])
    assert RunText(b).replace(0, 5, "Рисунок ")
    assert b.text == "Рисунок 3. Схема"


def test_runtext_refuses_field_result() -> None:
    b = Block(id="p1", runs=[Run(text="Рис. "), Run(text="3", in_field=True), Run(text=" x")])
    assert not RunText(b).replace(3, 6, "")
    assert b.text == "Рис. 3 x"


def test_trim_and_spaces(profiles: dict[str, Profile]) -> None:
    ir, st = norm(profiles["misis"], tp("\t   Текст  с   пробелами и   отступом.   " + LONG))
    assert ir.blocks[0].text.startswith("Текст с пробелами и отступом.")
    assert st[RULE_TRIM] == 1 and st[RULE_SPACES] == 1


def test_empty_paragraphs_counted(profiles: dict[str, Profile]) -> None:
    _, st = norm(profiles["misis"], tp(LONG), tp(""), tp(""), tp(LONG))
    assert st[RULE_EMPTY] == 2


def test_heading_number_and_dot(profiles: dict[str, Profile]) -> None:
    ir, st = norm(profiles["misis"], p(run("1.2.\tМетоды анализа.", bold=True)), tp(LONG))
    assert ir.blocks[0].text == "1.2 Методы анализа"
    assert st[RULE_HEADING_NUMBER] == 1 and st[RULE_HEADING_DOT] == 1


def test_glued_heading_number(profiles: dict[str, Profile]) -> None:
    ir, _ = norm(profiles["misis"], p(run("3Результаты", bold=True), style="1"), tp(LONG))
    assert ir.blocks[0].text == "3 Результаты"


def test_structural_case(profiles: dict[str, Profile]) -> None:
    ir, st = norm(profiles["misis"], tp("Заключение."), tp(LONG))
    assert ir.blocks[0].text == "ЗАКЛЮЧЕНИЕ"
    assert st[RULE_CASE] == 1


@pytest.mark.parametrize(
    ("profile_id", "expected"),
    [
        ("misis", "Рисунок 3 – Схема процесса"),
        ("gost_7_32_2017", "Рисунок 3 — Схема процесса"),
    ],
)
def test_caption_prefix(profiles: dict[str, Profile], profile_id: str, expected: str) -> None:
    ir, st = norm(profiles[profile_id], p(drawing()), tp("Рис.3. Схема процесса"), tp(LONG))
    assert ir.blocks[1].text == expected
    assert st[RULE_CAPTION] == 1


def test_table_caption_prefix(profiles: dict[str, Profile]) -> None:
    ir, _ = norm(profiles["misis"], tp("Таблица 2: Итоги"), table([["a", "b"]]), tp(LONG))
    assert ir.blocks[0].text == "Таблица 2 – Итоги"


def test_caption_with_seq_field_keeps_field(profiles: dict[str, Profile]) -> None:
    ir, _ = norm(
        profiles["misis"],
        p(drawing()),
        p(run("Рисунок "), word_field("SEQ Рисунок", "4"), run(". Схема")),
        tp(LONG),
    )
    b = ir.blocks[1]
    assert b.text == "Рисунок 4 – Схема"
    assert any(r.in_field and r.text == "4" for r in b.runs)


def test_list_markers_replaced(profiles: dict[str, Profile]) -> None:
    ir, st = norm(profiles["misis"], tp(LONG), tp("- первый пункт;"), tp("- второй пункт."))
    assert [b.text for b in ir.blocks[1:]] == ["первый пункт;", "второй пункт."]
    assert all(b.features["numbering"]["kind"] == "bullet" for b in ir.blocks[1:])
    assert st[RULE_LIST_MARKER] == 2


def test_nonsequential_typed_numbers_kept(profiles: dict[str, Profile]) -> None:
    ir, st = norm(profiles["misis"], tp(LONG), tp("1) первый пункт;"), tp("3) третий пункт."))
    assert ir.blocks[1].text.startswith("1)")
    assert RULE_LIST_MARKER not in st


def test_toc_replacement_counted(profiles: dict[str, Profile]) -> None:
    _, st = norm(profiles["misis"], tp("Содержание"), tp("Введение\t3"), tp("Введение"), tp(LONG))
    assert st[RULE_TOC] == 1


def test_profiles_exist() -> None:
    assert Path(ROOT_DIR / "app" / "profiles" / "misis" / "profile.yaml").exists()
