"""The claim graph — the protocol's central data structure.

A ``ClaimGraph`` is a typed directed multigraph whose nodes are claim
IDs and whose edges are the four edge kinds rrxiv defines:
``depends_on``, ``supports``, ``contradicts``, ``extends``.

Construction is from a :class:`rrxiv.models.CIR` (or directly from
claim records). Traversal is on adjacency lists — the implementation
is intentionally without ``networkx`` so this module stays light to
import. If you need richer graph algorithms, get the adjacency
structure via :py:meth:`ClaimGraph.adjacency` and feed it to whatever
graph library you like.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from rrxiv.models import CIR, Claim

EdgeKind = Literal["depends_on", "supports", "contradicts", "extends"]
ALL_EDGE_KINDS: tuple[EdgeKind, ...] = (
    "depends_on",
    "supports",
    "contradicts",
    "extends",
)


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """One typed edge in the claim graph."""

    source: str
    target: str
    kind: EdgeKind


class CycleError(Exception):
    """Raised when a cycle is detected in an edge kind that forbids them."""

    def __init__(self, kind: EdgeKind, cycle: list[str]):
        super().__init__(
            f"Cycle in {kind} edges: " + " -> ".join([*cycle, cycle[0]])
        )
        self.kind = kind
        self.cycle = cycle


@dataclass
class ClaimGraph:
    """A typed directed multigraph of claims.

    Nodes are claim ID strings. Edges carry their ``kind``. The graph
    can hold "dangling" target nodes — claim IDs referenced by edges
    that aren't themselves declared as nodes (cross-paper references).
    These show up in :py:meth:`dangling_targets`.
    """

    _nodes: set[str] = field(default_factory=set)
    _claims: dict[str, Claim] = field(default_factory=dict)
    _out: dict[str, list[GraphEdge]] = field(
        default_factory=lambda: defaultdict(list)
    )
    _in: dict[str, list[GraphEdge]] = field(
        default_factory=lambda: defaultdict(list)
    )

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_cir(cls, cir: CIR) -> ClaimGraph:
        """Build a ClaimGraph from a CIR's claims and their edge fields."""
        return cls.from_claims(cir.claims or [])

    @classmethod
    def from_claims(cls, claims: Iterable[Claim]) -> ClaimGraph:
        """Build a ClaimGraph from an iterable of Claim records."""
        g = cls()
        for c in claims:
            g.add_claim(c)
        return g

    def add_claim(self, claim: Claim) -> None:
        """Add a claim node plus its outgoing edges."""
        cid = claim.id
        self._nodes.add(cid)
        self._claims[cid] = claim

        def _add(kind: EdgeKind, targets: list[str] | None) -> None:
            for t in targets or []:
                self._out[cid].append(GraphEdge(cid, t, kind))
                self._in[t].append(GraphEdge(cid, t, kind))

        _add("depends_on", claim.depends_on)
        _add("supports", claim.supports)
        _add("contradicts", claim.contradicts)
        _add("extends", claim.extends)

    # ------------------------------------------------------------------
    # Read-only views
    # ------------------------------------------------------------------

    def nodes(self) -> set[str]:
        """All claim IDs declared as nodes (excludes dangling targets)."""
        return set(self._nodes)

    def claim(self, claim_id: str) -> Claim | None:
        """The Claim record for a node, or None if the ID is dangling."""
        return self._claims.get(claim_id)

    def edges(self, kind: EdgeKind | None = None) -> list[GraphEdge]:
        """All edges, optionally filtered by kind."""
        out: list[GraphEdge] = []
        for edges in self._out.values():
            for e in edges:
                if kind is None or e.kind == kind:
                    out.append(e)
        return out

    def outgoing(
        self, claim_id: str, kind: EdgeKind | None = None
    ) -> list[GraphEdge]:
        return [
            e for e in self._out.get(claim_id, []) if kind is None or e.kind == kind
        ]

    def incoming(
        self, claim_id: str, kind: EdgeKind | None = None
    ) -> list[GraphEdge]:
        return [
            e for e in self._in.get(claim_id, []) if kind is None or e.kind == kind
        ]

    def adjacency(self) -> dict[str, list[GraphEdge]]:
        """Source -> outgoing edges. Useful for handing the graph to
        a third-party graph library."""
        return {src: list(edges) for src, edges in self._out.items()}

    def dangling_targets(self) -> set[str]:
        """Claim IDs referenced by edges that aren't themselves nodes."""
        targets = set()
        for edges in self._out.values():
            for e in edges:
                targets.add(e.target)
        return targets - self._nodes

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------

    def dependencies(
        self, claim_id: str, *, depth: int | None = None
    ) -> set[str]:
        """All claims reachable via outgoing ``depends_on`` edges, up to
        ``depth`` (None = unbounded). Excludes ``claim_id`` itself.
        """
        return self._reach(claim_id, "depends_on", direction="out", depth=depth)

    def dependents(
        self, claim_id: str, *, depth: int | None = None
    ) -> set[str]:
        """All claims reachable via incoming ``depends_on`` edges
        (i.e. claims that depend on this one), up to ``depth``."""
        return self._reach(claim_id, "depends_on", direction="in", depth=depth)

    def _reach(
        self,
        start: str,
        kind: EdgeKind,
        *,
        direction: Literal["out", "in"],
        depth: int | None,
    ) -> set[str]:
        seen: set[str] = set()
        frontier: set[str] = {start}
        steps = 0
        while frontier:
            if depth is not None and steps >= depth:
                break
            next_frontier: set[str] = set()
            for node in frontier:
                edges = (
                    self.outgoing(node, kind)
                    if direction == "out"
                    else self.incoming(node, kind)
                )
                for e in edges:
                    neighbour = e.target if direction == "out" else e.source
                    if neighbour not in seen and neighbour != start:
                        seen.add(neighbour)
                        next_frontier.add(neighbour)
            frontier = next_frontier
            steps += 1
        return seen

    def find_cycles(self, kind: EdgeKind = "depends_on") -> list[list[str]]:
        """Find all simple cycles among edges of the given kind. Returns
        a list of cycle paths (each path is a list of claim IDs in the
        order traversed; the cycle closes with the first node)."""
        cycles: list[list[str]] = []
        # 0=unvisited, 1=on current DFS path, 2=fully explored
        unvisited, in_progress, done = 0, 1, 2
        color: dict[str, int] = {n: unvisited for n in self._nodes}
        stack: list[str] = []

        def dfs(u: str) -> None:
            color[u] = in_progress
            stack.append(u)
            for e in self.outgoing(u, kind):
                v = e.target
                if v not in color:
                    color[v] = unvisited
                if color[v] == in_progress:
                    if v in stack:
                        i = stack.index(v)
                        cycles.append(stack[i:])
                elif color[v] == unvisited:
                    dfs(v)
            stack.pop()
            color[u] = done

        for n in self._nodes:
            if color.get(n) == unvisited:
                dfs(n)
        return cycles

    def assert_no_cycles(self, kind: EdgeKind = "depends_on") -> None:
        """Raise CycleError if a cycle exists in edges of the given kind.

        ``depends_on`` cycles are a soft error per spec/0003 (the cls
        flags but doesn't reject). Callers who need stricter semantics
        — e.g. a dependency-walker — call this before walking.
        """
        cycles = self.find_cycles(kind)
        if cycles:
            raise CycleError(kind, cycles[0])

    # ------------------------------------------------------------------
    # Output formats
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view of the graph."""
        return {
            "nodes": sorted(self._nodes),
            "edges": [
                {"source": e.source, "target": e.target, "kind": e.kind}
                for e in sorted(self.edges(), key=lambda x: (x.source, x.target, x.kind))
            ],
            "dangling_targets": sorted(self.dangling_targets()),
        }

    def to_mermaid(self) -> str:
        """Render as a Mermaid ``flowchart LR``."""
        lines = ["flowchart LR"]
        # Declare nodes (sanitise IDs for Mermaid by replacing ':' with '_')
        for cid in sorted(self._nodes):
            label = cid.replace('"', '\\"')
            lines.append(f'    {_mermaid_id(cid)}["{label}"]')
        # Edges
        for e in sorted(self.edges(), key=lambda x: (x.source, x.target, x.kind)):
            arrow = _MERMAID_ARROW.get(e.kind, "-->")
            lines.append(
                f"    {_mermaid_id(e.source)} {arrow}|{e.kind}| {_mermaid_id(e.target)}"
            )
        return "\n".join(lines)

    def to_dot(self) -> str:
        """Render as a Graphviz ``digraph``."""
        lines = ["digraph claims {", '    rankdir="LR";', '    node [shape=box];']
        for cid in sorted(self._nodes):
            esc = cid.replace('"', '\\"')
            lines.append(f'    "{esc}";')
        for e in sorted(self.edges(), key=lambda x: (x.source, x.target, x.kind)):
            src = e.source.replace('"', '\\"')
            tgt = e.target.replace('"', '\\"')
            lines.append(f'    "{src}" -> "{tgt}" [label="{e.kind}"];')
        lines.append("}")
        return "\n".join(lines)


_MERMAID_ARROW: dict[str, str] = {
    "depends_on": "-->",
    "supports": "-->",
    "extends": "-->",
    "contradicts": "-.->",
}


def _mermaid_id(claim_id: str) -> str:
    """Sanitise a claim ID for use as a Mermaid node identifier."""
    return claim_id.replace(":", "__").replace("-", "_").replace(".", "_")
