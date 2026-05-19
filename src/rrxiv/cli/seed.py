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
from pathlib import Path
from typing import Annotated

import typer

from rrxiv.server.papers.slug import is_slug
from rrxiv.server.papers.slug import mint_slug
from rrxiv.server.store import store_from_url

seed_app = typer.Typer(no_args_is_help=False)


def _iter_cir_files(root: Path) -> list[Path]:
    """Discover all CIR JSON files under ``root``.

    Both ``foo.cir.json`` (flat) and ``foo/cir.json`` (subdir) layouts
    are recognised.
    """
    found: list[Path] = []
    for path in sorted(root.iterdir()):
        if path.is_file() and path.name.endswith(".cir.json"):
            found.append(path)
        elif path.is_dir() and (path / "cir.json").is_file():
            found.append(path / "cir.json")
    return found


def _sibling_source(cir_path: Path) -> Path | None:
    """Find a source tarball sibling to a CIR file, if any."""
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

    for cir_path in cir_files:
        with cir_path.open("r", encoding="utf-8") as f:
            cir = json.load(f)

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
        f"sources={sources_added} pdfs={pdfs_added} "
        f"slugs_minted={minted_slugs}"
    )
