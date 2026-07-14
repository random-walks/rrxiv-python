"""RRP-0030: claim authoring keys on the `claim` environment's optional arg.

Unit tests for the key=value parser plus an end-to-end build over an
inline fixture (mirrors the tests/fixtures/minimal layout but written to
tmp_path so the fixture stays self-documenting here).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rrxiv.models import CIR
from rrxiv.parser import build_cir
from rrxiv.parser.build import ClaimKeyError, _parse_claim_keys

# ---------------------------------------------------------------------------
# Unit: _parse_claim_keys


def test_plain_title_returns_none() -> None:
    """Back-compat: a title with no depth-0 '=' is not a key list."""
    assert _parse_claim_keys("A short label for the claim") is None
    assert _parse_claim_keys(None) is None
    assert _parse_claim_keys("") is None


def test_each_key_maps() -> None:
    keys = _parse_claim_keys(
        "type=empirical, evidence=experiment, confidence=0.72, "
        "confidence-low=0.55, confidence-high=0.86, "
        "rationale={small N, single site}, labels={negative-result, small-n}, "
        "models={gpt-4, claude-3}, datasets={mnist}, regimes={low-data}, "
        "assumptions={iid sampling}, title=Main result"
    )
    assert keys is not None
    assert keys["type"] == "empirical"
    assert keys["evidence"] == "experiment"
    assert keys["confidence"] == 0.72
    assert keys["confidence-low"] == 0.55
    assert keys["confidence-high"] == 0.86
    assert keys["rationale"] == "small N, single site"
    assert keys["labels"] == ["negative-result", "small-n"]
    assert keys["models"] == ["gpt-4", "claude-3"]
    assert keys["datasets"] == ["mnist"]
    assert keys["regimes"] == ["low-data"]
    assert keys["assumptions"] == ["iid sampling"]
    assert keys["title"] == "Main result"


def test_braces_protect_commas() -> None:
    keys = _parse_claim_keys("title={Estimation, revisited}, type=theoretical")
    assert keys is not None
    assert keys["title"] == "Estimation, revisited"
    assert keys["type"] == "theoretical"


def test_unknown_key_is_loud() -> None:
    with pytest.raises(ClaimKeyError, match="unknown claim key"):
        _parse_claim_keys("typ=empirical")


def test_invalid_claim_type_is_loud() -> None:
    with pytest.raises(ClaimKeyError, match="not a claim_type"):
        _parse_claim_keys("type=emprical")


def test_invalid_evidence_type_is_loud() -> None:
    with pytest.raises(ClaimKeyError, match="not an evidence_type"):
        _parse_claim_keys("evidence=vibes")


def test_confidence_bounds_are_loud() -> None:
    with pytest.raises(ClaimKeyError, match="outside"):
        _parse_claim_keys("confidence=1.2")
    with pytest.raises(ClaimKeyError, match="not a number"):
        _parse_claim_keys("confidence=high")


# ---------------------------------------------------------------------------
# End-to-end: .tex + sidecar → CIR

_KEYED_OPT = (
    "type=empirical, evidence=experiment, confidence=0.72, "
    "confidence-low=0.55, confidence-high=0.86, rationale={small N}, "
    "labels={negative-result, small-n}, models={gpt-4}, datasets={mnist}, "
    "regimes={low-data}, assumptions={iid sampling}, title=Keyed claim"
)

_TEX = r"""
\documentclass{rrxiv}
\rrxivid{rrxiv-test-rrp0030}
\rrxivversion{v1}
\rrxivlicense{CC-BY-4.0}
\rrxivtopics{test}
\title{RRP-0030 fixture}
\author{Test Author}
\begin{document}
\maketitle

\section{Claims}

\begin{claim}[Plain old title]
\label{claim:c1}
Back-compat claim keeps the v0.1 defaults.
\end{claim}

\begin{claim}[__KEYED_OPT__]
\label{claim:c2}
Keyed claim carries explicit epistemology.
\end{claim}

