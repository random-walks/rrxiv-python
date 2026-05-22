"""Server-side derivation of ``claim.replication_status`` (RRP-0019, RRP-0020).

The value persisted on a Claim at parse time is *advisory*. On every
read path the server recomputes the status from accumulated annotations
according to a deterministic rule:

1. A non-superseded, non-lifted ``claim_retraction`` (RRP-0020) by the
   paper's author wins; result is ``retracted``.
2. Counter from ``replication`` annotations:
   - ``contradicts`` ≥ ``supports`` → ``contradicted``.
   - ``fresh_replication``-kind ``supports`` ≥ per-discipline quorum
     (RRP-0019) → ``replicated``.
   - Any ``supports`` or ``partial`` outcome → ``partial``.
3. Otherwise → ``untested``.

Per-discipline quorum defaults (RRP-0019):

| discipline tags                                     | quorum |
|-----------------------------------------------------|--------|
| math, cs.formal-verification                        | 1      |
| cs.algorithms, cs.systems, crypto                   | 2      |
| ml, cs.nlp, cs.cv, rl, physics/chem/bio.experimental| 3      |
| psychology, social-sci, economics                   | 5      |
| (no tag / unknown)                                  | 3      |

Instances may override via ``RRXIV_REPLICATION_QUORUM`` env (TBD).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rrxiv.server.store import Store


# Discipline → quorum mapping. Ordered most-specific to fallback.
_QUORUM_TABLE: tuple[tuple[set[str], int], ...] = (
    ({"math", "cs.formal-verification"}, 1),
    ({"cs.algorithms", "cs.systems", "crypto", "cs.crypto"}, 2),
    (
        {
            "ml",
            "cs.nlp",
            "cs.cv",
            "rl",
            "cs.lg",
            "physics.experimental",
            "chem.experimental",
            "bio.experimental",
        },
        3,
    ),
    ({"psychology", "social-sci", "economics", "econ"}, 5),
)
_DEFAULT_QUORUM = 3


def _paper_id_of_claim(claim_id: str) -> str:
    """Extract the owning paper_id from a claim_id (``<paper>:<local>``)."""
    if ":" in claim_id:
        return claim_id.split(":", 1)[0]
    return claim_id


def quorum_for_claim(claim_id: str, store: Store) -> int:
    """Look up the per-discipline replication quorum for a claim.

    Uses the parent paper's ``topics`` field; falls back to
    ``_DEFAULT_QUORUM`` when topics are absent or unrecognised.
    """
    paper_id = _paper_id_of_claim(claim_id)
    paper = store.get_paper(paper_id)
    topics = set((paper.get("topics") if paper else None) or [])
    # Lowercase normalisation so "ML" matches "ml".
    topics = {t.lower() for t in topics}
    for tags, q in _QUORUM_TABLE:
        if topics & tags:
            return q
    return _DEFAULT_QUORUM


def _outcome(annotation: dict[str, Any]) -> str | None:
    payload = annotation.get("structured_payload")
    if isinstance(payload, dict):
        return payload.get("outcome")
    return None


def _reproduction_kind(annotation: dict[str, Any]) -> str:
    payload = annotation.get("structured_payload")
    if isinstance(payload, dict):
        # Default to fresh_replication for back-compat with pre-RRP-0019
        # annotations that omit the discriminator.
        return payload.get("reproduction_kind") or "fresh_replication"
    return "fresh_replication"


def _is_lifted(retraction: dict[str, Any], store: Store) -> bool:
    """A retraction is lifted iff there's a later comment from the same
    identity replying to it with ``lifts_retraction: true`` in the
    structured payload (RRP-0020)."""
    rid = retraction.get("id")
    if not rid:
        return False
    by = retraction.get("created_by")
    for a in store.list_annotations():
        if a.get("in_reply_to") != rid:
            continue
        if a.get("annotation_type") != "comment":
            continue
        payload = a.get("structured_payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("lifts_retraction") is not True:
            continue
        if a.get("created_by") != by:
            continue
        return True
    return False


def _is_superseded(annotation: dict[str, Any], store: Store) -> bool:
    """An annotation is superseded if any later annotation lists it in
    ``supersedes``."""
    aid = annotation.get("id")
    if not aid:
        return False
    return any(a.get("supersedes") == aid for a in store.list_annotations())


def derive_replication_status(
    claim_id: str,
    store: Store,
    *,
    authored_default: str | None = None,
) -> str:
    """Compute the server-derived ``replication_status`` for a claim.

    ``authored_default`` is the claim's persisted ``replication_status``
    (the value the author set at submission time). It is honoured **only
    when no annotations exist for this claim** — author intent becomes
    advisory the moment any replication or retraction lands. This is the
    v0.x compromise from RRP-0019: existing seed corpora that author-set
    ``replicated`` (e.g. Euclid) keep working without backfill, but as
    soon as the corpus accumulates real annotations they take over.
    """
    relevant = [
        a
        for a in store.list_annotations()
        if a.get("target_id") == claim_id
    ]

    # 1. Retraction wins (RRP-0020).
    for a in relevant:
        if a.get("annotation_type") != "claim_retraction":
            continue
        if _is_superseded(a, store):
            continue
        if _is_lifted(a, store):
            continue
        return "retracted"

    # 2. Aggregate replication annotations (RRP-0019).
    reps = [a for a in relevant if a.get("annotation_type") == "replication"]
    reps = [a for a in reps if not _is_superseded(a, store)]
    if not reps:
        # No annotations → respect author intent if persisted.
        if authored_default in (
            "untested",
            "partial",
            "replicated",
            "contradicted",
            "retracted",
        ):
            return authored_default
        return "untested"

    supports = sum(1 for a in reps if _outcome(a) == "supports")
    contradicts = sum(1 for a in reps if _outcome(a) == "contradicts")
    partials = sum(1 for a in reps if _outcome(a) == "partial")
    independent_supports = sum(
        1
        for a in reps
        if _outcome(a) == "supports"
        and _reproduction_kind(a) == "fresh_replication"
    )

    if contradicts > 0 and contradicts >= supports:
        return "contradicted"

    quorum = quorum_for_claim(claim_id, store)
    if independent_supports >= quorum:
        return "replicated"

    if supports > 0 or partials > 0:
        return "partial"

    return "untested"


def apply_derived_status(claim: dict[str, Any], store: Store) -> dict[str, Any]:
    """Return a shallow-copy of ``claim`` with ``replication_status``
    set to the server-derived value. Pure; doesn't mutate the input.

    Callers (claims router, projection) use this on every read so the
    field reflects current annotation state."""
    out = dict(claim)
    cid = claim.get("id")
    if cid:
        out["replication_status"] = derive_replication_status(
            cid, store, authored_default=claim.get("replication_status")
        )
    return out
