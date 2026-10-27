"""Правила классификации: по тесту на каждое правило (spec 5.2)."""

from app.core import docx_io as dx
from app.core.classify.rules import classify
from app.core.ir import BlockType, DocumentIR
from app.core.parser import parse
from tests.synth import (
    Doc,
    crooked_document,
    drawing,
    omml,
    p,
    page_break,
    run,
    table,
    title_page,
    tp,
)

T = BlockType


def ir_of(*body: str, image: bool = False) -> DocumentIR:
    return classify(parse(dx.read_package(Doc(image=image).add(*body).build())))


def types(ir: DocumentIR) -> list[str]:
    return [b.label() for b in ir.blocks if b.type != T.EMPTY]


LONG = "Это обычный абзац основного текста, в котором достаточно много слов для уверенности."


def test_text_invariant_on_crooked() -> None:
    pkg = dx.read_package(crooked_document())
    ir = parse(pkg)
    for b in ir.blocks:
        if b.id.startswith("p"):
            assert b.text == dx.paragraph_text(dx.resolve_path(pkg.body, b.xml_ref or ""))


def test_title_page_until_page_break() -> None:
    ir = ir_of(*title_page(), tp("Введение"), tp(LONG))
    assert [b.type for b in ir.blocks[:7]] == [T.TITLE_PAGE] * 7
    assert ir.blocks[7].type == T.STRUCTURAL_HEADING


def test_no_title_page_without_markers() -> None:
    ir = ir_of(tp("Просто строка"), page_break(), tp(LONG))
    assert ir.blocks[0].type != T.TITLE_PAGE


def test_structural_heading_variants() -> None:
    ir = ir_of(
        tp("ВВЕДЕНИЕ"),
        tp(LONG),
        tp("Список использованной литературы."),
        tp(LONG),
        tp("  заключение  "),
        tp(LONG),
    )
    assert [b.type for b in ir.blocks[::2]] == [T.STRUCTURAL_HEADING] * 3


def test_toc_title_and_entries() -> None:
    ir = ir_of(
        tp("Оглавление"), tp("Введение\t3"), tp("1 Анализ ............ 5"), tp("Введение"), tp(LONG)
    )
    assert [b.type for b in ir.blocks[:3]] == [T.TOC] * 3
    assert ir.blocks[3].type == T.STRUCTURAL_HEADING


def test_appendix_label_and_title() -> None:
    ir = ir_of(
        tp("ПРИЛОЖЕНИЕ Б", jc="center"), tp("Листинг программы", jc="center", bold=True), tp(LONG)
    )
    assert ir.blocks[0].type == T.APPENDIX_HEADING
    assert ir.blocks[0].features["appendix_part"] == "label"
    assert ir.blocks[1].features["appendix_part"] == "title"


def test_numbered_heading_levels() -> None:
    ir = ir_of(
        p(run("2 Анализ данных", bold=True)),
        tp(LONG),
        p(run("2.1 Описание выборки", bold=True)),
        tp(LONG),
        p(run("2.1.3 Детали", bold=True)),
        tp(LONG),
    )
    hs = [b for b in ir.blocks if b.type == T.HEADING]
    assert [h.level for h in hs] == [1, 2, 3]
    assert all(h.confidence >= 0.9 for h in hs)


def test_heading_style_raises_confidence() -> None:
    ir = ir_of(p(run("Обзор литературы"), style="1"), tp(LONG))
    assert ir.blocks[0].type == T.HEADING and ir.blocks[0].confidence >= 0.9


def test_unnumbered_bold_heading_is_gray() -> None:
    ir = ir_of(tp("Методика исследования", bold=True), tp(LONG))
    b = ir.blocks[0]
    assert b.type == T.HEADING and 0.3 < b.confidence < 0.9


def test_numbered_list_item_not_heading() -> None:
    ir = ir_of(tp("1. провести опрос сотрудников;"), tp("2. обработать результаты;"))
    assert all(b.type == T.LIST_ITEM for b in ir.blocks)


def test_figure_and_caption() -> None:
    ir = ir_of(p(drawing()), tp("Рис. 2 - Схема"), tp(LONG), image=True)
    assert ir.blocks[0].type == T.FIGURE
    assert ir.blocks[1].type == T.FIGURE_CAPTION and ir.blocks[1].confidence >= 0.9


def test_caption_like_sentence_is_body() -> None:
    ir = ir_of(
        tp(
            "Рисунок 3 показывает, что выручка росла на протяжении всего периода "
            "наблюдений, а затраты снижались, и это подтверждает основную гипотезу работы, "
            "сформулированную во введении, а также в первой главе, где описана методика "
            "и порядок расчёта всех показателей по каждому из подразделений компании."
        )
    )
    assert ir.blocks[0].type == T.BODY


def test_table_caption_and_continuation() -> None:
    ir = ir_of(
        tp("Таблица 1 – Данные"),
        table([["a", "b"]]),
        tp("Продолжение таблицы 1"),
        table([["c", "d"]]),
        tp(LONG),
    )
    assert ir.blocks[0].type == T.TABLE_CAPTION
    assert ir.blocks[2].type == T.TABLE_CAPTION


def test_table_note() -> None:
    ir = ir_of(table([["a", "b"]]), tp("Примечание – данные условные."), tp(LONG))
    assert ir.blocks[1].type == T.TABLE_NOTE


def test_formula_with_number() -> None:
    ir = ir_of(p(run("\t"), omml(), run("\t(3)")), tp(LONG))
    assert ir.blocks[0].type == T.FORMULA and ir.blocks[0].confidence >= 0.9


def test_inline_math_is_body() -> None:
    ir = ir_of(p(run("Пусть "), omml(), run(" — переменная, тогда выполняется равенство.")))
    assert ir.blocks[0].type == T.BODY


def test_lists_word_and_text_markers() -> None:
    ir = ir_of(
        tp(LONG),
        p(run("пункт один"), num=(1, 0)),
        p(run("пункт два"), num=(1, 0)),
        tp("– текстовый пункт;"),
        tp("а) буквенный пункт;"),
    )
    kinds = [b.features.get("list_kind") for b in ir.blocks[1:]]
    assert all(b.type == T.LIST_ITEM for b in ir.blocks[1:])
    assert kinds == ["bullet", "bullet", "bullet", "letter"]


def test_bibliography_region() -> None:
    ir = ir_of(
        tp("Список литературы"),
        tp("Иванов И. И. Книга. — М., 2020."),
        tp("Петров П. П. Статья. — 2021."),
        tp("Приложение А"),
        tp(LONG),
    )
    assert [b.type for b in ir.blocks[1:3]] == [T.BIBLIOGRAPHY_ITEM] * 2
    assert ir.blocks[3].type == T.APPENDIX_HEADING


def test_empty_and_breaks() -> None:
    ir = ir_of(tp(LONG), tp(""), page_break(), tp(LONG))
    assert [b.type for b in ir.blocks] == [T.BODY, T.EMPTY, T.PAGE_BREAK, T.BODY]


def test_short_doc_unnumbered_headings_level2() -> None:
    ir = ir_of(p(run("Цель работы"), style="1"), tp(LONG), p(run("Вывод"), style="1"), tp(LONG))
    assert [b.level for b in ir.blocks if b.type == T.HEADING] == [2, 2]
