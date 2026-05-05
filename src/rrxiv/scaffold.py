"""Scaffold a new rrxiv paper directory from the skeleton template.

The :func:`scaffold_paper` function writes a self-contained directory
with the paper's ``.tex``, ``.bib``, and a bundled ``rrxiv.cls``. It's
the implementation behind ``rrxiv init <path>``.

The scaffold is deliberately minimal — one claim, one evidence, one
open question, no edges — so authors start from working code rather
than empty files. The output compiles cleanly with tectonic and
produces a valid CIR.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rrxiv import __version__

# The cls is embedded as a string so the scaffolded directory is
# self-contained even when the user's environment has no rrxiv source
# tree available.
_RRXIV_CLS_TEMPLATE = r"""% rrxiv.cls -- rrxiv paper class v0.2 (bundled by rrxiv init)
%
% Provides a LaTeX class for rrxiv submissions. Extends `article` with
% semantic environments that the rrxiv parser extracts into the
% Canonical Intermediate Representation (CIR) without requiring
% post-hoc OCR.
%
% This is a copy of the canonical rrxiv.cls bundled with your paper
% per spec/0004-tex-template.md §"Distributing the class with your
% paper". Update by re-running `rrxiv init` (or by manually replacing
% with the upstream version).
%
% License: MIT.

\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{rrxiv}[2026/05/05 v0.2 rrxiv paper class (bundled)]

\DeclareOption*{\PassOptionsToClass{\CurrentOption}{article}}
\ProcessOptions\relax
\LoadClass[11pt]{article}

\RequirePackage[a4paper, margin=1in]{geometry}
\RequirePackage[utf8]{inputenc}
\RequirePackage[T1]{fontenc}
\RequirePackage{amsmath, amsthm, amssymb, mathtools}
\RequirePackage{graphicx}
\RequirePackage{xcolor}
\RequirePackage{enumitem}
\RequirePackage[numbers]{natbib}
\RequirePackage{booktabs}
\RequirePackage{authblk}
\RequirePackage[colorlinks=true,
                linkcolor=blue!50!black,
                citecolor=blue!50!black,
                urlcolor=blue!50!black]{hyperref}