\end{document}
""".replace("__KEYED_OPT__", _KEYED_OPT)

_SIDECAR = """RRXIV:meta:id:rrxiv-test-rrp0030
RRXIV:meta:version:v1
RRXIV:meta:protocol:0.1.0
RRXIV:meta:license:CC-BY-4.0
RRXIV:meta:topics:test
RRXIV:claim:1
RRXIV:claim:2
"""


@pytest.fixture
def rrp0030_cir(tmp_path: Path) -> CIR:
    tex = tmp_path / "paper.tex"
    tex.write_text(_TEX, encoding="utf-8")
    (tmp_path / "paper.rrxiv.aux").write_text(_SIDECAR, encoding="utf-8")
    return build_cir(tex)


def test_backcompat_claim_keeps_defaults(rrp0030_cir: CIR) -> None:
    c1 = next(c for c in rrp0030_cir.claims if c.id.endswith("claim:c1"))
    assert c1.claim_type.value == "theoretical"
    assert c1.evidence_type.value == "argument"
    assert c1.confidence is None
    assert not c1.labels
    assert c1.scope is None


def test_keyed_claim_carries_fields(rrp0030_cir: CIR) -> None:
    c2 = next(c for c in rrp0030_cir.claims if c.id.endswith("claim:c2"))
    assert c2.claim_type.value == "empirical"
    assert c2.evidence_type.value == "experiment"
    assert c2.confidence is not None
    assert c2.confidence.point == 0.72
    assert c2.confidence.lower == 0.55
    assert c2.confidence.upper == 0.86
    assert c2.confidence.rationale == "small N"
    assert c2.labels == ["negative-result", "small-n"]
    assert c2.scope is not None
    assert c2.scope.models == ["gpt-4"]
    assert c2.scope.datasets == ["mnist"]
    assert c2.scope.regimes == ["low-data"]
    assert c2.scope.assumptions == ["iid sampling"]
    # The key list must not leak into the statement.
    assert "type=" not in c2.statement
    assert c2.statement.startswith("Keyed claim")


def test_linewrapped_key_list_is_parsed(tmp_path: Path) -> None:
    """A line-wrapped optional arg must not leak into the statement."""
    wrapped = _TEX.replace(
        f"[{_KEYED_OPT}]",
        "[type=empirical, evidence=experiment,\n"
        "  labels={negative-result, small-n},\n"
        "  title=Keyed claim]",
    )
    tex = tmp_path / "paper.tex"
    tex.write_text(wrapped, encoding="utf-8")
    (tmp_path / "paper.rrxiv.aux").write_text(_SIDECAR, encoding="utf-8")
    cir = build_cir(tex)
    c2 = next(c for c in cir.claims if c.id.endswith("claim:c2"))
    assert c2.claim_type.value == "empirical"
    assert c2.labels == ["negative-result", "small-n"]
    assert "type=" not in c2.statement
    assert c2.statement.startswith("Keyed claim")


def test_prose_bracket_prefix_stays_in_statement(tmp_path: Path) -> None:
    """A leading citation bracket like [1] is prose, not a key list."""
    prose = _TEX.replace(
        f"[{_KEYED_OPT}]\n\\label{{claim:c2}}\nKeyed claim carries explicit epistemology.",
        "\n\\label{claim:c2}\n[1] establishes the bound we assume here.",
    )
    tex = tmp_path / "paper.tex"
    tex.write_text(prose, encoding="utf-8")
    (tmp_path / "paper.rrxiv.aux").write_text(_SIDECAR, encoding="utf-8")
    cir = build_cir(tex)
    c2 = next(c for c in cir.claims if c.id.endswith("claim:c2"))
    assert c2.claim_type.value == "theoretical"
    assert c2.statement.startswith("[1] establishes")


def test_invalid_key_fails_build(tmp_path: Path) -> None:
    tex = tmp_path / "paper.tex"
    tex.write_text(
        _TEX.replace("type=empirical", "type=emprical"), encoding="utf-8"
    )
    (tmp_path / "paper.rrxiv.aux").write_text(_SIDECAR, encoding="utf-8")
    with pytest.raises(ClaimKeyError):
        build_cir(tex)
