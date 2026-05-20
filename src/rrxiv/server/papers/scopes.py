"""UI-discovery scopes — instance metadata, not protocol-binding.

Scopes are predicates the canonical instance uses to surface different
slices of the corpus (active replications, agent-flagged, fresh, ...).
They're computed server-side because they run over the aggregate stats
that the server already computes.

Other instances may publish different scopes. The web client reads
``GET /api/v0/scopes`` and renders the result; the scope set is *not*
hardcoded into the protocol.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

ALL_SCOPE_ID = "all"


SCOPES: list[dict[str, str]] = [
    {"id": "all", "label": "Everything", "hint": "Full corpus, no filter"},
    {
        "id": "active",
        "label": "Active replications",
        "hint": "Currently being replicated",
    },
    {
        "id": "agent",
        "label": "Agent-flagged",
        "hint": "Surfaced by an LLM reviewer",
    },
    {
        "id": "human",
        "label": "Human-discussed",
        "hint": "Active human commentary",
    },
    {
        "id": "contested",
        "label": "Contested",
        "hint": "Conflicting replication evidence",
    },
    {
        "id": "fresh",
        "label": "Fresh",
        "hint": "Submitted in the last 7 days",
    },
]

SCOPE_IDS = {s["id"] for s in SCOPES}


_AGENT_TITLE = re.compile(r"agent|commentary|claim", re.IGNORECASE)
_AGENT_ABSTRACT = re.compile(r"agent", re.IGNORECASE)


def _stats(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("stats") or {}


def _is_active(item: dict[str, Any]) -> bool:
    s = _stats(item)
    return s.get("status") == "untested" and int(s.get("claims") or 0) > 4


def _is_agent(item: dict[str, Any]) -> bool:
    title = item.get("title") or ""
    abstract = item.get("abstract") or ""
    return bool(_AGENT_TITLE.search(title) or _AGENT_ABSTRACT.search(abstract))


def _is_human(item: dict[str, Any]) -> bool:
    s = _stats(item)
    return int(s.get("contested") or 0) > 0 or int(s.get("replicated") or 0) > 0


def _is_contested(item: dict[str, Any]) -> bool:
    s = _stats(item)
    return s.get("status") == "contested" or int(s.get("contradicted") or 0) > 0


def _is_fresh(item: dict[str, Any]) -> bool:
    submitted_at = item.get("submitted_at")
    if not submitted_at:
        return False
    try:
        # Accept "2026-05-04T12:00:00Z" or with timezone offset.
        when = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when >= datetime.now(UTC) - timedelta(days=7)


SCOPE_PREDICATES: dict[str, Callable[[dict[str, Any]], bool]] = {
    "all": lambda _: True,
    "active": _is_active,
    "agent": _is_agent,
    "human": _is_human,
    "contested": _is_contested,
    "fresh": _is_fresh,
}


def filter_by_scope(
    items: list[dict[str, Any]], scope_id: str
) -> list[dict[str, Any]]:
    """Filter list-item-shaped papers by scope. Unknown scope → no filter."""
    predicate = SCOPE_PREDICATES.get(scope_id, SCOPE_PREDICATES["all"])
    return [item for item in items if predicate(item)]
