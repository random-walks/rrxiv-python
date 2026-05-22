"""Best-effort LaTeX-source-to-plain-text cleaner.

Used by the build module to strip cosmetic LaTeX macros from CIR fields
that consumers expect to be readable as plain text — title, abstract,
claim statements. We keep the v0.1 parser regex-based across the board;
upgrading to pylatexenc is a v0.2 follow-up that will subsume this
module too.

Goal: strip cosmetic macros (``\\Large``, ``\\textit{...}``, ``\\\\``)
without losing the semantic content. Math mode is preserved as-is for
downstream renderers; this module does not attempt MathML or text-only
conversion of formulas.
"""

from __future__ import annotations

import re

# Style macros that take ONE argument and should be replaced by their
# argument. Order matters only for nested cases (we apply iteratively).
_STYLE_MACROS_1ARG: tuple[str, ...] = (
    "texttt",
    "textit",
    "textbf",
    "textsf",
    "textsc",
    "textmd",
    "textup",
    "textnormal",
    "emph",
    "underline",
    "sout",
    "uline",
    "uuline",
    "uwave",
    "mbox",
    "hbox",
)

# Macros that take ONE argument and should be DROPPED entirely along
# with their argument. These are footnote-style annotations that don't
# belong in the plain-text representation — e.g. ``\thanks{<email>}``
# appended to an author name, ``\footnote{<aside>}`` inside an abstract.
# Without this stripping, ``\author{Alice\thanks{alice@x.org}}`` would
# canonicalise as ``Alice\thanks{alice@x.org}`` and create duplicate
# author entries on read paths (one with the thanks, one without).
_DROP_MACROS_1ARG: tuple[str, ...] = (
    "thanks",
    "footnote",
    "footnotemark",
    "footnotetext",
    "marginpar",
    "marginnote",
)

# Bare font-size macros that take NO argument and should be dropped.
_BARE_FONT_MACROS: tuple[str, ...] = (
    "Huge",
    "huge",
    "LARGE",
    "Large",
    "large",
    "normalsize",
    "small",
    "footnotesize",
    "scriptsize",
    "tiny",
    "bfseries",
    "itshape",
    "ttfamily",
    "sffamily",
    "rmfamily",
    "upshape",
    "slshape",
)

# Backslash-escaped special characters: \& \% \$ \# \_ \{ \} \~  \^ \\
# Map to the plain character.
_ESCAPED_SPECIALS: dict[str, str] = {
    r"\&": "&",
    r"\%": "%",
    r"\$": "$",
    r"\#": "#",
    r"\_": "_",
    r"\{": "{",
    r"\}": "}",
    r"\~": "~",
    r"\^": "^",
}


def _strip_one_arg_macros(text: str) -> str:
    """Replace ``\\macro{X}`` with ``X`` for each style macro.

    Iterative because nested cases (``\\textbf{\\textit{X}}``) need
    multiple passes. We bound iteration to avoid pathological inputs.
    """
    macro_alt = "|".join(re.escape(m) for m in _STYLE_MACROS_1ARG)
    # Match \macro{ ... } where the content has no { or } itself.
    # Nested braces are handled by repeated passes.
    pattern = re.compile(rf"\\(?:{macro_alt})\s*\{{([^{{}}]*)\}}")

    for _ in range(8):  # bound to protect against pathological input
        new_text, n = pattern.subn(r"\1", text)
        if n == 0:
            return new_text
        text = new_text
    return text


def _drop_one_arg_macros(text: str) -> str:
    """Drop ``\\macro{X}`` entirely (macro + braces + content).

    Applied to footnote-style macros (``\\thanks``, ``\\footnote``)
    that don't belong in the plain-text representation. The argument
    body itself often contains nested LaTeX (``\\texttt{...}``) so we
    iterate to handle nesting.
    """
    macro_alt = "|".join(re.escape(m) for m in _DROP_MACROS_1ARG)
    pattern = re.compile(rf"\\(?:{macro_alt})\s*\{{[^{{}}]*\}}")

    for _ in range(8):
        new_text, n = pattern.subn("", text)
        if n == 0:
            return new_text
        text = new_text
    return text


def _strip_bare_font_macros(text: str) -> str:
    """Drop ``\\Large``, ``\\bfseries``, etc. when they appear bare
    (no following ``{...}``). We do NOT drop them when they are
    arguments to ``\\textbf{}``-style macros — that case is already
    handled by ``_strip_one_arg_macros``.
    """
    macro_alt = "|".join(re.escape(m) for m in _BARE_FONT_MACROS)
    # Match \macro followed by a non-letter (so we don't catch \Largest).
    return re.sub(rf"\\(?:{macro_alt})\b\s*", "", text)


def _strip_special_macros(text: str) -> str:
    # \href{url}{text} -> text
    text = re.sub(r"\\href\s*\{[^{}]*\}\s*\{([^{}]*)\}", r"\1", text)
    # \url{x} -> x (only when not inside a code/math environment; we don't
    # try to detect that here in v0.1)
    text = re.sub(r"\\url\s*\{([^{}]*)\}", r"\1", text)
    # \cite{key,...} -> drop entirely (the citation will be in citations[])
    text = re.sub(r"\\cite\s*(?:\[[^\]]*\])?\s*\{[^{}]*\}", "", text)
    # \ref{x}, \pageref{x}, \label{x} -> drop (referenced via section_id etc.)
    text = re.sub(r"\\(?:ref|pageref|label)\s*\{[^{}]*\}", "", text)
    return text