\newcommand{\rrxivid}[1]{\def\@rrxivid{#1}}
\newcommand{\rrxivversion}[1]{\def\@rrxivversion{#1}}
\newcommand{\rrxivprotocolversion}[1]{\def\@rrxivprotocolversion{#1}}
\newcommand{\rrxivlicense}[1]{\def\@rrxivlicense{#1}}
\newcommand{\rrxivtopics}[1]{\def\@rrxivtopics{#1}}

\rrxivid{TBD}
\rrxivversion{v1}
\rrxivprotocolversion{0.1.0}
\rrxivlicense{CC-BY-4.0}
\rrxivtopics{}

\newwrite\rrxiv@sidecar
\AtBeginDocument{%
  \immediate\openout\rrxiv@sidecar=\jobname.rrxiv.aux\relax
  \immediate\write\rrxiv@sidecar{RRXIV:meta:id:\@rrxivid}%
  \immediate\write\rrxiv@sidecar{RRXIV:meta:version:\@rrxivversion}%
  \immediate\write\rrxiv@sidecar{RRXIV:meta:protocol:\@rrxivprotocolversion}%
  \immediate\write\rrxiv@sidecar{RRXIV:meta:license:\@rrxivlicense}%
  \immediate\write\rrxiv@sidecar{RRXIV:meta:topics:\@rrxivtopics}%
}
\AtEndDocument{\immediate\closeout\rrxiv@sidecar}

\newcommand{\rrxiv@emit}[2]{%
  \immediate\write\rrxiv@sidecar{RRXIV:#1:\@currentlabel}%
}

\theoremstyle{definition}
\newtheorem{rrxiv@claim}{Claim}
\newtheorem{rrxiv@evidence}{Evidence}
\newtheorem{rrxiv@observation}{Observation}
\newtheorem{rrxiv@remark}{Remark}
\newtheorem{rrxiv@scope}{Scope}
\newtheorem{rrxiv@openquestion}{Open Question}

\newenvironment{claim}[1][]{\begin{rrxiv@claim}[#1]\rrxiv@emit{claim}{begin}}{\end{rrxiv@claim}}
\newenvironment{evidence}[1][]{\begin{rrxiv@evidence}[#1]\rrxiv@emit{evidence}{begin}}{\end{rrxiv@evidence}}
\newenvironment{observation}[1][]{\begin{rrxiv@observation}[#1]\rrxiv@emit{observation}{begin}}{\end{rrxiv@observation}}
\newenvironment{rrxivremark}[1][]{\begin{rrxiv@remark}[#1]\rrxiv@emit{remark}{begin}}{\end{rrxiv@remark}}
\newenvironment{scope}[1][]{\begin{rrxiv@scope}[#1]\rrxiv@emit{scope}{begin}}{\end{rrxiv@scope}}
\newenvironment{openquestion}[1][]{\begin{rrxiv@openquestion}[#1]\rrxiv@emit{openquestion}{begin}}{\end{rrxiv@openquestion}}

\newcommand{\dependson}[2]{\immediate\write\rrxiv@sidecar{RRXIV:edge:depends_on:#1|#2}}
\newcommand{\contradicts}[2]{\immediate\write\rrxiv@sidecar{RRXIV:edge:contradicts:#1|#2}}
\newcommand{\extendsclaim}[2]{\immediate\write\rrxiv@sidecar{RRXIV:edge:extends:#1|#2}}
\newcommand{\supports}[2]{\immediate\write\rrxiv@sidecar{RRXIV:edge:supports:#1|#2}}

\newcommand{\R}{\mathbb{R}}
\newcommand{\N}{\mathbb{N}}
\newcommand{\Z}{\mathbb{Z}}
\newcommand{\E}{\mathbb{E}}
\newcommand{\Prob}{\mathbb{P}}
\newcommand{\1}{\mathbf{1}}

\endinput
"""

_TEX_TEMPLATE = r"""\documentclass{rrxiv}

\rrxivid{__PAPER_ID__}
\rrxivversion{v1}
\rrxivprotocolversion{0.1.0}
\rrxivlicense{__LICENSE__}
\rrxivtopics{__TOPICS__}

\title{__TITLE__}
\author{__AUTHOR__}
\date{\today}

\begin{document}
\maketitle

\begin{abstract}
TODO: replace with your abstract. A short, plain-text summary of what
this paper claims and why a reader should care. Math can be inline.
The abstract is captured directly into the CIR's \texttt{abstract}
field; keep it standalone-readable.
\end{abstract}

\section{Introduction}
\label{sec:intro}

Open with the motivation. Cite prior work where relevant.

\section{Main result}
\label{sec:main}

\begin{claim}[A short label]
\label{claim:main}
TODO: replace with your claim, stated as a single falsifiable
assertion in plain language. A reader should be able to read this
paragraph in isolation and know what is being asserted.
\end{claim}

\begin{evidence}[Evidence for claim:main]
\label{ev:main}
TODO: sketch or describe the evidence supporting the claim above.
\end{evidence}

\section{Discussion}
\label{sec:discussion}

\begin{openquestion}[Something this paper does not resolve]
\label{oq:future}
TODO: an open question this paper raises but does not answer.
\end{openquestion}

\bibliographystyle{plainnat}
\bibliography{__BIB_BASE__}

\end{document}
"""

_BIB_TEMPLATE = r"""@misc{example-prior-work,
  author = {Example Author and Other Author},
  title  = {An example prior work cited from your rrxiv paper},
  year   = {2024},
  note   = {Replace with real bibliography entries.}
}
"""

_README_TEMPLATE = """# {paper_id}

A rrxiv paper. Scaffolded by `rrxiv init` from the bundled v0.2 template.

## Build

```bash
tectonic --keep-intermediates {basename}.tex
# or:
pdflatex {basename}.tex && bibtex {basename} && pdflatex {basename}.tex && pdflatex {basename}.tex
```

You'll get `{basename}.pdf` and `{basename}.rrxiv.aux`.

## Validate

```bash
rrxiv parse {basename}.tex --output {basename}.cir.json
rrxiv validate {basename}.cir.json
```

## Contents

| File | Role |
|------|------|
| `{basename}.tex` | Paper source. |
| `{basename}.bib` | Bibliography. |
| `rrxiv.cls`     | LaTeX class (bundled by `rrxiv init`; rrxiv v0.2 protocol). |

## Submit

When you're ready to submit:

1. Make sure `rrxiv parse` produces a CIR that `rrxiv validate` accepts.
2. (Future, when a rrxiv server exists.) `rrxiv submit` will package the
   directory into a tarball and upload it.
"""


@dataclass(frozen=True, slots=True)
class ScaffoldOptions:
    """Options for :func:`scaffold_paper`."""

    paper_id: str
    title: str
    author: str
    license: str = "CC-BY-4.0"
    topics: tuple[str, ...] = ()
    basename: str | None = None
    """Filename stem for the .tex/.bib. Defaults to ``paper_id``."""


def scaffold_paper(directory: Path | str, opts: ScaffoldOptions) -> Path:
    """Create a new rrxiv paper directory.

    Writes ``<basename>.tex``, ``<basename>.bib``, ``rrxiv.cls``, and
    a ``README.md`` describing how to build, validate, and (eventually)
    submit. Returns the absolute path of the created directory.

    Raises:
        FileExistsError: if ``directory`` already exists and is non-empty.
    """
    target = Path(directory).resolve()
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(
            f"{target} already exists and is not empty; refusing to overwrite."
        )
    target.mkdir(parents=True, exist_ok=True)

    basename = opts.basename or opts.paper_id

    tex = (
        _TEX_TEMPLATE
        .replace("__PAPER_ID__", opts.paper_id)
        .replace("__TITLE__", opts.title)
        .replace("__AUTHOR__", opts.author)
        .replace("__LICENSE__", opts.license)
        .replace("__TOPICS__", ",".join(opts.topics))
        .replace("__BIB_BASE__", basename)
    )
    (target / f"{basename}.tex").write_text(tex, encoding="utf-8")
    (target / f"{basename}.bib").write_text(_BIB_TEMPLATE, encoding="utf-8")
    (target / "rrxiv.cls").write_text(_RRXIV_CLS_TEMPLATE, encoding="utf-8")
    (target / "README.md").write_text(
        _README_TEMPLATE.format(paper_id=opts.paper_id, basename=basename),
        encoding="utf-8",
    )

    return target


__all__ = ["ScaffoldOptions", "__version__", "scaffold_paper"]
