"""rrxiv CLI entry point."""

from __future__ import annotations

import enum
import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from rrxiv.annotations import (
    AnnotationPayloadError,
    load_annotations_file,
    validate_annotation_payload,
)
from rrxiv.diff import diff_cir
from rrxiv.doctor import overall_status, run_doctor
from rrxiv.graph import ClaimGraph
from rrxiv.models import CIR
from rrxiv.parser import build_cir
from rrxiv.scaffold import ScaffoldOptions, scaffold_paper
from rrxiv.snapshot import SnapshotEntry, create_snapshot, validate_snapshot


class GraphFormat(enum.StrEnum):
    mermaid = "mermaid"
    dot = "dot"
    json = "json"

app = typer.Typer(
    no_args_is_help=True,
    help="rrxiv — reference client for the rrxiv protocol",
)

annotation_app = typer.Typer(
    no_args_is_help=True,
    help="Annotation utilities (load, validate).",
)
app.add_typer(annotation_app, name="annotation")

# Login subcommands (RRP-0006).
from rrxiv.cli.login import login_app  # noqa: E402

app.add_typer(login_app, name="login")


# Logout (alias for `login logout`).
@app.command()
def logout(
    server: Annotated[str | None, typer.Option("--server")] = None,
    identity_type: Annotated[
        str | None, typer.Option("--identity")
    ] = None,
    all_servers: Annotated[bool, typer.Option("--all")] = False,
) -> None:
    """Forget stored credentials. Alias for ``rrxiv login logout``."""
    from rrxiv.cli.login import logout as _logout

    _logout(server=server, identity_type=identity_type, all_servers=all_servers)


# Serve (RRP-0008).
@app.command()
def conformance(
    server: Annotated[
        str,
        typer.Argument(help="API base URL, e.g. http://127.0.0.1:8000/api/v0"),
    ],
    keep_state: Annotated[
        bool, typer.Option("--keep-state/--clean")
    ] = False,
) -> None:
    """Run the canonical conformance suite against any rrxiv server.

    Useful for validating other-language client+server pairs.
    """
    from rrxiv.cli.conformance import conformance as _conformance

    _conformance(server=server, keep_state=keep_state)


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8000,
    dev_mode: Annotated[
        bool, typer.Option("--dev-mode/--no-dev-mode")
    ] = True,
    reload: Annotated[bool, typer.Option("--reload/--no-reload")] = False,
    store: Annotated[
        str,
        typer.Option(
            "--store", help="Backend URL: memory:// or sqlite:///path."
        ),
    ] = "memory://",
) -> None:
    """Start the rrxiv reference FastAPI server."""
    from rrxiv.cli.serve import serve as _serve

    _serve(host=host, port=port, dev_mode=dev_mode, reload=reload, store=store)


@app.command()
def parse(
    file: Annotated[Path, typer.Argument(help="Path to the .tex source file.")],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Where to write the CIR JSON. Default: stdout."),
    ] = None,
    sidecar: Annotated[
        Path | None,
        typer.Option(
            "--sidecar",
            "-s",
            help="Path to the .rrxiv.aux sidecar. Default: <file>.rrxiv.aux beside the .tex.",
        ),
    ] = None,
    bib: Annotated[
        Path | None,
        typer.Option(
            "--bib",
            "-b",
            help="Path to the .bib file. Default: looks up \\bibliography{NAME} in the .tex.",
        ),
    ] = None,
    indent: Annotated[
        int,
        typer.Option(help="Indent for the output JSON. 0 for compact."),
    ] = 2,
) -> None:
    """Parse a rrxiv-format TeX paper into a CIR JSON object."""
    cir = build_cir(file, sidecar_path=sidecar, bib_path=bib)
    payload = cir.model_dump(mode="json", exclude_none=True)
    text = json.dumps(payload, indent=indent if indent > 0 else None, ensure_ascii=False)
    if output is None:
        typer.echo(text)
    else:
        output.write_text(text + "\n", encoding="utf-8")
        typer.echo(f"Wrote CIR to {output}", err=True)


@app.command()
def validate(
    file: Annotated[Path, typer.Argument(help="Path to a CIR JSON file.")],
) -> None:
    """Validate a CIR JSON file against the schema."""
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
        CIR.model_validate(data)
    except json.JSONDecodeError as e:
        typer.echo(f"FAIL: {file} is not valid JSON: {e}", err=True)
        sys.exit(1)
    except ValidationError as e:
        typer.echo(f"FAIL: {file} does not validate as a CIR:\n{e}", err=True)
        sys.exit(1)
    typer.echo(f"OK: {file} validates as a CIR.")


