"""LaTeX and cover letter to PDF (local tools)."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def compile_latex_to_pdf(tex_content: str, out_dir: Path, stem: str = "resume") -> Path:
    """Write stem.tex under out_dir, compile to stem.pdf; return path to PDF."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / f"{stem}.tex"
    tex_path.write_text(tex_content, encoding="utf-8")
    pdf_path = out_dir / f"{stem}.pdf"

    latexmk = shutil.which("latexmk")
    if latexmk:
        proc = subprocess.run(
            [
                latexmk,
                "-pdf",
                "-interaction=nonstopmode",
                f"-outdir={out_dir}",
                str(tex_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(out_dir),
            timeout=300,
        )
        if proc.returncode != 0 or not pdf_path.is_file():
            err = (proc.stderr or proc.stdout or "")[:4000]
            logger.error("latexmk failed: %s", err)
            raise RuntimeError("LaTeX compile failed; see logs for stderr.")
        return pdf_path

    pdflatex = shutil.which("pdflatex")
    if not pdflatex:
        raise RuntimeError(
            "Neither latexmk nor pdflatex found on PATH; install a TeX distribution."
        )
    for _ in range(2):
        proc = subprocess.run(
            [
                pdflatex,
                "-interaction=nonstopmode",
                f"-output-directory={out_dir}",
                str(tex_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(out_dir),
            timeout=300,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "")[:4000]
            logger.error("pdflatex failed: %s", err)
            raise RuntimeError("LaTeX compile failed; see logs for stderr.")
    if not pdf_path.is_file():
        raise RuntimeError("pdflatex did not produce PDF.")
    return pdf_path


def cover_to_pdf(text: str, out_pdf: Path) -> Path:
    """Write cover letter to PDF using pandoc; raises if pandoc missing."""
    pandoc = shutil.which("pandoc")
    if not pandoc:
        raise RuntimeError(
            "pandoc not found on PATH; install pandoc to build cover letter PDFs."
        )
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        encoding="utf-8",
        delete=False,
    ) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    try:
        proc = subprocess.run(
            [pandoc, str(tmp_path), "-o", str(out_pdf)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0 or not out_pdf.is_file():
            err = (proc.stderr or proc.stdout or "")[:2000]
            raise RuntimeError(f"pandoc failed: {err}")
    finally:
        tmp_path.unlink(missing_ok=True)
    return out_pdf
