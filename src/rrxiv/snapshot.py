"""Snapshot tarball creation + validation.

Snapshot exports are mandatory and free per
``spec/0008-governance.md`` locked principle 3. Any rrxiv server must
expose a complete, downloadable corpus archive at no cost. This module
implements both sides:

- ``create_snapshot(directory, manifest_out)`` — package a directory
  of CIRs + a manifest JSON into a tar.gz, with SHA-256 checksums.
- ``validate_snapshot(tarball)`` — verify a tarball's manifest, file
  list, and checksums.

This implementation is self-contained (no server needed). Useful for:
  * Developers testing snapshot pipelines locally.
  * Mirror operators downloading + verifying upstream snapshots.
  * Anyone forking the corpus.

The tarball format is conservative — gzipped POSIX tar with a single
top-level directory whose name is the snapshot ID, containing a
``MANIFEST.json`` and any number of ``papers/<paper_id>/`` directories
with the CIR JSON file plus optional ``source.tar.gz`` blobs.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import tarfile
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    """One paper's worth of files within a snapshot."""

    paper_id: str
    cir_path: Path
    source_blob_path: Path | None = None


@dataclass
class SnapshotManifest:
    """The manifest emitted alongside the tarball.

    Mirrors the SnapshotManifest schema in ``schema/api.openapi.yaml``;
    this in-memory form is what create + validate operate on.
    """

    snapshot_id: str
    created_at: str
    papers: int
    annotations: int
    sha256: str
    size_bytes: int
    download_uri: str

    files: list[dict[str, Any]] = field(default_factory=list)
    """Per-file metadata: {path, sha256, size_bytes, kind}."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "papers": self.papers,
            "annotations": self.annotations,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "download_uri": self.download_uri,
            "files": list(self.files),
        }


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Result of :func:`validate_snapshot`."""

    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _sha256_file(path: Path) -> str:
    """Stream-hash a file. SHA-256 hex digest."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def _utc_now_isoformat() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")


def create_snapshot(
    entries: Iterable[SnapshotEntry],
    output_tarball: Path | str,
    *,
    snapshot_id: str | None = None,
    download_uri: str = "",
) -> SnapshotManifest:
    """Create a snapshot tarball from a set of paper entries.

    Each entry contributes its CIR JSON (required) and optionally a
    source tarball. The output is a gzipped tar containing:

        <snapshot_id>/MANIFEST.json
        <snapshot_id>/papers/<paper_id>/cir.json
        <snapshot_id>/papers/<paper_id>/source.tar.gz   (if entry.source_blob_path)

    Returns the manifest (which is also embedded in the tarball).
    """
    output_path = Path(output_tarball).resolve()
    snap_id = snapshot_id or f"snap-{_utc_now_isoformat()}-{uuid.uuid4().hex[:8]}"
    created_at = _utc_now_isoformat()

    entries_list = list(entries)
    file_records: list[dict[str, Any]] = []

    # Stage in a temp directory under <snap_id>/, then tar it.
    with tempfile.TemporaryDirectory() as staging_root:
        staging = Path(staging_root) / snap_id
        staging.mkdir()
        papers_dir = staging / "papers"
        papers_dir.mkdir()

        annotations_total = 0
        for entry in entries_list:
            paper_dir = papers_dir / entry.paper_id
            paper_dir.mkdir()

            # CIR
            cir_dest = paper_dir / "cir.json"
            cir_dest.write_text(entry.cir_path.read_text(encoding="utf-8"), encoding="utf-8")
            cir_sha = _sha256_file(cir_dest)
            file_records.append(
                {
                    "path": f"papers/{entry.paper_id}/cir.json",
                    "sha256": cir_sha,
                    "size_bytes": cir_dest.stat().st_size,
                    "kind": "cir",
                }
            )
            # Count annotations from this CIR for the manifest
            try:
                cir_data = json.loads(cir_dest.read_text(encoding="utf-8"))
                annotations_total += len(cir_data.get("annotations") or [])
            except json.JSONDecodeError:
                pass

            # Optional source bundle
            if entry.source_blob_path is not None:
                src_dest = paper_dir / "source.tar.gz"
                src_dest.write_bytes(entry.source_blob_path.read_bytes())
                file_records.append(
                    {
                        "path": f"papers/{entry.paper_id}/source.tar.gz",
                        "sha256": _sha256_file(src_dest),
                        "size_bytes": src_dest.stat().st_size,
                        "kind": "source-bundle",
                    }
                )

        # Manifest goes inside the staging dir BEFORE we tar
        manifest_path = staging / "MANIFEST.json"
        manifest_dict: dict[str, Any] = {
            "snapshot_id": snap_id,
            "created_at": created_at,
            "papers": len(entries_list),
            "annotations": annotations_total,
            # sha256 + size_bytes describe the *outer* tarball; we stamp
            # them in below after writing.
            "sha256": "",
            "size_bytes": 0,
            "download_uri": download_uri,
            "files": file_records,
        }
        manifest_path.write_text(
            json.dumps(manifest_dict, indent=2), encoding="utf-8"
        )

        # Tar everything under staging into the output path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output_path, "w:gz") as tar:
            tar.add(staging, arcname=snap_id)

        # Now compute outer-tarball checksum and update the manifest
        outer_sha = _sha256_file(output_path)
        outer_size = output_path.stat().st_size
        manifest_dict["sha256"] = outer_sha
        manifest_dict["size_bytes"] = outer_size

    # Write the *outer* manifest next to the tarball for clients that
    # want the manifest without un-taring.
    side_manifest = output_path.with_suffix(".manifest.json")
    side_manifest.write_text(
        json.dumps(manifest_dict, indent=2), encoding="utf-8"
    )

    return SnapshotManifest(
        snapshot_id=snap_id,
        created_at=created_at,
        papers=len(entries_list),
        annotations=annotations_total,
        sha256=outer_sha,
        size_bytes=outer_size,
        download_uri=download_uri,
        files=file_records,
    )


def validate_snapshot(tarball: Path | str) -> ValidationReport:
    """Verify a snapshot tarball.

    Checks:
    - The tarball is a well-formed .tar.gz.
    - It contains a single top-level directory.
    - That directory contains a `MANIFEST.json` parseable as JSON.
    - Every file the manifest lists is present in the tarball.
    - SHA-256 of every listed file matches the manifest's claim.

    Returns a :class:`ValidationReport`. If ``ok`` is False, ``errors``
    enumerates the problems.
    """
    tarball_path = Path(tarball).resolve()
    errors: list[str] = []
    warnings: list[str] = []

    if not tarball_path.is_file():
        return ValidationReport(ok=False, errors=(f"no such file: {tarball_path}",))

    try:
        with tarfile.open(tarball_path, "r:gz") as tar:
            members = tar.getmembers()
            top_level = {m.name.split("/", 1)[0] for m in members}
            if len(top_level) != 1:
                return ValidationReport(
                    ok=False,
                    errors=(f"expected single top-level dir; got {sorted(top_level)}",),
                )
            snap_id = next(iter(top_level))

            manifest_member = next(
                (m for m in members if m.name == f"{snap_id}/MANIFEST.json"),
                None,
            )
            if manifest_member is None:
                return ValidationReport(
                    ok=False, errors=(f"no MANIFEST.json under {snap_id}/",)
                )

            f = tar.extractfile(manifest_member)
            if f is None:
                return ValidationReport(ok=False, errors=("manifest unreadable",))
            try:
                manifest = json.loads(f.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                return ValidationReport(
                    ok=False, errors=(f"manifest not parseable JSON: {e}",)
                )

            for record in manifest.get("files", []):
                rel_path = record["path"]
                expected_sha = record["sha256"]
                member_name = f"{snap_id}/{rel_path}"
                member = next((m for m in members if m.name == member_name), None)
                if member is None:
                    errors.append(f"missing file in tarball: {rel_path}")
                    continue
                fr = tar.extractfile(member)
                if fr is None:
                    errors.append(f"unreadable: {rel_path}")
                    continue
                h = hashlib.sha256()
                while chunk := fr.read(8192):
                    h.update(chunk)
                actual_sha = h.hexdigest()
                if actual_sha != expected_sha:
                    errors.append(
                        f"checksum mismatch for {rel_path}: "
                        f"manifest says {expected_sha}, actual {actual_sha}"
                    )

            # Optional: warn if manifest's outer-tarball sha doesn't match
            # the file we received. (Common: the manifest is computed
            # before the tarball is written, so the outer sha is
            # post-write.)
            outer_sha = _sha256_file(tarball_path)
            if manifest.get("sha256") and manifest["sha256"] != outer_sha:
                warnings.append(
                    f"outer tarball sha256 ({outer_sha[:12]}…) does not match "
                    f"manifest's claim ({manifest['sha256'][:12]}…). This is OK "
                    f"if the manifest was sealed after the tarball was created."
                )

    except tarfile.TarError as e:
        return ValidationReport(ok=False, errors=(f"tar error: {e}",))

    return ValidationReport(
        ok=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


__all__ = [
    "SnapshotEntry",
    "SnapshotManifest",
    "ValidationReport",
    "create_snapshot",
    "validate_snapshot",
]
