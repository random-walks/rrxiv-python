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
from rrxiv.graph import ClaimGraph
from rrxiv.models import CIR
from rrxiv.parser import build_cir


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


if __name__ == "__main__":
    app()
