"""Workspace + environment health check.

Runs a series of cheap checks that surface common setup problems
before a user files a "rrxiv parse breaks" issue:

- Is `tectonic` (or another LaTeX engine) on PATH?
- Is the bundled rrxiv.cls findable from the workspace pattern?
- Are the vendored schemas present and parseable?
- Are the auto-generated pydantic models importable?
- Does the protocol version in the schemas match what this rrxiv-python
  build expects?

Each check is small and reports independently, so partial failures
don't mask later passes.
"""

from __future__ import annotations

import importlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import rrxiv

CheckStatus = Literal["pass", "fail", "warn"]


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str = ""

    def render(self) -> str:
        glyph = {"pass": "[OK]", "warn": "[WARN]", "fail": "[FAIL]"}[self.status]
        out = f"  {glyph}  {self.name}"
        if self.detail:
            out += f"\n         {self.detail}"
        return out


def _check_python_package() -> CheckResult:
    return CheckResult(
        name="rrxiv package importable",
        status="pass",
        detail=f"rrxiv {rrxiv.__version__}",
    )


def _check_latex_engine() -> CheckResult:
    for engine in ("tectonic", "lualatex", "xelatex", "pdflatex"):
        if shutil.which(engine):
            return CheckResult(
                name="LaTeX engine on PATH",
                status="pass",
                detail=f"found {engine}",
            )
    return CheckResult(
        name="LaTeX engine on PATH",
        status="warn",
        detail=(
            "no tectonic / lualatex / xelatex / pdflatex on PATH. "
            "You can still parse already-compiled papers (the parser only needs "
            "the .tex source + the .rrxiv.aux sidecar) but you can't compile "
            "papers locally. `brew install tectonic` is the easiest fix."
        ),
    )


def _check_vendored_schemas() -> CheckResult:
    pkg_root = Path(rrxiv.__file__).resolve().parent
    schemas_dir = pkg_root / "_schemas"
    if not schemas_dir.is_dir():
        return CheckResult(
            name="vendored schemas present",
            status="fail",
            detail=f"no directory at {schemas_dir}; reinstall the package",
        )
    expected = {
        "paper.schema.json",
        "claim.schema.json",
        "annotation.schema.json",
        "citation.schema.json",
        "section.schema.json",
        "figure.schema.json",
        "cir.schema.json",
    }
    actual = {p.name for p in schemas_dir.glob("*.schema.json")}
    missing = expected - actual
    if missing:
        return CheckResult(
            name="vendored schemas present",
            status="fail",
            detail=f"missing: {sorted(missing)}; run scripts/sync_schemas.sh",
        )
    return CheckResult(
        name="vendored schemas present",
        status="pass",
        detail=f"{len(actual)} schemas at {schemas_dir}",
    )


def _check_schema_parseable() -> CheckResult:
    pkg_root = Path(rrxiv.__file__).resolve().parent
    schemas_dir = pkg_root / "_schemas"
    if not schemas_dir.is_dir():
        return CheckResult(
            name="schemas are parseable JSON",
            status="fail",
            detail="no _schemas/ dir (see previous check)",
        )
    for path in schemas_dir.glob("*.schema.json"):
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return CheckResult(
                name="schemas are parseable JSON",
                status="fail",
                detail=f"{path.name}: {e}",
            )
    return CheckResult(
        name="schemas are parseable JSON",
        status="pass",
    )


def _check_models_importable() -> CheckResult:
    try:
        importlib.import_module("rrxiv.models")
    except Exception as e:
        return CheckResult(
            name="generated models importable",
            status="fail",
            detail=f"import error: {e}; run scripts/regen_models.sh",
        )
    return CheckResult(
        name="generated models importable",
        status="pass",
    )


def _check_protocol_version_consistency() -> CheckResult:
    """Sanity: the version field in CIR schema matches the package's
    declared expectation."""
    pkg_root = Path(rrxiv.__file__).resolve().parent
    cir_path = pkg_root / "_schemas" / "cir.schema.json"
    if not cir_path.is_file():
        return CheckResult(
            name="CIR schema version matches package",
            status="fail",
            detail="no cir.schema.json (see vendored schemas check)",
        )
    cir = json.loads(cir_path.read_text(encoding="utf-8"))
    schema_version = cir.get("version", "unknown")
    expected = "0.1.0"
    if schema_version != expected:
        return CheckResult(
            name="CIR schema version matches package",
            status="warn",
            detail=(
                f"schema declares version {schema_version}; this rrxiv-python "
                f"build expects {expected}. Could be fine if forward-compat,"
                " but worth checking."
            ),
        )
    return CheckResult(
        name="CIR schema version matches package",
        status="pass",
        detail=f"v{schema_version}",
    )


def _check_optional_extra(
    extra_name: str, modules: list[str], *, fail_text: str
) -> CheckResult:
    """Generic helper: report whether a [extra] is installed."""
    import importlib.util

    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    if not missing:
        return CheckResult(
            name=f"[{extra_name}] extra installed",
            status="pass",
        )
    return CheckResult(
        name=f"[{extra_name}] extra installed",
        status="warn",
        detail=fail_text + f" Missing: {', '.join(missing)}.",
    )


def _check_agent_extra() -> CheckResult:
    return _check_optional_extra(
        "agent",
        ["cryptography", "http_message_signatures"],
        fail_text=(
            "agent identity flows (rrxiv login agent, "
            "RFC 9421 signing) need `pip install 'rrxiv[agent]'`."
        ),
    )


def _check_cli_extra() -> CheckResult:
    return _check_optional_extra(
        "cli",
        ["keyring"],
        fail_text=(
            "rrxiv login uses `keyring` for OS-native secure token "
            "storage. Without it, credentials fall back to a 0600 "
            "file at ~/.config/rrxiv/credentials.json. "
            "`pip install 'rrxiv[cli]'` for the keyring backend."
        ),
    )


def _check_server_extra() -> CheckResult:
    return _check_optional_extra(
        "server",
        ["fastapi", "uvicorn"],
        fail_text=(
            "the reference server (rrxiv serve) needs "
            "`pip install 'rrxiv[server]'`."
        ),
    )


def _check_keyring_backend() -> CheckResult:
    """If keyring is installed, is the backend usable?"""
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
        from keyring.backends.null import Keyring as NullKeyring
    except ImportError:
        return CheckResult(
            name="keyring backend usable",
            status="warn",
            detail="keyring not installed; the file fallback will be used.",
        )
    backend = keyring.get_keyring()
    name = type(backend).__name__
    if isinstance(backend, FailKeyring | NullKeyring):
        return CheckResult(
            name="keyring backend usable",
            status="warn",
            detail=(
                f"keyring is installed but no usable backend is configured "
                f"(found {name}). Credentials will fall back to "
                f"~/.config/rrxiv/credentials.json."
            ),
        )
    return CheckResult(
        name="keyring backend usable",
        status="pass",
        detail=f"using {name}",
    )


_CHECKS = (
    _check_python_package,
    _check_latex_engine,
    _check_vendored_schemas,
    _check_schema_parseable,
    _check_models_importable,
    _check_protocol_version_consistency,
    _check_agent_extra,
    _check_cli_extra,
    _check_server_extra,
    _check_keyring_backend,
)


def run_doctor() -> list[CheckResult]:
    """Run all checks and return their results."""
    return [check() for check in _CHECKS]


def overall_status(results: list[CheckResult]) -> CheckStatus:
    if any(r.status == "fail" for r in results):
        return "fail"
    if any(r.status == "warn" for r in results):
        return "warn"
    return "pass"


__all__ = ["CheckResult", "CheckStatus", "overall_status", "run_doctor"]