@app.command()
def graph(
    file: Annotated[
        Path,
        typer.Argument(
            help="Path to a CIR JSON file or a .tex source. If .tex, it's parsed first.",
        ),
    ],
    fmt: Annotated[
        GraphFormat,
        typer.Option("--format", "-f", help="Output format."),
    ] = GraphFormat.mermaid,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Where to write the output. Default: stdout."),
    ] = None,
) -> None:
    """Dump the claim graph from a CIR (or a .tex parsed on the fly).

    The Mermaid output can be pasted into a Markdown file rendered by any
    Mermaid-aware viewer (GitHub, mkdocs-material). The DOT output feeds
    Graphviz. The JSON output is the structured graph view, suitable for
    further processing.
    """
    if file.suffix == ".tex":
        cir = build_cir(file)
    else:
        cir = CIR.model_validate(json.loads(file.read_text(encoding="utf-8")))

    g = ClaimGraph.from_cir(cir)

    if fmt is GraphFormat.mermaid:
        text = g.to_mermaid()
    elif fmt is GraphFormat.dot:
        text = g.to_dot()
    else:  # json
        text = json.dumps(g.to_dict(), indent=2)

    if output is None:
        typer.echo(text)
    else:
        output.write_text(text + "\n", encoding="utf-8")
        typer.echo(f"Wrote graph to {output}", err=True)


@annotation_app.command("validate")
def annotation_validate(
    file: Annotated[
        Path,
        typer.Argument(
            help="Path to a JSON file with one annotation (object) or an array of annotations.",
        ),
    ],
    strict: Annotated[
        bool,
        typer.Option(
            "--strict/--no-strict",
            help="If --strict (default), also validate per-type structured_payload shapes.",
        ),
    ] = True,
) -> None:
    """Validate annotations against the schema, optionally per-type strict.

    Without ``--strict``, this checks only that each annotation matches the
    base ``annotation.schema.json``. With ``--strict`` (the default), each
    annotation's ``structured_payload`` is also checked against the
    per-type schema documented in ``spec/0006-annotations.md``.
    """
    try:
        annotations = load_annotations_file(file)
    except ValidationError as e:
        typer.echo(f"FAIL: {file} contains invalid annotation(s):\n{e}", err=True)
        sys.exit(1)
    except (json.JSONDecodeError, ValueError) as e:
        typer.echo(f"FAIL: {file} is not loadable: {e}", err=True)
        sys.exit(1)

    if strict:
        bad: list[str] = []
        for i, ann in enumerate(annotations):
            try:
                validate_annotation_payload(ann)
            except AnnotationPayloadError as e:
                bad.append(f"  [{i}] {ann.id}: {e}")
        if bad:
            typer.echo(
                f"FAIL: {len(bad)} annotation(s) had invalid payloads:", err=True
            )
            for line in bad:
                typer.echo(line, err=True)
            sys.exit(1)

    typer.echo(
        f"OK: {len(annotations)} annotation(s) validate"
        + (" (incl. per-type payloads)." if strict else " (base schema only).")
    )


@app.command()
def init(
    directory: Annotated[
        Path,
        typer.Argument(help="Directory to create. Must not exist or must be empty."),
    ],
    paper_id: Annotated[
        str,
        typer.Option(
            "--id",
            help="Stable paper ID for \\rrxivid. Conventionally the directory name.",
        ),
    ],
    title: Annotated[
        str,
        typer.Option("--title", "-t", help="Paper title."),
    ] = "TODO: title",
    author: Annotated[
        str,
        typer.Option(
            "--author",
            "-a",
            help="Author name (single string for v0; future: ORCID-aware).",
        ),
    ] = "TODO: Author Name",
    license: Annotated[
        str,
        typer.Option("--license", help="SPDX license identifier."),
    ] = "CC-BY-4.0",
    topics: Annotated[
        str,
        typer.Option("--topics", help="Comma-separated topic IDs."),
    ] = "",
    basename: Annotated[
        str | None,
        typer.Option(
            "--basename",
            help="Filename stem for the .tex/.bib (defaults to paper_id).",
        ),
    ] = None,
) -> None:
    """Scaffold a new rrxiv paper directory.

    Writes a self-contained directory with the paper's .tex, .bib, a
    bundled rrxiv.cls, and a README. The output compiles cleanly with
    tectonic and produces a valid CIR via `rrxiv parse`.
    """
    topics_tuple = tuple(t.strip() for t in topics.split(",") if t.strip())
    opts = ScaffoldOptions(
        paper_id=paper_id,
        title=title,
        author=author,
        license=license,
        topics=topics_tuple,
        basename=basename,
    )
    try:
        target = scaffold_paper(directory, opts)
    except FileExistsError as e:
        typer.echo(f"FAIL: {e}", err=True)
        sys.exit(1)
    typer.echo(f"OK: scaffolded {target}")
    bn = basename or paper_id
    typer.echo(f"  Build: tectonic --keep-intermediates {target}/{bn}.tex")
    typer.echo(f"  Parse: rrxiv parse {target}/{bn}.tex")


