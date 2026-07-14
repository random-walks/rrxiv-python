"""Tests for the LaTeX-to-text cleaner."""

from __future__ import annotations

from rrxiv.parser.clean import _EDGE_MACRO_RE, tex_to_text


class TestStyleMacros:
    def test_texttt(self) -> None:
        assert tex_to_text(r"foo \texttt{bar} baz") == "foo bar baz"

    def test_textit(self) -> None:
        assert tex_to_text(r"\textit{italic}") == "italic"

    def test_textbf(self) -> None:
        assert tex_to_text(r"\textbf{bold}") == "bold"

    def test_emph(self) -> None:
        assert tex_to_text(r"this is \emph{emphasis}") == "this is emphasis"

    def test_nested_styles(self) -> None:
        assert tex_to_text(r"\textbf{very \textit{important}}") == "very important"

    def test_unknown_macro_left_alone(self) -> None:
        # \mathbb is mathy, we don't know about it; leave it
        out = tex_to_text(r"\mathbb{R}")
        assert "mathbb" in out


class TestBareFonts:
    def test_large_dropped(self) -> None:
        # The bare-font macro and its trailing whitespace are dropped.
        assert tex_to_text(r"{\Large rrxiv}") == "{rrxiv}"

    def test_huge_dropped(self) -> None:
        out = tex_to_text(r"\Huge title text")
        assert out == "title text"

    def test_bfseries_dropped(self) -> None:
        assert tex_to_text(r"\bfseries hello") == "hello"

    def test_largest_not_matched(self) -> None:
        """`\\Largest` is not in our macro list; should pass through."""
        out = tex_to_text(r"\Largest text")
        assert "Largest" in out


class TestEscapedSpecials:
    def test_amp(self) -> None:
        assert tex_to_text(r"AT\&T") == "AT&T"

    def test_pct(self) -> None:
        assert tex_to_text(r"50\%") == "50%"

    def test_hash(self) -> None:
        assert tex_to_text(r"\#hashtag") == "#hashtag"

    def test_underscore(self) -> None:
        assert tex_to_text(r"foo\_bar") == "foo_bar"


class TestSpecialMacros:
    def test_href(self) -> None:
        assert tex_to_text(r"see \href{https://x.org}{the link}") == (
            "see the link"
        )

    def test_url(self) -> None:
        assert tex_to_text(r"\url{https://x.org}") == "https://x.org"

    def test_cite_dropped(self) -> None:
        out = tex_to_text(r"as shown by~\cite{tao2024}")
        assert "tao2024" not in out
        assert "as shown by" in out

    def test_label_ref_dropped(self) -> None:
        out = tex_to_text(r"see Section~\ref{sec:intro} (\label{sec:foo})")
        assert "sec:intro" not in out
        assert "sec:foo" not in out
        assert "see Section" in out


class TestLineBreaks:
    def test_double_backslash(self) -> None:
        assert tex_to_text(r"line one \\ line two") == "line one line two"

    def test_double_backslash_with_spacing(self) -> None:
        assert tex_to_text(r"a \\[0.2em] b") == "a b"


class TestWhitespace:
    def test_tilde_becomes_space(self) -> None:
        assert tex_to_text(r"a~b") == "a b"

    def test_collapse_spaces(self) -> None:
        assert tex_to_text("a   b\t\tc") == "a b c"

    def test_strip(self) -> None:
        assert tex_to_text("  hello  \n\n") == "hello"


class TestDashes:
    """LaTeX dash conventions → Unicode."""

    def test_en_dash(self) -> None:
        # `--` → en-dash (U+2013)
        assert tex_to_text("Human--Agent") == "Human–Agent"

    def test_em_dash(self) -> None:
        # `---` → em-dash (U+2014); converted before `--` so we don't eat the inner pair
        assert tex_to_text("foo---bar") == "foo—bar"

    def test_single_hyphen_preserved(self) -> None:
        assert tex_to_text("first-class") == "first-class"

    def test_dashes_in_real_title(self) -> None:
        # Regression: the whitepaper title carried `Human--Agent`
        # which round-tripped as two literal hyphens before this fix.
        out = tex_to_text(
            "rrxiv: An Open Protocol for Research Preprints in the "
            "Era of Human--Agent Coproduction"
        )
        assert "Human–Agent" in out
        assert "Human--Agent" not in out


