#!/usr/bin/env python3
"""Generate synthetic LaTeX, source tarball, and PDF for every seed CIR.

Usage::

    uv run python scripts/build-seed-pdfs.py [--force]

Inputs: every ``seed/*.cir.json`` (other than the whitepaper which is
compiled from the real .tex in the rrxiv repo).

Outputs (idempotent — skipped if present unless ``--force``):

- ``seed/<paper>.tex``           — synthetic LaTeX rendered from the CIR
- ``seed/<paper>.source.tar.gz`` — tarball containing the .tex + rrxiv.cls
- ``seed/<paper>.pdf``           — tectonic-compiled output

The synthetic LaTeX uses the rrxiv document class. It carries title /
authors / abstract / claims / topics so the seed PDFs are recognisably
the same paper as the CIR. This is good enough for a credible demo
without forging external citations.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_DIR = REPO_ROOT / "seed"
CLS_SOURCE = REPO_ROOT.parent / "rrxiv" / "template" / "rrxiv.cls"


def _latex_escape(text: str) -> str:
    """Escape user-supplied text for LaTeX body."""
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
        "<": r"\textless{}",
        ">": r"\textgreater{}",
        '"': "''",
    }
    out: list[str] = []
    for ch in text:
        out.append(replacements.get(ch, ch))
    return "".join(out)


def _format_authors(authors: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for author in authors:
        name = author.get("name") or ""
        if not name:
            continue
        if author.get("is_agent"):
            parts.append(f"{_latex_escape(name)} (agent)")
        else:
            parts.append(_latex_escape(name))
    if not parts:
        return "rrxiv"
    if len(parts) == 1:
        return parts[0]
    return r" \and ".join(parts)


def _render_claim_block(claim: dict[str, Any], index: int) -> str:
    statement = _latex_escape(claim.get("statement") or "")
    status = claim.get("replication_status") or "untested"
    label = f"claim:c{index}"
    name = f"Claim {index}"
    return (
        f"\\begin{{claim}}[{name}]\n"
        f"\\label{{{label}}}\n"
        f"{statement}\n\n"
        f"\\emph{{Replication status: {_latex_escape(status)}.}}\n"
        f"\\end{{claim}}\n"
    )


def _render_latex(cir: dict[str, Any]) -> str:
    title = _latex_escape(cir.get("title") or "Untitled")
    authors = _format_authors(cir.get("authors") or [])
    abstract = _latex_escape(cir.get("abstract") or "")
    submitted_at = (cir.get("submitted_at") or "")[:10]
    topics = ",".join(_latex_escape(t) for t in (cir.get("topics") or []))
    rrxiv_id = cir.get("id_slug") or cir.get("id") or "rrxiv-seed"
    version = cir.get("version") or "v1"
    license_ = cir.get("license") or "CC-BY-4.0"

    claims = cir.get("claims") or []
    claim_blocks = "\n".join(
        _render_claim_block(c, i + 1) for i, c in enumerate(claims)
    )

    return (
        "\\documentclass{rrxiv}\n"
        f"\\rrxivid{{{_latex_escape(rrxiv_id)}}}\n"
        f"\\rrxivversion{{{_latex_escape(version)}}}\n"
        "\\rrxivprotocolversion{0.1.0}\n"
        f"\\rrxivlicense{{{_latex_escape(license_)}}}\n"
        f"\\rrxivtopics{{{topics}}}\n\n"
        f"\\title{{{title}}}\n"
        f"\\author{{{authors}}}\n"
        f"\\date{{{_latex_escape(submitted_at) or '2026-05-04'}}}\n\n"
        "\\begin{document}\n"
        "\\maketitle\n\n"
        "\\begin{abstract}\n"
        f"{abstract}\n"
        "\\end{abstract}\n\n"
        "\\section{Claims}\n"
        f"{claim_blocks}\n"
        "\\section{Notes}\n"
        "This document is a synthetic render of the canonical CIR for "
        "demonstration purposes. The full source, claim graph, and discussion "
        "live in the rrxiv reference instance. See the \\texttt{rrxiv} "
        "protocol repository for the schema.\n\n"
        "\\end{document}\n"
    )


def _write_tex(out_dir: Path, slug: str, tex_source: str) -> Path:
    tex_path = out_dir / f"{slug}.tex"
    tex_path.write_text(tex_source, encoding="utf-8")
    return tex_path


def _bundle_source_tar(out_dir: Path, slug: str, tex_path: Path) -> Path:
    """Bundle the .tex + rrxiv.cls into <slug>.source.tar.gz."""
    out = out_dir / f"{slug}.source.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(tex_path, arcname=f"{slug}/{tex_path.name}")
        if CLS_SOURCE.is_file():
            tar.add(CLS_SOURCE, arcname=f"{slug}/rrxiv.cls")
    return out


def _compile_tex(out_dir: Path, slug: str, tex_path: Path) -> Path:
    """Compile <slug>.tex with tectonic.  Returns path to the produced .pdf."""
    # Tectonic emits next to the input .tex by default. We provide a
    # working dir that also contains rrxiv.cls.
    work_dir = out_dir / f"_build-{slug}"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tex_path, work_dir / f"{slug}.tex")
    if CLS_SOURCE.is_file():
        shutil.copy2(CLS_SOURCE, work_dir / "rrxiv.cls")
    try:
        subprocess.run(
            ["tectonic", "-X", "compile", f"{slug}.tex", "--outdir", "."],
            cwd=work_dir,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(
            f"tectonic failed for {slug}: "
            + (exc.stderr.decode("utf-8", errors="replace") if exc.stderr else "")
            + "\n"
        )
        raise
    pdf_src = work_dir / f"{slug}.pdf"
    pdf_dst = out_dir / f"{slug}.pdf"
    shutil.copy2(pdf_src, pdf_dst)
    shutil.rmtree(work_dir)
    return pdf_dst


def _seed_slug(cir_path: Path) -> str:
    # Filename is <slug>.cir.json.
    name = cir_path.name
    assert name.endswith(".cir.json"), f"unexpected CIR filename: {name}"
    return name[: -len(".cir.json")]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-build artefacts even if they already exist.",
    )
    parser.add_argument(
        "--skip-pdf",
        action="store_true",
        help="Skip the tectonic compilation step (useful for CI without TeX).",
    )
    args = parser.parse_args()

    if not SEED_DIR.is_dir():
        sys.stderr.write(f"seed dir not found: {SEED_DIR}\n")
        return 2
    if not CLS_SOURCE.is_file():
        sys.stderr.write(
            f"rrxiv.cls not found at {CLS_SOURCE} — workspace layout?"
            "\n"
        )
        return 2

    cirs = sorted(SEED_DIR.glob("*.cir.json"))
    if not cirs:
        sys.stderr.write(f"no CIRs in {SEED_DIR}\n")
        return 2

    built = 0
    skipped = 0
    failed: list[str] = []
    for cir_path in cirs:
        slug = _seed_slug(cir_path)
        # Whitepaper already has a real PDF compiled from the canonical source.
        if slug == "rrxiv-whitepaper":
            skipped += 1
            continue
        pdf_path = SEED_DIR / f"{slug}.pdf"
        src_path = SEED_DIR / f"{slug}.source.tar.gz"
        if (
            pdf_path.exists()
            and src_path.exists()
            and not args.force
        ):
            skipped += 1
            continue
        try:
            with cir_path.open("r", encoding="utf-8") as fh:
                cir = json.load(fh)
            tex_source = _render_latex(cir)
            tex_path = _write_tex(SEED_DIR, slug, tex_source)
            _bundle_source_tar(SEED_DIR, slug, tex_path)
            if not args.skip_pdf:
                _compile_tex(SEED_DIR, slug, tex_path)
            built += 1
            print(f"built {slug}")
        except (subprocess.CalledProcessError, OSError) as exc:
            failed.append(slug)
            print(f"FAILED {slug}: {exc}", file=sys.stderr)

    print(f"done. built={built} skipped={skipped} failed={len(failed)}")
    if failed:
        print("failed: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
