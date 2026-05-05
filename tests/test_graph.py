"""Tests for the claim graph module."""

from __future__ import annotations

import pytest

from rrxiv.graph import ClaimGraph, CycleError, GraphEdge
from rrxiv.models import Claim


def _claim(
    cid: str,
    *,
    statement: str = "X under Y.",
    depends_on: list[str] | None = None,
    supports: list[str] | None = None,
    contradicts: list[str] | None = None,
    extends: list[str] | None = None,
) -> Claim:
    return Claim.model_validate(
        {
            "id": cid,
            "statement": statement,
            "claim_type": "theoretical",
            "evidence_type": "argument",
            "depends_on": depends_on or [],
            "supports": supports or [],
            "contradicts": contradicts or [],
            "extends": extends or [],
        }
    )


class TestConstruction:
    def test_empty(self) -> None:
        g = ClaimGraph.from_claims([])
        assert g.nodes() == set()
        assert g.edges() == []

    def test_single_claim(self) -> None:
        g = ClaimGraph.from_claims([_claim("p:c1")])
        assert g.nodes() == {"p:c1"}
        assert g.edges() == []

    def test_one_depends_on_edge(self) -> None:
        g = ClaimGraph.from_claims(
            [
                _claim("p:c1"),
                _claim("p:c2", depends_on=["p:c1"]),
            ]
        )
        assert g.nodes() == {"p:c1", "p:c2"}
        edges = g.edges()
        assert len(edges) == 1
        assert edges[0] == GraphEdge(source="p:c2", target="p:c1", kind="depends_on")

    def test_dangling_target(self) -> None:
        g = ClaimGraph.from_claims(
            [_claim("p:c1", depends_on=["external:claim:foo"])]
        )
        assert g.nodes() == {"p:c1"}
        assert g.dangling_targets() == {"external:claim:foo"}


class TestEdgeKinds:
    def test_supports_extends_contradicts(self) -> None:
        g = ClaimGraph.from_claims(
            [
                _claim("p:c1", supports=["p:base"]),
                _claim("p:c2", extends=["p:base"]),
                _claim("p:c3", contradicts=["p:base"]),
            ]
        )
        assert {e.kind for e in g.edges()} == {"supports", "extends", "contradicts"}
        assert len(g.edges("supports")) == 1
        assert len(g.edges("contradicts")) == 1

    def test_outgoing_filter(self) -> None:
        g = ClaimGraph.from_claims(
            [
                _claim(
                    "p:c1",
                    depends_on=["p:foo"],
                    supports=["p:bar"],
                    contradicts=["p:baz"],
                )
            ]
        )
        assert {e.target for e in g.outgoing("p:c1")} == {"p:foo", "p:bar", "p:baz"}
        assert {e.target for e in g.outgoing("p:c1", "depends_on")} == {"p:foo"}


class TestTraversal:
    def test_dependencies_one_hop(self) -> None:
        g = ClaimGraph.from_claims(
            [
                _claim("a"),
                _claim("b", depends_on=["a"]),
            ]
        )
        assert g.dependencies("b") == {"a"}
        assert g.dependencies("a") == set()

    def test_dependencies_transitive(self) -> None:
        g = ClaimGraph.from_claims(
            [
                _claim("a"),
                _claim("b", depends_on=["a"]),
                _claim("c", depends_on=["b"]),
                _claim("d", depends_on=["c"]),
            ]
        )
        assert g.dependencies("d") == {"a", "b", "c"}

    def test_dependencies_with_depth_limit(self) -> None:
        g = ClaimGraph.from_claims(
            [
                _claim("a"),
                _claim("b", depends_on=["a"]),
                _claim("c", depends_on=["b"]),
            ]
        )
        assert g.dependencies("c", depth=1) == {"b"}
        assert g.dependencies("c", depth=2) == {"a", "b"}

    def test_dependents_reverse(self) -> None:
        g = ClaimGraph.from_claims(
            [
                _claim("a"),
                _claim("b", depends_on=["a"]),
                _claim("c", depends_on=["a"]),
            ]
        )
        assert g.dependents("a") == {"b", "c"}

    def test_dependencies_only_follows_depends_on(self) -> None:
        """`supports` is a separate edge kind; dependencies() should not
        traverse it."""
        g = ClaimGraph.from_claims(
            [
                _claim("a"),
                _claim("b", supports=["a"]),
            ]
        )
        assert g.dependencies("b") == set()