class TestDropMacros:
    """``\\thanks{...}`` and friends should be dropped entirely.

    Regression coverage for the author-name dedup bug: the Euclid CIR
    declared ``\\author{Blaise Albis-Burdige\\thanks{albisburdige@protonmail.com}}``
    which round-tripped through the parser with the ``\\thanks`` still
    attached, causing the same author to appear twice on read paths.
    """

    def test_thanks_dropped(self) -> None:
        out = tex_to_text(r"Alice\thanks{alice@example.com}")
        assert out == "Alice"

    def test_thanks_with_inner_latex(self) -> None:
        out = tex_to_text(
            r"Blaise Albis-Burdige\thanks{\texttt{albisburdige@protonmail.com}}"
        )
        # \texttt{} inside \thanks{} doesn't matter — the whole \thanks block
        # is dropped before we recurse into style macros.
        assert out == "Blaise Albis-Burdige"

    def test_footnote_dropped(self) -> None:
        out = tex_to_text(r"see\footnote{the appendix} the proof")
        assert out == "see the proof"

    def test_marginpar_dropped(self) -> None:
        out = tex_to_text(r"text\marginpar{aside} continues")
        assert out == "text continues"

    def test_thanks_doesnt_break_other_text(self) -> None:
        # Sanity: text with no \thanks is unaffected.
        out = tex_to_text(r"Plain author name")
        assert out == "Plain author name"


class TestEdgeMacros:
    """Edge-declaration macros (\\dependson / \\supports / \\contradicts /
    \\extendsclaim) must be stripped from every CIR prose field.

    Their graph structure already rides on the Claim's edge arrays
    (extracted from the sidecar), so an author inlining one inside a
    claim environment body must not leak the raw macro into the
    statement. Regression coverage for two bugs:

    - ``_EDGE_MACRO_RE`` matched a bare ``\\extends`` that no cls macro
      emits; the real macro is ``\\extendsclaim`` (two args), so
      ``\\extendsclaim{a}{b}`` was silently never stripped.
    - Only the proof path (``tex_to_proof_text``) stripped edge macros;
      the statement path (``tex_to_text``) did not, so inlined edge
      macros leaked into ``claim.statement``.
    """

    def test_edge_macro_re_strips_extendsclaim(self) -> None:
        # The exact macro rrxiv.cls emits — previously survived because
        # the pattern only knew about a (non-existent) bare `\extends`.
        assert _EDGE_MACRO_RE.sub("", r"\extendsclaim{a}{b}") == ""

    def test_edge_macro_re_strips_all_four_kinds(self) -> None:
        for macro in (
            r"\dependson{a}{b}",
            r"\supports{a}{b}",
            r"\contradicts{a}{b}",
            r"\extendsclaim{a}{b}",
        ):
            assert _EDGE_MACRO_RE.sub("", macro) == "", macro

    def test_tex_to_text_strips_edge_macros_from_statement(self) -> None:
        out = tex_to_text(
            r"A claim graph is queryable. "
            r"\dependson{prop:main}{x} \supports{prop:main}{y} "
            r"\extendsclaim{prop:main}{z}"
        )
        assert out == "A claim graph is queryable."
        for token in ("dependson", "supports", "extendsclaim", "contradicts"):
            assert token not in out


class TestPreservation:
    def test_inline_math_preserved(self) -> None:
        out = tex_to_text(r"the function $f(x) = x^2$ is convex")
        assert "$f(x) = x^2$" in out

    def test_display_math_left_alone(self) -> None:
        # \[ ... \] is not in our strip list
        out = tex_to_text(r"thus \[ x = 1 \] holds")
        assert r"\[ x = 1 \]" in out

    def test_normal_prose_unchanged(self) -> None:
        prose = "This is a normal sentence with no LaTeX in it."
        assert tex_to_text(prose) == prose


class TestRealWorldFragments:
    def test_whitepaper_title(self) -> None:
        # The whitepaper title uses \Large + \\[0.2em] + \large
        # plus `--` (LaTeX en-dash → U+2013).
        title_tex = (
            r"\Large rrxiv: An Open Protocol for Research Preprints \\[0.2em] "
            r"\large in the Era of Human--Agent Coproduction"
        )
        out = tex_to_text(title_tex)
        assert out == (
            "rrxiv: An Open Protocol for Research Preprints "
            "in the Era of Human–Agent Coproduction"
        )

    def test_claim_with_texttt(self) -> None:
        claim = (
            r"A minimal rrxiv paper, when compiled by \texttt{rrxiv.cls} v0.1, "
            r"emits a sidecar \texttt{*.rrxiv.aux} file from which the rrxiv "
            r"parser can produce a CIR object that validates against "
            r"\texttt{cir.schema.json}~\cite{rrxiv-cir-schema} v0.1.0."
        )
        out = tex_to_text(claim)
        assert "rrxiv.cls" in out
        assert r"\texttt" not in out
        assert "rrxiv-cir-schema" not in out  # cite stripped
        assert "validates against cir.schema.json" in out