@app.command()
def diff(
    before: Annotated[Path, typer.Argument(help="Earlier CIR JSON file.")],
    after: Annotated[Path, typer.Argument(help="Later CIR JSON file.")],
    output_format: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="Output format: 'summary' (default, human-readable) or 'json' (structured).",
        ),
    ] = "summary",
) -> None:
    """Semantic diff between two CIR documents (e.g. two paper revisions).

    Reports added/removed/changed claims, added/removed edges,
    citation deltas, annotation deltas, and top-level field changes.
    Ignores environment-specific fields like submitted_at.
    """
    before_cir = CIR.model_validate(json.loads(before.read_text(encoding="utf-8")))
    after_cir = CIR.model_validate(json.loads(after.read_text(encoding="utf-8")))
    d = diff_cir(before_cir, after_cir)

    if output_format == "json":
        typer.echo(json.dumps(d.to_dict(), indent=2, default=str))
    elif output_format == "summary":
        typer.echo(d.summary())
    else:
        typer.echo(
            f"FAIL: unknown format '{output_format}' (expected 'summary' or 'json')",
            err=True,
        )
        sys.exit(1)


@app.command()
def doctor() -> None:
    """Check workspace + environment health.

    Reports per-check status (PASS/WARN/FAIL) for: package importable,
    LaTeX engine on PATH, vendored schemas present and parseable,
    generated models importable, CIR schema version matches the
    package's expectation. Exit code 1 on any FAIL, 0 otherwise.
    WARN does not affect the exit code.
    """
    results = run_doctor()
    typer.echo("rrxiv doctor")
    typer.echo("------------")
    for r in results:
        typer.echo(r.render())
    typer.echo("")
    status = overall_status(results)
    if status == "fail":
        typer.echo("Overall: FAIL — fix the [FAIL] checks above.", err=True)
        sys.exit(1)
    if status == "warn":
        typer.echo("Overall: PASS with warnings.")
    else:
        typer.echo("Overall: PASS.")


snapshot_app = typer.Typer(
    no_args_is_help=True,
    help="Snapshot tarball create / validate.",
)
app.add_typer(snapshot_app, name="snapshot")


@snapshot_app.command("create")
def snapshot_create(
    directory: Annotated[
        Path,
        typer.Argument(
            help=(
                "Directory containing per-paper subdirs. Each subdir's name is "
                "the paper_id; each must contain at least cir.json (and may "
                "optionally contain source.tar.gz)."
            ),
        ),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Output tarball path."),
    ],
    snapshot_id: Annotated[
        str | None,
        typer.Option("--snapshot-id", help="Snapshot ID. Defaults to a generated value."),
    ] = None,
    download_uri: Annotated[
        str,
        typer.Option(
            "--download-uri",
            help="Public retrieval URI to record in the manifest. Optional.",
        ),
    ] = "",
) -> None:
    """Create a snapshot tarball from a directory of per-paper subdirs."""
    if not directory.is_dir():
        typer.echo(f"FAIL: not a directory: {directory}", err=True)
        sys.exit(1)
    entries: list[SnapshotEntry] = []
    for paper_dir in sorted(directory.iterdir()):
        if not paper_dir.is_dir():
            continue
        cir_path = paper_dir / "cir.json"
        if not cir_path.is_file():
            typer.echo(
                f"WARN: skipping {paper_dir.name}: no cir.json", err=True
            )
            continue
        candidate_blob = paper_dir / "source.tar.gz"
        source_blob_path: Path | None = (
            candidate_blob if candidate_blob.is_file() else None
        )
        entries.append(
            SnapshotEntry(
                paper_id=paper_dir.name,
                cir_path=cir_path,
                source_blob_path=source_blob_path,
            )
        )
    if not entries:
        typer.echo(f"FAIL: no per-paper subdirectories with cir.json in {directory}", err=True)
        sys.exit(1)
    manifest = create_snapshot(
        entries,
        output,
        snapshot_id=snapshot_id,
        download_uri=download_uri,
    )
    typer.echo(f"OK: wrote {output} ({manifest.papers} papers, {manifest.size_bytes} bytes)")
    typer.echo(f"  snapshot_id: {manifest.snapshot_id}")
    typer.echo(f"  sha256:      {manifest.sha256}")


@snapshot_app.command("validate")
def snapshot_validate(
    tarball: Annotated[Path, typer.Argument(help="Path to the snapshot tarball.")],
) -> None:
    """Verify a snapshot tarball's manifest, file list, and checksums."""
    report = validate_snapshot(tarball)
    if not report.ok:
        for err in report.errors:
            typer.echo(f"FAIL: {err}", err=True)
        sys.exit(1)
    if report.warnings:
        for w in report.warnings:
            typer.echo(f"WARN: {w}", err=True)
    typer.echo(f"OK: {tarball} validates as a rrxiv snapshot.")


if __name__ == "__main__":
    app()
