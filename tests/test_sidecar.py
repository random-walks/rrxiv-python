"""Tests for the rrxiv sidecar reader."""

from __future__ import annotations

import warnings

import pytest

from rrxiv.parser.sidecar import (
    EnvMarker,
    MetaMarker,
    parse_sidecar_text,
)


def test_minimal_sidecar() -> None:
    text = """\
RRXIV:meta:id:rrxiv-example-minimal
RRXIV:meta:version:v1
RRXIV:meta:protocol:0.1.0
RRXIV:meta:license:CC-BY-4.0
RRXIV:meta:topics:example,conformance
RRXIV:claim:1
RRXIV:evidence:1
RRXIV:openquestion:1
"""
    sc = parse_sidecar_text(text)

    assert sc.meta == (
        MetaMarker("id", "rrxiv-example-minimal"),
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
RRXIV:meta:id:rrxiv-0001
RRXIV:meta:version:v1
RRXIV:meta:protocol:0.1.0
RRXIV:meta:license:CC-BY-4.0
RRXIV:meta:topics:infrastructure,scientific-publishing,ai-research,protocol-design
RRXIV:claim:1
RRXIV:observation:1
RRXIV:claim:2
RRXIV:edge:depends_on:rrxiv-0001:claim:queryability|rrxiv-0001:claim:volume-structure
RRXIV:remark:1
RRXIV:evidence:1
RRXIV:scope:1
RRXIV:claim:3
RRXIV:remark:2
RRXIV:openquestion:1
RRXIV:openquestion:2
RRXIV:claim:4
RRXIV:openquestion:3
RRXIV:remark:3
"""
    sc = parse_sidecar_text(text)

    assert sc.meta_dict() == {
        "id": "rrxiv-0001",
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
    assert edge.source == "rrxiv-0001:claim:queryability"
    assert edge.target == "rrxiv-0001:claim:volume-structure"


def test_topics_with_commas_in_value_preserved() -> None:
    sc = parse_sidecar_text("RRXIV:meta:topics:a,b,c\n")
    assert sc.meta_dict()["topics"] == "a,b,c"


def test_unknown_meta_key_skipped() -> None:
    """Forward compat: unknown RRXIV:meta:<key>:... lines must not crash."""
    text = "RRXIV:meta:id:p1\nRRXIV:meta:newfield:future\n"
    sc = parse_sidecar_text(text)
    assert sc.meta_dict() == {"id": "p1"}


def test_unknown_env_kind_skipped() -> None:
    """Forward compat: unknown RRXIV:<kind>:... environment markers ignored."""
    text = "RRXIV:claim:1\nRRXIV:newenv:1\n"
    sc = parse_sidecar_text(text)
    assert sc.envs == (EnvMarker("claim", "1"),)


def test_unknown_edge_kind_skipped() -> None:
    text = "RRXIV:edge:depends_on:a|b\nRRXIV:edge:newrel:c|d\n"
    sc = parse_sidecar_text(text)
    assert len(sc.edges) == 1
    assert sc.edges[0].edge_type == "depends_on"


def test_blank_and_non_rrxiv_lines_skipped() -> None:
    text = """
% LaTeX comment
RRXIV:claim:1

random noise
RRXIV:meta:id:p1
"""
    sc = parse_sidecar_text(text)
    assert len(sc.envs) == 1
    assert sc.meta_dict() == {"id": "p1"}


def test_v01_edge_with_colons_in_ids_emits_deprecation() -> None:
    """v0.1 colon-joined edges are still parsed (heuristic) but warn."""
    text = "RRXIV:edge:supports:p1:claim:foo:p2:claim:bar\n"
    with pytest.warns(DeprecationWarning, match="RRP-0002"):
        sc = parse_sidecar_text(text)
    assert sc.edges[0].source == "p1:claim:foo"
    assert sc.edges[0].target == "p2:claim:bar"


def test_v02_pipe_edge_no_warning() -> None:
    """v0.2 pipe-separated edges parse cleanly with no warning."""
    text = "RRXIV:edge:supports:p1:claim:foo|p2:claim:bar\n"
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning becomes a test failure
        sc = parse_sidecar_text(text)
    assert sc.edges[0].source == "p1:claim:foo"
    assert sc.edges[0].target == "p2:claim:bar"


def test_v02_pipe_edge_handles_unusual_ids() -> None:
    """v0.2 format unambiguously preserves IDs with arbitrary colon counts."""
    text = "RRXIV:edge:depends_on:arxiv:2305.12345|p2:claim:foo:bar:baz\n"
    sc = parse_sidecar_text(text)
    assert sc.edges[0].source == "arxiv:2305.12345"
    assert sc.edges[0].target == "p2:claim:foo:bar:baz"


def test_v01_warning_fires_only_once_per_call() -> None:
    """Even with many v0.1 edges in one file, only one DeprecationWarning."""
    text = "\n".join(
        f"RRXIV:edge:depends_on:p1:claim:c{i}:p2:claim:d{i}" for i in range(5)
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


# ---- Pre-rename RRVIX: prefix back-compat ----


def test_legacy_rrvix_prefix_still_parses() -> None:
    """Sidecars compiled with the pre-rename rrvix.cls used the RRVIX:
    prefix. The renamed parser still accepts those, with a
    DeprecationWarning."""
    text = """\
RRVIX:meta:id:legacy-paper
RRVIX:meta:version:v1
RRVIX:claim:1
RRVIX:edge:depends_on:legacy-paper:claim:a|legacy-paper:claim:b
"""
    with pytest.warns(DeprecationWarning, match="legacy RRVIX: prefix"):
        sc = parse_sidecar_text(text)
    assert sc.meta_dict()["id"] == "legacy-paper"
    assert len(sc.envs) == 1
    assert sc.envs[0].kind == "claim"
    assert len(sc.edges) == 1
    assert sc.edges[0].source == "legacy-paper:claim:a"
    assert sc.edges[0].target == "legacy-paper:claim:b"


def test_legacy_warning_only_once_per_call() -> None:
    text = "\n".join([f"RRVIX:meta:id:p{i}" for i in range(5)])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        parse_sidecar_text(text)
    legacy = [
        w
        for w in caught
        if issubclass(w.category, DeprecationWarning)
        and "legacy RRVIX:" in str(w.message)
    ]
    assert len(legacy) == 1


def test_mixed_prefix_lines_both_parse() -> None:
    """A sidecar with both prefixes (rare but possible during a
    transition) still parses; RRVIX: triggers the warning; both
    contribute to the Sidecar."""
    text = """\
RRXIV:meta:id:new-paper
RRVIX:claim:1
RRXIV:claim:2
"""
    with pytest.warns(DeprecationWarning):
        sc = parse_sidecar_text(text)
    assert sc.meta_dict()["id"] == "new-paper"
    assert len(sc.envs) == 2
