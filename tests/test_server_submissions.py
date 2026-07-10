"""Tests for /submissions, /papers/{id}/source, /papers/{id}/versions,
/search/*, /snapshots POST + blob endpoints (Phase 2 of the gap-fill)."""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from typing import Any

import httpx
import pytest

from rrxiv.auth import exchange_orcid_code
from rrxiv.server import ServerSettings, build_app

pytest.importorskip("fastapi")


def _assert_is_uuid7(value: str) -> None:
    """Assert ``value`` is a canonical UUIDv7 string (RRP-0029)."""
    parsed = uuid.UUID(value)
    assert parsed.version == 7, f"expected UUIDv7, got version {parsed.version}"
    assert parsed.variant == uuid.RFC_4122
    assert not value.startswith("paper-"), value


def _submit(
    sync: httpx.Client,
    cir: dict[str, Any],
    bundle_bytes: bytes = b"x",
) -> str:
    """POST a non-revision submission and return the server-minted paper_id."""
    resp = sync.post(
        "/submissions",
        files={
            "cir": ("c.json", json.dumps(cir).encode("utf-8"), "application/json"),
            "bundle": ("p.tar.gz", bundle_bytes, "application/gzip"),
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["paper_id"]  # type: ignore[no-any-return]


def _fixture_paper(paper_id: str = "p-fixture") -> dict[str, Any]:
    return {
        "rrxiv_version": "0.1.0",
        "id": paper_id,
        "version": "v1",
        "title": "A fixture paper",
        "authors": [{"name": "A. Author"}],
        "abstract": "abstract goes here",
        "submitted_at": "2026-05-04T00:00:00Z",
        "license": "CC-BY-4.0",
        "source": {"format": "latex", "uri": "https://x.org/p.tar.gz"},
    }


def _client_with_orcid_bearer() -> tuple[Any, httpx.Client, str]:
    """Helper: build a server, mint an ORCID bearer in dev mode, return
    (app, sync httpx client, bearer token).

    The client is *not* wrapped in a `with` block by callers — it's a
    single instance shared across the test's calls. Tests close it
    explicitly or rely on the fixture teardown."""
    from fastapi.testclient import TestClient

    app = build_app(settings=ServerSettings(dev_mode=True))
    test_client = TestClient(app)
    transport = test_client._transport

    sync = httpx.Client(transport=transport, base_url="http://testserver/api/v0")

    resp = sync.get(
        "/auth/orcid/start",
        params={"redirect_uri": "http://x/cb", "state": "s"},
        follow_redirects=False,
    )
    code = resp.headers["location"].split("code=", 1)[1].split("&")[0]
    bearer = exchange_orcid_code(
        api_base="http://testserver/api/v0",
        code=code,
        state="s",
        expected_state="s",
        transport=transport,
    )
    sync.headers["Authorization"] = f"Bearer {bearer.token}"
    return app, sync, bearer.token


# ----- Submissions -----


def test_submit_paper_round_trip() -> None:
    app, sync, _ = _client_with_orcid_bearer()
    cir = _fixture_paper()
    bundle_bytes = b"fake source archive bytes"

    resp = sync.post(
        "/submissions",
        files={
            "cir": ("cir.json", json.dumps(cir).encode("utf-8"), "application/json"),
            "bundle": ("paper.tar.gz", bundle_bytes, "application/gzip"),
        },
    )
    sync.close()
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # RRP-0029: the server mints the machine id for a new submission and
    # ignores the client-supplied CIR id ("p-fixture").
    paper_id = body["paper_id"]
    assert paper_id != "p-fixture"
    _assert_is_uuid7(paper_id)
    assert body["retrieval_uri"].endswith(f"/papers/{paper_id}/source")
    assert paper_id in app.state.store.state.papers
    # The client id was NOT used as a storage key.
    assert "p-fixture" not in app.state.store.state.papers


def test_submit_new_paper_id_is_server_minted_uuid7() -> None:
    """RRP-0029 security: a fresh (non-revision) submission's paper_id is
    a server-minted UUIDv7, regardless of the id the client put in the CIR."""
    app, sync, _ = _client_with_orcid_bearer()
    cir = _fixture_paper("main")  # `rrxiv parse` default (tex file stem)
    paper_id = _submit(sync, cir)
    sync.close()
    _assert_is_uuid7(paper_id)
    assert paper_id != "main"
    assert "main" not in app.state.store.state.papers
    assert paper_id in app.state.store.state.papers


def test_submit_echoing_existing_id_does_not_overwrite() -> None:
    """RRP-0029 security: a non-revision submission that echoes an
    existing paper's id must NOT overwrite it — the server mints a fresh
    id and leaves the existing (e.g. seeded-corpus) paper untouched."""
    from rrxiv.server.ids import uuid7

    app, sync, _ = _client_with_orcid_bearer()

    # A pre-existing paper in the corpus (as if seeded).
    existing_id = str(uuid7())
    existing = _fixture_paper(existing_id)
    existing["title"] = "Seeded corpus paper — do not clobber"
    app.state.store.add_paper(existing)

    # A malicious/naive submission that echoes the existing paper's id.
    attacker = _fixture_paper(existing_id)
    attacker["title"] = "MALICIOUS OVERWRITE"
    new_id = _submit(sync, attacker)
    sync.close()

    # The submission got its own freshly-minted id, distinct from the
    # target it tried to echo.
    _assert_is_uuid7(new_id)
    assert new_id != existing_id
    # The seeded paper survives with its original title.
    survivor = app.state.store.state.papers[existing_id]
    assert survivor["title"] == "Seeded corpus paper — do not clobber"


def test_submit_persists_pdf_when_provided() -> None:
    """Regression for whitepaper v4 — the submit flow accepted a bundle
    but never persisted a PDF, so /pdf 404'd until a later seed-store
    pass filled it in. After this fix the PDF endpoint serves the
    submitted bytes directly."""
    _, sync, _ = _client_with_orcid_bearer()
    cir = _fixture_paper("p-pdf")
    pdf_bytes = b"%PDF-1.4 fake pdf content"

    resp = sync.post(
        "/submissions",
        files={
            "cir": ("cir.json", json.dumps(cir).encode("utf-8"), "application/json"),
            "bundle": ("p.tar.gz", b"src", "application/gzip"),
            "pdf": ("main.pdf", pdf_bytes, "application/pdf"),
        },
    )
    assert resp.status_code == 201, resp.text
    paper_id = resp.json()["paper_id"]

    pdf_resp = sync.get(f"/papers/{paper_id}/pdf")
    sync.close()
    assert pdf_resp.status_code == 200, pdf_resp.text
    assert pdf_resp.content == pdf_bytes
    assert pdf_resp.headers["content-type"] == "application/pdf"


def test_submit_rewrites_source_uri_to_server_relative() -> None:
    """Regression for whitepaper v4 — the submit flow stored the bundle
    but never rewrote ``source.uri`` on the CIR, so live papers carried
    stale (often ``file://``) URIs forever. After this fix the saved
    paper record has a server-relative URI matching what
    ``rrxiv seed-store`` produces."""
    app, sync, _ = _client_with_orcid_bearer()
    cir = _fixture_paper("p-srcuri")
    # Client-side stale URI — the kind that breaks the front-end.
    cir["source"] = {"format": "latex", "uri": "file:///Users/x/main.tex"}

    resp = sync.post(
        "/submissions",
        files={
            "cir": ("cir.json", json.dumps(cir).encode("utf-8"), "application/json"),
            "bundle": ("p.tar.gz", b"src", "application/gzip"),
        },
    )
    sync.close()
    assert resp.status_code == 201, resp.text
    paper_id = resp.json()["paper_id"]

    stored = app.state.store.state.papers.get(paper_id)
    assert stored is not None
    source = stored.get("source")
    assert isinstance(source, dict)
    assert source.get("uri", "").endswith(f"/papers/{paper_id}/source"), source
    assert not source.get("uri", "").startswith("file://"), source


def test_submit_invalid_cir_returns_422() -> None:
    _, sync, _ = _client_with_orcid_bearer()
    resp = sync.post(
        "/submissions",
        files={
            "cir": ("cir.json", b"{}", "application/json"),
            "bundle": ("p.tar.gz", b"...", "application/gzip"),
        },
    )
    sync.close()
    assert resp.status_code == 422
    assert resp.json()["title"] == "Validation Error"


def test_submit_unauthenticated_returns_401() -> None:
    from fastapi.testclient import TestClient

    app = build_app(settings=ServerSettings(dev_mode=True))
    test_client = TestClient(app)
    sync = httpx.Client(
        transport=test_client._transport, base_url="http://testserver/api/v0"
    )
    resp = sync.post(
        "/submissions",
        files={
            "cir": ("c.json", b"{}", "application/json"),
            "bundle": ("p.tar.gz", b"x", "application/gzip"),
        },
    )
    sync.close()
    assert resp.status_code == 401


# ----- Source download -----


def test_paper_source_round_trip() -> None:
    _app, sync, _ = _client_with_orcid_bearer()
    cir = _fixture_paper("p-src")
    bundle_bytes = b"unique-bytes-for-source-test"
    paper_id = _submit(sync, cir, bundle_bytes)
    resp = sync.get(f"/papers/{paper_id}/source")
    sync.close()
    assert resp.status_code == 200
    assert resp.content == bundle_bytes
    assert "attachment" in resp.headers["content-disposition"]


def test_paper_source_404_when_unknown_paper() -> None:
    _, sync, _ = _client_with_orcid_bearer()
    resp = sync.get("/papers/nope/source")
    sync.close()
    assert resp.status_code == 404


def _tarball_with(files: dict[str, str]) -> bytes:
    """Helper: build a tar.gz bundle from a dict of filename → text."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _tarball_with_bytes(files: dict[str, bytes]) -> bytes:
    """Helper: build a tar.gz bundle from a dict of filename → raw bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_paper_source_manifest_lists_files() -> None:
    _, sync, _ = _client_with_orcid_bearer()
    cir = _fixture_paper("p-manifest")
    bundle = _tarball_with(
        {
            "main.tex": "\\documentclass{article}\\begin{document}hi\\end{document}",
            "rrxiv.cls": "% class file",
            "refs.bib": "@misc{x, title={x}}",
        }
    )
    paper_id = _submit(sync, cir, bundle)
    resp = sync.get(f"/papers/{paper_id}/source/manifest")
    sync.close()
    assert resp.status_code == 200
    body = resp.json()
    assert body["paper_id"] == paper_id
    paths = [f["path"] for f in body["files"]]
    # main.tex comes first by the sort key.
    assert paths[0] == "main.tex"
    assert "rrxiv.cls" in paths and "refs.bib" in paths
    kinds = {f["path"]: f["kind"] for f in body["files"]}
    assert kinds["main.tex"] == "tex"
    assert kinds["rrxiv.cls"] == "cls"
    assert kinds["refs.bib"] == "bib"


def test_paper_source_file_returns_utf8_text() -> None:
    _, sync, _ = _client_with_orcid_bearer()
    cir = _fixture_paper("p-srcfile")
    main_tex = "\\section{Hello}\nworld\n"
    bundle = _tarball_with({"main.tex": main_tex})
    paper_id = _submit(sync, cir, bundle)
    resp = sync.get(f"/papers/{paper_id}/source/file?path=main.tex")
    sync.close()
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == main_tex


def test_paper_source_file_resolves_under_bundle_root_and_serves_image() -> None:
    """Claim figures point at ``figures/fig.png`` (relative to the source
    root), but bundles ship a single top-level directory (``paper/...``).
    The endpoint resolves the path under that root and serves it as image/png
    so the client's <img> renders — previously this 404'd (broken figures)."""
    _, sync, _ = _client_with_orcid_bearer()
    cir = _fixture_paper("p-fig")
    png = b"\x89PNG\r\n\x1a\n\x00fake-png-bytes"
    bundle = _tarball_with_bytes(
        {
            "paper/main.tex": b"\\section{H}\nworld\n",
            "paper/figures/fig.png": png,
        }
    )
    paper_id = _submit(sync, cir, bundle)
    # request the figure by its source-root-relative path (no `paper/` prefix)
    resp = sync.get(f"/papers/{paper_id}/source/file?path=figures/fig.png")
    sync.close()
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.content == png


def test_paper_source_file_rejects_path_traversal() -> None:
    _, sync, _ = _client_with_orcid_bearer()
    cir = _fixture_paper("p-traversal")
    bundle = _tarball_with({"main.tex": "x"})
    paper_id = _submit(sync, cir, bundle)
    resp = sync.get(f"/papers/{paper_id}/source/file?path=../etc/passwd")
    sync.close()
    assert resp.status_code == 404


def test_paper_source_manifest_404_when_no_source() -> None:
    _, sync, _ = _client_with_orcid_bearer()
    resp = sync.get("/papers/nope/source/manifest")
    sync.close()
    assert resp.status_code == 404


# ----- Versions -----


def test_paper_versions_walks_chain() -> None:
    _, sync, _ = _client_with_orcid_bearer()
    # The head (v1) is a new submission → server-minted id. Each revision
    # points its previous_version at the id the server actually assigned.
    ids: list[str] = []
    prev: str | None = None
    for label in ("r1", "r2", "r3"):
        cir = _fixture_paper(label)
        data: dict[str, str] = {}
        if prev:
            cir["previous_version"] = prev
            data["previous_version"] = prev
        resp = sync.post(
            "/submissions",
            files={
                "cir": (
                    "c.json",
                    json.dumps(cir).encode("utf-8"),
                    "application/json",
                ),
                "bundle": ("p.tar.gz", b"x", "application/gzip"),
            },
            data=data,
        )
        assert resp.status_code == 201, resp.text
        pid = resp.json()["paper_id"]
        ids.append(pid)
        prev = pid
    resp = sync.get(f"/papers/{ids[-1]}/versions")
    sync.close()
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [i["id"] for i in items] == ids


# ----- Search -----


def test_search_papers_matches_title_and_abstract() -> None:
    _, sync, _ = _client_with_orcid_bearer()
    minted: dict[str, str] = {}
    for pid, title in [("p1", "Alpha quantum"), ("p2", "Beta classical")]:
        cir = _fixture_paper(pid)
        cir["title"] = title
        minted[pid] = _submit(sync, cir)
    resp = sync.get("/search/papers", params={"q": "quantum"})
    sync.close()
    assert resp.status_code == 200
    ids = [p["id"] for p in resp.json()["items"]]
    assert ids == [minted["p1"]]


def test_search_papers_empty_query_returns_all(
) -> None:
    """RRP-0028: empty q is the default-match behaviour, not an error."""
    app, sync, _ = _client_with_orcid_bearer()
    app.state.store.add_paper({
        "id": "p1",
        "id_slug": "rrxiv:2605.00091",
        "version": "v1",
        "title": "Test",
        "submitted_at": "2026-05-26T18:00:00Z",
        "abstract": "test",
        "authors": [{"name": "Anyone"}],
        "license": "CC-BY-4.0",
        "source": {"format": "latex", "uri": "/x.tar.gz"},
    })
    resp = sync.get("/search/papers", params={"q": " "})
    sync.close()
    assert resp.status_code == 200
    assert any(p["id"] == "p1" for p in resp.json()["items"])


def test_search_claims_matches_statement() -> None:
    app, sync, _ = _client_with_orcid_bearer()
    app.state.store.add_claim(
        {
            "id": "p1:c1",
            "statement": "We assert that thresholds matter.",
            "claim_type": "theoretical",
            "evidence_type": "argument",
        }
    )
    resp = sync.get("/search/claims", params={"q": "threshold"})
    sync.close()
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == "p1:c1"


# ----- Snapshot creation -----


def test_create_snapshot_round_trip() -> None:
    app, sync, _ = _client_with_orcid_bearer()
    app.state.store.add_paper(_fixture_paper("snap-p1"))
    app.state.store.add_claim(
        {
            "id": "snap-p1:c1",
            "statement": "X.",
            "claim_type": "theoretical",
            "evidence_type": "argument",
        }
    )
    resp = sync.post("/snapshots")
    assert resp.status_code == 201
    manifest = resp.json()
    assert manifest["counts"]["papers"] >= 1
    assert manifest["counts"]["claims"] >= 1
    assert manifest["content_digest"].startswith("sha-256=:")
    blob_uri = manifest["blob_uri"]

    latest = sync.get("/snapshots/latest")
    assert latest.status_code == 200
    assert latest.json()["snapshot_id"] == manifest["snapshot_id"]

    relative = blob_uri.replace("/api/v0", "")
    blob = sync.get(relative)
    sync.close()
    assert blob.status_code == 200
    assert blob.content[:2] == b"\x1f\x8b"
    with tarfile.open(fileobj=io.BytesIO(blob.content), mode="r:gz") as tar:
        names = sorted(tar.getnames())
        assert any(n.startswith("papers/") for n in names)
        assert any(n.startswith("claims/") for n in names)


def test_create_snapshot_anonymous_forbidden() -> None:
    from fastapi.testclient import TestClient

    from rrxiv.auth import (
        AnonymousChallengeResponse,
        request_anonymous_challenge,
        verify_anonymous_challenge,
    )

    app = build_app(settings=ServerSettings(dev_mode=True))
    test_client = TestClient(app)
    transport = test_client._transport
    sync = httpx.Client(transport=transport, base_url="http://testserver/api/v0")

    challenge = request_anonymous_challenge(
        api_base="http://testserver/api/v0", transport=transport
    )
    bearer = verify_anonymous_challenge(
        api_base="http://testserver/api/v0",
        response=AnonymousChallengeResponse(
            challenge_id=challenge.challenge_id, response="x"
        ),
        transport=transport,
    )
    sync.headers["Authorization"] = f"Bearer {bearer.token}"
    resp = sync.post("/snapshots")
    sync.close()
    assert resp.status_code == 403
