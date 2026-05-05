"""LaTeX source walker for rrxiv papers.

For v0.1, the parser uses pragmatic regex extraction. The rrxiv.cls
papers are structurally simple (no nested macros that defeat regex), so
this gets us 95% there with 5% of the complexity. v0.2 can move to
pylatexenc for robustness once we hit a paper that breaks the regex.

What this module extracts:
- ``\\title{...}``
- ``\\author{...}`` (with optional ``[N]`` affiliation refs) and
  ``\\affil[N]{...}``
- ``\\rrxiv*{...}`` metadata commands (fallback; the sidecar is canonical)
- ``\\begin{abstract}...\\end{abstract}``
- ``\\section{}`` / ``\\subsection{}`` / ``\\subsubsection{}`` hierarchy
- ``\\begin{<env>}[<title>]...\\end{<env>}`` blocks for the six rrxiv
  environments, with each block's body, optional title, and any
  ``\\label{...}`` inside the body
- ``\\cite{<key>[,<key>...]}`` calls
- ``\\bibliography{<filename>}`` references

It does NOT understand math mode, comments inside environments, or
nested braces. Don't author rrxiv papers in a way that requires those
to be parsed; the sidecar is the canonical source of truth for claim
graph semantics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# rrxiv.cls semantic environments. Mirrors EnvKind in sidecar.py — but the
# sidecar uses ``remark`` while the LaTeX environment is ``rrxivremark``.
TEX_ENV_NAMES: tuple[str, ...] = (
    "claim",
    "evidence",
    "observation",
    "scope",
    "openquestion",
    "rrxivremark",
)

_TEX_ENV_TO_SIDECAR_KIND: dict[str, str] = {
    "claim": "claim",
    "evidence": "evidence",
    "observation": "observation",
    "scope": "scope",
    "openquestion": "openquestion",
    "rrxivremark": "remark",
}


def tex_env_to_sidecar_kind(name: str) -> str:
    return _TEX_ENV_TO_SIDECAR_KIND[name]


@dataclass(frozen=True, slots=True)
class TexAuthor:
    name: str
    affil_indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TexSection:
    """A section heading. ``level`` is 1 for section, 2 for subsection, etc."""

    level: int
    title: str
    label: str | None
    char_offset: int


@dataclass(frozen=True, slots=True)
class TexEnvironment:
    """A semantic environment block from rrxiv.cls."""

    name: str  # one of TEX_ENV_NAMES
    title: str | None
    label: str | None
    body: str
    char_offset: int


@dataclass(frozen=True, slots=True)
class TexCitation:
    """A single ``\\cite`` call. ``keys`` is the comma-separated argument."""

    keys: tuple[str, ...]
    char_offset: int


@dataclass(frozen=True, slots=True)
class TexMetadata:
    """Metadata extracted from rrxiv.cls's ``\\rrxiv*`` commands. The
    sidecar is canonical; this is fallback only."""

    rrxiv_id: str | None
    rrxiv_version: str | None
    rrxiv_protocol_version: str | None
    rrxiv_license: str | None
    rrxiv_topics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TexDocument:
    """All structured content extracted from a LaTeX source."""

    title: str | None
    authors: tuple[TexAuthor, ...]
    affiliations: dict[int, str]
    abstract: str | None
    metadata: TexMetadata
    sections: tuple[TexSection, ...]
    environments: tuple[TexEnvironment, ...]
    citations: tuple[TexCitation, ...]
    bibliography_files: tuple[str, ...]


# ---- Regex helpers ----

_RE_TITLE = re.compile(r"\\title\s*\{([^}]*)\}")
_RE_AUTHOR = re.compile(r"\\author\s*(?:\[([\d,]+)\])?\s*\{([^}]*)\}")
_RE_AFFIL = re.compile(r"\\affil\s*\[(\d+)\]\s*\{([^}]*)\}")
_RE_ABSTRACT = re.compile(
    r"\\begin\{abstract\}(.*?)\\end\{abstract\}", re.DOTALL
)
_RE_RRXIVID = re.compile(r"\\rrxivid\s*\{([^}]*)\}")
_RE_RRXIVVER = re.compile(r"\\rrxivversion\s*\{([^}]*)\}")
_RE_RRXIVPROTO = re.compile(r"\\rrxivprotocolversion\s*\{([^}]*)\}")
_RE_RRXIVLIC = re.compile(r"\\rrxivlicense\s*\{([^}]*)\}")
_RE_RRXIVTOPICS = re.compile(r"\\rrxivtopics\s*\{([^}]*)\}")
_RE_SECTION = re.compile(
    r"\\(section|subsection|subsubsection|paragraph)\*?\s*\{([^}]*)\}"
)
_RE_LABEL = re.compile(r"\\label\s*\{([^}]+)\}")
_RE_CITE = re.compile(r"\\cite\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_RE_BIBLIOGRAPHY = re.compile(r"\\bibliography\s*\{([^}]+)\}")

_SECTION_LEVELS: dict[str, int] = {
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
    "paragraph": 4,
}


def _strip_comments(tex: str) -> str:
    """Remove LaTeX line comments (``%`` to end-of-line). A backslash
    before ``%`` (i.e. the literal ``\\%``) is preserved.
    """
    out: list[str] = []
    for line in tex.splitlines(keepends=True):
        # Find first unescaped %
        i = 0
        while i < len(line):
            if line[i] == "%" and (i == 0 or line[i - 1] != "\\"):
                break
            i += 1
        if i < len(line):
            out.append(line[:i])
            if line.endswith("\n"):
                out.append("\n")
        else:
            out.append(line)
    return "".join(out)


def _find_environment_blocks(tex: str, env_name: str) -> list[TexEnvironment]:
    """Find all `\\begin{env_name}[...]...\\end{env_name}` blocks.

    Greedy across nested envs of the *same* name is unsupported; we don't
    expect nested claims, evidences, etc., in a v0.1 paper.
    """
    blocks: list[TexEnvironment] = []
    pattern = re.compile(
        rf"\\begin\{{{env_name}}}(?:\[([^\]]*)\])?(.*?)\\end\{{{env_name}}}",
        re.DOTALL,
    )
    for m in pattern.finditer(tex):
        title = m.group(1)
        body = m.group(2).strip()
        label_match = _RE_LABEL.search(body)
        label = label_match.group(1) if label_match else None
        blocks.append(
            TexEnvironment(
                name=env_name,
                title=title,
                label=label,
                body=body,
                char_offset=m.start(),
            )
        )
    return blocks


def parse_tex(tex_source: str) -> TexDocument:
    """Parse a LaTeX source string into a TexDocument."""
    tex = _strip_comments(tex_source)

    # Title: take the last \title{} (the document's, not earlier macros).
    title_matches = _RE_TITLE.findall(tex)
    title = title_matches[-1].strip() if title_matches else None

    # Authors and affiliations
    authors: list[TexAuthor] = []
    for m in _RE_AUTHOR.finditer(tex):
        affil_str = m.group(1) or ""
        affil_indices = tuple(
            int(x.strip()) for x in affil_str.split(",") if x.strip().isdigit()
        )
        authors.append(TexAuthor(name=m.group(2).strip(), affil_indices=affil_indices))

    affiliations: dict[int, str] = {}
    for m in _RE_AFFIL.finditer(tex):
        affiliations[int(m.group(1))] = m.group(2).strip()

    # Abstract
    abstract_match = _RE_ABSTRACT.search(tex)
    abstract = abstract_match.group(1).strip() if abstract_match else None

    # Metadata commands (fallback only; sidecar is canonical)
    rrxiv_id_m = _RE_RRXIVID.search(tex)
    rrxiv_ver_m = _RE_RRXIVVER.search(tex)
    rrxiv_proto_m = _RE_RRXIVPROTO.search(tex)
    rrxiv_lic_m = _RE_RRXIVLIC.search(tex)
    rrxiv_topics_m = _RE_RRXIVTOPICS.search(tex)
    rrxiv_topics_raw = rrxiv_topics_m.group(1) if rrxiv_topics_m else ""
    metadata = TexMetadata(
        rrxiv_id=rrxiv_id_m.group(1).strip() if rrxiv_id_m else None,
        rrxiv_version=rrxiv_ver_m.group(1).strip() if rrxiv_ver_m else None,
        rrxiv_protocol_version=rrxiv_proto_m.group(1).strip() if rrxiv_proto_m else None,
        rrxiv_license=rrxiv_lic_m.group(1).strip() if rrxiv_lic_m else None,
        rrxiv_topics=tuple(t.strip() for t in rrxiv_topics_raw.split(",") if t.strip()),
    )

    # Sections
    sections: list[TexSection] = []
    for m in _RE_SECTION.finditer(tex):
        kind = m.group(1)
        section_title = m.group(2).strip()
        # Look for a \label within ~150 chars after this section heading
        tail = tex[m.end() : m.end() + 150]
        label_m = _RE_LABEL.search(tail)
        label = label_m.group(1) if label_m else None
        sections.append(
            TexSection(
                level=_SECTION_LEVELS[kind],
                title=section_title,
                label=label,
                char_offset=m.start(),
            )
        )

    # Environments
    environments: list[TexEnvironment] = []
    for env_name in TEX_ENV_NAMES:
        environments.extend(_find_environment_blocks(tex, env_name))
    environments.sort(key=lambda e: e.char_offset)

    # Citations
    citations: list[TexCitation] = []
    for m in _RE_CITE.finditer(tex):
        keys = tuple(k.strip() for k in m.group(1).split(",") if k.strip())
        if keys:
            citations.append(TexCitation(keys=keys, char_offset=m.start()))

    # Bibliography
    bib_files = tuple(m.group(1).strip() for m in _RE_BIBLIOGRAPHY.finditer(tex))

    return TexDocument(
        title=title,
        authors=tuple(authors),
        affiliations=affiliations,
        abstract=abstract,
        metadata=metadata,
        sections=tuple(sections),
        environments=tuple(environments),
        citations=tuple(citations),
        bibliography_files=bib_files,
    )


def parse_tex_file(path: Path | str) -> TexDocument:
    """Read and parse a .tex file."""
    return parse_tex(Path(path).read_text(encoding="utf-8"))
