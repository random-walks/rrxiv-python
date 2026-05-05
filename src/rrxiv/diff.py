"""Semantic diff between two CIR documents.

Emits a structured report of what changed between two paper revisions:
title / abstract / topic changes, claim additions/removals/edits,
edge additions/removals, citation deltas, annotation deltas.

Intended use: review a v2 → v3 revision before submission. The output
is human-readable in summary form and JSON-serialisable for tooling.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from rrxiv.models import CIR, Annotation, Claim


@dataclass(frozen=True, slots=True)
class FieldChange:
    """A scalar field that changed between two CIRs."""

    field: str
    before: Any
    after: Any


@dataclass(frozen=True, slots=True)
class ClaimChange:
    """A claim that changed between two CIRs (existed in both, but differs)."""

    claim_id: str
    fields: tuple[FieldChange, ...]


@dataclass
class CIRDiff:
    """The result of diffing two CIRs.

    Most lists are sorted by ID for stable output. The diff is from
    ``before`` to ``after`` — additions / removals are relative to the
    earlier revision.
    """

    field_changes: list[FieldChange] = field(default_factory=list)
    """Top-level scalar fields that changed: title, abstract, license, etc."""

    topic_added: list[str] = field(default_factory=list)
    topic_removed: list[str] = field(default_factory=list)

    claim_added: list[str] = field(default_factory=list)
    """Claim IDs that exist in `after` but not in `before`."""

    claim_removed: list[str] = field(default_factory=list)
    """Claim IDs that exist in `before` but not in `after`."""

    claim_changed: list[ClaimChange] = field(default_factory=list)
    """Claims present in both but with at least one differing field."""

    edge_added: list[tuple[str, str, str]] = field(default_factory=list)
    """(source, target, kind) edges added in `after`."""

    edge_removed: list[tuple[str, str, str]] = field(default_factory=list)

    citation_added: list[str] = field(default_factory=list)
    """Citation IDs added."""

    citation_removed: list[str] = field(default_factory=list)

    annotation_added: list[str] = field(default_factory=list)
    annotation_removed: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            (
                self.field_changes,
                self.topic_added,
                self.topic_removed,
                self.claim_added,
                self.claim_removed,
                self.claim_changed,
                self.edge_added,
                self.edge_removed,
                self.citation_added,
                self.citation_removed,
                self.annotation_added,
                self.annotation_removed,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_changes": [
                {"field": fc.field, "before": fc.before, "after": fc.after}
                for fc in self.field_changes
            ],
            "topic_added": list(self.topic_added),
            "topic_removed": list(self.topic_removed),
            "claim_added": list(self.claim_added),
            "claim_removed": list(self.claim_removed),
            "claim_changed": [
                {
                    "claim_id": cc.claim_id,
                    "fields": [
                        {"field": fc.field, "before": fc.before, "after": fc.after}
                        for fc in cc.fields
                    ],
                }
                for cc in self.claim_changed
            ],
            "edge_added": [list(e) for e in self.edge_added],
            "edge_removed": [list(e) for e in self.edge_removed],
            "citation_added": list(self.citation_added),
            "citation_removed": list(self.citation_removed),
            "annotation_added": list(self.annotation_added),
            "annotation_removed": list(self.annotation_removed),
        }

    def summary(self) -> str:
        """Human-readable summary, suitable for CLI display."""
        if self.is_empty():
            return "No changes."

        lines: list[str] = []
        if self.field_changes:
            lines.append("Fields:")
            for fc in self.field_changes:
                lines.append(f"  {fc.field}: {fc.before!r} → {fc.after!r}")
        if self.topic_added or self.topic_removed:
            lines.append("Topics:")
            for t in self.topic_added:
                lines.append(f"  + {t}")
            for t in self.topic_removed:
                lines.append(f"  - {t}")
        if self.claim_added or self.claim_removed or self.claim_changed:
            n_add = len(self.claim_added)
            n_rem = len(self.claim_removed)
            n_chg = len(self.claim_changed)
            lines.append(f"Claims: +{n_add} added, -{n_rem} removed, ~{n_chg} changed")
            for cid in self.claim_added:
                lines.append(f"  + {cid}")
            for cid in self.claim_removed:
                lines.append(f"  - {cid}")
            for cc in self.claim_changed:
                fields_summary = ", ".join(fc.field for fc in cc.fields)
                lines.append(f"  ~ {cc.claim_id} ({fields_summary})")
        if self.edge_added or self.edge_removed:
            lines.append(
                f"Edges: +{len(self.edge_added)} added, -{len(self.edge_removed)} removed"
            )
            for src, tgt, kind in self.edge_added:
                lines.append(f"  + {kind}: {src} → {tgt}")
            for src, tgt, kind in self.edge_removed:
                lines.append(f"  - {kind}: {src} → {tgt}")
        if self.citation_added or self.citation_removed:
            n_add = len(self.citation_added)
            n_rem = len(self.citation_removed)
            lines.append(f"Citations: +{n_add} added, -{n_rem} removed")
        if self.annotation_added or self.annotation_removed:
            n_add = len(self.annotation_added)
            n_rem = len(self.annotation_removed)
            lines.append(f"Annotations: +{n_add} added, -{n_rem} removed")
        return "\n".join(lines)


# ---- Internal helpers ----


_TOP_LEVEL_FIELDS_TO_DIFF: tuple[str, ...] = (
    "title",
    "abstract",
    "license",
    "version",
    "previous_version",
)
"""Top-level scalar fields whose change is worth surfacing. submitted_at and
id are excluded — id is supposed to be stable; submitted_at is server-set
and trivially different across revisions."""


_CLAIM_FIELDS_TO_DIFF: tuple[str, ...] = (
    "statement",
    "claim_type",
    "evidence_type",
    "replication_status",
    "canonical",
)


def _claim_id(claim: Claim) -> str:
    return claim.id


def _citation_id(c: Any) -> str:
    """Citation is a RootModel union; pull .id from its root variant."""
    if hasattr(c, "root"):
        return str(getattr(c.root, "id", ""))
    return str(c.id)


def _annotation_id(a: Annotation) -> str:
    return a.id


def _claims_by_id(claims: Iterable[Claim] | None) -> dict[str, Claim]:
    return {_claim_id(c): c for c in (claims or [])}


def _edges_of(claim: Claim) -> set[tuple[str, str, str]]:
    """Return the set of (source, target, kind) edges this claim emits."""
    edges: set[tuple[str, str, str]] = set()
    for kind, attr in (
        ("depends_on", "depends_on"),
        ("supports", "supports"),
        ("contradicts", "contradicts"),
        ("extends", "extends"),
    ):
        for target in getattr(claim, attr) or []:
            edges.add((claim.id, target, kind))
    return edges


def _diff_claim(before: Claim, after: Claim) -> ClaimChange | None:
    fields: list[FieldChange] = []
    for f in _CLAIM_FIELDS_TO_DIFF:
        b = getattr(before, f, None)
        a = getattr(after, f, None)
        # Normalise enums for comparison
        b_n = str(b) if b is not None else None
        a_n = str(a) if a is not None else None
        if b_n != a_n:
            fields.append(FieldChange(field=f, before=b, after=a))
    if not fields:
        return None
    return ClaimChange(claim_id=before.id, fields=tuple(fields))


def diff_cir(before: CIR, after: CIR) -> CIRDiff:
    """Produce a semantic diff between two CIRs."""
    out = CIRDiff()

    # Top-level scalar fields
    for f in _TOP_LEVEL_FIELDS_TO_DIFF:
        b = getattr(before, f, None)
        a = getattr(after, f, None)
        if b != a:
            out.field_changes.append(FieldChange(field=f, before=b, after=a))

    # Topics
    before_topics = set(before.topics or [])
    after_topics = set(after.topics or [])
    out.topic_added = sorted(after_topics - before_topics)
    out.topic_removed = sorted(before_topics - after_topics)

    # Claims
    before_claims = _claims_by_id(before.claims)
    after_claims = _claims_by_id(after.claims)
    before_ids = set(before_claims.keys())
    after_ids = set(after_claims.keys())
    out.claim_added = sorted(after_ids - before_ids)
    out.claim_removed = sorted(before_ids - after_ids)
    for cid in sorted(before_ids & after_ids):
        change = _diff_claim(before_claims[cid], after_claims[cid])
        if change is not None:
            out.claim_changed.append(change)

    # Edges
    before_edges: set[tuple[str, str, str]] = set()
    after_edges: set[tuple[str, str, str]] = set()
    for c in before.claims or []:
        before_edges |= _edges_of(c)
    for c in after.claims or []:
        after_edges |= _edges_of(c)
    out.edge_added = sorted(after_edges - before_edges)
    out.edge_removed = sorted(before_edges - after_edges)

    # Citations
    before_cite_ids = {_citation_id(c) for c in (before.citations or [])}
    after_cite_ids = {_citation_id(c) for c in (after.citations or [])}
    out.citation_added = sorted(after_cite_ids - before_cite_ids)
    out.citation_removed = sorted(before_cite_ids - after_cite_ids)

    # Annotations
    before_ann_ids = {_annotation_id(a) for a in (before.annotations or [])}
    after_ann_ids = {_annotation_id(a) for a in (after.annotations or [])}
    out.annotation_added = sorted(after_ann_ids - before_ann_ids)
    out.annotation_removed = sorted(before_ann_ids - after_ann_ids)

    return out


__all__ = ["CIRDiff", "ClaimChange", "FieldChange", "diff_cir"]