# Edge-declaration macros from rrxiv.cls. Their info already lives on the
# Claim's depends_on / supports / contradicts / extends edges (extracted
# from the sidecar), so re-emitting them in a rendered proof body would
# be redundant noise. Stripping them is a tex_to_proof_text-specific
# step (we keep them in the generic tex_to_text path because abstracts
# and titles do not contain edge macros in practice and we don't want to
# expand tex_to_text's responsibility).
_EDGE_MACRO_RE = re.compile(
    r"\\(?:dependson|supports|contradicts|extends)\s*\{[^{}]*\}\s*\{[^{}]*\}"
)
# \input{...} / \include{...} references — the parser captures these
# separately into the Claim's `figures` array, so they're stripped from
# the rendered proof text.
_INPUT_MACRO_RE = re.compile(r"\\(?:input|include)\s*\{[^{}]*\}")


def _strip_line_breaks(text: str) -> str:
    # \\[Xpt] or \\ at end of line -> single space
    text = re.sub(r"\\\\\s*(?:\[[^\]]*\])?", " ", text)
    return text


def _replace_escaped_specials(text: str) -> str:
    for esc, plain in _ESCAPED_SPECIALS.items():
        text = text.replace(esc, plain)
    return text


def _normalize_whitespace(text: str) -> str:
    # Tilde ~ in TeX is a non-breaking space.
    text = text.replace("~", " ")
    # Collapse runs of whitespace
    text = re.sub(r"[ \t]+", " ", text)
    # Collapse blank-line runs to one blank line
    text = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", text)
    return text.strip()


def _convert_dashes(text: str) -> str:
    """Convert LaTeX dash conventions to Unicode equivalents.

    LaTeX source uses ``--`` for an en-dash (U+2013) and ``---`` for an
    em-dash (U+2014). Plain readers (HTML, Markdown, terminal) don't
    perform this rewrite automatically, so titles like
    ``Human--Agent Coproduction`` render with two literal hyphens.

    Convert ``---`` first (more specific) so we don't eat its inner
    ``--`` first.
    """
    text = text.replace("---", "—")
    text = text.replace("--", "–")
    return text


def tex_to_text(text: str) -> str:
    """Convert a LaTeX source fragment to plain text suitable for the CIR.

    Best-effort. Preserves math mode (``$...$``, ``\\[...\\]``) verbatim
    so downstream renderers can decide what to do with formulas. Strips
    cosmetic macros (style, font-size), resolves common compound macros
    (``\\href``, ``\\url``), and normalizes whitespace.

    Caller can roundtrip-test via:

    >>> assert tex_to_text(r"\\\\textit{Hello}") == r"\\Hello".replace("\\\\", "")  # doctest: +SKIP
    """
    text = _replace_escaped_specials(text)
    # Alternate drop + strip until both stabilise. Required for nested
    # cases like ``\thanks{\texttt{x}}`` where the inner ``\texttt`` has
    # braces of its own — strip resolves the inner argument first, then
    # the outer ``\thanks{x}`` is a flat-brace match for drop.
    for _ in range(8):
        prev = text
        text = _drop_one_arg_macros(text)
        text = _strip_one_arg_macros(text)
        if text == prev:
            break
    text = _strip_bare_font_macros(text)
    text = _strip_special_macros(text)
    text = _strip_line_breaks(text)
    text = _convert_dashes(text)
    text = _normalize_whitespace(text)
    return text


def _strip_line_comments(text: str) -> str:
    """Drop LaTeX line comments (``%`` to end of line).

    A literal ``%`` is escaped in LaTeX as ``\\%``; preserve those. We
    match unescaped ``%`` from a non-backslash boundary (or beginning of
    line) to the next newline.

    Pylatexenc's ``latex_verbatim`` preserves comments as part of an
    environment's body, including comments inserted by the
    ``flatten-tex.py`` pre-pass (``% [flatten-tex.py] inlined: …``).
    Stripping them here keeps them out of ``Claim.proof``.
    """
    return re.sub(r"(^|[^\\])%[^\n]*", lambda m: m.group(1), text, flags=re.MULTILINE)


def tex_to_proof_text(text: str) -> str:
    """Convert an ``\\begin{evidence}...\\end{evidence}`` body to plain
    text suitable for ``Claim.proof``.

    Same contract as :func:`tex_to_text` — preserves ``$...$`` math
    markers, strips cosmetic macros, normalises whitespace — with three
    extra strips that are evidence-block-specific:

    - LaTeX line comments (``%...\\n``) are dropped. The parser's TeX
      walker preserves them in the captured body via ``latex_verbatim``,
      but they're never content the reader wants to see.
    - ``\\dependson{}{}`` and friends (``\\supports``, ``\\contradicts``,
      ``\\extends``) are dropped. Their information already rides on the
      Claim's edge arrays; re-emitting them in the rendered proof would
      be noise.
    - ``\\input{...}`` / ``\\include{...}`` references are dropped. The
      parser captures figure inputs into the Claim's ``figures`` array
      separately; the proof text reads better without bare ``\\input``
      calls (they don't render to anything in a plain-text view).
    """
    text = _strip_line_comments(text)
    text = _EDGE_MACRO_RE.sub("", text)
    text = _INPUT_MACRO_RE.sub("", text)
    return tex_to_text(text)
