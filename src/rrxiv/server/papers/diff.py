"""Server-side semantic diff between two paper versions (RRP-0017).

Wraps the lower-level ``rrxiv.diff.diff_cir`` with claim-matching rules
that survive the per-version `claim_id` rebase: claims are matched on
``local_id`` first, then on byte-identical ``statement`` as a fallback,
and unmatched claims become added/removed.

The output shape conforms to ``schema/revision_diff.schema.json``.

Two surfaces consume this module:

- ``diff_endpoint.py`` (FastAPI handler for ``GET /papers/{id}/diff``).
- ``submissions/router.py`` (computes + attaches the diff to revision
  submission responses).
"""

from __future__ import annotations

import datetime as dt
import difflib
from collections.abc import Callable
from typing import Any

from rrxiv.models import CIR, Claim

# ---------------------------------------------------------------------------
# Claim matching
# ---------------------------------------------------------------------------


def claim_local_id(claim_id: str) -> str | None:
    """Extract the local_id portion of a global claim_id.

    Global IDs are ``<paper_uuid>:<local_id>`` per the schema, where the
    paper UUID never contains a colon and the local_id may contain
    further colons (e.g. ``prop:I.10``). We split on the first colon.

    Returns None for unparseable IDs.
    """
    if not claim_id or ":" not in claim_id:
        return None
    return claim_id.split(":", 1)[1]


def _index_by_local(claims: list[Claim]) -> dict[str, list[Claim]]:
    """Group claims by their local_id. Buckets are lists so we can detect
    accidental duplicates and fall through to statement-matching."""
    by_local: dict[str, list[Claim]] = {}
    for claim in claims:
        local = claim_local_id(claim.id)
        if local is None:
            continue
        by_local.setdefault(local, []).append(claim)
    return by_local


def _match_claims(
    prev_claims: list[Claim],
    curr_claims: list[Claim],
) -> tuple[list[tuple[Claim, Claim]], list[Claim], list[Claim]]:
    """Match claims across two CIRs.

    Returns ``(matched_pairs, unmatched_prev, unmatched_curr)``.

    Matching rule (RRP-0017 §Claim matching rule):
      1. ``local_id`` exact match — dominant case.
      2. Identical ``statement`` (after no further normalisation; the
         parser already runs tex_to_text) and uniquely matching on each
         side — handles cases where an author renamed a label but kept
         the prose verbatim.
      3. Otherwise unmatched.
    """
    prev_by_local = _index_by_local(prev_claims)
    curr_by_local = _index_by_local(curr_claims)

    matched: list[tuple[Claim, Claim]] = []
    matched_prev_ids: set[str] = set()
    matched_curr_ids: set[str] = set()

    # Pass 1: local_id with unique partner on both sides.
    for local, prev_bucket in prev_by_local.items():
        curr_bucket = curr_by_local.get(local, [])
        if len(prev_bucket) == 1 and len(curr_bucket) == 1:
            matched.append((prev_bucket[0], curr_bucket[0]))
            matched_prev_ids.add(prev_bucket[0].id)
            matched_curr_ids.add(curr_bucket[0].id)

    # Pass 2: statement-exact among the still-unmatched.
    prev_unmatched = [c for c in prev_claims if c.id not in matched_prev_ids]
    curr_unmatched = [c for c in curr_claims if c.id not in matched_curr_ids]
    by_stmt_prev: dict[str, list[Claim]] = {}
    for c in prev_unmatched:
        by_stmt_prev.setdefault(c.statement, []).append(c)
    for cur in list(curr_unmatched):
        bucket = by_stmt_prev.get(cur.statement, [])
        if len(bucket) == 1:
            pair = bucket.pop()
            matched.append((pair, cur))
            matched_prev_ids.add(pair.id)
            matched_curr_ids.add(cur.id)

    unmatched_prev = [c for c in prev_claims if c.id not in matched_prev_ids]
    unmatched_curr = [c for c in curr_claims if c.id not in matched_curr_ids]
    return matched, unmatched_prev, unmatched_curr


