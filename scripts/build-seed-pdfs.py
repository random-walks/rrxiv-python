#!/usr/bin/env python3
"""Generate synthetic LaTeX, source tarball, and PDF for every seed CIR.

Usage::

    uv run python scripts/build-seed-pdfs.py [--force] [--seed-dir DIR]

Inputs: every ``*.cir.json`` in the seed dir (defaults to ``seed/``),
other than the rrxiv-whitepaper which is compiled from real source in
the rrxiv repo.

Outputs (idempotent --- skipped if present unless ``--force``):

- ``<paper>.tex``           synthetic LaTeX rendered from the CIR
- ``<paper>.source.tar.gz`` tarball containing the .tex + rrxiv.cls
- ``<paper>.pdf``           tectonic-compiled output

The synthetic LaTeX uses the rrxiv document class and synthesises a
multi-page paper-shaped document (introduction, methodology, per-claim
results section, discussion, references) from the CIR's title /
authors / abstract / claims / topics fields. The result is clearly
marked as a demo fixture --- no claim of original research --- but
reads as a credible paper rather than a CIR dump.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEED_DIR = REPO_ROOT / "seed"
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


# ---- Body-text synthesis from CIR fields --------------------------------

_CLAIM_TYPE_INTRO = {
    "empirical": "an empirical observation supported by data",
    "theoretical": "a theoretical claim derived from formal reasoning",
    "methodological": "a methodological proposal",
    "review": "a synthesis of prior work",
}

_EVIDENCE_TYPE_INTRO = {
    "experimental": "experimental evidence from controlled trials",
    "argument": "a deductive argument from prior results",
    "computational": "computational evidence from simulation or numerical experiment",
    "observational": "observational evidence from naturalistic data",
}

_STATUS_PHRASES = {
    "replicated": "has been independently replicated",
    "contradicted": "has been contradicted by subsequent work",
    "contested": "is currently contested",
    "untested": "has not yet been independently tested",
}


def _topics_sentence(topics: list[str]) -> str:
    """Render a topics list as English prose."""
    topics = [t for t in topics if t]
    if not topics:
        return ""
    escaped = [f"\\texttt{{{_latex_escape(t)}}}" for t in topics]
    if len(escaped) == 1:
        return f"It engages with the topic {escaped[0]}."
    if len(escaped) == 2:
        return f"It engages with the topics {escaped[0]} and {escaped[1]}."
    head = ", ".join(escaped[:-1])
    return f"It engages with the topics {head}, and {escaped[-1]}."


def _scope_phrase(claims: list[dict[str, Any]]) -> str:
    """Synthesise a one-liner describing the claim graph's shape."""
    n = len(claims)
    if n == 0:
        return "This paper carries no machine-readable claims yet."
    statuses: dict[str, int] = {}
    for c in claims:
        s = c.get("replication_status") or "untested"
        statuses[s] = statuses.get(s, 0) + 1
    pretty = ", ".join(f"{v} {k}" for k, v in sorted(statuses.items()))
    return (
        f"The encoding registers {n} formal claim"
        f"{'s' if n != 1 else ''} ({pretty})."
    )


def _intro_paragraphs(cir: dict[str, Any]) -> str:
    abstract = (cir.get("abstract") or "").strip()
    topics = cir.get("topics") or []
    claims = cir.get("claims") or []
    topics_line = _topics_sentence(topics)
    scope = _scope_phrase(claims)

    paragraphs: list[str] = []

    # First paragraph: re-state the abstract as prose.
    if abstract:
        paragraphs.append(_latex_escape(abstract))

    # Second paragraph: orientation. What kind of paper this is, what
    # it claims, where it sits in the rrxiv corpus.
    paragraphs.append(
        "This document is a structured encoding of the paper in the "
        "\\texttt{rrxiv} protocol's Canonical Intermediate Representation "
        f"(CIR). {topics_line} {scope} Each claim is annotated with its "
        "claim type, evidence type, and current replication status; "
        "dependency edges between claims, when present, form a "
        "machine-readable proof DAG."
    )

    return "\n\n".join(paragraphs)


