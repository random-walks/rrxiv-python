"""Sidecar reader for rrvix.cls's `\\write` channel.

When a paper authored with ``rrvix.cls`` compiles, the class emits a
sidecar file alongside the PDF: ``<basename>.rrvix.aux``. The file is
line-oriented and contains markers in this format::

    RRVIX:meta:<key>:<value>            # paper metadata fields
    RRVIX:<env>:<index>                 # one per claim/evidence/observation/...
    RRVIX:edge:<edge_type>:<src>:<dst>  # claim-graph edges

This module parses that file into a list of structured records. The
parser/build module then pairs sidecar records with the matching source
environments to produce a CIR.

Sidecar emission is line-buffered TeX ``\\write`` output, so the file
may be missing trailing newlines or contain blank lines; the parser is
tolerant of both.
"""

from __future__ import annotations

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
    """A `RRVIX:meta:<key>:<value>` line."""

    key: str
    value: str


@dataclass(frozen=True, slots=True)
class EnvMarker:
    """A `RRVIX:<env>:<index>` line. Index is the LaTeX counter value at
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
    """A `RRVIX:edge:<edge_type>:<src>:<dst>` line."""

    edge_type: EdgeKind
    source: str
    target: str


@dataclass(frozen=True, slots=True)
class Sidecar:
    """All markers parsed from one ``*.rrvix.aux`` file."""

    meta: tuple[MetaMarker, ...]
    envs: tuple[EnvMarker, ...]
    edges: tuple[EdgeMarker, ...]

    def meta_dict(self) -> dict[str, str]:
        """Return metadata as a key→value dict. Later markers shadow earlier."""
        return {m.key: m.value for m in self.meta}

    def envs_of_kind(self, kind: EnvKind) -> tuple[EnvMarker, ...]:
        return tuple(e for e in self.envs if e.kind == kind)


def parse_sidecar_text(text: str) -> Sidecar:
    """Parse the contents of a ``*.rrvix.aux`` file into a Sidecar.

    Unrecognised lines are silently skipped. This is intentional — the
    sidecar format is line-prefixed (``RRVIX:``) precisely so that future
    versions of ``rrvix.cls`` can introduce new prefixes without breaking
    older parsers.
    """
    metas: list[MetaMarker] = []
    envs: list[EnvMarker] = []
    edges: list[EdgeMarker] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("RRVIX:"):
            continue
        parts = line.split(":", 1)[1].split(":")
        if not parts:
            continue
        head = parts[0]

        if head == "meta" and len(parts) >= 3 and parts[1] in _META_KEYS:
            value = ":".join(parts[2:])  # values may contain colons
            metas.append(MetaMarker(key=parts[1], value=value))
            continue

        if head == "edge" and len(parts) >= 4 and parts[1] in _EDGE_KINDS:
            edge_type: EdgeKind = parts[1]  # type: ignore[assignment]
            # FIXME(v0.2): rrvix.cls writes `RRVIX:edge:<type>:<src>:<dst>` with
            # `:` joining src and dst, but `:` is ALSO the conventional
            # separator inside IDs (paper_id:claim:label). The format is
            # genuinely ambiguous without a delimiter change. The
            # midpoint-split heuristic below works as long as src and dst
            # have the same colon count, which is true for the canonical
            # `<paper_id>:claim:<label>` shape but breaks otherwise. Track
            # the cls change in a future RRP.
            id_tokens = parts[2:]
            if len(id_tokens) % 2 != 0:
                # Odd token count: best-effort, take the last token as dst.
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

        # Unrecognised RRVIX:* line — skip silently.

    return Sidecar(meta=tuple(metas), envs=tuple(envs), edges=tuple(edges))


def parse_sidecar_file(path: Path | str) -> Sidecar:
    """Read and parse a sidecar file from disk."""
    return parse_sidecar_text(Path(path).read_text(encoding="utf-8"))
