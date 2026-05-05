"""Tests for the LaTeX walker."""

from __future__ import annotations

from rrvix.parser.tex import (
    parse_tex,
    tex_env_to_sidecar_kind,
)

MINIMAL_TEX = r"""
\documentclass{rrvix}

\rrvixid{rrvix-example-minimal}
\rrvixversion{v1}
\rrvixprotocolversion{0.1.0}
\rrvixlicense{CC-BY-4.0}
\rrvixtopics{example,conformance}

\title{A minimal rrvix paper}
\author{rrvix Project}

\begin{document}
\maketitle

\begin{abstract}
A short abstract for testing. It contains \textit{italics} but no
math mode and no nested environments.
\end{abstract}

\section{The claim}
\label{sec:claim}

\begin{claim}[Conformance fixture]
\label{claim:fixture}
A minimal rrvix paper produces a valid CIR~\cite{rrvix-cir-schema}.
\end{claim}

\begin{evidence}[CI conformance test]
\label{ev:ci-test}
The conformance suite runs the parser on this file in CI.
\end{evidence}

\begin{openquestion}[Future schema bumps]
\label{oq:schema-bumps}
What happens when the schema bumps?
\end{openquestion}

\bibliographystyle{plainnat}
\bibliography{minimal}

\end{document}
"""


def test_title() -> None:
    doc = parse_tex(MINIMAL_TEX)
    assert doc.title == "A minimal rrvix paper"


def test_authors() -> None:
    doc = parse_tex(MINIMAL_TEX)
    assert len(doc.authors) == 1
    assert doc.authors[0].name == "rrvix Project"
    assert doc.authors[0].affil_indices == ()


def test_authors_with_affiliations() -> None:
    src = r"""
\title{T}
\author[1]{First Author}
\author[2]{Second Author}
\affil[1]{Department of X}
\affil[2]{Independent}
\begin{document}
\end{document}
"""
    doc = parse_tex(src)
    assert len(doc.authors) == 2
    assert doc.authors[0].affil_indices == (1,)
    assert doc.authors[1].affil_indices == (2,)
    assert doc.affiliations == {1: "Department of X", 2: "Independent"}


def test_abstract() -> None:
    doc = parse_tex(MINIMAL_TEX)
    assert doc.abstract is not None
    assert "A short abstract for testing" in doc.abstract
    assert "italics" in doc.abstract


def test_metadata() -> None:
    doc = parse_tex(MINIMAL_TEX)
    assert doc.metadata.rrvix_id == "rrvix-example-minimal"
    assert doc.metadata.rrvix_version == "v1"
    assert doc.metadata.rrvix_protocol_version == "0.1.0"
    assert doc.metadata.rrvix_license == "CC-BY-4.0"
    assert doc.metadata.rrvix_topics == ("example", "conformance")


def test_sections() -> None:
    doc = parse_tex(MINIMAL_TEX)
    assert len(doc.sections) == 1
    assert doc.sections[0].title == "The claim"
    assert doc.sections[0].level == 1
    assert doc.sections[0].label == "sec:claim"


def test_environments() -> None:
    doc = parse_tex(MINIMAL_TEX)
    names = [e.name for e in doc.environments]
    assert names == ["claim", "evidence", "openquestion"]

    claim = doc.environments[0]
    assert claim.title == "Conformance fixture"
    assert claim.label == "claim:fixture"
    assert "minimal rrvix paper produces a valid CIR" in claim.body

    ev = doc.environments[1]
    assert ev.title == "CI conformance test"
    assert ev.label == "ev:ci-test"

    oq = doc.environments[2]
    assert oq.title == "Future schema bumps"
    assert oq.label == "oq:schema-bumps"


def test_citations() -> None:
    doc = parse_tex(MINIMAL_TEX)
    assert len(doc.citations) == 1
    assert doc.citations[0].keys == ("rrvix-cir-schema",)


def test_bibliography_file() -> None:
    doc = parse_tex(MINIMAL_TEX)
    assert doc.bibliography_files == ("minimal",)


def test_comment_stripping() -> None:
    src = r"""
\title{Real} % a comment that says \title{Fake}
\begin{document}
\end{document}
"""
    doc = parse_tex(src)
    assert doc.title == "Real"


def test_multiple_subsection_levels() -> None:
    src = r"""
\title{T}
\section{One}
\label{sec:one}
\subsection{One.A}
\subsubsection{One.A.i}
\section{Two}
"""
    doc = parse_tex(src)
    levels = [s.level for s in doc.sections]
    assert levels == [1, 2, 3, 1]


def test_rrvixremark_maps_to_remark() -> None:
    """The TeX env name 'rrvixremark' maps to sidecar kind 'remark'."""
    assert tex_env_to_sidecar_kind("rrvixremark") == "remark"
    assert tex_env_to_sidecar_kind("claim") == "claim"
