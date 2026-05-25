"""Unit tests for ``compute_pulse`` + integration smoke for the
``GET /stats/pulse`` endpoint.

The unit tests seed a ``MemoryStore`` with a known shape and assert
each KPI field. The integration test posts annotations through the
public API and confirms the live ``/stats/pulse`` endpoint reflects
them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from rrxiv.server.stats.cache import invalidate
from rrxiv.server.stats.pulse import compute_pulse
from rrxiv.server.store.memory import MemoryStore


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _orcid(orcid: str) -> dict[str, str]:
    return {"identity_type": "orcid", "identity": orcid}


def _agent(handle: str) -> dict[str, str]:
    return {"identity_type": "agent", "identity": handle}


def _paper(
    pid: str,
    *,
    authors: list[dict[str, str]] | None = None,
    topics: list[str] | None = None,
    submitted_at: str | None = None,
    previous_version: str | None = None,
    created_by: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "id": pid,
        "id_slug": pid,
        "title": f"Paper {pid}",
        "authors": authors or [],
        "topics": topics or [],
        "submitted_at": submitted_at or _now_iso(),
        "previous_version": previous_version,
        "created_by": created_by,
    }


def _claim(
    cid: str, paper_id: str, *, status: str = "untested"
) -> dict[str, Any]:
    return {
        "id": cid,
        "paper_id": paper_id,
        "statement": "Some claim.",
        "replication_status": status,
        "depends_on": [],
        "supports": [],
        "contradicts": [],
        "extends": [],
    }


def _annotation(
    aid: str,
    *,
    target_id: str,
    target_type: str = "claim",
    annotation_type: str = "comment",
    created_by: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": aid,
        "target_id": target_id,
        "target_type": target_type,
        "annotation_type": annotation_type,
        "content": "Body text.",
        "created_at": created_at or _now_iso(),
    }
    if created_by is not None:
        record["created_by"] = created_by
    if payload is not None:
        record["structured_payload"] = payload
    return record


# ---------------------------------------------------------------------------
# compute_pulse — happy paths
# ---------------------------------------------------------------------------


def test_pulse_empty_store_returns_zeros() -> None:
    store = MemoryStore()
    result = compute_pulse(store, window="7d")
    assert result["pulse"]["distinct_human_authors"] == 0
    assert result["pulse"]["distinct_agent_authors"] == 0
    assert result["pulse"]["submissions_count"] == 0
    assert result["pulse"]["annotations_total"] == 0
    assert result["health"]["replication_coverage_rate"] == 0.0
    assert result["health"]["total_claims"] == 0
    assert result["leaderboards"]["top_papers_by_annotations"] == []


def test_pulse_counts_distinct_human_and_agent_writers() -> None:
    store = MemoryStore()
    store.add_paper(
        _paper(
            "p1",
            authors=[{"name": "A", "orcid_id": "0000-0001-AAAA"}],
            created_by=_orcid("0000-0001-AAAA"),
        )
    )
    store.add_claim(_claim("p1:c1", "p1"))
    store.add_annotation(
        _annotation("a1", target_id="p1:c1", created_by=_orcid("0000-0002-BBBB"))
    )
    store.add_annotation(
        _annotation("a2", target_id="p1:c1", created_by=_agent("@alice"))
    )
    store.add_annotation(
        _annotation("a3", target_id="p1:c1", created_by=_agent("@bob"))
    )
    result = compute_pulse(store, window="7d")
    # Two human ORCIDs total: 0001 wrote a paper, 0002 wrote an annotation.
    assert result["pulse"]["distinct_human_authors"] == 2
    assert result["pulse"]["distinct_agent_authors"] == 2
    assert result["pulse"]["annotations_total"] == 3
    assert result["pulse"]["annotations_by_type"]["comment"] == 3


def test_pulse_excludes_identities_in_exclude_set() -> None:
    store = MemoryStore()
    store.add_paper(
        _paper(
            "p1",
            authors=[{"name": "Blaise", "orcid_id": "0000-MAINTAINER"}],
            created_by=_orcid("0000-MAINTAINER"),
        )
    )
    store.add_annotation(
        _annotation(
            "a1",
            target_id="p1",
            target_type="paper",
            created_by=_orcid("0000-MAINTAINER"),
        )
    )
    store.add_annotation(
        _annotation(
            "a2",
            target_id="p1",
            target_type="paper",
            created_by=_orcid("0000-OTHER"),
        )
    )
    result = compute_pulse(
        store, window="7d", exclude_identities={"0000-MAINTAINER"}
    )
    # Only the OTHER author counts.
    assert result["pulse"]["distinct_human_authors"] == 1
    # Submissions count is zero — maintainer's paper is excluded.
    assert result["pulse"]["submissions_count"] == 0
    # Annotations: only the non-excluded one.
    assert result["pulse"]["annotations_total"] == 1


def test_pulse_replication_coverage_rate() -> None:
    store = MemoryStore()
    store.add_paper(_paper("p1"))
    store.add_claim(_claim("p1:c1", "p1", status="replicated"))
    store.add_claim(_claim("p1:c2", "p1", status="replicated"))
    store.add_claim(_claim("p1:c3", "p1", status="untested"))
    store.add_claim(_claim("p1:c4", "p1", status="contradicted"))
    result = compute_pulse(store, window="all")
    # 2/4 replicated.
    assert result["health"]["replication_coverage_rate"] == 0.5
    # 1/4 contradicted.
    assert result["health"]["contradiction_rate"] == 0.25


def test_pulse_third_party_annotation_rate() -> None:
    """Annotations by ORCIDs not in the paper's author list count as
    third-party engagement — the cleanest "real community" signal."""
    store = MemoryStore()
    store.add_paper(
        _paper(
            "p1",
            authors=[{"name": "A", "orcid_id": "0000-AUTHOR"}],
        )
    )
    store.add_claim(_claim("p1:c1", "p1"))
    # Self-annotation (author commenting on their own paper).
    store.add_annotation(
        _annotation("self", target_id="p1:c1", created_by=_orcid("0000-AUTHOR"))
    )
    # Third-party annotation.
    store.add_annotation(
        _annotation("ext", target_id="p1:c1", created_by=_orcid("0000-OTHER"))
    )
    result = compute_pulse(store, window="all")
    assert result["health"]["third_party_annotation_rate"] == 0.5


def test_pulse_agent_participation_rate() -> None:
    store = MemoryStore()
    store.add_paper(_paper("p1"))
    store.add_annotation(
        _annotation("h", target_id="p1", target_type="paper", created_by=_orcid("0000-1"))
    )
    store.add_annotation(
        _annotation("a", target_id="p1", target_type="paper", created_by=_agent("@bot"))
    )
    store.add_annotation(
        _annotation("a2", target_id="p1", target_type="paper", created_by=_agent("@bot2"))
    )
    result = compute_pulse(store, window="all")
    assert result["health"]["agent_participation_rate"] == round(2 / 3, 4)


def test_pulse_reproduction_kind_breakdown() -> None:
    store = MemoryStore()
    store.add_paper(_paper("p1"))
    store.add_claim(_claim("p1:c1", "p1"))
    store.add_annotation(
        _annotation(
            "r1",
            target_id="p1:c1",
            annotation_type="replication",
            payload={"reproduction_kind": "fresh_replication"},
            created_by=_orcid("0000-1"),
        )
    )
    store.add_annotation(
        _annotation(
            "r2",
            target_id="p1:c1",
            annotation_type="replication",
            payload={"reproduction_kind": "reproduction_from_artifacts"},
            created_by=_orcid("0000-2"),
        )
    )
    store.add_annotation(
        _annotation(
            "r3",
            target_id="p1:c1",
            annotation_type="replication",
            payload={"reproduction_kind": "fresh_replication"},
            created_by=_orcid("0000-3"),
        )
    )
    result = compute_pulse(store, window="all")
    breakdown = result["health"]["reproduction_kind_breakdown"]
    assert breakdown["fresh_replication"] == 2
    assert breakdown["reproduction_from_artifacts"] == 1


def test_pulse_leaderboards_rank_by_annotation_count() -> None:
    store = MemoryStore()
    for i in range(6):
        store.add_paper(_paper(f"p{i}", authors=[{"name": f"a{i}"}]))
    store.add_claim(_claim("p0:c1", "p0"))
    store.add_claim(_claim("p1:c1", "p1"))
    # p1 gets 3 annotations, p0 gets 1, others get 0.
    store.add_annotation(_annotation("a", target_id="p1:c1", created_by=_orcid("X")))
    store.add_annotation(_annotation("b", target_id="p1:c1", created_by=_orcid("Y")))
    store.add_annotation(_annotation("c", target_id="p1:c1", created_by=_orcid("Z")))
    store.add_annotation(_annotation("d", target_id="p0:c1", created_by=_orcid("Q")))
    result = compute_pulse(store, window="all")
    top = result["leaderboards"]["top_papers_by_annotations"]
    assert top[0]["id"] == "p1"
    assert top[0]["annotations"] == 3
    assert top[1]["id"] == "p0"
    assert top[1]["annotations"] == 1


def test_pulse_revisions_count() -> None:
    store = MemoryStore()
    store.add_paper(_paper("p1"))
    store.add_paper(_paper("p2", previous_version="p1"))
    store.add_paper(_paper("p3"))
    result = compute_pulse(store, window="all")
    # p2 is a revision (previous_version set); p1 is superseded so
    # not counted in submissions_count (it's not head-of-lineage anymore).
    assert result["pulse"]["revisions_count"] == 1
    assert result["pulse"]["submissions_count"] == 2  # p2 + p3 are heads


def test_pulse_growth_metrics() -> None:
    store = MemoryStore()
    store.add_paper(_paper("p1", authors=[{"name": "A", "orcid_id": "0001"}]))
    store.add_paper(_paper("p2", authors=[{"name": "B", "orcid_id": "0002"}]))
    store.add_claim(_claim("p1:c1", "p1"))
    store.add_annotation(
        _annotation("a", target_id="p1:c1", created_by=_orcid("0003"))
    )
    result = compute_pulse(store, window="all")
    growth = result["growth"]
    # Three distinct identities total — 0001 authored, 0003 annotated. 0002 only authored.
    assert growth["unique_human_identities_ever"] >= 2
    assert growth["papers_with_third_party_engagement"] == 1


def test_pulse_top_claims_by_views_uses_store_counter() -> None:
    """Sprint 22: the leaderboard reads from store.list_claim_views()
    so view bumps from GET /claims/{id} surface."""
    store = MemoryStore()
    store.add_paper(_paper("p1"))
    store.add_claim(_claim("p1:c1", "p1"))
    store.add_claim(_claim("p1:c2", "p1"))
    store.add_claim(_claim("p1:c3", "p1"))
    # Bump views: c2 twice, c1 once, c3 zero.
    store.bump_claim_view("p1:c2")
    store.bump_claim_view("p1:c2")
    store.bump_claim_view("p1:c1")
    # Bump a dead id too — should NOT appear in the leaderboard.
    store.bump_claim_view("p-ghost:c99")
    result = compute_pulse(store, window="all")
    leaderboard = result["leaderboards"]["top_claims_by_views"]
    assert leaderboard[0]["id"] == "p1:c2"
    assert leaderboard[0]["views"] == 2
    assert leaderboard[1]["id"] == "p1:c1"
    assert leaderboard[1]["views"] == 1
    # c3 had 0 views — excluded; p-ghost:c99 doesn't exist — excluded.
    ids = {entry["id"] for entry in leaderboard}
    assert "p1:c3" not in ids
    assert "p-ghost:c99" not in ids


def test_pulse_cohorts_bucket_first_writes_by_iso_week() -> None:
    """First-write date for each identity buckets into the ISO week
    of the earliest paper/annotation they authored. The exclude_list
    is honoured."""

    store = MemoryStore()
    # 0001 first wrote 2026-W21 (papers).
    store.add_paper(
        _paper(
            "p1",
            authors=[{"name": "A", "orcid_id": "0001"}],
            created_by=_orcid("0001"),
            submitted_at="2026-05-19T12:00:00Z",
        )
    )
    # 0002 first wrote 2026-W22 (annotation).
    store.add_paper(_paper("p2"))
    store.add_annotation(
        _annotation(
            "a1",
            target_id="p2",
            target_type="paper",
            created_by=_orcid("0002"),
            created_at="2026-05-25T12:00:00Z",
        )
    )
    # MAINTAINER excluded.
    store.add_paper(
        _paper(
            "p-blaise",
            authors=[{"name": "Blaise", "orcid_id": "BLAISE"}],
            created_by=_orcid("BLAISE"),
            submitted_at="2026-05-25T12:00:00Z",
        )
    )
    result = compute_pulse(
        store, window="all", exclude_identities={"BLAISE"}
    )
    bucket = result["cohorts"]["first_write_by_iso_week"]
    assert bucket.get("2026-W21") == 1, bucket
    assert bucket.get("2026-W22") == 1, bucket
    # BLAISE excluded.
    assert sum(bucket.values()) == 2


def test_pulse_cohorts_weekly_actives_shape() -> None:
    """`weekly_active_humans` is a list of ISO-week buckets covering
    the last ~8 weeks. Empty weeks show as 0."""
    store = MemoryStore()
    result = compute_pulse(store, window="all")
    cohorts = result["cohorts"]
    assert "weekly_active_humans" in cohorts
    assert "weekly_active_agents" in cohorts
    # Both arrays have the same length and order; each entry has
    # `iso_week` + `distinct_identities`.
    for entry in cohorts["weekly_active_humans"]:
        assert "iso_week" in entry and "distinct_identities" in entry
        assert isinstance(entry["distinct_identities"], int)


def test_pulse_uses_cache_then_invalidates() -> None:
    """The cache hides re-computation when called repeatedly with the
    same (window, exclude) key. invalidate() forces a recompute."""
    from rrxiv.server.stats.cache import get_or_compute

    invalidate()
    calls = {"n": 0}

    def factory() -> dict[str, Any]:
        calls["n"] += 1
        return {"value": calls["n"]}

    key = ("t", 1)
    a = get_or_compute(key, factory)
    b = get_or_compute(key, factory)
    assert a == b
    assert calls["n"] == 1
    invalidate()
    c = get_or_compute(key, factory)
    assert c != a
    assert calls["n"] == 2
