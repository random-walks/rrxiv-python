"""Tests for the rrvix sidecar reader."""

from __future__ import annotations

import warnings

import pytest

from rrvix.parser.sidecar import (
    EnvMarker,
    MetaMarker,
    parse_sidecar_text,
)


def test_minimal_sidecar() -> None:
    text = """\
RRVIX:meta:id:rrvix-example-minimal
RRVIX:meta:version:v1
RRVIX:meta:protocol:0.1.0
RRVIX:meta:license:CC-BY-4.0
RRVIX:meta:topics:example,conformance
RRVIX:claim:1
RRVIX:evidence:1
RRVIX:openquestion:1
"""
    sc = parse_sidecar_text(text)

    assert sc.meta == (
        MetaMarker("id", "rrvix-example-minimal"),
        MetaMarker("version", "v1"),
        MetaMarker("protocol", "0.1.0"),
        MetaMarker("license", "CC-BY-4.0"),
        MetaMarker("topics", "example,conformance"),
    )
    assert sc.envs == (
        EnvMarker("claim", "1"),
        EnvMarker("evidence", "1"),
        EnvMarker("openquestion", "1"),
    )
    assert sc.edges == ()


def test_whitepaper_style_sidecar() -> None:
    """The actual whitepaper sidecar from the repo (verified to compile)."""
    text = """\
RRVIX:meta:id:rrvix-0001
RRVIX:meta:version:v1
RRVIX:meta:protocol:0.1.0
RRVIX:meta:license:CC-BY-4.0
RRVIX:meta:topics:infrastructure,scientific-publishing,ai-research,protocol-design
RRVIX:claim:1
RRVIX:observation:1
RRVIX:claim:2
RRVIX:edge:depends_on:rrvix-0001:claim:queryability|rrvix-0001:claim:volume-structure
RRVIX:remark:1
RRVIX:evidence:1
RRVIX:scope:1
RRVIX:claim:3
RRVIX:remark:2
RRVIX:openquestion:1
RRVIX:openquestion:2
RRVIX:claim:4
RRVIX:openquestion:3
RRVIX:remark:3
"""
    sc = parse_sidecar_text(text)

    assert sc.meta_dict() == {
        "id": "rrvix-0001",
        "version": "v1",
        "protocol": "0.1.0",
        "license": "CC-BY-4.0",
        "topics": "infrastructure,scientific-publishing,ai-research,protocol-design",
    }
    assert len(sc.envs) == 13  # 4 claims + 1 obs + 3 remarks + 1 ev + 1 scope + 3 oq
    assert len(sc.envs_of_kind("claim")) == 4
    assert len(sc.envs_of_kind("openquestion")) == 3
    assert len(sc.envs_of_kind("remark")) == 3
    assert len(sc.envs_of_kind("evidence")) == 1
    assert len(sc.envs_of_kind("scope")) == 1
    assert len(sc.envs_of_kind("observation")) == 1
    assert len(sc.edges) == 1

    edge = sc.edges[0]
    assert edge.edge_type == "depends_on"
    assert edge.source == "rrvix-0001:claim:queryability"
    assert edge.target == "rrvix-0001:claim:volume-structure"


def test_topics_with_commas_in_value_preserved() -> None:
    sc = parse_sidecar_text("RRVIX:meta:topics:a,b,c\n")
    assert sc.meta_dict()["topics"] == "a,b,c"


def test_unknown_meta_key_skipped() -> None:
    """Forward compat: unknown RRVIX:meta:<key>:... lines must not crash."""
    text = "RRVIX:meta:id:p1\nRRVIX:meta:newfield:future\n"
    sc = parse_sidecar_text(text)
    assert sc.meta_dict() == {"id": "p1"}


def test_unknown_env_kind_skipped() -> None:
    """Forward compat: unknown RRVIX:<kind>:... environment markers ignored."""
    text = "RRVIX:claim:1\nRRVIX:newenv:1\n"
    sc = parse_sidecar_text(text)
    assert sc.envs == (EnvMarker("claim", "1"),)


def test_unknown_edge_kind_skipped() -> None:
    text = "RRVIX:edge:depends_on:a|b\nRRVIX:edge:newrel:c|d\n"
    sc = parse_sidecar_text(text)
    assert len(sc.edges) == 1
    assert sc.edges[0].edge_type == "depends_on"


def test_blank_and_non_rrvix_lines_skipped() -> None:
    text = """
% LaTeX comment
RRVIX:claim:1

random noise
RRVIX:meta:id:p1
"""
    sc = parse_sidecar_text(text)
    assert len(sc.envs) == 1
    assert sc.meta_dict() == {"id": "p1"}


def test_v01_edge_with_colons_in_ids_emits_deprecation() -> None:
    """v0.1 colon-joined edges are still parsed (heuristic) but warn."""
    text = "RRVIX:edge:supports:p1:claim:foo:p2:claim:bar\n"
    with pytest.warns(DeprecationWarning, match="RRP-0002"):
        sc = parse_sidecar_text(text)
    assert sc.edges[0].source == "p1:claim:foo"
    assert sc.edges[0].target == "p2:claim:bar"


def test_v02_pipe_edge_no_warning() -> None:
    """v0.2 pipe-separated edges parse cleanly with no warning."""
    text = "RRVIX:edge:supports:p1:claim:foo|p2:claim:bar\n"
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning becomes a test failure
        sc = parse_sidecar_text(text)
    assert sc.edges[0].source == "p1:claim:foo"
    assert sc.edges[0].target == "p2:claim:bar"


def test_v02_pipe_edge_handles_unusual_ids() -> None:
    """v0.2 format unambiguously preserves IDs with arbitrary colon counts."""
    text = "RRVIX:edge:depends_on:arxiv:2305.12345|p2:claim:foo:bar:baz\n"
    sc = parse_sidecar_text(text)
    assert sc.edges[0].source == "arxiv:2305.12345"
    assert sc.edges[0].target == "p2:claim:foo:bar:baz"


def test_v01_warning_fires_only_once_per_call() -> None:
    """Even with many v0.1 edges in one file, only one DeprecationWarning."""
    text = "\n".join(
        f"RRVIX:edge:depends_on:p1:claim:c{i}:p2:claim:d{i}" for i in range(5)
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        parse_sidecar_text(text)
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1


def test_empty_input() -> None:
    sc = parse_sidecar_text("")
    assert sc.meta == ()
    assert sc.envs == ()
    assert sc.edges == ()
