"""Community pulse — server-side KPI aggregation.

``compute_pulse(store, window)`` returns a single ``PulseSnapshot``
JSON blob covering activity ("how many distinct human ORCIDs wrote
this week"), health ("what fraction of claims are replicated"), and
growth ("how interconnected is the claim graph"). It's a pure
function over the storage Protocol — no app state, no caching here;
the router wraps it in a 60s TTL cache.

The shape is documented in RRP-0022 (Protocol Observability); the
JSON Schema in ``rrxiv/schema/pulse_snapshot.schema.json`` is the
contract third-party rrxiv instances should implement.

Self-exclusion: identities listed in ``ServerSettings.exclude_identities``
(comma-separated env var ``RRXIV_EXCLUDE_IDENTITIES``) are stripped
from the activity aggregates. The default is empty, so dev instances
report honest numbers. Production excludes Blaise's ORCID + the
maintainer test agents so the dashboard shows *real* community
participation.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

# `timedelta` is already imported via `from datetime import ...` above —
# the `_compute_cohorts` helper needs both `datetime` and `timedelta`,
# both already in scope.
from rrxiv.server.store import Store

PulseWindow = Literal["7d", "30d", "90d", "all"]


_WINDOW_TO_DELTA: dict[PulseWindow, timedelta | None] = {
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
    "all": None,
}


def parse_window(raw: str | None) -> PulseWindow:
    """Coerce a query-param value to a known window. Defaults to 7d."""
    if raw is None:
        return "7d"
    cleaned = raw.strip().lower()
    if cleaned in _WINDOW_TO_DELTA:
        return cleaned  # type: ignore[return-value,unused-ignore]
    return "7d"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    # Accept the "Z" suffix and assume UTC.
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _created_by_descriptor(record: dict[str, Any]) -> tuple[str, str] | None:
    """Read an annotation/paper's ``created_by`` and return
    ``(identity_type, identity)`` if present. Both legacy
    (``{identity_type, identity}``) and historic ORCID-only shapes
    are tolerated."""
    created = record.get("created_by")
    if isinstance(created, dict):
        kind = created.get("identity_type")
        ident = created.get("identity") or created.get("orcid_id") or created.get(
            "handle"
        )
        if isinstance(kind, str) and isinstance(ident, str):
            return (kind, ident)
    return None


def _paper_author_identities(paper: dict[str, Any]) -> set[str]:
    """Authors that count as "self" for the third-party-engagement
    calculation. Returns a set of ``identity`` strings (ORCID iDs or
    agent handles, whichever the author entry provides)."""
    out: set[str] = set()
    for author in paper.get("authors") or []:
        if not isinstance(author, dict):
            continue
        ident = author.get("orcid_id") or author.get("identity") or author.get(
            "handle"
        )
        if isinstance(ident, str) and ident:
            out.add(ident)
    return out


def _in_window(record: dict[str, Any], cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    ts = _parse_iso(record.get("created_at") or record.get("submitted_at"))
    if ts is None:
        return False
    return ts >= cutoff


def _filter_excluded(
    records: Iterable[dict[str, Any]],
    exclude_identities: set[str],
) -> list[dict[str, Any]]:
    """Drop records whose ``created_by.identity`` is in the excluded
    set. Records without a structured ``created_by`` are kept (we
    can't attribute them to anyone, so they don't pollute the count)."""
    out = []
    for r in records:
        desc = _created_by_descriptor(r)
        if desc is not None and desc[1] in exclude_identities:
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------


@dataclass
class _CoreData:
    papers: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    annotations: list[dict[str, Any]]
    head_paper_ids: set[str]


def _gather(store: Store) -> _CoreData:
    papers = list(store.list_papers())
    superseded = {
        prev
        for p in papers
        if (prev := p.get("previous_version")) and isinstance(prev, str)
    }
    head_ids = {p["id"] for p in papers if p.get("id") not in superseded}
    return _CoreData(
        papers=papers,
        claims=list(store.list_claims()),
        annotations=list(store.list_annotations()),
        head_paper_ids=head_ids,
    )


def compute_pulse(
    store: Store,
    window: PulseWindow = "7d",
    *,
    exclude_identities: set[str] | None = None,
) -> dict[str, Any]:
    """Compute the full pulse snapshot.

    Args:
        store: any Store implementation.
        window: time horizon for activity metrics. Health + growth
            metrics use the full corpus regardless.
        exclude_identities: set of identity strings (ORCID iDs, agent
            handles) to drop from activity counts. Defaults to empty.

    Returns:
        Dict matching ``pulse_snapshot.schema.json``.
    """
    exclude = exclude_identities or set()
    data = _gather(store)

    delta = _WINDOW_TO_DELTA[window]
    now = datetime.now(UTC)
    cutoff = (now - delta) if delta is not None else None
    window_label = window

    # --- Activity (windowed) ------------------------------------------------
    fresh_annotations = _filter_excluded(
        [a for a in data.annotations if _in_window(a, cutoff)],
        exclude,
    )
    fresh_papers = _filter_excluded(
        [
            p
            for p in data.papers
            if p["id"] in data.head_paper_ids and _in_window(p, cutoff)
        ],
        exclude,
    )
    revisions = [
        p
        for p in data.papers
        if _in_window(p, cutoff)
        and isinstance(p.get("previous_version"), str)
        and p.get("previous_version")
    ]

    distinct_humans: set[str] = set()
    distinct_agents: set[str] = set()
    for r in [*fresh_annotations, *fresh_papers]:
        desc = _created_by_descriptor(r)
        if desc is None:
            continue
        kind, ident = desc
        if kind == "orcid":
            distinct_humans.add(ident)
        elif kind == "agent":
            distinct_agents.add(ident)

    annotations_by_type: Counter[str] = Counter()
    for a in fresh_annotations:
        t = a.get("annotation_type")
        if isinstance(t, str):
            annotations_by_type[t] += 1

    # --- Health (full corpus) ----------------------------------------------
    total_claims = len(data.claims)
    by_status: Counter[str] = Counter()
    for c in data.claims:
        status = c.get("replication_status") or "untested"
        if isinstance(status, str):
            by_status[status] += 1

    def _rate(numer: int) -> float:
        return round(numer / total_claims, 4) if total_claims else 0.0

    replication_coverage_rate = _rate(by_status.get("replicated", 0))
    partial_replication_rate = _rate(by_status.get("partial", 0))
    contradiction_rate = _rate(by_status.get("contradicted", 0))

    # Third-party engagement: annotations not by the paper's own authors.
    paper_authors_by_id: dict[str, set[str]] = {
        p["id"]: _paper_author_identities(p) for p in data.papers
    }
    # Build a claim_id → paper_id resolver so we don't need to assume a
    # particular claim-id string shape. The production convention is
    # "rrxiv:2605.00008:claim:c7" but tests + custom instances may use
    # shorter forms like "p1:c1"; using the stored paper_id field
    # works for both.
    claim_to_paper: dict[str, str] = {}
    for c in data.claims:
        cid = c.get("id")
        pid = c.get("paper_id")
        if isinstance(cid, str) and isinstance(pid, str):
            claim_to_paper[cid] = pid

    def _resolve_paper_id(target_id: str, target_type: str | None) -> str | None:
        if target_type == "paper":
            return target_id
        if target_id in claim_to_paper:
            return claim_to_paper[target_id]
        # Production claim ids embed the paper id ("rrxiv:...:claim:cN").
        if ":claim:" in target_id:
            return target_id.split(":claim:", 1)[0]
        return None

    third_party = 0
    total_with_attribution = 0
    for a in data.annotations:
        desc = _created_by_descriptor(a)
        if desc is None:
            continue
        total_with_attribution += 1
        target_id = a.get("target_id")
        if not isinstance(target_id, str):
            continue
        paper_id = _resolve_paper_id(
            target_id, a.get("target_type") if isinstance(a.get("target_type"), str) else None
        )
        if paper_id is None:
            continue
        owner_set = paper_authors_by_id.get(paper_id, set())
        if desc[1] not in owner_set:
            third_party += 1

    third_party_rate = (
        round(third_party / total_with_attribution, 4)
        if total_with_attribution
        else 0.0
    )

    agent_annotations = sum(
        1
        for a in data.annotations
        if (desc := _created_by_descriptor(a)) and desc[0] == "agent"
    )
    agent_participation_rate = (
        round(agent_annotations / len(data.annotations), 4)
        if data.annotations
        else 0.0
    )

    reproduction_kind: Counter[str] = Counter()
    for a in data.annotations:
        if a.get("annotation_type") != "replication":
            continue
        payload = a.get("structured_payload")
        repro_kind: Any = (
            payload.get("reproduction_kind")
            if isinstance(payload, dict)
            else None
        )
        if isinstance(repro_kind, str):
            reproduction_kind[repro_kind] += 1

    # --- Growth (full corpus, lifetime) ------------------------------------
    unique_humans_lifetime: set[str] = set()
    unique_agents_lifetime: set[str] = set()
    # Count both: (1) created_by on annotations + papers (the writer)
    # and (2) author entries on papers (humans/agents listed as
    # authors regardless of who submitted the record). This is the
    # north-star "how many identities have *ever participated*".
    for r in [*data.annotations, *data.papers]:
        desc = _created_by_descriptor(r)
        if desc is None:
            continue
        kind, ident = desc
        if ident in exclude:
            continue
        if kind == "orcid":
            unique_humans_lifetime.add(ident)
        elif kind == "agent":
            unique_agents_lifetime.add(ident)
    for p in data.papers:
        for author in p.get("authors") or []:
            if not isinstance(author, dict):
                continue
            orcid = author.get("orcid_id")
            if isinstance(orcid, str) and orcid and orcid not in exclude:
                unique_humans_lifetime.add(orcid)
            handle = author.get("handle")
            if isinstance(handle, str) and handle and handle not in exclude:
                # Agents in the author list are first-class participants
                # per RRP-0021's Author.role taxonomy.
                role = author.get("role")
                if role == "agent" or handle.startswith("@"):
                    unique_agents_lifetime.add(handle)

    papers_with_third_party = 0
    for p in data.papers:
        if p["id"] not in data.head_paper_ids:
            continue
        owners = paper_authors_by_id.get(p["id"], set())
        for a in data.annotations:
            target_id = a.get("target_id")
            if not isinstance(target_id, str):
                continue
            paper_id = _resolve_paper_id(
                target_id,
                a.get("target_type") if isinstance(a.get("target_type"), str) else None,
            )
            if paper_id != p["id"]:
                continue
            desc = _created_by_descriptor(a)
            if desc is None:
                continue
            if desc[1] not in owners:
                papers_with_third_party += 1
                break

    total_edges = 0
    cross_paper_extends = 0
    for c in data.claims:
        owner_paper = c.get("paper_id")
        for edge_kind in ("depends_on", "supports", "contradicts", "extends"):
            edges = c.get(edge_kind) or []
            if not isinstance(edges, list):
                continue
            total_edges += len(edges)
            if edge_kind == "extends" and isinstance(owner_paper, str):
                for target in edges:
                    if not isinstance(target, str):
                        continue
                    target_paper = target.split(":claim:", 1)[0]
                    if target_paper and target_paper != owner_paper:
                        cross_paper_extends += 1

    claim_graph_density = (
        round(total_edges / total_claims, 4) if total_claims else 0.0
    )

    # --- Leaderboards -------------------------------------------------------
    annotations_per_paper: Counter[str] = Counter()
    for a in data.annotations:
        target_id = a.get("target_id")
        if not isinstance(target_id, str):
            continue
        target_type = (
            a.get("target_type") if isinstance(a.get("target_type"), str) else None
        )
        paper_id = _resolve_paper_id(target_id, target_type)
        if paper_id is not None:
            annotations_per_paper[paper_id] += 1

    paper_by_id: dict[str, dict[str, Any]] = {p["id"]: p for p in data.papers}

    def _paper_summary(pid: str) -> dict[str, Any] | None:
        p = paper_by_id.get(pid)
        if p is None:
            return None
        authors = [
            a.get("name")
            for a in (p.get("authors") or [])
            if isinstance(a, dict)
        ]
        return {
            "id": pid,
            "id_slug": p.get("id_slug"),
            "title": p.get("title"),
            "authors": [a for a in authors if a],
        }

    top_papers = []
    for pid, _count in annotations_per_paper.most_common(5):
        summary = _paper_summary(pid)
        if summary is not None:
            summary["annotations"] = _count
            top_papers.append(summary)

    replications_per_claim: Counter[str] = Counter()
    for a in data.annotations:
        if a.get("annotation_type") != "replication":
            continue
        target_id = a.get("target_id")
        if isinstance(target_id, str):
            replications_per_claim[target_id] += 1

    top_claims = [
        {"id": cid, "replications": cnt}
        for cid, cnt in replications_per_claim.most_common(5)
    ]

    # Sprint 22: top-viewed claims, pulled from the per-claim counter
    # the get_claim handler bumps on every read. Distinct signal from
    # `top_claims_by_replications` — replications measure *contributed*
    # engagement (someone re-ran the experiment), views measure
    # *discovery* engagement (someone looked at it). Both matter.
    try:
        claim_views = store.list_claim_views()
    except Exception:
        claim_views = {}
    # Drop counts for claims that no longer exist (e.g. corpus reset
    # didn't wipe the counter, or a future stat type) so the
    # leaderboard never surfaces a dead id.
    live_claim_ids = {c.get("id") for c in data.claims if isinstance(c.get("id"), str)}
    top_viewed = sorted(
        (
            (cid, count)
            for cid, count in claim_views.items()
            if cid in live_claim_ids and count > 0
        ),
        key=lambda kv: -kv[1],
    )[:5]
    top_claims_by_views = [
        {"id": cid, "views": count} for cid, count in top_viewed
    ]

    topic_counter: Counter[str] = Counter()
    for p in data.papers:
        if p["id"] not in data.head_paper_ids:
            continue
        for t in p.get("topics") or []:
            if isinstance(t, str) and t:
                topic_counter[t] += 1
    top_topics = [
        {"topic": t, "count": c} for t, c in topic_counter.most_common(5)
    ]

    # --- Cohorts (Sprint 22) -----------------------------------------------
    # Pure derivation from existing data — no schema migration. The
    # first-write date for each identity = min(created_at) over their
    # papers + annotations. Weekly buckets = ISO calendar week.
    # The numbers seed cohort analysis: "how many *new* ORCIDs wrote
    # for the first time last week?" and "WAU-by-week" curves without
    # waiting for a separate time-series store.
    cohorts = _compute_cohorts(data, now=now, exclude=exclude)

    computed_at = now.isoformat(timespec="seconds").replace("+00:00", "Z")

    return {
        "window": window_label,
        "computed_at": computed_at,
        "pulse": {
            "distinct_human_authors": len(distinct_humans),
            "distinct_agent_authors": len(distinct_agents),
            "submissions_count": len(fresh_papers),
            "revisions_count": len(revisions),
            "annotations_by_type": dict(annotations_by_type),
            "annotations_total": sum(annotations_by_type.values()),
            "replications_posted": annotations_by_type.get("replication", 0),
            "retractions_posted": (
                annotations_by_type.get("claim_retraction", 0)
                + annotations_by_type.get("paper_retraction", 0)
            ),
        },
        "health": {
            "total_papers_head": len(data.head_paper_ids),
            "total_claims": total_claims,
            "total_annotations": len(data.annotations),
            "replication_coverage_rate": replication_coverage_rate,
            "partial_replication_rate": partial_replication_rate,
            "contradiction_rate": contradiction_rate,
            "third_party_annotation_rate": third_party_rate,
            "agent_participation_rate": agent_participation_rate,
            "reproduction_kind_breakdown": dict(reproduction_kind),
        },
        "growth": {
            "unique_human_identities_ever": len(unique_humans_lifetime),
            "unique_agent_identities_ever": len(unique_agents_lifetime),
            "papers_with_third_party_engagement": papers_with_third_party,
            "claim_graph_density": claim_graph_density,
            "cross_paper_extends_count": cross_paper_extends,
        },
        "leaderboards": {
            "top_papers_by_annotations": top_papers,
            "top_claims_by_replications": top_claims,
            "top_claims_by_views": top_claims_by_views,
            "top_topics": top_topics,
        },
        "cohorts": cohorts,
    }


# ---------------------------------------------------------------------------
# Cohort helpers (Sprint 22 — groundwork for retention analysis later)
# ---------------------------------------------------------------------------


def _iso_week_key(dt: datetime) -> str:
    """ISO-8601 calendar week, e.g. ``2026-W22``. Stable + sortable +
    locale-independent. Numpy/Pandas not required."""
    iso = dt.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def _compute_cohorts(
    data: _CoreData,
    *,
    now: datetime,
    exclude: set[str],
) -> dict[str, Any]:
    """Two cohort surfaces, both derived from existing data:

    - ``first_write_by_iso_week``: how many *new* identities wrote for
      the first time in each ISO week. Bucketed map keyed by
      ``YYYY-Www``.  Empty weeks omitted.

    - ``weekly_active_humans`` / ``weekly_active_agents``: list of
      ``{iso_week, distinct_identities}`` for the last 8 ISO weeks
      (inclusive of the current one). Sorted oldest-first so a chart
      reads left-to-right. Seeds the WAU curve without needing a
      time-series store.

    Self-exclusion via the same `exclude` set the activity aggregates
    use: maintainer dogfood writes don't count as "new identities."
    """
    # Collect first-write timestamp per identity.
    first_seen: dict[tuple[str, str], datetime] = {}
    week_actives: dict[str, dict[str, set[str]]] = {}  # week -> {orcid: set, agent: set}

    cutoff = now - timedelta(weeks=8)

    def _record(kind: str, ident: str, ts: datetime) -> None:
        key = (kind, ident)
        if key not in first_seen or ts < first_seen[key]:
            first_seen[key] = ts
        if ts >= cutoff:
            week_key = _iso_week_key(ts)
            bucket = week_actives.setdefault(week_key, {"orcid": set(), "agent": set()})
            if kind in bucket:
                bucket[kind].add(ident)

    for record in [*data.papers, *data.annotations]:
        desc = _created_by_descriptor(record)
        if desc is None:
            continue
        kind, ident = desc
        if ident in exclude:
            continue
        if kind not in ("orcid", "agent"):
            continue
        ts = _parse_iso(record.get("created_at") or record.get("submitted_at"))
        if ts is None:
            continue
        _record(kind, ident, ts)

    first_write_by_iso_week: Counter[str] = Counter()
    for ts in first_seen.values():
        first_write_by_iso_week[_iso_week_key(ts)] += 1

    # Last 8 weeks, sorted oldest-first.
    week_order: list[str] = []
    cursor = now
    for _ in range(8):
        week_order.append(_iso_week_key(cursor))
        cursor -= timedelta(weeks=1)
    week_order.reverse()
    # Dedup while keeping order.
    seen = set()
    week_order = [w for w in week_order if not (w in seen or seen.add(w))]

    weekly_active_humans = [
        {
            "iso_week": w,
            "distinct_identities": len(week_actives.get(w, {}).get("orcid", set())),
        }
        for w in week_order
    ]
    weekly_active_agents = [
        {
            "iso_week": w,
            "distinct_identities": len(week_actives.get(w, {}).get("agent", set())),
        }
        for w in week_order
    ]

    return {
        "first_write_by_iso_week": dict(first_write_by_iso_week),
        "weekly_active_humans": weekly_active_humans,
        "weekly_active_agents": weekly_active_agents,
    }