# ---------------------------------------------------------------------------
# Per-field diffing
# ---------------------------------------------------------------------------


def _word_diff_hunks(before: str, after: str) -> list[dict[str, str]]:
    """Word-level diff in the RevisionDiff hunk shape.

    Uses difflib.SequenceMatcher on whitespace-tokenised words; emits a
    minimal hunk stream with ``equal`` / ``added`` / ``removed`` kinds.
    Whitespace inside hunks is preserved when joining.
    """
    if before == after:
        return [{"kind": "equal", "text": before}]

    # Tokenise preserving whitespace.
    before_tokens = _tokenise(before)
    after_tokens = _tokenise(after)
    sm = difflib.SequenceMatcher(a=before_tokens, b=after_tokens, autojunk=False)
    out: list[dict[str, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            text = "".join(before_tokens[i1:i2])
            if text:
                out.append({"kind": "equal", "text": text})
        elif tag == "replace":
            removed = "".join(before_tokens[i1:i2])
            added = "".join(after_tokens[j1:j2])
            if removed:
                out.append({"kind": "removed", "text": removed})
            if added:
                out.append({"kind": "added", "text": added})
        elif tag == "delete":
            text = "".join(before_tokens[i1:i2])
            if text:
                out.append({"kind": "removed", "text": text})
        elif tag == "insert":
            text = "".join(after_tokens[j1:j2])
            if text:
                out.append({"kind": "added", "text": text})
    return out


def _tokenise(s: str) -> list[str]:
    """Split into a sequence of word + adjacent-whitespace tokens.

    Stable enough for word-level diff; doesn't try to be word-perfect on
    Unicode boundaries.
    """
    import re

    return re.findall(r"\S+\s*|\s+", s)


_TRACKED_FIELDS = (
    "statement",
    "proof",
    "claim_type",
    "evidence_type",
    "figures",
    "depends_on",
    "supports",
    "contradicts",
    "extends",
    "source_location",
)


def _modified_claim_payload(prev: Claim, curr: Claim) -> dict[str, Any] | None:
    """Return the ModifiedClaim payload for two matched claims, or None
    if they are byte-identical across the tracked fields."""
    fields_changed: list[str] = []
    out: dict[str, Any] = {
        "from_claim_id": prev.id,
        "to_claim_id": curr.id,
        "local_id": claim_local_id(curr.id) or "",
    }

    for field in _TRACKED_FIELDS:
        pv = getattr(prev, field, None)
        cv = getattr(curr, field, None)
        if _normalise_field(field, pv) != _normalise_field(field, cv):
            fields_changed.append(field)

    if not fields_changed:
        return None

    out["fields_changed"] = fields_changed

    if "statement" in fields_changed:
        out["statement_diff"] = {
            "hunks": _word_diff_hunks(prev.statement or "", curr.statement or "")
        }
    if "proof" in fields_changed:
        out["proof_diff"] = {
            "hunks": _word_diff_hunks(prev.proof or "", curr.proof or "")
        }

    return out


def _normalise_field(field: str, value: Any) -> Any:
    """Normalise a claim field for comparison.

    Lists become sets where order is irrelevant; pydantic models become
    their dict form; None and missing collapse together.
    """
    if value is None:
        return None
    if field in ("depends_on", "supports", "contradicts", "extends"):
        return tuple(sorted(value or []))
    if field == "figures":
        return tuple(
            sorted(
                ((f.path, f.caption) for f in (value or [])),
                key=lambda kv: kv[0],
            )
        )
    if field == "source_location":
        if hasattr(value, "model_dump"):
            return value.model_dump()
        return value
    if hasattr(value, "value"):  # enum
        return value.value
    return value


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


def compute_revision_diff(
    prev_paper: dict[str, Any],
    prev_cir: CIR,
    curr_paper: dict[str, Any],
    curr_cir: CIR,
) -> dict[str, Any]:
    """Compute a RevisionDiff between two paper versions.

    Args:
        prev_paper: the older paper record (dict from the store).
        prev_cir: the older paper's CIR (pydantic model).
        curr_paper: the newer paper record.
        curr_cir: the newer paper's CIR.

    Returns:
        A dict conforming to ``schema/revision_diff.schema.json``.
    """
    abstract_changed = (prev_cir.abstract or "") != (curr_cir.abstract or "")
    abstract_diff: dict[str, Any] | None = None
    if abstract_changed:
        abstract_diff = {
            "hunks": _word_diff_hunks(prev_cir.abstract or "", curr_cir.abstract or "")
        }

    prev_topics = list(curr_cir.topics or [])  # placeholder; recomputed below
    prev_topics = list(prev_cir.topics or [])
    curr_topics = list(curr_cir.topics or [])
    topics_added = sorted(set(curr_topics) - set(prev_topics))
    topics_removed = sorted(set(prev_topics) - set(curr_topics))

    prev_claims = list(prev_cir.claims or [])
    curr_claims = list(curr_cir.claims or [])
    matched, unmatched_prev, unmatched_curr = _match_claims(prev_claims, curr_claims)

    modified: list[dict[str, Any]] = []
    unchanged_count = 0
    for prev_c, curr_c in matched:
        payload = _modified_claim_payload(prev_c, curr_c)
        if payload is None:
            unchanged_count += 1
        else:
            modified.append(payload)

    added: list[dict[str, Any]] = [
        {
            "claim_id": c.id,
            "local_id": claim_local_id(c.id) or "",
            "statement": c.statement,
            "claim_type": (
                c.claim_type.value
                if hasattr(c.claim_type, "value")
                else str(c.claim_type)
            ),
        }
        for c in unmatched_curr
    ]
    removed: list[dict[str, Any]] = [
        {
            "claim_id": c.id,
            "local_id": claim_local_id(c.id) or "",
            "statement": c.statement,
        }
        for c in unmatched_prev
    ]

    return {
        "from": {
            "paper_id": str(prev_paper.get("id") or prev_paper.get("paper_id") or ""),
            "version": str(prev_paper.get("version") or ""),
            "id_slug": prev_paper.get("id_slug"),
        },
        "to": {
            "paper_id": str(curr_paper.get("id") or curr_paper.get("paper_id") or ""),
            "version": str(curr_paper.get("version") or ""),
            "id_slug": curr_paper.get("id_slug"),
        },
        "abstract_changed": abstract_changed,
        "abstract_diff": abstract_diff,
        "topics_changed": bool(topics_added or topics_removed),
        "topics_added": topics_added,
        "topics_removed": topics_removed,
        "claims": {
            "added": added,
            "removed": removed,
            "modified": modified,
            "unchanged_count": unchanged_count,
        },
        "computed_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
    }


# ---------------------------------------------------------------------------
# Lineage check
# ---------------------------------------------------------------------------


def papers_in_same_lineage(
    store_get_paper: Callable[[str], dict[str, Any] | None],
    paper_id_a: str,
    paper_id_b: str,
    *,
    max_depth: int = 64,
) -> bool:
    """Walk both papers' previous_version chains; return True if one is in
    the other's lineage.

    ``store_get_paper`` is the store's get_paper callable (or any
    function that takes an id and returns the paper dict or None). The
    depth bound prevents pathological lineages from causing unbounded
    work.
    """
    if paper_id_a == paper_id_b:
        return True

    def _chain(start: str) -> set[str]:
        out: set[str] = set()
        cur = start
        for _ in range(max_depth):
            paper = store_get_paper(cur)
            if not paper:
                break
            out.add(str(paper.get("id") or cur))
            prev = paper.get("previous_version")
            if not prev:
                break
            cur = prev
        return out

    chain_a = _chain(paper_id_a)
    chain_b = _chain(paper_id_b)
    return paper_id_a in chain_b or paper_id_b in chain_a or bool(chain_a & chain_b)
