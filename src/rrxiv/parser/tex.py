"""LaTeX source walker for rrxiv papers.

v0.2: AST-based, using ``pylatexenc.latexwalker``. Replaces the v0.1
regex-based implementation per RRP-0004. The public surface
(``TexDocument`` dataclass, ``parse_tex``, ``parse_tex_file``,
``tex_env_to_sidecar_kind``) is unchanged so build.py is unaffected.

What this module extracts:
- ``\\title{...}``
- ``\\author{...}`` (with optional ``[N]`` affiliation refs) and
  ``\\affil[N]{...}``
- ``\\rrxiv*{...}`` metadata commands (fallback; the sidecar is canonical)
- ``\\begin{abstract}...\\end{abstract}``
- ``\\section{}`` / ``\\subsection{}`` / ``\\subsubsection{}`` /
  ``\\paragraph{}`` hierarchy
- ``\\begin{<env>}[<title>]...\\end{<env>}`` blocks for the six rrxiv
  environments, with each block's body, optional title, and any
  ``\\label{...}`` inside the body
- ``\\cite{<key>[,<key>...]}`` calls
- ``\\bibliography{<filename>}`` references

Math mode (``$...$``, ``\\[...\\]``) and LaTeX comments (``%...``) are
properly handled by the AST — no source contamination.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pylatexenc import latexwalker as _lw
from pylatexenc.macrospec import MacroSpec

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


def _build_latex_context() -> Any:
    """Extend pylatexenc's default context with rrxiv-specific macros.

    pylatexenc's defaults don't know about ``\\author[N]{Name}`` (they
    declare ``\\author`` as taking only ``{...}``), nor about our
    ``\\affil``, nor the ``\\rrxiv*`` metadata commands. Without this,
    ``\\author[1]{Foo}`` is parsed with ``[`` as a chars node and the
    optional arg is silently lost.
    """
    ctx = _lw.get_default_latex_context_db()
    ctx.add_context_category(
        "rrxiv",
        macros=[
            MacroSpec("author", "[{"),
            MacroSpec("affil", "[{"),
            MacroSpec("rrxivid", "{"),
            MacroSpec("rrxivversion", "{"),
            MacroSpec("rrxivprotocolversion", "{"),
            MacroSpec("rrxivlicense", "{"),
            MacroSpec("rrxivtopics", "{"),
        ],
        prepend=True,
    )
    return ctx


_LATEX_CONTEXT = _build_latex_context()

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
    """A semantic environment block from rrxiv.cls.

    ``char_offset`` is the byte offset of ``\\begin{<env>}`` in the source.
    ``char_end`` is the byte offset of the character just past the
    matching ``\\end{<env>}``; callers can convert both to 1-indexed line
    numbers by counting newlines up to each offset. ``inputs`` is the
    tuple of ``\\input{<path>}`` (and ``\\include{<path>}``) references
    that appear inside the environment's body — the parser captures them
    verbatim so build.py can attach figures to a claim's paired
    evidence block.
    """

    name: str  # one of TEX_ENV_NAMES
    title: str | None
    label: str | None
    body: str
    char_offset: int
    char_end: int = 0
    inputs: tuple[str, ...] = ()


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


_SECTION_LEVELS: dict[str, int] = {
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
    "paragraph": 4,
}


def _verbatim(node: Any) -> str:
    """Return the source-equivalent string for a node, or ''."""
    if node is None:
        return ""
    if hasattr(node, "latex_verbatim"):
        return str(node.latex_verbatim())
    return ""


def _strip_outer_braces(s: str) -> str:
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        return s[1:-1].strip()
    return s


def _macro_arg_groups(node: _lw.LatexMacroNode) -> list[Any]:
    """Return the (non-None) groups pylatexenc parsed as args of this macro.

    Standard LaTeX macros (\\title, \\author, \\section, \\cite, etc.)
    are declared with argspecs in pylatexenc's default context. Their
    args end up in ``node.nodeargd.argnlist``. Unknown macros have no
    declared args and an empty argnlist; for those we fall back to
    sibling-group scanning via :func:`_sibling_group_arg`.
    """
    if node.nodeargd is None:
        return []
    return [a for a in (node.nodeargd.argnlist or []) if a is not None]


def _group_inner_text(group: _lw.LatexGroupNode) -> str:
    """Inner text of a ``{...}`` group."""
    return "".join(_verbatim(c) for c in group.nodelist).strip()


def _macro_required_arg(node: _lw.LatexMacroNode) -> str | None:
    """First mandatory ({...}) arg as inner text, or None."""
    for g in _macro_arg_groups(node):
        if isinstance(g, _lw.LatexGroupNode) and g.delimiters == ("{", "}"):
            return _group_inner_text(g)
    return None


def _macro_optional_arg(node: _lw.LatexMacroNode) -> str | None:
    """First optional ([...]) arg as inner text, or None."""
    for g in _macro_arg_groups(node):
        if isinstance(g, _lw.LatexGroupNode) and g.delimiters == ("[", "]"):
            return _group_inner_text(g)
    return None


def _sibling_group_arg(nodes: list[Any], i: int) -> tuple[str | None, int]:
    """Fallback for unknown macros: scan for the next sibling group.

    Skips whitespace-only chars nodes. Returns ``(content, j)`` where
    ``j`` is the index of the consumed sibling group (so callers can
    continue from ``j + 1``).
    """
    j = i + 1
    while j < len(nodes):
        n = nodes[j]
        if isinstance(n, _lw.LatexCharsNode):
            if n.chars.strip() == "":
                j += 1
                continue
            return None, j
        if isinstance(n, _lw.LatexGroupNode):
            return _group_inner_text(n), j
        return None, j
    return None, j


def _next_group_arg(nodes: list[Any], i: int) -> tuple[str | None, int]:
    """Get the macro's first mandatory arg, preferring pylatexenc's
    declared args, falling back to sibling scanning.

    Returns ``(content, advance_to)`` where ``advance_to`` is the
    index callers should continue from.
    """
    n = nodes[i]
    if isinstance(n, _lw.LatexMacroNode):
        arg = _macro_required_arg(n)
        if arg is not None:
            return arg, i  # the macro itself "consumed" the arg
    return _sibling_group_arg(nodes, i)


def _next_optional_arg(nodes: list[Any], i: int) -> tuple[str | None, int]:
    """Get the optional ``[...]`` arg of a macro.

    Tries the macro's declared args first (most stdlib macros are known
    to pylatexenc and have their optional in ``argnlist``). Falls back
    to scanning following sibling chars for a leading ``[...]``.
    """
    n = nodes[i]
    if isinstance(n, _lw.LatexMacroNode):
        arg = _macro_optional_arg(n)
        if arg is not None:
            return arg, i

    j = i + 1
    while j < len(nodes):
        nj = nodes[j]
        if isinstance(nj, _lw.LatexCharsNode):
            text = nj.chars
            stripped = text.lstrip()
            if not stripped:
                j += 1
                continue
            if stripped.startswith("["):
                idx = stripped.find("]")
                if idx == -1:
                    return None, j
                return stripped[1:idx], j
            return None, j
        return None, j
    return None, j


def _scan_label_in_nodes(nodes: list[Any]) -> str | None:
    """First ``\\label{...}`` argument anywhere in the subtree, or None."""
    for i, n in enumerate(nodes):
        if isinstance(n, _lw.LatexMacroNode) and n.macroname == "label":
            arg, _ = _next_group_arg(nodes, i)
            if arg is not None:
                return arg
        if isinstance(n, _lw.LatexEnvironmentNode):
            inner = _scan_label_in_nodes(n.nodelist)
            if inner is not None:
                return inner
        if isinstance(n, _lw.LatexGroupNode):
            inner = _scan_label_in_nodes(n.nodelist)
            if inner is not None:
                return inner
    return None


def _scan_cites_in_nodes(nodes: list[Any], out: list[TexCitation]) -> None:
    """Collect ``\\cite{...}`` calls recursively."""
    for i, n in enumerate(nodes):
        if isinstance(n, _lw.LatexMacroNode) and n.macroname == "cite":
            arg, _ = _next_group_arg(nodes, i)
            if arg:
                keys = tuple(k.strip() for k in arg.split(",") if k.strip())
                if keys:
                    out.append(
                        TexCitation(
                            keys=keys,
                            char_offset=getattr(n, "pos", 0) or 0,
                        )
                    )
        if isinstance(n, _lw.LatexEnvironmentNode):
            _scan_cites_in_nodes(n.nodelist, out)
        if isinstance(n, _lw.LatexGroupNode):
            _scan_cites_in_nodes(n.nodelist, out)


def _scan_inputs_in_nodes(nodes: list[Any], out: list[str]) -> None:
    """Collect ``\\input{<path>}`` and ``\\include{<path>}`` references
    recursively. Order-preserving; duplicates retained — callers can
    de-dupe if needed.
    """
    for i, n in enumerate(nodes):
        if isinstance(n, _lw.LatexMacroNode) and n.macroname in ("input", "include"):
            arg, _ = _next_group_arg(nodes, i)
            if arg:
                out.append(arg.strip())
        if isinstance(n, _lw.LatexEnvironmentNode):
            _scan_inputs_in_nodes(n.nodelist, out)
        if isinstance(n, _lw.LatexGroupNode):
            _scan_inputs_in_nodes(n.nodelist, out)


def _env_optional_title(env: _lw.LatexEnvironmentNode) -> str | None:
    """Pull the first optional ``[title]`` arg from an environment's
    declared args, or scan the head of the body."""
    args = getattr(env, "nodeargd", None)
    if args is not None:
        argnlist = getattr(args, "argnlist", None) or []
        for a in argnlist:
            if a is None:
                continue
            text = _verbatim(a).strip()
            if text.startswith("[") and text.endswith("]"):
                return text[1:-1].strip()
    if env.nodelist:
        first = env.nodelist[0]
        if isinstance(first, _lw.LatexCharsNode):
            chars = str(first.chars).lstrip()
            if chars.startswith("["):
                end = chars.find("]")
                if end != -1:
                    return chars[1:end]
    return None


def _node_body_text(nodes: list[Any]) -> str:
    """Source-equivalent rendering of a list of nodes."""
    return "".join(_verbatim(n) for n in nodes)


def _walk(
    nodes: list[Any],
    *,
    authors: list[TexAuthor],
    affils: dict[int, str],
    sections: list[TexSection],
    environments: list[TexEnvironment],
    bib_files: list[str],
    metadata: dict[str, Any],
    title_box: list[str | None],
    abstract_box: list[str | None],
) -> None:
    """Walk the top-level node list, populating accumulators."""
    i = 0
    while i < len(nodes):
        n = nodes[i]

        if isinstance(n, _lw.LatexEnvironmentNode):
            env_name = n.environmentname
            if env_name == "abstract":
                abstract_box[0] = _node_body_text(n.nodelist).strip()
            elif env_name in TEX_ENV_NAMES:
                opt_title = _env_optional_title(n)
                body_text = _node_body_text(n.nodelist).strip()
                # Drop the leading [title] if it was scraped from chars
                if opt_title and body_text.startswith("[") and "]" in body_text:
                    end = body_text.find("]")
                    body_text = body_text[end + 1 :].lstrip()
                label = _scan_label_in_nodes(n.nodelist)
                inputs_list: list[str] = []
                _scan_inputs_in_nodes(n.nodelist, inputs_list)
                pos = getattr(n, "pos", 0) or 0
                # pos + len(latex_verbatim) gives the byte offset *just
                # past* \end{<env>}. pylatexenc preserves the verbatim
                # source span for environment nodes, so this is exact
                # rather than an estimate.
                end_offset = pos + len(_verbatim(n))
                environments.append(
                    TexEnvironment(
                        name=env_name,
                        title=opt_title,
                        label=label,
                        body=body_text,
                        char_offset=pos,
                        char_end=end_offset,
                        inputs=tuple(inputs_list),
                    )
                )
            elif env_name == "document":
                _walk(
                    n.nodelist,
                    authors=authors,
                    affils=affils,
                    sections=sections,
                    environments=environments,
                    bib_files=bib_files,
                    metadata=metadata,
                    title_box=title_box,
                    abstract_box=abstract_box,
                )
            i += 1
            continue

        if isinstance(n, _lw.LatexMacroNode):
            name = n.macroname

            if name == "title":
                arg, j = _next_group_arg(nodes, i)
                if arg is not None:
                    title_box[0] = arg
                i = j + 1 if arg is not None else i + 1
                continue

            if name == "author":
                optional, j = _next_optional_arg(nodes, i)
                if optional is not None:
                    affil_indices = tuple(
                        int(x.strip())
                        for x in optional.split(",")
                        if x.strip().isdigit()
                    )
                    arg, k = _next_group_arg(nodes, j)
                else:
                    affil_indices = ()
                    arg, k = _next_group_arg(nodes, i)
                if arg is not None:
                    authors.append(
                        TexAuthor(name=arg, affil_indices=affil_indices)
                    )
                i = k + 1 if arg is not None else i + 1
                continue

            if name == "affil":
                optional, j = _next_optional_arg(nodes, i)
                if optional is None or not optional.strip().isdigit():
                    i = j + 1 if optional is not None else i + 1
                    continue
                arg, k = _next_group_arg(nodes, j)
                if arg is not None:
                    affils[int(optional.strip())] = arg
                i = k + 1 if arg is not None else i + 1
                continue

            if name == "rrxivid":
                arg, j = _next_group_arg(nodes, i)
                if arg is not None:
                    metadata["rrxiv_id"] = arg
                i = j + 1 if arg is not None else i + 1
                continue
            if name == "rrxivversion":
                arg, j = _next_group_arg(nodes, i)
                if arg is not None:
                    metadata["rrxiv_version"] = arg
                i = j + 1 if arg is not None else i + 1
                continue
            if name == "rrxivprotocolversion":
                arg, j = _next_group_arg(nodes, i)
                if arg is not None:
                    metadata["rrxiv_protocol_version"] = arg
                i = j + 1 if arg is not None else i + 1
                continue
            if name == "rrxivlicense":
                arg, j = _next_group_arg(nodes, i)
                if arg is not None:
                    metadata["rrxiv_license"] = arg
                i = j + 1 if arg is not None else i + 1
                continue
            if name == "rrxivtopics":
                arg, j = _next_group_arg(nodes, i)
                if arg is not None:
                    metadata["rrxiv_topics"] = tuple(
                        t.strip() for t in arg.split(",") if t.strip()
                    )
                i = j + 1 if arg is not None else i + 1
                continue

            if name in _SECTION_LEVELS:
                arg, j = _next_group_arg(nodes, i)
                if arg is None:
                    i += 1
                    continue
                # Look ahead a small window for a label.
                section_label: str | None = None
                k = j + 1
                window_end = min(len(nodes), k + 12)
                while k < window_end:
                    nk = nodes[k]
                    if isinstance(nk, _lw.LatexMacroNode) and nk.macroname == "label":
                        lbl, _ = _next_group_arg(nodes, k)
                        section_label = lbl
                        break
                    k += 1
                sections.append(
                    TexSection(
                        level=_SECTION_LEVELS[name],
                        title=arg,
                        label=section_label,
                        char_offset=getattr(n, "pos", 0) or 0,
                    )
                )
                i = j + 1
                continue

            if name == "bibliography":
                arg, j = _next_group_arg(nodes, i)
                if arg is not None:
                    bib_files.append(arg)
                i = j + 1 if arg is not None else i + 1
                continue

            i += 1
            continue

        i += 1


def parse_tex(tex_source: str) -> TexDocument:
    """Parse a LaTeX source string into a TexDocument."""
    walker = _lw.LatexWalker(tex_source, latex_context=_LATEX_CONTEXT)
    nodes, _, _ = walker.get_latex_nodes()

    authors: list[TexAuthor] = []
    affils: dict[int, str] = {}
    sections: list[TexSection] = []
    environments: list[TexEnvironment] = []
    bib_files: list[str] = []
    metadata: dict[str, Any] = {}
    title_box: list[str | None] = [None]
    abstract_box: list[str | None] = [None]

    _walk(
        nodes,
        authors=authors,
        affils=affils,
        sections=sections,
        environments=environments,
        bib_files=bib_files,
        metadata=metadata,
        title_box=title_box,
        abstract_box=abstract_box,
    )

    citations: list[TexCitation] = []
    _scan_cites_in_nodes(nodes, citations)

    metadata_obj = TexMetadata(
        rrxiv_id=metadata.get("rrxiv_id"),
        rrxiv_version=metadata.get("rrxiv_version"),
        rrxiv_protocol_version=metadata.get("rrxiv_protocol_version"),
        rrxiv_license=metadata.get("rrxiv_license"),
        rrxiv_topics=tuple(metadata.get("rrxiv_topics") or ()),
    )

    environments.sort(key=lambda e: e.char_offset)

    return TexDocument(
        title=title_box[0],
        authors=tuple(authors),
        affiliations=affils,
        abstract=abstract_box[0],
        metadata=metadata_obj,
        sections=tuple(sections),
        environments=tuple(environments),
        citations=tuple(citations),
        bibliography_files=tuple(bib_files),
    )


_INPUT_DIRECTIVE_RE = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")

# `\input{figures/<name>}` directives are *figure references*, not
# text-include directives — the parser's meaty-claim path picks them
# up by pattern-matching the literal `\input{}` line inside an
# evidence body. Inlining them would erase the reference and break
# Claim.figures extraction. Skip any path that starts with `figures/`
# (or `fig/`, the older convention from spec/0003).
_FIGURE_PATH_PREFIXES = ("figures/", "fig/")


def read_tex_resolving_inputs(
    path: Path | str,
    *,
    max_depth: int = 16,
) -> str:
    """Return the .tex source with ``\\input{...}`` / ``\\include{...}``
    directives recursively inlined.

    Multi-file papers (Euclid's ``main.tex`` → ``books/book01.tex`` …)
    used to silently drop their claim envs because the parser only saw
    ``main.tex`` and never followed ``\\input{}``. Resolving inline
    here keeps the rest of the parser (which scans environments on a
    flat source string) unchanged.

    Resolution rules:
      - ``\\input{path}`` and ``\\include{path}`` are both honoured.
      - Paths are resolved relative to the *importing* file's
        directory.
      - The file extension is optional; ``.tex`` is appended when the
        bare path doesn't exist.
      - Paths under ``figures/`` (or ``fig/``) are **left as
        directives** — they're handled by the meaty-claim figure
        path, which needs the literal ``\\input{figures/...}`` line
        to survive into the env body so it can be discovered.
      - Cyclic includes are broken at ``max_depth``; missing files are
        replaced with a comment marker so the parse doesn't blow up.
    """
    root_path = Path(path).resolve()

    def _read(p: Path, depth: int, seen: set[Path]) -> str:
        if depth > max_depth or p in seen:
            return f"% rrxiv-parser: skipped {p.name} (cycle or depth>{max_depth})\n"
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            return f"% rrxiv-parser: missing {p.name}\n"

        next_seen = seen | {p}

        def _replace(match: re.Match[str]) -> str:
            target = match.group(1).strip()
            # Figure references stay as directives — see module docstring.
            if any(target.startswith(prefix) for prefix in _FIGURE_PATH_PREFIXES):
                return match.group(0)
            # Strip a trailing .tex if the author wrote it; we add it
            # ourselves when missing.
            base = p.parent / target
            candidates = [base, base.with_suffix(".tex")]
            chosen = next((c for c in candidates if c.is_file()), None)
            if chosen is None:
                return f"% rrxiv-parser: missing \\input{{{target}}}\n"
            return _read(chosen, depth + 1, next_seen)

        return _INPUT_DIRECTIVE_RE.sub(_replace, text)

    return _read(root_path, 0, set())


def parse_tex_file(path: Path | str) -> TexDocument:
    """Read and parse a .tex file, recursively resolving ``\\input{}``.

    Sprint 26.K: multi-file LaTeX papers (Euclid's Elements, split
    across 13 book files via ``\\input{books/bookNN}``) used to lose
    every claim env because the parser only saw ``main.tex``. We now
    inline the included files first so the downstream environment
    scan sees the full document.
    """
    return parse_tex(read_tex_resolving_inputs(path))
