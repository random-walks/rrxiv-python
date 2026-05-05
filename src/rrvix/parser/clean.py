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
    text = _strip_one_arg_macros(text)
    text = _strip_bare_font_macros(text)
    text = _strip_special_macros(text)
    text = _strip_line_breaks(text)
    text = _normalize_whitespace(text)
    return text
