# Changelog

All notable changes to `rrxiv-python` are recorded here. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows [SemVer](https://semver.org/spec/v2.0.0.html) once it ships its first stable release. While in pre-1.0, breaking changes can land at any minor version.

## [Unreleased]

### Added

- `rrxiv doctor` — health check for the package + environment.
- `rrxiv diff` — semantic diff between two CIRs (claims, edges, citations, annotations, top-level fields).
- `rrxiv init` — scaffold a new rrxiv paper directory with a bundled `rrxiv.cls`.
- Standalone `Section` and `Figure` pydantic models (was: `$defs` of `cir.schema.json`).
- `AsyncRrxivClient` — `httpx.AsyncClient`-backed mirror of the sync client, with `async for paper in client.iter_papers(...)` style helpers.
- Configurable opt-in retry policy on the HTTP client; default = 3 retries / 30s total budget / no 5xx retry. `Retry-After` honoured; jittered exponential backoff otherwise.
- `rrxiv.annotations` module with per-type `structured_payload` validators per `spec/0006-annotations.md`. `rrxiv annotation validate <file>` CLI subcommand.
- `rrxiv.graph.ClaimGraph` — typed directed multigraph with traversal, cycle detection, Mermaid / DOT / JSON output. `rrxiv graph` CLI.
- TeX-to-text cleaning of CIR title/abstract/claim statements; the canonical fields no longer carry `\Large`, `\\[0.2em]`, `\texttt{...}` etc.
- `\bibitem` extraction from inline `thebibliography` blocks (the rrxiv whitepaper's bibliography now resolves into the CIR).
- Sidecar parser back-compat: accepts both the post-rename `RRXIV:` prefix and the pre-rename `RRVIX:` prefix (with a `DeprecationWarning`).

### Changed

- **Project renamed: `rrvix` → `rrxiv`.** Every Python identifier (`from rrvix import ...` → `from rrxiv import ...`), the CLI command name (`rrvix parse` → `rrxiv parse`), the package name on PyPI (when published) all moved. The legacy `RRVIX:` sidecar prefix and `.rrvix.aux` extension are still parsed, with a deprecation warning.
- HTTP client errors now follow [RFC 9457 Problem Details](https://datatracker.ietf.org/doc/rfc9457/) for the parsed body when the server provides one.
- TeX parser now applies the TeX-to-text cleaner to title, abstract, and claim statements; CIRs read like prose.
- Edge marker delimiter in the cls is `|` (was `:`); the parser accepts both, with a deprecation warning on the colon form (per [RRP-0002](https://github.com/random-walks/rrxiv/blob/main/proposals/0002-edge-marker-delimiter.md)).

### Fixed

- DOI regex in `citation.schema.json` now allows lowercase letters (real-world DOIs are mixed case).
- Citation field URL strips a wrapping `\url{...}` macro so it passes pydantic `AnyUrl` validation.

## [0.1.0] — initial scaffolding

- Pydantic v2 models generated from the rrxiv JSON Schemas (paper, claim, annotation, citation, cir).
- TeX parser v0: regex-based walker, sidecar reader, build_cir() producing a validated CIR.
- `rrxiv parse` and `rrxiv validate` CLI commands.
- Schema sync mechanism (`scripts/sync_schemas.sh`) and codegen mechanism (`scripts/regen_models.sh`).
- MIT license.
