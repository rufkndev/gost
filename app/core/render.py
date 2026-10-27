"""LibreOffice headless: PDF для номеров страниц в оглавлении и визуальной проверки.

LibreOffice запускается с отдельным профилем на каждый вызов, с таймаутом.
Пагинация LibreOffice может отличаться от Word на ±1 страницу, поэтому в settings.xml
выставляется updateFields: Word пересчитает оглавление при открытии.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf._page import PageObject


class RenderError(Exception):
    pass


def soffice_path() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def soffice_available() -> bool:
    return soffice_path() is not None


def render_pdf(docx: bytes, timeout_s: int = 90) -> bytes:
    exe = soffice_path()
    if exe is None:
        raise RenderError("soffice_not_found")
    with tempfile.TemporaryDirectory(prefix="gost_lo_") as tmp:
        d = Path(tmp)
        src = d / "doc.docx"
        src.write_bytes(docx)
        env = {**os.environ, "HOME": str(d)}
        cmd = [
            exe,
            f"-env:UserInstallation=file://{d / 'profile'}",
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nodefault",
            "--convert-to",
            "pdf",
            "--outdir",
            str(d),
            str(src),
        ]
        try:
            subprocess.run(  # noqa: S603 — аргументы формируем сами, без shell
                cmd,
                cwd=d,
                env=env,
                timeout=timeout_s,
                capture_output=True,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RenderError("soffice_timeout") from exc
        out = d / "doc.pdf"
        if not out.exists():
            raise RenderError("soffice_failed")
        return out.read_bytes()


def pdf_pages_text(pdf: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(pdf))
    return [(p.extract_text() or "") for p in reader.pages]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def find_heading_pages(pdf: bytes, entries: list[tuple[str, str]]) -> dict[str, int]:
    """Номер страницы для каждой записи оглавления (поиск по порядку после оглавления)."""
    pages = [_norm(t) for t in pdf_pages_text(pdf)]
    if not pages or not entries:
        return {}
    # Страницы оглавления: на них много отточий.
    toc_pages = [i for i, t in enumerate(pages) if t.count("....") >= 2]
    start = (max(toc_pages) + 1) if toc_pages else 0
    out: dict[str, int] = {}
    cur = start
    for bm, text in entries:
        key = _norm(text)[:60]
        if not key:
            continue
        for i in range(cur, len(pages)):
            if key in pages[i]:
                out[bm] = i + 1
                cur = i
                break
    return out


def side_by_side(left_pdf: bytes, right_pdf: bytes) -> bytes:
    """Склеивает два PDF постранично: слева исходник, справа результат."""
    left = PdfReader(io.BytesIO(left_pdf)).pages
    right = PdfReader(io.BytesIO(right_pdf)).pages
    writer = PdfWriter()
    for i in range(max(len(left), len(right))):
        lp = left[i] if i < len(left) else None
        rp = right[i] if i < len(right) else None
        ref = lp or rp
        if ref is None:
            continue
        w = float(ref.mediabox.width)
        h = float(ref.mediabox.height)
        page = PageObject.create_blank_page(width=w * 2 + 20, height=h)
        if lp is not None:
            page.merge_page(lp)
        if rp is not None:
            page.merge_transformed_page(rp, Transformation().translate(w + 20, 0))
        writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