def _methodology_paragraph(cir: dict[str, Any]) -> str:
    return (
        "We follow the \\texttt{rrxiv} convention of separating "
        "\\emph{claims} (the proposition under consideration) from "
        "\\emph{evidence} (the argument or data supporting it). Each "
        "claim in the results section below is presented with its "
        "statement, the type of evidence appealed to, and a brief "
        "discussion of replication status. Where claims depend on prior "
        "results --- internal or external --- the dependency is recorded "
        "in the CIR as a \\texttt{\\textbackslash dependson} edge, so "
        "the full inferential structure is machine-traversable. "
        "Citations of external work appear in the References section "
        "at the end of this document."
    )


def _per_claim_discussion(claim: dict[str, Any]) -> str:
    claim_type = (claim.get("claim_type") or "").lower()
    evidence_type = (claim.get("evidence_type") or "").lower()
    status = (claim.get("replication_status") or "untested").lower()

    pieces: list[str] = []
    type_phrase = _CLAIM_TYPE_INTRO.get(claim_type)
    ev_phrase = _EVIDENCE_TYPE_INTRO.get(evidence_type)
    if type_phrase and ev_phrase:
        pieces.append(
            f"This claim is {type_phrase}, supported by {ev_phrase}."
        )
    elif type_phrase:
        pieces.append(f"This claim is {type_phrase}.")
    elif ev_phrase:
        pieces.append(f"The claim is supported by {ev_phrase}.")
    status_phrase = _STATUS_PHRASES.get(status)
    if status_phrase:
        pieces.append(f"As of the encoding date, it {status_phrase}.")
    deps = claim.get("depends_on") or []
    if deps:
        pieces.append(
            f"It depends on {len(deps)} prior claim"
            f"{'s' if len(deps) != 1 else ''} in the same paper."
        )
    return " ".join(pieces) if pieces else ""


def _render_claim_block(claim: dict[str, Any], index: int) -> str:
    statement = _latex_escape(claim.get("statement") or "")
    status = claim.get("replication_status") or "untested"
    label = f"claim:c{index}"
    name = f"Claim {index}"
    discussion = _per_claim_discussion(claim)
    return (
        f"\\subsection*{{Claim {index}}}\n"
        f"\\begin{{claim}}[{name}]\n"
        f"\\label{{{label}}}\n"
        f"{statement}\n\n"
        f"\\emph{{Replication status: {_latex_escape(status)}.}}\n"
        f"\\end{{claim}}\n"
        f"{discussion}\n"
    )


def _references_section(cir: dict[str, Any]) -> str:
    cits = cir.get("citations") or []
    if not cits:
        slug = cir.get("id_slug") or cir.get("id") or "rrxiv-seed"
        return (
            "\\section{References}\n"
            "No external citations are recorded in this paper's CIR. "
            "The canonical machine-readable version of this document, "
            "including any future-added citations and claim-graph "
            "edges, is available at "
            f"\\href{{https://rrxiv.com/papers/{_latex_escape(slug)}}}"
            f"{{rrxiv.com/papers/{_latex_escape(slug)}}}.\n"
        )
    items: list[str] = []
    for c in cits:
        text = c.get("formatted") or c.get("title") or c.get("doi") or ""
        if text:
            items.append(f"\\item {_latex_escape(text)}")
    body = (
        "\\begin{itemize}[leftmargin=*]\n"
        + "\n".join(items)
        + "\n\\end{itemize}"
    )
    return f"\\section{{References}}\n{body}\n"


def _demo_banner(cir: dict[str, Any]) -> str:
    """Visible note explaining this is a demo render, not a research paper."""
    slug = cir.get("id_slug") or cir.get("id") or "rrxiv-seed"
    return (
        "\\begin{center}\n"
        "\\small\\itshape\n"
        "Demonstration paper in the rrxiv reference corpus. "
        "The canonical machine-readable version lives at "
        f"\\href{{https://rrxiv.com/papers/{_latex_escape(slug)}}}"
        f"{{rrxiv.com/papers/{_latex_escape(slug)}}}.\n"
        "\\end{center}\n\n"
    )


