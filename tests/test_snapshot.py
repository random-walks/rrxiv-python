"""Tests for snapshot creation + validation."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

from rrxiv.snapshot import (
    SnapshotEntry,
    create_snapshot,
    validate_snapshot,
)


def _write_cir(path: Path, paper_id: str = "p1") -> None:
    cir = {
        "rrxiv_version": "0.1.0",
        "id": paper_id,
        "version": "v1",
        "title": "T",
        "authors": [{"name": "A. Author"}],
        "abstract": "x",
        "submitted_at": "2026-05-04T00:00:00Z",
        "license": "CC-BY-4.0",
        "source": {"format": "latex", "uri": "https://x.org/p.tar.gz"},
        "annotations": [],
    }
    path.write_text(json.dumps(cir), encoding="utf-8")


def test_create_one_paper(tmp_path: Path) -> None:
    cir_path = tmp_path / "p1.cir.json"
    _write_cir(cir_path, "p1")
    out = tmp_path / "snap.tar.gz"
    manifest = create_snapshot(
        [SnapshotEntry(paper_id="p1", cir_path=cir_path)],
        out,
        snapshot_id="snap-test",
    )
    assert out.is_file()
    assert manifest.papers == 1
    assert manifest.snapshot_id == "snap-test"
    assert manifest.sha256
    assert manifest.size_bytes > 0
    # Side manifest file
    assert out.with_suffix(".manifest.json").is_file()


def test_validate_freshly_created(tmp_path: Path) -> None:
    cir_path = tmp_path / "p1.cir.json"
    _write_cir(cir_path)
    out = tmp_path / "snap.tar.gz"
    create_snapshot([SnapshotEntry(paper_id="p1", cir_path=cir_path)], out)
    report = validate_snapshot(out)
    assert report.ok, f"errors: {report.errors}"


def test_validate_detects_corrupted_file(tmp_path: Path) -> None:
    cir_path = tmp_path / "p1.cir.json"
    _write_cir(cir_path)
    out = tmp_path / "snap.tar.gz"
    create_snapshot(
        [SnapshotEntry(paper_id="p1", cir_path=cir_path)],
        out,
        snapshot_id="snap-corrupt",
    )

    # Open the tarball, replace cir.json with garbage, repackage
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(out, "r:gz") as orig, tarfile.open(bad, "w:gz") as new:
        for member in orig.getmembers():
            if member.name.endswith("cir.json"):
                # Replace contents with garbage
                replacement = b"corrupted"
                member.size = len(replacement)
                import io
                new.addfile(member, io.BytesIO(replacement))
            else:
                f = orig.extractfile(member)
                if f is None:
                    new.addfile(member)
                else:
                    new.addfile(member, f)

    report = validate_snapshot(bad)
    assert not report.ok
    assert any("checksum mismatch" in e for e in report.errors)


def test_create_multiple_papers(tmp_path: Path) -> None:
    cir1 = tmp_path / "p1.json"
    cir2 = tmp_path / "p2.json"
    _write_cir(cir1, "p1")
    _write_cir(cir2, "p2")

    out = tmp_path / "snap.tar.gz"
    manifest = create_snapshot(
        [
            SnapshotEntry(paper_id="p1", cir_path=cir1),
            SnapshotEntry(paper_id="p2", cir_path=cir2),
        ],
        out,
    )
    assert manifest.papers == 2
    paper_paths = {f["path"] for f in manifest.files}
    assert "papers/p1/cir.json" in paper_paths
    assert "papers/p2/cir.json" in paper_paths


def test_with_optional_source_blob(tmp_path: Path) -> None:
    cir_path = tmp_path / "p1.cir.json"
    _write_cir(cir_path)
    src_path = tmp_path / "p1.source.tar.gz"
    src_path.write_bytes(b"fake source bundle data")

    out = tmp_path / "snap.tar.gz"
    manifest = create_snapshot(
        [SnapshotEntry(paper_id="p1", cir_path=cir_path, source_blob_path=src_path)],
        out,
    )
    paths = {f["path"] for f in manifest.files}
    assert "papers/p1/cir.json" in paths
    assert "papers/p1/source.tar.gz" in paths


def test_validate_returns_error_for_missing_file(tmp_path: Path) -> None:
    report = validate_snapshot(tmp_path / "does-not-exist.tar.gz")
    assert not report.ok
    assert any("no such file" in e for e in report.errors)
