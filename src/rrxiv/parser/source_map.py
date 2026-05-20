"""Map flattened-TeX byte offsets back to original source files.

When a paper uses ``\\input{...}`` to split itself across multiple .tex
files (e.g. Euclid's Elements, with one file per Book), the parser
pipeline first flattens everything into a single .tex via the workspace
helper ``scripts/flatten-tex.py``. flatten-tex emits marker comments
around each inlined region::

    % [flatten-tex.py] inlined: books/book01.tex
    ... contents of book01.tex ...
    % [flatten-tex.py] end: books/book01.tex

This module reads a flattened source, indexes the markers, and exposes a
``SourceMap`` that converts a flat byte offset back to ``(file, line)``
in the original source — what the parser needs to populate
``Claim.source_location.{file, line_start, line_end}``.

If the input contains no marker comments (single-file papers, or papers
that were never flattened), the map is degenerate and reports the input
filename for every offset, with the line number computed against the
input itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_MARK_START_RE = re.compile(r"^% \[flatten-tex\.py\] inlined: (.+?)\s*$")
_MARK_END_RE = re.compile(r"^% \[flatten-tex\.py\] end: (.+?)\s*$")
# flatten-tex.py inserts a `MISSING: <path>` comment line BEFORE any
# \input{<path>} it couldn't resolve from disk. The original-source had
# no equivalent line, so the SourceMap must skip these lines when
# computing orig_line counts (otherwise everything after a MISSING line
# is shifted by +1 per insertion).
_MARK_MISSING_RE = re.compile(r"^\s*% \[flatten-tex\.py\] MISSING:")
# Likewise the cycle marker.
_MARK_CYCLE_RE = re.compile(r"^\s*% \[flatten-tex\.py\] cycle skipped:")


@dataclass(frozen=True, slots=True)
class _Region:
    """One contiguous run of original-file lines inside the flattened source.

    ``flat_start_line`` and ``flat_end_line`` are 1-indexed line numbers
    in the flattened source. ``orig_start_line`` is the 1-indexed line in
    ``file`` that corresponds to ``flat_start_line + 1`` (the marker
    line itself sits at ``flat_start_line``; original content starts on
    the next line).
    """

    file: str
    flat_start_line: int  # line of `% inlined: <file>` marker (or 1 for unmarked head)
    flat_end_line: int  # line of `% end: <file>` marker (or last line for unmarked tail)
    orig_start_line: int  # 1-indexed line in the original file where this region's content begins


class SourceMap:
    """Index of flattened-source line ranges back to original source files.

    Construct via :meth:`from_flat_text` (or :meth:`from_flat_file`).
    Query via :meth:`locate(offset)` which returns ``(file, line)``.
    """

    def __init__(
        self,
        *,
        flat_text: str,
        default_file: str,
        regions: list[_Region],
        line_starts: list[int],
        synthetic_lines: list[int],
    ) -> None:
        self._text = flat_text
        self._default_file = default_file
        self._regions = regions  # sorted by flat_start_line; non-overlapping
        # Byte offset of the start of each 1-indexed line in flat_text.
        # ``_line_starts[k]`` is the offset of line k+1.
        self._line_starts = line_starts
        # 1-indexed flat lines that flatten-tex injected and have no
        # corresponding line in the original source (MISSING / cycle
        # markers). The mapping subtracts these when computing orig_line.
        self._synthetic_lines = sorted(synthetic_lines)

    @classmethod
    def from_flat_text(cls, flat_text: str, *, default_file: str) -> SourceMap:
        """Build a SourceMap from the flattened source text.

        ``default_file`` is the name reported for regions that are not
        inside any flatten-tex marker block (e.g. the parts of
        ``main.tex`` that flatten-tex didn't inline — front matter and
        the document trailer).
        """
        # Precompute line-start offsets so locate() is O(log n).
        line_starts = [0]
        for i, ch in enumerate(flat_text):
            if ch == "\n":
                line_starts.append(i + 1)
        # line_starts[k] = offset of (1-indexed) line k+1 (length is
        # total_lines, but the trailing empty "line" after the final \n
        # is fine — locate() bounds-checks).

        regions: list[_Region] = []
        # synthetic_lines tracks lines that flatten-tex inserted with no
        # original-source equivalent (MISSING / cycle markers, and nested
        # inlined/end markers when they appear inside another inlining
        # region). The outer-most inlining marker on its own is not
        # synthetic for the formula below (it's the anchor; the `-1` in
        # the orig_line computation accounts for it).
        synthetic_lines: list[int] = []
        # Stack-based scan: when an `inlined` marker opens a region, push
        # the file + flat line; when the matching `end` closes it, pop
        # and emit a Region. Inlined regions can nest (book that itself
        # \inputs a chapter), and flatten-tex emits markers for each
        # level — we model that by recording the most recent open region
        # as the "active" one for any offset inside it.
        stack: list[tuple[str, int, int]] = []  # (file, flat_open_line, orig_line_at_open)
        lines = flat_text.splitlines()
        for idx, line in enumerate(lines, start=1):
            m_start = _MARK_START_RE.match(line)
            if m_start:
                # A nested inlining marker counts as synthetic for the
                # outer file; the inner file's own anchor still uses
                # the `-1` adjustment, so we don't double-count from
                # within the inner region's POV.
                if stack:
                    synthetic_lines.append(idx)
                stack.append((m_start.group(1), idx, 1))
                continue
            m_end = _MARK_END_RE.match(line)
            if m_end:
                # `end` markers are always synthetic to the enclosing
                # region — they have no original-source equivalent.
                synthetic_lines.append(idx)
                if stack:
                    file_open, flat_open, orig_open = stack[-1]
                    if file_open == m_end.group(1):
                        regions.append(
                            _Region(
                                file=file_open,
                                flat_start_line=flat_open,
                                flat_end_line=idx,
                                orig_start_line=orig_open,
                            )
                        )
                        stack.pop()
                # Unmatched end markers are ignored. They shouldn't
                # occur if the input came out of flatten-tex.
                continue
            if _MARK_MISSING_RE.match(line) or _MARK_CYCLE_RE.match(line):
                synthetic_lines.append(idx)
                continue

        # Stack non-empty at EOF: orphaned `inlined` with no closing
        # `end`. Emit a region spanning to EOF so we can still locate
        # offsets inside it.
        while stack:
            file_open, flat_open, orig_open = stack.pop()
            regions.append(
                _Region(
                    file=file_open,
                    flat_start_line=flat_open,
                    flat_end_line=len(lines),
                    orig_start_line=orig_open,
                )
            )

        # Sort by flat_start_line so locate() can bisect / linear-scan
        # in document order.
        regions.sort(key=lambda r: r.flat_start_line)

        return cls(
            flat_text=flat_text,
            default_file=default_file,
            regions=regions,
            line_starts=line_starts,
            synthetic_lines=synthetic_lines,
        )

    @classmethod
    def from_flat_file(cls, path: Path | str) -> SourceMap:
        """Build a SourceMap by reading the flattened source from disk.

        ``default_file`` is the input filename (basename), matching the
        convention used by single-file paper sources.
        """
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        return cls.from_flat_text(text, default_file=path.name)

    def _offset_to_flat_line(self, offset: int) -> int:
        """1-indexed flat-source line for a byte offset."""
        if offset <= 0:
            return 1
        if offset >= len(self._text):
            return max(1, len(self._line_starts))
        # Binary search would be faster but linear is fine for v0.1
        # corpora; flat_text is typically <500 KB.
        lo, hi = 0, len(self._line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    def _synthetic_between(self, lo: int, hi: int) -> int:
        """Count synthetic flat lines (markers / MISSING / cycle) in the
        range ``(lo, hi]`` — i.e. strictly after ``lo``, up to and
        including ``hi``. Used to discount inserted lines when mapping
        flat → original line numbers.
        """
        if hi <= lo or not self._synthetic_lines:
            return 0
        count = 0
        for ln in self._synthetic_lines:
            if lo < ln <= hi:
                count += 1
            elif ln > hi:
                break
        return count

    def locate(self, offset: int) -> tuple[str, int]:
        """Return ``(file, line)`` for a flat-source byte offset.

        ``file`` is the original source filename (relative to the source
        archive root); ``line`` is the 1-indexed line number inside that
        file. For offsets outside any flatten-tex marker block, ``file``
        is the ``default_file`` passed at construction, with ``line``
        computed against the flat source itself.
        """
        flat_line = self._offset_to_flat_line(offset)
        # Find the innermost region containing this line. Regions are
        # sorted by flat_start_line; pick the latest region whose start
        # <= flat_line and whose end >= flat_line.
        match: _Region | None = None
        for r in self._regions:
            if r.flat_start_line < flat_line <= r.flat_end_line:
                # Strictly inside (i.e., past the inlined-marker line).
                if match is None or r.flat_start_line > match.flat_start_line:
                    match = r
        if match is None:
            return self._default_file, flat_line
        # Lines inside the region are 1-indexed against the *original*
        # file. flatten-tex emits the inlined marker on its own line,
        # then the original-file content starting on the next line.
        # So in the absence of MISSING/cycle markers,
        # flat_line == match.flat_start_line + k means original
        # line == match.orig_start_line + k - 1.
        # MISSING/cycle/nested-end markers are synthetic (no source
        # equivalent) and must be additionally subtracted so the
        # original-file line lines up.
        synthetic_inside = self._synthetic_between(match.flat_start_line, flat_line)
        adjusted_delta = (flat_line - match.flat_start_line - 1) - synthetic_inside
        orig_line = match.orig_start_line + adjusted_delta
        return match.file, max(1, orig_line)
