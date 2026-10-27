"""Конвейер ядра (spec, раздел 4.1). Не зависит от бота, БД и оплаты."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core import docx_io as dx
from app.core.builder import BuildResult, build
from app.core.classify.rules import classify
from app.core.integrity import IntegrityResult, check
from app.core.ir import DocumentIR
from app.core.linter import LintIssue, lint
from app.core.normalize import Journal, normalize
from app.core.parser import parse
from app.core.profile import Profile
from app.core.render import RenderError, find_heading_pages, render_pdf, soffice_available
from app.core.report import FinalReport, PreliminaryReport, final_report, preliminary
from app.core.template import build_template

log = logging.getLogger(__name__)

# Резолвер серой зоны: ИИ и/или ответы пользователя. Получает IR, меняет типы блоков.
Resolver = Callable[[DocumentIR], None]


@dataclass
class Analysis:
    pkg: dx.Package
    ir: DocumentIR
    report: PreliminaryReport


@dataclass
class FormatOutcome:
    ok: bool
    data: bytes | None
    ir: DocumentIR
    journal: Journal
    integrity: IntegrityResult
    lint: list[LintIssue]
    report: FinalReport
    build_meta: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None


def load_template(profiles_dir: Path, profile: Profile) -> dx.Package:
    """Шаблон всегда строится из profile.yaml — единственного источника правил.

    Файл template.docx (cli build-template) нужен только чтобы посмотреть стили в Word:
    правка YAML без пересборки файла не приведёт к оформлению по устаревшим стилям.
    """
    del profiles_dir
    return dx.read_package(build_template(profile))


def analyze(
    data: bytes,
    profile: Profile,
    price_rub: int,
    accept_threshold: float,
    max_unzipped_bytes: int = 200 * 1024 * 1024,
) -> Analysis:
    pkg = dx.read_package(data, max_unzipped_bytes)
    ir = classify(parse(pkg))
    return Analysis(pkg, ir, preliminary(ir, profile, price_rub, accept_threshold))


def format_document(
    data: bytes,
    profile: Profile,
    template: dx.Package,
    *,
    accept_threshold: float = 0.9,
    resolve: Resolver | None = None,
    ir: DocumentIR | None = None,
    use_render: bool = True,
    timeout_s: int = 90,
) -> FormatOutcome:
    pkg = dx.read_package(data)
    if ir is None:
        ir = classify(parse(pkg))
    if resolve is not None:
        resolve(ir)
    work_ir = ir.model_copy(deep=True)  # нормализация меняет run — исходный IR не трогаем
    journal = normalize(work_ir, profile)
    built: BuildResult = build(pkg, work_ir, profile, template)
    pages: dict[str, int] = {}
    if use_render and built.toc and soffice_available():
        try:
            pdf = render_pdf(built.data, timeout_s=timeout_s)
            pages = find_heading_pages(pdf, [(e.bookmark, e.text) for e in built.toc])
            built = build(pkg, work_ir, profile, template, toc_pages=pages)
        except RenderError as exc:
            log.warning("render failed, TOC pages left for Word", extra={"code": str(exc)})
    built.meta["toc_pages_found"] = len(pages)
    integ = check(pkg, work_ir, journal, built.data)
    issues = lint(built.data, profile, expect_toc=bool(built.toc))
    errors = [i for i in issues if i.severity == "error"]
    ok = integ.ok and not errors
    rep = final_report(work_ir, journal, issues, profile, accept_threshold, built.meta)
    code = None if ok else ("integrity" if not integ.ok else "lint")
    return FormatOutcome(
        ok, built.data if ok else None, work_ir, journal, integ, issues, rep, built.meta, code
    )
