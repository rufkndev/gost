"""CLI ядра: python -m app.core.cli <команда> ..."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.core import docx_io as dx
from app.core.classify.rules import classify
from app.core.ir import DocumentIR
from app.core.parser import parse


def load_ir(path: Path) -> tuple[dx.Package, DocumentIR]:
    pkg = dx.read_package(path.read_bytes())
    return pkg, classify(parse(pkg))


def cmd_dump(args: argparse.Namespace) -> int:
    _, ir = load_ir(Path(args.file))
    for b in ir.blocks:
        if b.type.value in ("empty",) and not args.all:
            continue
        txt = (b.text.strip() or str(b.features.get("text_preview", "")))[: args.width]
        print(f"{b.id} {b.label():22} {b.confidence:.2f} p{b.page_estimate or 0:<3} {txt!r}")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    import json
    import time

    from app.config import get_settings
    from app.core.profile import load_profiles
    from app.core.report import preliminary, render_preliminary_text

    settings = get_settings()
    profile = load_profiles(settings.profiles_dir)[args.profile]
    for f in args.files:
        t0 = time.perf_counter()
        _, ir = load_ir(Path(f))
        rep = preliminary(ir, profile, settings.price_rub, settings.classify_accept_threshold)
        dt = time.perf_counter() - t0
        if args.json:
            print(json.dumps(rep.to_dict(), ensure_ascii=False))
        else:
            print(f"=== {Path(f).name} ({dt:.2f} с)")
            print(render_preliminary_text(rep))
            print()
    return 0


def cmd_annotate(args: argparse.Namespace) -> int:
    import json

    from app.core.corpus import apply_review, draft_annotation, write_review_csv

    corpus = Path(args.corpus)
    out = corpus / "annotated"
    out.mkdir(exist_ok=True)
    if args.apply:
        for name, n in apply_review(out, Path(args.apply)).items():
            print(f"{name}: исправлений {n}")
        return 0
    anns = []
    for f in sorted((corpus / "raw").glob("*.docx")):
        target = out / f"{f.stem}.json"
        if target.exists() and json.loads(target.read_text(encoding="utf-8")).get("confirmed"):
            print(f"{f.name}: эталон подтверждён, пропускаю")
            anns.append(json.loads(target.read_text(encoding="utf-8")))
            continue
        _, ir = load_ir(f)
        ann = draft_annotation(f.name, ir)
        target.write_text(json.dumps(ann, ensure_ascii=False, indent=1), encoding="utf-8")
        anns.append(ann)
        print(f"{f.name}: блоков {len(ann['blocks'])}")
    write_review_csv([a for a in anns if not a.get("confirmed")], corpus / "review.csv")
    print(f"Список для проверки: {corpus / 'review.csv'}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    import json

    from app.config import get_settings
    from app.core.corpus import evaluate

    corpus = Path(args.corpus)
    pairs = []
    for f in sorted((corpus / "raw").glob("*.docx")):
        ann_path = corpus / "annotated" / f"{f.stem}.json"
        if not ann_path.exists():
            continue
        _, ir = load_ir(f)
        pairs.append((json.loads(ann_path.read_text(encoding="utf-8")), ir))
    print(evaluate(pairs, get_settings().classify_accept_threshold).to_text())
    return 0


def cmd_build_template(args: argparse.Namespace) -> int:
    from app.config import get_settings
    from app.core.profile import load_profiles, template_path
    from app.core.template import build_template

    settings = get_settings()
    profiles = load_profiles(settings.profiles_dir)
    ids = list(profiles) if args.profile == "all" else [args.profile]
    for pid in ids:
        path = template_path(settings.profiles_dir, pid)
        path.write_bytes(build_template(profiles[pid]))
        print(f"{pid}: {path}")
    return 0


def cmd_format(args: argparse.Namespace) -> int:
    import json
    import time

    from app.config import get_settings
    from app.core.normalize import RULE_LABELS
    from app.core.pipeline import format_document, load_template
    from app.core.profile import load_profiles
    from app.core.report import render_final_text

    settings = get_settings()
    profile = load_profiles(settings.profiles_dir)[args.profile]
    template = load_template(settings.profiles_dir, profile)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    failed = 0
    summary = []
    for f in args.files:
        path = Path(f)
        t0 = time.perf_counter()
        res = format_document(
            path.read_bytes(),
            profile,
            template,
            accept_threshold=settings.classify_accept_threshold,
            use_render=not args.no_render,
        )
        dt = time.perf_counter() - t0
        errors = [i for i in res.lint if i.severity == "error"]
        warns = [i for i in res.lint if i.severity == "warning"]
        status = "OK " if res.ok else "FAIL"
        integ = "ok" if res.integrity.ok else "FAIL"
        print(
            f"{status} {path.name[:50]:50} {dt:5.2f}s integrity={integ}"
            f" lint_errors={len(errors)} warnings={len(warns)}"
        )
        for msg in res.integrity.errors[:5]:
            print(f"      integrity: {msg}")
        for e in errors[: args.show]:
            print(f"      lint: {e.rule} {e.ref} {e.message} | {e.preview[:60]}")
        summary.append(
            {
                "file": path.name,
                "ok": res.ok,
                "integrity": res.integrity.ok,
                "lint_errors": len(errors),
                "warnings": len(warns),
                "seconds": round(dt, 2),
            }
        )
        if res.data is not None:
            (out_dir / f"{path.stem}_ГОСТ.docx").write_bytes(res.data)
            (out_dir / f"{path.stem}_отчёт.txt").write_text(
                render_final_text(res.report, RULE_LABELS), encoding="utf-8"
            )
        elif args.keep_failed:
            from app.core.builder import build
            from app.core.docx_io import read_package
            from app.core.normalize import normalize

            ir = res.ir.model_copy(deep=True)
            pkg = read_package(path.read_bytes())
            build_res = build(pkg, ir, profile, template)
            del normalize
            (out_dir / f"{path.stem}_FAILED.docx").write_bytes(build_res.data)
        failed += 0 if res.ok else 1
    if args.summary:
        Path(args.summary).write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    print(f"Итого: {len(args.files) - failed}/{len(args.files)} без ошибок")
    return 1 if failed else 0


def cmd_compare(args: argparse.Namespace) -> int:
    from app.core.render import render_pdf, side_by_side

    left = render_pdf(Path(args.source).read_bytes())
    right = render_pdf(Path(args.result).read_bytes())
    Path(args.out).write_bytes(side_by_side(left, right))
    print(args.out)
    return 0


def cmd_llm_eval(args: argparse.Namespace) -> int:
    """Точность ИИ на серой зоне и стоимость. --dry-run — только оценка без вызовов API."""
    import asyncio
    import json

    from app.config import get_settings
    from app.core.classify.llm import (
        PROMPT_PATH,
        LlmClassifier,
        build_payload,
        cost_of,
        gray_blocks,
        make_client,
    )

    settings = get_settings()
    corpus = Path(args.corpus)
    files = sorted((corpus / "raw").glob("*.docx"))
    low, high = settings.classify_llm_threshold, settings.classify_accept_threshold
    system_chars = len(PROMPT_PATH.read_text(encoding="utf-8"))
    total_cost, total_blocks, ok, n_gold = 0.0, 0, 0, 0
    key = settings.openai_api_key.get_secret_value()
    if not args.dry_run and not key:
        print("OPENAI_API_KEY не задан — запустите с --dry-run для оценки стоимости")
        return 2
    for f in files:
        _, ir = load_ir(f)
        gray = gray_blocks(ir, low, high)[: settings.llm_max_blocks]
        if not gray:
            continue
        total_blocks += len(gray)
        if args.dry_run:
            chars = system_chars + len(json.dumps(build_payload(ir, gray), ensure_ascii=False))
            est = cost_of(settings.llm_model, chars // 2, 300 + 60 * len(gray))
            total_cost += est
            print(f"{f.name[:45]:45} блоков {len(gray):3}  ≈ ${est:.4f}")
            continue
        clf = LlmClassifier(
            make_client(key, settings.llm_timeout_s),
            settings.llm_model,
            settings.llm_escalation_model,
            max_blocks=settings.llm_max_blocks,
            max_cost_usd=settings.llm_max_cost_per_doc_usd,
        )
        res = asyncio.run(clf.classify(ir, low, high))
        total_cost += res.cost_usd
        ann_path = corpus / "annotated" / f"{f.stem}.json"
        gold = {}
        if ann_path.exists():
            ann = json.loads(ann_path.read_text(encoding="utf-8"))
            if ann.get("confirmed"):
                gold = {b["id"]: (b["type"], b["level"]) for b in ann["blocks"]}
        for bid, lab in res.labels.items():
            if bid in gold:
                n_gold += 1
                t, lvl = gold[bid]
                ok += int(lab.type == t and (t != "heading" or (lab.level or None) == lvl))
        print(
            f"{f.name[:45]:45} блоков {len(gray):3}  ${res.cost_usd:.4f}  вызовов {len(res.calls)}"
        )
    docs = len(files) or 1
    print(
        f"Итого блоков серой зоны: {total_blocks}, стоимость ${total_cost:.4f}, "
        f"в среднем ${total_cost / docs:.4f} на документ"
    )
    if n_gold:
        print(f"Точность ИИ на серой зоне: {ok / n_gold:.1%} ({ok}/{n_gold})")
    elif not args.dry_run:
        print("Точность не посчитана: нет подтверждённой разметки (annotate --apply)")
    return 0


def cmd_legal_docs(args: argparse.Namespace) -> int:
    """Готовые .docx оферты, политики ПДн и правил с реквизитами из .env."""
    from app.config import get_settings
    from app.services.legal_docs import all_files

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, data in all_files(get_settings()):
        (out / name).write_bytes(data)
        print(out / name)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.core.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("dump", help="типы блоков документа")
    p.add_argument("file")
    p.add_argument("--all", action="store_true")
    p.add_argument("--width", type=int, default=90)
    p.set_defaults(func=cmd_dump)
    p = sub.add_parser("analyze", help="предварительный отчёт (без ИИ)")
    p.add_argument("files", nargs="+")
    p.add_argument("--profile", default="gost_7_32_2017")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_analyze)
    p = sub.add_parser("annotate", help="черновая разметка корпуса и список для проверки")
    p.add_argument("--corpus", default="tests/corpus")
    p.add_argument("--apply", help="review.csv с исправлениями владельца")
    p.set_defaults(func=cmd_annotate)
    p = sub.add_parser("eval", help="метрики классификации по подтверждённой разметке")
    p.add_argument("--corpus", default="tests/corpus")
    p.set_defaults(func=cmd_eval)
    p = sub.add_parser("build-template", help="template.docx из profile.yaml")
    p.add_argument("profile", help="id профиля или all")
    p.set_defaults(func=cmd_build_template)
    p = sub.add_parser("format", help="оформить документы")
    p.add_argument("files", nargs="+")
    p.add_argument("--profile", default="gost_7_32_2017")
    p.add_argument("--out", default="out")
    p.add_argument("--no-render", action="store_true", help="без LibreOffice")
    p.add_argument("--show", type=int, default=5)
    p.add_argument("--summary", help="JSON со сводкой по файлам")
    p.add_argument("--keep-failed", action="store_true", help="сохранить и не прошедшие проверки")
    p.set_defaults(func=cmd_format)
    p = sub.add_parser("llm-eval", help="точность и стоимость ИИ на серой зоне")
    p.add_argument("--corpus", default="tests/corpus")
    p.add_argument("--dry-run", action="store_true", help="только оценка, без вызовов API")
    p.set_defaults(func=cmd_llm_eval)
    p = sub.add_parser("legal-docs", help="готовые .docx юридических документов")
    p.add_argument("--out", default="docs/legal/ready")
    p.set_defaults(func=cmd_legal_docs)
    p = sub.add_parser("compare", help="исходник и результат рядом в одном PDF")
    p.add_argument("source")
    p.add_argument("result")
    p.add_argument("--out", default="compare.pdf")
    p.set_defaults(func=cmd_compare)
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
