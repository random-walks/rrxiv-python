"""rrxiv seed-store — bulk-load CIRs into a store, bypassing /submissions.

Used to seed the canonical instance with a small corpus of papers
without going through the auth + signature path. Idempotent on
rebuild: papers with the same id are upserted.

Usage::

    rrxiv seed-store --store sqlite:////data/rrxiv.db --from ./seed/

Where ``./seed/`` contains either:

  - One JSON-per-paper layout (flat):

        seed/
          rrxiv-whitepaper.cir.json
          rrxiv-whitepaper.source.tar.gz  (optional)
          paper-002.cir.json
          ...

  - Or one subdirectory per paper:

        seed/
          rrxiv-whitepaper/
            cir.json
            source.tar.gz   (optional)

Both layouts are accepted. Each CIR is split into a paper-metadata
record + claims; the sources tarball is persisted if present.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from rrxiv.server.papers.slug import is_slug, mint_slug
from rrxiv.server.store import store_from_url

seed_app = typer.Typer(no_args_is_help=False)


def _today_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iter_cir_files(root: Path) -> list[Path]:
    """Discover all CIR JSON files under ``root``.

    Three layouts are recognised:

    1. **Flat** — ``foo.cir.json`` in ``root``. Used by the in-repo seed
       corpus.
    2. **Subdir** — ``foo/cir.json``. Used by older test fixtures.
    3. **Paper-repo** — ``root`` is itself a paper repo following the
       ``rrxiv-paper-template`` convention (``paper/main.tex`` +
       ``build/main.cir.json`` + ``rrxiv-meta.json``). The build output
       is the canonical CIR; the loader falls back to building it from
       ``rrxiv-meta.json`` if ``build/main.cir.json`` is missing (this
       lets a fresh clone be ingested without first running tectonic
       locally, at the cost of a meta-only CIR with no claims).
    """
    found: list[Path] = []
    # Paper-repo layout — recognised by sibling files.
    if (root / "paper" / "main.tex").is_file() and (root / "rrxiv-meta.json").is_file():
        built = root / "build" / "main.cir.json"
        if built.is_file():
            found.append(built)
            return found
        # No built CIR — synthesise a placeholder from rrxiv-meta.json
        # alongside paper/main.tex. The placeholder is recognised by the
        # seed loader below.
        found.append(root / "rrxiv-meta.json")
        return found

    for path in sorted(root.iterdir()):
        if path.is_file() and path.name.endswith(".cir.json"):
            found.append(path)
        elif path.is_dir() and (path / "cir.json").is_file():
            found.append(path / "cir.json")
        elif (
            path.is_dir()
            and (path / "paper" / "main.tex").is_file()
            and (path / "rrxiv-meta.json").is_file()
        ):
            # Nested paper repos inside `root` (e.g. a `papers/` dir
            # containing many cloned paper repos).
            built = path / "build" / "main.cir.json"
            if built.is_file():
                found.append(built)
            else:
                found.append(path / "rrxiv-meta.json")
    return found


def _is_paper_repo_root(path: Path) -> bool:
    """True when ``path`` looks like a rrxiv-paper-template-shaped repo root."""
    return (path / "paper" / "main.tex").is_file() and (path / "rrxiv-meta.json").is_file()


def _paper_repo_root(cir_or_meta_path: Path) -> Path | None:
    """If the loader was handed a paper-repo CIR or meta, return its root."""
    p = cir_or_meta_path
    # build/main.cir.json case
    if (
        p.name == "main.cir.json"
        and p.parent.name == "build"
        and _is_paper_repo_root(p.parent.parent)
    ):
        return p.parent.parent
    # rrxiv-meta.json case (no built CIR yet)
    if p.name == "rrxiv-meta.json" and _is_paper_repo_root(p.parent):
        return p.parent
    return None


def _sibling_source(cir_path: Path) -> Path | None:
    """Find a source tarball sibling to a CIR file, if any."""
    repo_root = _paper_repo_root(cir_path)
    if repo_root is not None:
        # Paper-repo layout: prefer the built tarball, fall back to
        # tar'ing paper/main.tex + paper/rrxiv.cls on the fly.
        for c in (
            repo_root / "build" / "source.tar.gz",
            repo_root / "build" / "main.source.tar.gz",
        ):
            if c.is_file():
                return c
        return None
    if cir_path.parent.name and cir_path.name == "cir.json":
        # subdir layout
        candidates = [
            cir_path.parent / "source.tar.gz",
            cir_path.parent / "source.tgz",
        ]
    else:
        stem = cir_path.name[: -len(".cir.json")]
        candidates = [
            cir_path.parent / f"{stem}.source.tar.gz",
            cir_path.parent / f"{stem}.tar.gz",
        ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _canonicalise_claim_ids(cir: dict[str, Any], paper_id: str) -> int:
    """Rewrite every claim id / paper_id / edge target / annotation
    target_id that uses the parser's meta-slug prefix to use the
    canonical ``paper_id`` (the CIR's ``id``, which is the UUIDv7 the
    instance keys claims off).

    Why: ``rrxiv parse`` stamps IDs as ``<meta_slug>:<kind>:<label>``
    because at build-time the canonical UUID isn't known. Seed-store
    sees the canonical UUID as ``cir["id"]`` — substitute every
    parser-prefix occurrence accordingly so the deployed instance
    finds the claims (``list_claims_for_paper(paper_id=...)``).

    Idempotent: if the CIR already uses ``paper_id`` as the prefix
    (e.g.\\ a manually-curated demo fixture), this is a no-op.

    Returns the number of substitutions made — useful for both
    progress reporting and tests.
    """
    claims = cir.get("claims") or []
    if not claims:
        return 0

    # All claims from a single parse run share the same prefix. Peek
    # at the first one to discover what the parser emitted.
    sample_id = claims[0].get("id") or ""
    parts = sample_id.split(":", 1)
    if len(parts) != 2:
        return 0
    parser_prefix = parts[0]
    if parser_prefix == paper_id:
        # Already canonical or no prefix to rewrite.
        return 0

    old = parser_prefix + ":"
    new = paper_id + ":"

    def _rewrite(s: str) -> str:
        return new + s[len(old) :] if s.startswith(old) else s

    n = 0
    for c in claims:
        if (cid := c.get("id")) and cid.startswith(old):
            c["id"] = _rewrite(cid)
            n += 1
        if c.get("paper_id") != paper_id:
            c["paper_id"] = paper_id
            n += 1
        for key in ("depends_on", "supports", "contradicts", "extends"):
            edges = c.get(key)
            if not edges:
                continue
            new_edges = [_rewrite(t) for t in edges]
            if new_edges != edges:
                c[key] = new_edges
                n += sum(
                    1 for a, b in zip(edges, new_edges, strict=True) if a != b
                )

    # Annotations targeting claims also need their target_id rewritten.
    # Annotations targeting other papers (cross-paper context) are
    # safe because _rewrite only touches strings starting with the
    # local parser prefix.
    for ann in cir.get("annotations") or []:
        if (tid := ann.get("target_id")) and tid.startswith(old):
            ann["target_id"] = _rewrite(tid)
            n += 1

    return n


def _sibling_pdf(cir_path: Path) -> Path | None:
    """Find a rendered PDF sibling to a CIR file, if any."""
    repo_root = _paper_repo_root(cir_path)
    if repo_root is not None:
        candidate = repo_root / "build" / "main.pdf"
        return candidate if candidate.is_file() else None
    if cir_path.name == "cir.json":
        candidates = [cir_path.parent / "paper.pdf", cir_path.parent / "rendered.pdf"]
    else:
        stem = cir_path.name[: -len(".cir.json")]
        candidates = [
            cir_path.parent / f"{stem}.pdf",
            cir_path.parent / f"{stem}.rendered.pdf",
        ]
    for c in candidates:
        if c.is_file():
            return c
    return None


@seed_app.callback(invoke_without_command=True)
def seed_store_cmd(
    from_: Annotated[
        Path,
        typer.Option(
            "--from",
            "-f",
            help="Directory containing CIR JSON files (and optional source tarballs).",
        ),
    ],
    store_url: Annotated[
        str,
        typer.Option(
            "--store",
            help=(
                "Store URL. Defaults to memory:// (lost on exit). "
                "Use sqlite:///./rrxiv.db for persistence."
            ),
        ),
    ] = "memory://",
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet", "-q", help="Suppress per-file progress output."
        ),
    ] = False,
    reset: Annotated[
        bool,
        typer.Option(
            "--reset",
            help=(
                "Truncate every paper/CIR/claim/annotation/source/PDF "
                "row before re-seeding. Use when claim ids or paper ids "
                "have changed between releases so the store doesn't keep "
                "orphans alongside the new canonical records."
            ),
        ),
    ] = False,
) -> None:
    """Bulk-load CIRs into a Store, bypassing /submissions."""
    if not from_.is_dir():
        typer.secho(f"ERROR: --from path is not a directory: {from_}", fg="red", err=True)
        raise typer.Exit(code=2)

    store = store_from_url(store_url)

    if reset:
        store.clear_corpus()
        if not quiet:
            typer.secho(
                "Cleared existing corpus (papers/CIRs/claims/annotations/sources/PDFs).",
                fg="yellow",
            )

    cir_files = _iter_cir_files(from_)
    if not cir_files:
        typer.secho(
            f"WARNING: no *.cir.json files found in {from_}",
            fg="yellow",
            err=True,
        )
        raise typer.Exit(code=0)

    totals: dict[str, int] = {
        "papers": 0,
        "claims": 0,
        "annotations": 0,
        "sources": 0,
        "pdfs": 0,
        "slugs_minted": 0,
    }

    for cir_path in cir_files:
        per_file = load_cir_into_store(cir_path, store, quiet=quiet)
        if per_file is None:
            continue
        for k, v in per_file.items():
            if k.startswith("_"):
                continue
            totals[k] = totals.get(k, 0) + int(v)
        if not quiet:
            typer.echo(
                f"  load: {cir_path.relative_to(from_)} → "
                f"id={per_file['_paper_id']} slug={per_file['_slug']!r}"
            )

    typer.echo("")
    typer.echo(
        f"Done. papers={totals['papers']} claims={totals['claims']} "
        f"annotations={totals['annotations']} "
        f"sources={totals['sources']} pdfs={totals['pdfs']} "
        f"slugs_minted={totals['slugs_minted']}"
    )


def load_cir_into_store(
    cir_path: Path,
    store: Any,
    *,
    quiet: bool = True,
) -> dict[str, Any] | None:
    """Load a single CIR JSON file (or paper-repo meta) into the store.

    This is the canonical pathway: handles slug minting, paper-repo
    meta promotion, claim/edge canonicalisation, source/PDF persistence,
    and source.uri / source.rendered_pdf_uri rewriting in one place.
    Used by both ``rrxiv seed-store`` and ``rrxiv serve --seed-dir``.

    Returns a dict of counters (papers/claims/annotations/sources/pdfs/
    slugs_minted) plus ``_paper_id`` and ``_slug`` for caller logging,
    or ``None`` if the file was skipped (e.g. missing id).
    """
    with cir_path.open("r", encoding="utf-8") as f:
        cir = json.load(f)

    # If this is a paper-repo's rrxiv-meta.json (no built CIR yet),
    # promote it into a minimal CIR-shaped dict. The paper is
    # ingested with no claims/annotations until the user runs the
    # paper's own scripts/extract-cir.sh and reseeds.
    if cir_path.name == "rrxiv-meta.json":
        paper_id = cir.get("id") or cir.get("id_slug")
        if not paper_id:
            # Use the repo dir name as a deterministic placeholder.
            paper_id = cir_path.parent.name
        cir.setdefault("id", paper_id)
        cir.setdefault("claims", [])
        cir.setdefault("annotations", [])
        cir.setdefault("citations", [])
        cir.setdefault("sections", [])
        cir.setdefault("figures", [])
        cir.setdefault("source", {"format": "latex", "uri": None})
        cir.setdefault("submitted_at", _today_iso())

    paper_id = cir.get("id")
    if not paper_id:
        if not quiet:
            typer.secho(
                f"  skip: {cir_path.name} (missing id)", fg="yellow"
            )
        return None

    slugs_minted = 0
    if not cir.get("id_slug"):
        cir["id_slug"] = mint_slug(store)
        slugs_minted = 1
    elif not is_slug(cir["id_slug"]) and not quiet:
        typer.secho(
            f"  warn: {cir_path.name} has malformed id_slug "
            f"'{cir['id_slug']}' — will not match pattern",
            fg="yellow",
        )

    # Canonicalise parser-stamped meta-slug prefixes to canonical UUIDs.
    rewrites = _canonicalise_claim_ids(cir, paper_id)
    if rewrites and not quiet:
        typer.echo(
            f"    canonicalised {rewrites} id/paper_id/edge references"
        )

    # Persist source archive + PDF first so we can stamp their API
    # URIs onto the CIR/paper record before saving.
    sources_added = 0
    src_path = _sibling_source(cir_path)
    if src_path is not None:
        source_uri = store.save_source(paper_id, src_path.read_bytes())
        cir.setdefault("source", {})
        cir["source"]["uri"] = source_uri
        cir["source"].setdefault("format", "latex")
        sources_added = 1

    pdfs_added = 0
    pdf_path = _sibling_pdf(cir_path)
    if pdf_path is not None:
        pdf_uri = store.save_rendered_pdf(paper_id, pdf_path.read_bytes())
        cir.setdefault("source", {})
        cir["source"]["rendered_pdf_uri"] = pdf_uri
        cir["source"].setdefault("format", "latex")
        pdfs_added = 1

    paper_metadata = {
        k: v
        for k, v in cir.items()
        if k not in ("claims", "citations", "annotations", "sections", "figures")
    }
    store.add_paper(paper_metadata)
    store.add_cir(cir)

    claims_added = 0
    for claim in cir.get("claims") or []:
        claim["paper_id"] = paper_id
        store.add_claim(claim)
        claims_added += 1

    annotations_added = 0
    for ann in cir.get("annotations") or []:
        store.add_annotation(ann)
        annotations_added += 1

    return {
        "papers": 1,
        "claims": claims_added,
        "annotations": annotations_added,
        "sources": sources_added,
        "pdfs": pdfs_added,
        "slugs_minted": slugs_minted,
        "_paper_id": paper_id,
        "_slug": cir.get("id_slug"),
    }
