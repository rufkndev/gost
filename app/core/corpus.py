"""Разметка корпуса и метрики классификации (spec, раздел 11).

Порядок работы:
1. `annotate` — черновая разметка правилами: annotated/<имя>.json + review.csv.
2. Владелец открывает review.csv (Excel), заполняет колонки correct_type/correct_level
   там, где тип неверный, и ставит «да» в колонке checked у проверенных файлов.
3. `annotate --apply review.csv` — исправления попадают в JSON, файл помечается confirmed.
4. `eval` — метрики по подтверждённым файлам (черновики считаются отдельно).
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.ir import BlockType, DocumentIR

SKIP = {BlockType.EMPTY, BlockType.PAGE_BREAK, BlockType.SECTION_BREAK}
REVIEW_COLUMNS = [
    "file",
    "id",
    "page",
    "type",
    "level",
    "confidence",
    "text",
    "correct_type",
    "correct_level",
    "checked",
]


def _preview(text: str, words: int = 12) -> str:
    parts = text.split()
    return " ".join(parts[:words]) + (" …" if len(parts) > words else "")


def draft_annotation(name: str, ir: DocumentIR) -> dict[str, Any]:
    blocks = []
    for b in ir.blocks:
        if b.type in SKIP:
            continue
        blocks.append(
            {
                "id": b.id,
                "type": b.type.value,
                "level": b.level,
                "confidence": b.confidence,
                "page": b.page_estimate,
                "text": _preview(b.text or str(b.features.get("text_preview", ""))),
            }
        )
    return {"file": name, "confirmed": False, "blocks": blocks}


def write_review_csv(annotations: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(REVIEW_COLUMNS)
        for ann in annotations:
            for b in ann["blocks"]:
                w.writerow(
                    [
                        ann["file"],
                        b["id"],
                        b["page"],
                        b["type"],
                        b["level"] or "",
                        f"{b['confidence']:.2f}",
                        b["text"],
                        "",
                        "",
                        "",
                    ]
                )


def apply_review(annotated_dir: Path, review: Path) -> dict[str, int]:
    """Переносит исправления владельца в JSON. Возвращает число исправлений по файлам."""
    fixes: dict[str, dict[str, tuple[str, int | None]]] = defaultdict(dict)
    checked: set[str] = set()
    valid = {t.value for t in BlockType}
    with review.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh, delimiter=";"):
            if (row.get("checked") or "").strip().lower() in ("да", "yes", "1", "+"):
                checked.add(row["file"])
            ct = (row.get("correct_type") or "").strip()
            if ct:
                if ct not in valid:
                    raise ValueError(f"{row['file']} {row['id']}: unknown type {ct!r}")
                cl = (row.get("correct_level") or "").strip()
                fixes[row["file"]][row["id"]] = (ct, int(cl) if cl else None)
    stats: dict[str, int] = {}
    for path in sorted(annotated_dir.glob("*.json")):
        ann = json.loads(path.read_text(encoding="utf-8"))
        n = 0
        for b in ann["blocks"]:
            fix = fixes.get(ann["file"], {}).get(b["id"])
            if fix:
                b["type"], b["level"] = fix
                b["corrected"] = True
                n += 1
        if ann["file"] in checked:
            ann["confirmed"] = True
        path.write_text(json.dumps(ann, ensure_ascii=False, indent=1), encoding="utf-8")
        stats[ann["file"]] = n
    return stats


@dataclass
class EvalResult:
    files_confirmed: int = 0
    files_draft: int = 0
    total: int = 0
    correct: int = 0
    per_type: dict[str, list[int]] = field(default_factory=dict)  # тип → [верно, всего]
    gray: int = 0
    errors_confident: list[str] = field(default_factory=list)  # ошибки при conf ≥ порога
    errors_caught: int = 0  # ошибки, попавшие в «Проверьте вручную»
    errors_total: int = 0

    def to_text(self) -> str:
        lines = [
            f"Файлов: подтверждённых {self.files_confirmed}, черновых {self.files_draft}"
            " (черновые в метрики не входят)",
        ]
        if not self.total:
            lines.append("Нет подтверждённой разметки — метрики не считаются.")
            return "\n".join(lines)
        lines.append(
            f"Точность в целом: {self.correct / self.total:.1%} ({self.correct}/{self.total})"
        )
        for t, (ok, n) in sorted(self.per_type.items()):
            lines.append(f"  {t:22} {ok / n:6.1%}  ({ok}/{n})")
        lines.append(f"Серая зона: {self.gray}")
        lines.append(f"Ошибок при уверенности ≥ порога: {len(self.errors_confident)}")
        for e in self.errors_confident[:30]:
            lines.append(f"  {e}")
        if self.errors_total:
            lines.append(
                f"Ошибок поймано отчётом «Проверьте вручную»: "
                f"{self.errors_caught}/{self.errors_total}"
            )
        return "\n".join(lines)


def evaluate(pairs: list[tuple[dict[str, Any], DocumentIR]], threshold: float) -> EvalResult:
    res = EvalResult()
    per: dict[str, Counter[str]] = defaultdict(Counter)
    for ann, ir in pairs:
        if not ann.get("confirmed"):
            res.files_draft += 1
            continue
        res.files_confirmed += 1
        pred = ir.by_id()
        for gold in ann["blocks"]:
            b = pred.get(gold["id"])
            if b is None:
                continue
            res.total += 1
            ok = b.type.value == gold["type"] and (
                gold["type"] != "heading" or b.level == gold["level"]
            )
            per[gold["type"]]["n"] += 1
            if b.confidence < threshold and b.type not in SKIP:
                res.gray += 1
            if ok:
                res.correct += 1
                per[gold["type"]]["ok"] += 1
            else:
                res.errors_total += 1
                if b.confidence >= threshold:
                    res.errors_confident.append(
                        f"{ann['file']} {b.id}: {b.label()} вместо {gold['type']}"
                        f"{gold['level'] or ''} ({b.confidence:.2f})"
                    )
                else:
                    res.errors_caught += 1
    res.per_type = {t: [c["ok"], c["n"]] for t, c in per.items()}
    return res
