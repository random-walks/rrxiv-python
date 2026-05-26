"""Sidecar reader for rrxiv.cls's `\\write` channel.

When a paper authored with ``rrxiv.cls`` compiles, the class emits a
sidecar file alongside the PDF: ``<basename>.rrxiv.aux``. The file is
line-oriented and contains markers in two formats:

**v0.2 format (current — RRP-0002):**

    RRXIV:meta:<key>:<value>            # paper metadata fields
    RRXIV:<env>:<index>                 # one per claim/evidence/observation/...
    RRXIV:edge:<edge_type>:<src>|<dst>  # claim-graph edges, pipe-separated IDs

**v0.1 format (deprecated, still parsed with a warning):**

    RRXIV:edge:<edge_type>:<src>:<dst>  # colons everywhere

The v0.1 format was ambiguous because `:` joined ID components AND
separated src from dst. RRP-0002 changed the separator to `|`. The
parser still accepts the v0.1 format via a midpoint-split heuristic,
emitting a ``DeprecationWarning`` per file containing v0.1 edges so
authors know to recompile with the v0.2 cls.

Sidecar emission is line-buffered TeX ``\\write`` output, so the file
may be missing trailing newlines or contain blank lines; the parser is
tolerant of both.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

EnvKind = Literal[
    "claim",
    "evidence",
    "observation",
    "scope",
    "openquestion",
    "remark",
]
"""The semantic environments that emit a sidecar marker on `\\begin{...}`."""

EdgeKind = Literal["depends_on", "supports", "contradicts", "extends"]
"""Edge kinds declared via inline `\\dependson` / `\\supports` / etc."""

_ENV_KINDS = frozenset(
    ("claim", "evidence", "observation", "scope", "openquestion", "remark")
)
_EDGE_KINDS = frozenset(("depends_on", "supports", "contradicts", "extends"))
_META_KEYS = frozenset(("id", "version", "protocol", "license", "topics"))


@dataclass(frozen=True, slots=True)
class MetaMarker:
    """A `RRXIV:meta:<key>:<value>` line."""

    key: str
    value: str


@dataclass(frozen=True, slots=True)
class EnvMarker:
    """A `RRXIV:<env>:<index>` line. Index is the LaTeX counter value at
    `\\begin` time (``\\@currentlabel`` from the matching ``\\newtheorem``).

    Note that `index` is a *string* — it carries whatever TeX wrote, even
    if the value is empty (which happens when the marker fires before the
    counter has been incremented; such markers are filtered before
    pairing with source environments).
    """

    kind: EnvKind
    index: str


@dataclass(frozen=True, slots=True)
class EdgeMarker:
    """A `RRXIV:edge:<edge_type>:<src>:<dst>` line."""

    edge_type: EdgeKind
    source: str
    target: str


@dataclass(frozen=True, slots=True)
class AuthorMarker:
    """A `RRXIV:author:<n>|key=value|key=value|...` line, emitted by
    ``rrxiv.cls`` v0.6's ``\\rrxivauthor`` macro (RRP-0021 + RRP-0025).

    ``index`` is the declaration order from the .tex. ``fields`` is a
    plain dict of the structured keys (name, orcid, role, handle,
    is_agent, affiliation, email, model_slug, model_family,
    model_release_date, inference_environment). Empty values are
    stripped at parse time.
    """

    index: int
    fields: dict[str, str]


@dataclass(frozen=True, slots=True)
class Sidecar:
    """All markers parsed from one ``*.rrxiv.aux`` file."""

    meta: tuple[MetaMarker, ...]
    envs: tuple[EnvMarker, ...]
    edges: tuple[EdgeMarker, ...]
    authors: tuple[AuthorMarker, ...] = ()

    def meta_dict(self) -> dict[str, str]:
        """Return metadata as a key→value dict. Later markers shadow earlier."""
        return {m.key: m.value for m in self.meta}

    def envs_of_kind(self, kind: EnvKind) -> tuple[EnvMarker, ...]:
        return tuple(e for e in self.envs if e.kind == kind)


def parse_sidecar_text(text: str) -> Sidecar:
    """Parse the contents of a ``*.rrxiv.aux`` file into a Sidecar.

    Unrecognised lines are silently skipped. This is intentional — the
    sidecar format is line-prefixed (``RRXIV:``) precisely so that future
    versions of ``rrxiv.cls`` can introduce new prefixes without breaking
    older parsers.

    If the input contains v0.1-format edges (colon-joined ``src:dst`` rather
    than v0.2's ``src|dst``), emits a single ``DeprecationWarning`` per
    parse call so authors know to recompile with the v0.2 cls.
    """
    metas: list[MetaMarker] = []
    envs: list[EnvMarker] = []
    edges: list[EdgeMarker] = []
    authors: list[AuthorMarker] = []
    # The set of structured-author keys cls v0.6 emits. Anything not on
    # this list is dropped silently — future cls versions can add new
    # keys without breaking older parsers.
    _AUTHOR_KEYS = frozenset((
        "name",
        "orcid",
        "role",
        "handle",
        "is_agent",
        "affiliation",
        "email",
        "model_slug",
        "model_family",
        "model_release_date",
        "inference_environment",
    ))

    # Per-call latch: only warn once even if many v0.1 edges appear.
    _v01_deprecation_seen = [False]

    # Per-call latch: only warn once if any RRVIX:-prefixed legacy lines
    # appear (papers compiled with the pre-rename rrvix.cls).
    _legacy_prefix_seen = [False]

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("RRXIV:"):
            line_prefix = "RRXIV"
        elif line.startswith("RRVIX:"):
            # v0.1 (pre-rename) prefix. Normalise to RRXIV: for
            # downstream processing; latch a deprecation warning.
            _legacy_prefix_seen[0] = True
            line_prefix = "RRXIV"
            line = "RRXIV:" + line[len("RRVIX:") :]
        else:
            continue
        parts = line.split(":", 1)[1].split(":")
        if not parts:
            continue
        head = parts[0]

        if head == "meta" and len(parts) >= 3 and parts[1] in _META_KEYS:
            value = ":".join(parts[2:])  # values may contain colons
            metas.append(MetaMarker(key=parts[1], value=value))
            continue

        if head == "author":
            # RRXIV:author:<n>|name=...|orcid=...|... — the prefix is
            # colon-split, so the index is in parts[1] and the rest is
            # in the original line after the second `:`.
            if len(parts) < 2:
                continue
            try:
                idx = int(parts[1].split("|", 1)[0])
            except ValueError:
                continue
            suffix = line[len(f"{line_prefix}:author:") :]
            # suffix is "<n>|name=...|orcid=..."
            after_idx = suffix.split("|", 1)
            if len(after_idx) < 2:
                continue
            fields: dict[str, str] = {}
            for kv in after_idx[1].split("|"):
                if "=" not in kv:
                    continue
                key, _, value = kv.partition("=")
                key = key.strip()
                value = value.strip()
                if key in _AUTHOR_KEYS and value:
                    fields[key] = value
            if fields:
                authors.append(AuthorMarker(index=idx, fields=fields))
            continue

        if head == "edge" and len(parts) >= 3 and parts[1] in _EDGE_KINDS:
            edge_type: EdgeKind = parts[1]  # type: ignore[assignment]
            # Reconstruct the suffix after `<prefix>:edge:<type>:` so we can
            # check for the v0.2 pipe delimiter, which the colon-tokenisation
            # above would have split.
            suffix = line[len(f"{line_prefix}:edge:{edge_type}:") :]

            if "|" in suffix:
                # v0.2 (RRP-0002) format: src|dst, unambiguous.
                source, target = suffix.split("|", 1)
            else:
                # v0.1 fallback: midpoint-split on colons. Works only when
                # src and dst share the same colon count, which is true for
                # the canonical `<paper_id>:claim:<label>` shape but
                # ambiguous in general.
                _v01_deprecation_seen[0] = True
                id_tokens = parts[2:]
                if len(id_tokens) % 2 != 0:
                    source = ":".join(id_tokens[:-1])
                    target = id_tokens[-1]
                else:
                    mid = len(id_tokens) // 2
                    source = ":".join(id_tokens[:mid])
                    target = ":".join(id_tokens[mid:])

            if not source or not target:
                continue
            edges.append(EdgeMarker(edge_type=edge_type, source=source, target=target))
            continue

        if head in _ENV_KINDS and len(parts) >= 2:
            kind: EnvKind = head  # type: ignore[assignment]
            index = ":".join(parts[1:])
            envs.append(EnvMarker(kind=kind, index=index))
            continue

        # Unrecognised RRXIV:* line — skip silently.

    if _legacy_prefix_seen[0]:
        warnings.warn(
            "Sidecar uses the legacy RRVIX: prefix (pre-rename rrvix.cls). "
            "The protocol was renamed to rrxiv; recompile with rrxiv.cls so "
            "the sidecar emits RRXIV:* markers.",
            DeprecationWarning,
            stacklevel=2,
        )

    if _v01_deprecation_seen[0]:
        warnings.warn(
            "Sidecar contains v0.1-format edges (colon-joined). The format is "
            "ambiguous when source/target IDs contain colons. Recompile with "
            "rrxiv.cls v0.2 or later, which uses '|' as the src/dst delimiter "
            "(see RRP-0002).",
            DeprecationWarning,
            stacklevel=2,
        )

    return Sidecar(
        meta=tuple(metas),
        envs=tuple(envs),
        edges=tuple(edges),
        authors=tuple(authors),
    )


def parse_sidecar_file(path: Path | str) -> Sidecar:
    """Read and parse a sidecar file from disk."""
    return parse_sidecar_text(Path(path).read_text(encoding="utf-8"))
