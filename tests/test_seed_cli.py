"""End-to-end tests for ``rrxiv seed-store``.

These cover the bits that touch the store + paper-metadata mutation:

- The ``--reset`` flag wipes a populated DB before re-seeding.
- After save_source/save_rendered_pdf, the paper record's
  ``source.uri`` and ``rendered_pdf_uri`` are rewritten to the
  API-relative endpoints (so the web client can resolve them via
  ``resolveApiUri`` instead of opening a ``file://...`` dev path).
- Claims are slug-keyed (``claim.paper_id == paper.id_slug``, RRP-0013
  / RRP-0029): after seeding a paper whose machine ``id`` is a UUID,
  its claims resolve via BOTH the UUID and the slug, and cross-paper
  slug-based claim edges survive verbatim + wire up across papers.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any

import httpx
from typer.testing import CliRunner

from rrxiv.cli.seed import load_cir_into_store, seed_app
from rrxiv.server import ServerSettings, build_app
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
    assert paper["source"]["rendered_pdf_uri"] == (
        f"/api/v0/papers/{paper_id}/pdf"
    )
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


def test_seed_preserve_community_keeps_other_papers_and_annotations(
    tmp_path: Path,
) -> None:
    """``--preserve-community`` refreshes ONLY the incoming seed papers,
    leaving externally submitted papers AND all annotations intact —
    unlike ``--reset``, which truncates everything."""
    db = tmp_path / "rrxiv.db"
    seed_slug = "rrxiv:2605.00050"

    # Pre-populate: an OLD version of the seed paper (with a stale claim),
    # a community-submitted paper, and annotations on BOTH papers.
    store = store_from_url(f"sqlite:///{db}")
    store.add_paper({"id": "seed-1", "id_slug": seed_slug, "title": "old title"})
    store.add_cir({"id": "seed-1", "id_slug": seed_slug, "claims": []})
    store.add_claim(
        {
            "id": f"{seed_slug}:claim:stale",
            "paper_id": seed_slug,
            "statement": "stale claim",
            "claim_type": "theoretical",
            "evidence_type": "argument",
            "replication_status": "untested",
            "depends_on": [],
            "supports": [],
            "contradicts": [],
            "extends": [],
        }
    )
    store.add_paper(
        {"id": "community-1", "id_slug": "rrxiv:2605.09999", "title": "community"}
    )
    for aid, target in [("ann-community", "community-1"), ("ann-seed", seed_slug)]:
        store.add_annotation(
            {
                "id": aid,
                "target_id": target,
                "target_type": "paper",
                "annotation_type": "comment",
                "content": "x",
                "created_at": "2026-05-20T00:00:00Z",
                "created_by": {"identity_type": "orcid", "identity": "0000-0001"},
            }
        )
    store.close()

    # New seed dir: UPDATED seed-1 (new title + a fresh claim, no stale one).
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    updated = {
        "rrxiv_version": "0.1.0",
        "id": "seed-1",
        "id_slug": seed_slug,
        "version": "v1",
        "title": "new title",
        "authors": [{"name": "X"}],
        "abstract": "abstract",
        "submitted_at": "2026-05-21T00:00:00Z",
        "license": "CC-BY-4.0",
        "source": {"format": "latex", "uri": None},
        "claims": [
            {
                "id": f"{seed_slug}:claim:fresh",
                "paper_id": seed_slug,
                "statement": "fresh claim",
                "claim_type": "theoretical",
                "evidence_type": "argument",
                "replication_status": "untested",
                "depends_on": [],
                "supports": [],
                "contradicts": [],
                "extends": [],
            }
        ],
        "annotations": [],
        "citations": [],
        "sections": [],
        "figures": [],
    }
    (seed_dir / "seed-1.cir.json").write_text(json.dumps(updated), encoding="utf-8")

    result = CliRunner().invoke(
        seed_app,
        [
            "--from",
            str(seed_dir),
            "--store",
            f"sqlite:///{db}",
            "--quiet",
            "--preserve-community",
        ],
    )
    assert result.exit_code == 0, result.stdout

    store = store_from_url(f"sqlite:///{db}")
    papers = {p["id"]: p for p in store.list_papers()}
    # The externally submitted paper survives.
    assert "community-1" in papers
    # The seed paper is updated in place.
    assert papers["seed-1"]["title"] == "new title"
    # Stale claim gone; the fresh claim from the new CIR is present.
    claim_ids = {c["id"] for c in store.list_claims()}
    assert f"{seed_slug}:claim:stale" not in claim_ids
    assert f"{seed_slug}:claim:fresh" in claim_ids
    # ALL annotations intact — on BOTH the community and the seed paper.
    assert {a["id"] for a in store.list_annotations()} == {
        "ann-community",
        "ann-seed",
    }
    store.close()


def test_seed_reset_and_preserve_community_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    """The two reseed modes conflict — passing both is an error."""
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    _write_cir(seed_dir, "01923f8e-0009-7c4d-9e1f-3a2b1c0d4e5f")

    result = CliRunner().invoke(
        seed_app,
        [
            "--from",
            str(seed_dir),
            "--store",
            "memory://",
            "--reset",
            "--preserve-community",
        ],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


# ---------- Slug-keyed claim resolution (RRP-0013 / RRP-0029) -----------


def _full_cir(
    *,
    paper_id: str,
    slug: str,
    claims: list[dict[str, Any]],
) -> dict[str, Any]:
    """A minimal-but-complete CIR with a UUID machine ``id`` distinct
    from its citable ``id_slug``."""
    return {
        "rrxiv_version": "0.1.0",
        "id": paper_id,
        "id_slug": slug,
        "version": "v1",
        "title": f"Paper {slug}",
        "authors": [{"name": "A. Author"}],
        "abstract": "abstract",
        "submitted_at": "2026-05-20T00:00:00Z",
        "license": "CC-BY-4.0",
        "source": {"format": "latex", "uri": None},
        "claims": claims,
        "annotations": [],
        "citations": [],
        "sections": [],
        "figures": [],
    }


def _seed_cir_into_app(app: Any, cir: dict[str, Any], tmp_path: Path) -> None:
    """Write ``cir`` to a temp file and load it into the app's store via
    the canonical seed pathway (so canonicalisation + paper_id handling
    match production)."""
    p = tmp_path / f"{cir['id']}.cir.json"
    p.write_text(json.dumps(cir), encoding="utf-8")
    load_cir_into_store(p, app.state.store, quiet=True)


def _app_and_client() -> tuple[Any, httpx.Client]:
    from fastapi.testclient import TestClient

    app = build_app(settings=ServerSettings(dev_mode=True))
    transport = TestClient(app)._transport
    return app, httpx.Client(transport=transport, base_url="http://test/api/v0")


def test_seeded_claims_resolve_by_both_uuid_and_slug(tmp_path: Path) -> None:
    """A paper whose machine ``id`` is a UUID but whose claims are
    slug-keyed must surface those claims from BOTH
    ``GET /papers/<uuid>/claims`` and ``GET /papers/<slug>/claims``."""
    paper_uuid = "01923f8e-0099-7c4d-9e1f-3a2b1c0d4e5f"
    slug = "rrxiv:2605.00099"
    claim = {
        "id": f"{slug}:claim:c1",
        "paper_id": slug,
        "statement": "A slug-keyed claim.",
        "claim_type": "theoretical",
        "evidence_type": "argument",
        "replication_status": "untested",
        "depends_on": [],
        "supports": [],
        "contradicts": [],
        "extends": [],
    }
    cir = _full_cir(paper_id=paper_uuid, slug=slug, claims=[claim])

    app, client = _app_and_client()
    try:
        _seed_cir_into_app(app, cir, tmp_path)

        # Sanity: the stored claim kept its slug-based paper_id (NOT the
        # UUID) — seed must not rewrite claims to the machine id.
        stored = app.state.store.list_claims()
        assert len(stored) == 1
        assert stored[0]["paper_id"] == slug
        assert stored[0]["id"] == f"{slug}:claim:c1"

        by_uuid = client.get(f"/papers/{paper_uuid}/claims")
        by_slug = client.get(f"/papers/{slug}/claims")
        assert by_uuid.status_code == 200, by_uuid.text
        assert by_slug.status_code == 200, by_slug.text
        ids_uuid = [c["id"] for c in by_uuid.json()["items"]]
        ids_slug = [c["id"] for c in by_slug.json()["items"]]
        assert ids_uuid == [f"{slug}:claim:c1"]
        assert ids_slug == [f"{slug}:claim:c1"]
    finally:
        client.close()


def test_cross_paper_claim_edge_survives_seed_and_resolves(
    tmp_path: Path,
) -> None:
    """Two papers with distinct UUIDs + slugs; the second paper's claim
    declares ``depends_on`` a claim in the first (slug-based id). The
    edge string is preserved verbatim and the depends-on / dependents
    endpoints resolve it across papers."""
    uuid_a = "01923f8e-0098-7c4d-9e1f-3a2b1c0d4e5f"
    uuid_b = "01923f8e-0099-7c4d-9e1f-3a2b1c0d4e5f"
    slug_a = "rrxiv:2605.00098"
    slug_b = "rrxiv:2605.00099"
    claim_a_id = f"{slug_a}:claim:c1"
    claim_b_id = f"{slug_b}:claim:c1"

    def _claim(cid: str, paper_slug: str, depends_on: list[str]) -> dict[str, Any]:
        return {
            "id": cid,
            "paper_id": paper_slug,
            "statement": "x",
            "claim_type": "theoretical",
            "evidence_type": "argument",
            "replication_status": "untested",
            "depends_on": depends_on,
            "supports": [],
            "contradicts": [],
            "extends": [],
        }

    cir_a = _full_cir(
        paper_id=uuid_a, slug=slug_a, claims=[_claim(claim_a_id, slug_a, [])]
    )
    cir_b = _full_cir(
        paper_id=uuid_b,
        slug=slug_b,
        claims=[_claim(claim_b_id, slug_b, [claim_a_id])],
    )

    app, client = _app_and_client()
    try:
        _seed_cir_into_app(app, cir_a, tmp_path)
        _seed_cir_into_app(app, cir_b, tmp_path)

        # The cross-paper edge string is preserved byte-for-byte.
        b = client.get(f"/claims/{claim_b_id}")
        assert b.status_code == 200, b.text
        assert b.json()["depends_on"] == [claim_a_id]

        # depends-on from B resolves to A's claim across the paper boundary.
        dep = client.get(f"/claims/{claim_b_id}/depends-on")
        assert dep.status_code == 200, dep.text
        assert dep.json()["edges"] == [
            {"source": claim_b_id, "target": claim_a_id, "kind": "depends_on"}
        ]

        # dependents of A surface B.
        deps = client.get(f"/claims/{claim_a_id}/dependents")
        assert deps.status_code == 200, deps.text
        assert deps.json()["edges"] == [
            {"source": claim_b_id, "target": claim_a_id, "kind": "depends_on"}
        ]
    finally:
        client.close()
