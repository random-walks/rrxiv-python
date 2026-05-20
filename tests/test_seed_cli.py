"""End-to-end tests for ``rrxiv seed-store``.

These cover the bits that touch the store + paper-metadata mutation:

- The ``--reset`` flag wipes a populated DB before re-seeding.
- After save_source/save_rendered_pdf, the paper record's
  ``source.uri`` and ``rendered_pdf_uri`` are rewritten to the
  API-relative endpoints (so the web client can resolve them via
  ``resolveApiUri`` instead of opening a ``file://...`` dev path).
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

from typer.testing import CliRunner

from rrxiv.cli.seed import seed_app
from rrxiv.server.store import store_from_url


def _write_cir(dir_: Path, paper_id: str, slug: str | None = None) -> Path:
    cir = {
        "rrxiv_version": "0.1.0",
        "id": paper_id,
        "version": "v1",
        "title": "Fixture",
        "authors": [{"name": "X"}],
        "abstract": "abstract",
        "submitted_at": "2026-05-20T00:00:00Z",
        "license": "CC-BY-4.0",
        # Parser-style URI that should be rewritten on seed.
        "source": {"format": "latex", "uri": f"file:///tmp/{paper_id}.tex"},
        "claims": [],
        "annotations": [],
        "citations": [],
        "sections": [],
        "figures": [],
    }
    if slug is not None:
        cir["id_slug"] = slug
    path = dir_ / f"{paper_id}.cir.json"
    path.write_text(json.dumps(cir), encoding="utf-8")
    return path


def _write_tarball(path: Path, files: dict[str, str]) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    path.write_bytes(buf.getvalue())


def _write_pdf(path: Path, payload: bytes = b"%PDF-1.4 stub") -> None:
    path.write_bytes(payload)


def test_seed_rewrites_source_uri_to_api_endpoint(tmp_path: Path) -> None:
    """After seed, ``paper.source.uri`` and ``rendered_pdf_uri`` point at
    the server-relative endpoints — not the parser's ``file://`` URI."""
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    db = tmp_path / "rrxiv.db"

    paper_id = "01923f8e-0009-7c4d-9e1f-3a2b1c0d4e5f"
    _write_cir(seed_dir, paper_id, slug="rrxiv:2605.00009")
    main_tex = "\\documentclass{article}\\begin{document}x\\end{document}"
    _write_tarball(
        seed_dir / f"{paper_id}.source.tar.gz", {"main.tex": main_tex}
    )
    _write_pdf(seed_dir / f"{paper_id}.pdf")

    result = CliRunner().invoke(
        seed_app,
        [
            "--from",
            str(seed_dir),
            "--store",
            f"sqlite:///{db}",
            "--quiet",
        ],
    )
    assert result.exit_code == 0, result.stdout

    store = store_from_url(f"sqlite:///{db}")
    paper = store.get_paper(paper_id)
    assert paper is not None
    assert paper["source"]["uri"] == f"/api/v0/papers/{paper_id}/source"
    assert paper["rendered_pdf_uri"] == f"/api/v0/papers/{paper_id}/pdf"
    assert store.load_source(paper_id) is not None
    assert store.load_rendered_pdf(paper_id) is not None
    store.close()


def test_seed_reset_wipes_existing_corpus(tmp_path: Path) -> None:
    """``--reset`` truncates the corpus tables before re-seeding so a
    paper whose claim IDs have changed prefix leaves no orphans."""
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    db = tmp_path / "rrxiv.db"

    # Pre-populate with an "old" paper that won't be in the new seed dir.
    store = store_from_url(f"sqlite:///{db}")
    store.add_paper({"id": "old", "title": "old"})
    store.add_claim(
        {
            "id": "old:c1",
            "paper_id": "old",
            "statement": "stale",
            "claim_type": "theoretical",
            "evidence_type": "argument",
            "replication_status": "untested",
            "depends_on": [],
            "supports": [],
            "contradicts": [],
            "extends": [],
        }
    )
    store.close()

    # New seed dir contains a different paper.
    new_id = "01923f8e-0009-7c4d-9e1f-3a2b1c0d4e5f"
    _write_cir(seed_dir, new_id)

    # Run without --reset: the old paper survives alongside the new.
    result = CliRunner().invoke(
        seed_app,
        ["--from", str(seed_dir), "--store", f"sqlite:///{db}", "--quiet"],
    )
    assert result.exit_code == 0
    store = store_from_url(f"sqlite:///{db}")
    ids = sorted(p["id"] for p in store.list_papers())
    assert ids == sorted(["old", new_id])
    store.close()

    # Re-run with --reset: only the new paper remains.
    result = CliRunner().invoke(
        seed_app,
        [
            "--from",
            str(seed_dir),
            "--store",
            f"sqlite:///{db}",
            "--quiet",
            "--reset",
        ],
    )
    assert result.exit_code == 0
    store = store_from_url(f"sqlite:///{db}")
    assert [p["id"] for p in store.list_papers()] == [new_id]
    assert [c["id"] for c in store.list_claims()] == []
    store.close()
