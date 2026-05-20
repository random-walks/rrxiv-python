"""Tests for ``_canonicalise_claim_ids`` in ``rrxiv.cli.seed``.

The function rewrites parser-emitted meta-slug-prefixed IDs (e.g.\
``rrxiv-paper-euclid-elements:prop:I.1``) to canonical-UUID-prefixed
IDs at seed-store time so the deployed instance — which keys claims
off the canonical ``paper_id`` — finds them.
"""

from __future__ import annotations

from typing import Any

from rrxiv.cli.seed import _canonicalise_claim_ids

CANONICAL = "01923f8e-0009-7c4d-9e1f-3a2b1c0d4e5f"
METASLUG = "rrxiv-paper-euclid-elements"


def _cir(**overrides: Any) -> dict:
    base: dict[str, Any] = {
        "id": CANONICAL,
        "claims": [],
        "annotations": [],
    }
    base.update(overrides)
    return base


def test_noop_when_no_claims() -> None:
    cir = _cir()
    assert _canonicalise_claim_ids(cir, CANONICAL) == 0


def test_noop_when_already_canonical() -> None:
    cir = _cir(
        claims=[
            {
                "id": f"{CANONICAL}:prop:I.1",
                "paper_id": CANONICAL,
                "depends_on": [f"{CANONICAL}:prop:I.2"],
            }
        ]
    )
    assert _canonicalise_claim_ids(cir, CANONICAL) == 0
    assert cir["claims"][0]["id"] == f"{CANONICAL}:prop:I.1"


def test_rewrites_claim_id_paper_id_and_edges() -> None:
    cir = _cir(
        claims=[
            {
                "id": f"{METASLUG}:prop:I.1",
                "paper_id": METASLUG,
                "depends_on": [f"{METASLUG}:prop:post:1"],
                "supports": [],
            },
            {
                "id": f"{METASLUG}:prop:I.47",
                "paper_id": METASLUG,
                "depends_on": [
                    f"{METASLUG}:prop:I.4",
                    f"{METASLUG}:prop:I.41",
                ],
                "supports": [f"{METASLUG}:prop:I.48"],
            },
        ]
    )
    n = _canonicalise_claim_ids(cir, CANONICAL)
    # 2 claim.id + 2 claim.paper_id + 1 edge in claim 1 + 3 edges in claim 2 = 8
    assert n == 8
    assert cir["claims"][0]["id"] == f"{CANONICAL}:prop:I.1"
    assert cir["claims"][0]["paper_id"] == CANONICAL
    assert cir["claims"][0]["depends_on"] == [f"{CANONICAL}:prop:post:1"]
    assert cir["claims"][1]["supports"] == [f"{CANONICAL}:prop:I.48"]


def test_leaves_cross_paper_edge_targets_alone() -> None:
    """An edge to another paper's claim should not be rewritten."""
    other = "deadbeef-1234-7c4d-9e1f-3a2b1c0d4e5f"
    cir = _cir(
        claims=[
            {
                "id": f"{METASLUG}:prop:I.1",
                "paper_id": METASLUG,
                "depends_on": [
                    f"{METASLUG}:prop:I.2",  # local — rewrite
                    f"{other}:prop:X.5",     # cross-paper — leave
                ],
            }
        ]
    )
    _canonicalise_claim_ids(cir, CANONICAL)
    assert cir["claims"][0]["depends_on"] == [
        f"{CANONICAL}:prop:I.2",
        f"{other}:prop:X.5",
    ]


def test_rewrites_annotation_target_id() -> None:
    cir = _cir(
        claims=[
            {"id": f"{METASLUG}:prop:I.1", "paper_id": METASLUG},
        ],
        annotations=[
            {
                "id": "ann-1",
                "target_id": f"{METASLUG}:prop:I.1",
                "target_type": "claim",
                "annotation_type": "comment",
                "content": "x",
                "created_at": "2026-05-20T00:00:00Z",
                "created_by": {"identity_type": "anonymous", "identity": ""},
            },
            {
                "id": "ann-2",
                # paper-level annotation — target the paper itself; should
                # NOT be rewritten because it's the CIR id, not a claim id.
                "target_id": CANONICAL,
                "target_type": "paper",
                "annotation_type": "summary",
                "content": "y",
                "created_at": "2026-05-20T00:00:00Z",
                "created_by": {"identity_type": "anonymous", "identity": ""},
            },
        ],
    )
    _canonicalise_claim_ids(cir, CANONICAL)
    assert cir["annotations"][0]["target_id"] == f"{CANONICAL}:prop:I.1"
    assert cir["annotations"][1]["target_id"] == CANONICAL


def test_idempotent_on_second_run() -> None:
    cir = _cir(
        claims=[
            {
                "id": f"{METASLUG}:prop:I.1",
                "paper_id": METASLUG,
                "depends_on": [f"{METASLUG}:prop:I.2"],
            }
        ]
    )
    first = _canonicalise_claim_ids(cir, CANONICAL)
    second = _canonicalise_claim_ids(cir, CANONICAL)
    assert first > 0
    assert second == 0


def test_unknown_claim_id_structure_skipped() -> None:
    """If the parser emits an id without a `<prefix>:` separator,
    don't crash — just skip canonicalisation for it."""
    cir = _cir(
        claims=[
            {"id": "no-prefix-here", "paper_id": METASLUG},
        ]
    )
    # No prefix detected → returns 0, leaves the claim as-is.
    assert _canonicalise_claim_ids(cir, CANONICAL) == 0
    assert cir["claims"][0]["id"] == "no-prefix-here"