def _render_latex(cir: dict[str, Any]) -> str:
    title = _latex_escape(cir.get("title") or "Untitled")
    authors = _format_authors(cir.get("authors") or [])
    submitted_at = (cir.get("submitted_at") or "")[:10]
    topics = ",".join(_latex_escape(t) for t in (cir.get("topics") or []))
    rrxiv_id = cir.get("id_slug") or cir.get("id") or "rrxiv-seed"
    version = cir.get("version") or "v1"
    license_ = cir.get("license") or "CC-BY-4.0"
    abstract = _latex_escape(cir.get("abstract") or "")

    claims = cir.get("claims") or []
    claim_blocks = "\n".join(
        _render_claim_block(c, i + 1) for i, c in enumerate(claims)
    )

    intro = _intro_paragraphs(cir)
    methodology = _methodology_paragraph(cir)
    references = _references_section(cir)
    banner = _demo_banner(cir)

    discussion = (
        "\\section{Discussion}\n"
        "The claim graph above is the primary product of this paper. "
        "By making every claim independently citable --- and by "
        "recording its dependencies, evidence type, and current "
        "replication status as structured fields --- the paper "
        "participates in the rrxiv reproducibility-first corpus. "
        "Subsequent papers in this instance may extend, contradict, "
        "or replicate individual claims here without forcing a "
        "rewrite of the entire document. See the canonical version "
        "online for the live discourse layer.\n"
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
        f"{banner}"
        "\\begin{abstract}\n"
        f"{abstract}\n"
        "\\end{abstract}\n\n"
        "\\section{Introduction}\n"
        f"{intro}\n\n"
        "\\section{Methodology}\n"
        f"{methodology}\n\n"
        "\\section{Results: registered claims}\n"
        f"{claim_blocks}\n"
        f"{discussion}\n"
        f"{references}"
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
    parser.add_argument(
        "--seed-dir",
        type=Path,
        default=DEFAULT_SEED_DIR,
        help="Directory containing *.cir.json files (default: ./seed/).",
    )
    parser.add_argument(
        "--skip-slugs",
        nargs="*",
        default=["rrxiv-whitepaper", "euclid-elements"],
        help=(
            "Slugs to skip (real papers already have their own source). "
            "Default: rrxiv-whitepaper euclid-elements."
        ),
    )
    args = parser.parse_args()

    seed_dir: Path = args.seed_dir
    if not seed_dir.is_dir():
        sys.stderr.write(f"seed dir not found: {seed_dir}\n")
        return 2
    if not CLS_SOURCE.is_file():
        sys.stderr.write(
            f"rrxiv.cls not found at {CLS_SOURCE} --- workspace layout?\n"
        )
        return 2

    cirs = sorted(seed_dir.glob("*.cir.json"))
    if not cirs:
        sys.stderr.write(f"no CIRs in {seed_dir}\n")
        return 2

    built = 0
    skipped = 0
    failed: list[str] = []
    for cir_path in cirs:
        slug = _seed_slug(cir_path)
        if slug in args.skip_slugs:
            skipped += 1
            continue
        pdf_path = seed_dir / f"{slug}.pdf"
        src_path = seed_dir / f"{slug}.source.tar.gz"
        if pdf_path.exists() and src_path.exists() and not args.force:
            skipped += 1
            continue
        try:
            with cir_path.open("r", encoding="utf-8") as fh:
                cir = json.load(fh)
            tex_source = _render_latex(cir)
            tex_path = _write_tex(seed_dir, slug, tex_source)
            _bundle_source_tar(seed_dir, slug, tex_path)
            if not args.skip_pdf:
                _compile_tex(seed_dir, slug, tex_path)
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
