"""End-to-end conformance fixture (Phase 3 of the gap-fill plan).

This is the single test we'd want a downstream client implementation
(Rust, Go, JS) to be able to drive against any conforming server. It
exercises the full identity + write story:

1. Spin up the reference server (live uvicorn).
2. Enroll an agent (Ed25519 keypair gen → POST /auth/agent/enroll).
3. Submit a paper as the agent (multipart with cir + bundle).
4. Verify the paper shows up in GET /papers and /search/papers.
5. Annotate the paper as the agent (signed write per RRP-0007).
6. Read the annotation back and assert round-trip.
7. Trigger a snapshot (POST /snapshots).
8. Verify the snapshot manifest references the paper + annotation.
9. Download the snapshot tar.gz and assert the paper + annotation
   files are present.

Equivalent of a "smoke test for the protocol".
"""

from __future__ import annotations

import base64
import io
import json
import socket
import tarfile
import threading
import time
from typing import Any

import httpx
import pytest

from rrxiv.auth import (
    AgentEnrollmentRequest,
    enroll_agent,
)
from rrxiv.auth.agent import build_enrollment_payload, sign_enrollment_payload
from rrxiv.client import AgentSigningKey, RrxivClient

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")
pytest.importorskip("uvicorn")


@pytest.fixture()
def live_server() -> Any:
    """A real uvicorn process, accepting on an ephemeral port."""
    import uvicorn

    from rrxiv.server import ServerSettings, build_app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    app = build_app(settings=ServerSettings(dev_mode=True))
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=0.5) as c:
                if c.get(f"http://127.0.0.1:{port}/api/v0/version").status_code == 200:
                    break
        except (httpx.ConnectError, httpx.ReadTimeout):
            time.sleep(0.05)
    else:  # pragma: no cover
        pytest.fail("conformance server failed to start")

    yield {"app": app, "url": f"http://127.0.0.1:{port}/api/v0", "port": port}

    server.should_exit = True
    thread.join(timeout=5)


def test_protocol_e2e_conformance(live_server: Any) -> None:
    """The single canonical conformance story."""
    api_base = live_server["url"]

    # ---- Step 1: enroll an agent ----
    signing = AgentSigningKey.generate(handle="@conformance-bot")
    pub_b64 = base64.standard_b64encode(signing.public_key_bytes()).decode("ascii")
    payload = build_enrollment_payload(
        handle=signing.handle,
        public_key_b64=pub_b64,
        issued_at=int(time.time()),
    )
    sig_b64 = sign_enrollment_payload(
        payload=payload, private_key_bytes=signing.private_key_bytes()
    )
    bearer = enroll_agent(
        api_base=api_base,
        request=AgentEnrollmentRequest(
            handle=signing.handle,
            public_key_b64=pub_b64,
            payload_b64=base64.standard_b64encode(payload).decode("ascii"),
            signature_b64=sig_b64,
        ),
    )
    assert bearer.identity == "@conformance-bot"

    # ---- Step 2: submit a paper as the agent ----
    cir = {
        "rrxiv_version": "0.1.0",
        "id": "p-conformance",
        "version": "v1",
        "title": "A conformance fixture paper",
        "authors": [{"name": "Test Author"}],
        "abstract": "This paper exercises the conformance suite.",
        "submitted_at": "2026-05-06T00:00:00Z",
        "license": "CC-BY-4.0",
        "source": {"format": "latex", "uri": f"{api_base}/papers/p-conformance/source"},
        "topics": ["testing", "conformance"],
    }
    with httpx.Client(headers={"Authorization": f"Bearer {bearer.token}"}) as c:
        # Submissions are POSTs by an agent identity, which means
        # they require the RFC 9421 signature. We attach the signing
        # auth ad-hoc for this multipart upload.
        from rrxiv.client.signatures import AgentSigningAuth

        resp = c.post(
            f"{api_base}/submissions",
            files={
                "cir": ("c.json", json.dumps(cir).encode("utf-8"), "application/json"),
                "bundle": (
                    "p.tar.gz",
                    b"fake source archive",
                    "application/gzip",
                ),
            },
            auth=AgentSigningAuth(signing),
        )
    assert resp.status_code == 201, resp.text
    submit_body = resp.json()
    # RRP-0029: the server mints the machine id for a new submission and
    # ignores the client-supplied CIR id ("p-conformance").
    paper_id = submit_body["paper_id"]
    assert paper_id and paper_id != "p-conformance"

    # ---- Step 3: paper shows up in /papers ----
    with RrxivClient(api_base, auth=bearer, agent_signing_key=signing) as client:
        page = client.list_papers()
        assert any(p["id"] == paper_id for p in page["items"])

        # ---- Step 4: search finds it ----
        with httpx.Client() as raw:
            search = raw.get(f"{api_base}/search/papers", params={"q": "conformance"})
        assert search.status_code == 200
        ids = [p["id"] for p in search.json()["items"]]
        assert paper_id in ids

        # ---- Step 5: annotate as the agent (signed) ----
        ann = client.create_annotation(
            {
                "id": "ann-conformance-1",
                "target_id": paper_id,
                "target_type": "paper",
                "annotation_type": "comment",
                "content": "Conformance suite test annotation.",
                "created_at": "2026-05-06T00:00:00Z",
                "created_by": {
                    "identity_type": "agent",
                    "identity": "@conformance-bot",
                },
            }
        )
        assert ann.id == "ann-conformance-1"

        # ---- Step 6: round-trip read ----
        ann2 = client.get_annotation("ann-conformance-1")
        assert ann2.id == "ann-conformance-1"
        assert ann2.content == "Conformance suite test annotation."

        # ---- Step 7: trigger a snapshot (signed POST) ----
        with httpx.Client(
            headers={"Authorization": f"Bearer {bearer.token}"}
        ) as raw:
            snap = raw.post(
                f"{api_base}/snapshots", auth=AgentSigningAuth(signing)
            )
        assert snap.status_code == 201, snap.text
        manifest = snap.json()
        assert manifest["counts"]["papers"] >= 1
        assert manifest["counts"]["annotations"] >= 1

        # ---- Step 8: snapshot is the latest ----
        latest = client._http.get(f"{api_base}/snapshots/latest")
        assert latest.status_code == 200
        assert latest.json()["snapshot_id"] == manifest["snapshot_id"]

        # ---- Step 9: download blob and verify contents ----
        blob_resp = client._http.get(
            manifest["blob_uri"].replace("/api/v0", api_base.split("/api/v0")[0] + "/api/v0")
            if manifest["blob_uri"].startswith("/api/v0")
            else manifest["blob_uri"]
        )
        if blob_resp.status_code == 404:
            # Some httpx URL-resolving variations; try the raw path.
            blob_resp = client._http.get(manifest["blob_uri"])
        assert blob_resp.status_code == 200, blob_resp.text
        assert blob_resp.content[:2] == b"\x1f\x8b"

    with tarfile.open(fileobj=io.BytesIO(blob_resp.content), mode="r:gz") as tar:
        names = tar.getnames()
        assert f"papers/{paper_id}.json" in names
        assert "annotations/ann-conformance-1.json" in names
