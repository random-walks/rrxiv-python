"""rrvix CLI entry point."""

import typer

app = typer.Typer(no_args_is_help=True, help="rrvix — reference client for the rrvix protocol")


@app.command()
def parse(file: str, output: str | None = None) -> None:
    """Parse a rrvix-format TeX paper into a CIR JSON object."""
    typer.echo(f"[stub] would parse {file} → {output or 'stdout'}")


@app.command()
def validate(file: str) -> None:
    """Validate a CIR JSON file against the schema."""
    typer.echo(f"[stub] would validate {file}")


if __name__ == "__main__":
    app()