class TestCycles:
    def test_no_cycle(self) -> None:
        g = ClaimGraph.from_claims(
            [_claim("a"), _claim("b", depends_on=["a"])]
        )
        assert g.find_cycles() == []

    def test_self_loop(self) -> None:
        g = ClaimGraph.from_claims([_claim("a", depends_on=["a"])])
        cycles = g.find_cycles()
        assert len(cycles) == 1
        assert cycles[0] == ["a"]

    def test_two_cycle(self) -> None:
        g = ClaimGraph.from_claims(
            [
                _claim("a", depends_on=["b"]),
                _claim("b", depends_on=["a"]),
            ]
        )
        cycles = g.find_cycles()
        assert len(cycles) == 1
        assert set(cycles[0]) == {"a", "b"}

    def test_assert_no_cycles_raises(self) -> None:
        g = ClaimGraph.from_claims([_claim("a", depends_on=["a"])])
        with pytest.raises(CycleError, match="depends_on"):
            g.assert_no_cycles()

    def test_assert_no_cycles_passes(self) -> None:
        g = ClaimGraph.from_claims([_claim("a")])
        g.assert_no_cycles()  # no raise


class TestOutputs:
    def test_to_dict(self) -> None:
        g = ClaimGraph.from_claims(
            [
                _claim("p:a"),
                _claim("p:b", depends_on=["p:a"]),
            ]
        )
        d = g.to_dict()
        assert d["nodes"] == ["p:a", "p:b"]
        assert d["edges"] == [
            {"source": "p:b", "target": "p:a", "kind": "depends_on"}
        ]

    def test_to_mermaid_contains_flowchart(self) -> None:
        g = ClaimGraph.from_claims(
            [_claim("p:a"), _claim("p:b", depends_on=["p:a"])]
        )
        out = g.to_mermaid()
        assert out.startswith("flowchart LR")
        assert "p__a" in out  # sanitised ID
        assert "depends_on" in out

    def test_to_dot_contains_digraph(self) -> None:
        g = ClaimGraph.from_claims(
            [_claim("p:a"), _claim("p:b", depends_on=["p:a"])]
        )
        out = g.to_dot()
        assert out.startswith("digraph claims {")
        assert '"p:b" -> "p:a"' in out


class TestFromCir:
    def test_minimal_cir(self) -> None:
        from rrxiv.models import CIR

        cir = CIR.model_validate(
            {
                "rrxiv_version": "0.1.0",
                "id": "p1",
                "version": "v1",
                "title": "T",
                "authors": [{"name": "A"}],
                "abstract": "x",
                "submitted_at": "2026-05-04T00:00:00Z",
                "license": "CC-BY-4.0",
                "source": {"format": "latex", "uri": "https://x.org/p.tar.gz"},
                "claims": [
                    {
                        "id": "p1:a",
                        "statement": "X.",
                        "claim_type": "theoretical",
                        "evidence_type": "argument",
                    },
                    {
                        "id": "p1:b",
                        "statement": "Y.",
                        "claim_type": "theoretical",
                        "evidence_type": "argument",
                        "depends_on": ["p1:a"],
                    },
                ],
            }
        )
        g = ClaimGraph.from_cir(cir)
        assert g.nodes() == {"p1:a", "p1:b"}
        assert g.dependencies("p1:b") == {"p1:a"}
