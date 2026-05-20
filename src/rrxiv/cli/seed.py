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
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer

from rrxiv.server.papers.slug import is_slug
from rrxiv.server.papers.slug import mint_slug
from rrxiv.server.store import store_from_url

seed_app = typer.Typer(no_args_is_help=False)


def _today_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
        elif path.is_dir() and (path / "paper" / "main.tex").is_file() and (path / "rrxiv-meta.json").is_file():
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
    if p.name == "main.cir.json" and p.parent.name == "build" and _is_paper_repo_root(p.parent.parent):
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
) -> None:
    """Bulk-load CIRs into a Store, bypassing /submissions."""
    if not from_.is_dir():
        typer.secho(f"ERROR: --from path is not a directory: {from_}", fg="red", err=True)
        raise typer.Exit(code=2)

    store = store_from_url(store_url)
    cir_files = _iter_cir_files(from_)
    if not cir_files:
        typer.secho(
            f"WARNING: no *.cir.json files found in {from_}",
            fg="yellow",
            err=True,
        )
        raise typer.Exit(code=0)

    papers_added = 0
    claims_added = 0
    sources_added = 0
    pdfs_added = 0
    minted_slugs = 0
    annotations_added = 0

    for cir_path in cir_files:
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
            # Source uri: link to the paper-repo on GitHub if we can guess.
            cir.setdefault("source", {"format": "latex", "uri": None})
            cir.setdefault("submitted_at", _today_iso())

        paper_id = cir.get("id")
        if not paper_id:
            typer.secho(
                f"  skip: {cir_path.name} (missing id)", fg="yellow"
            )
            continue

        # Mint a slug if missing — seed CIRs may omit it.
        if not cir.get("id_slug"):
            cir["id_slug"] = mint_slug(store)
            minted_slugs += 1
        elif not is_slug(cir["id_slug"]):
            typer.secho(
                f"  warn: {cir_path.name} has malformed id_slug "
                f"'{cir['id_slug']}' — will not match pattern",
                fg="yellow",
            )

        paper_metadata = {
            k: v
            for k, v in cir.items()
            if k not in ("claims", "citations", "annotations", "sections", "figures")
        }
        store.add_paper(paper_metadata)
        store.add_cir(cir)
        papers_added += 1

        for claim in cir.get("claims") or []:
            claim.setdefault("paper_id", paper_id)
            store.add_claim(claim)
            claims_added += 1

        for ann in cir.get("annotations") or []:
            store.add_annotation(ann)
            annotations_added += 1

        src_path = _sibling_source(cir_path)
        if src_path is not None:
            store.save_source(paper_id, src_path.read_bytes())
            sources_added += 1

        pdf_path = _sibling_pdf(cir_path)
        if pdf_path is not None:
            store.save_rendered_pdf(paper_id, pdf_path.read_bytes())
            pdfs_added += 1

        if not quiet:
            typer.echo(
                f"  load: {cir_path.relative_to(from_)} → "
                f"id={paper_id} slug={cir.get('id_slug')!r}"
            )

    typer.echo("")
    typer.echo(
        f"Done. papers={papers_added} claims={claims_added} "
        f"annotations={annotations_added} "
        f"sources={sources_added} pdfs={pdfs_added} "
        f"slugs_minted={minted_slugs}"
    )
