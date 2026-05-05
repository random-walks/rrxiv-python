"""Bibliography parsing for rrxiv.

Two paths into Citation records:

1. **External .bib file** referenced via ``\\bibliography{NAME}`` in the
   .tex source. Parsed by :func:`parse_bib` using ``bibtexparser`` v1.x.
2. **Inline ``thebibliography`` block** with ``\\bibitem[...]{key}``
   entries in the .tex source itself. Parsed by
   :func:`parse_thebibliography`. Used by papers (including the rrxiv
   whitepaper) that don't ship a separate .bib.

Both paths return :class:`BibEntry` records with the same shape; the
build module merges them when both are present.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import bibtexparser  # type: ignore[import-untyped]


@dataclass(frozen=True, slots=True)
class BibEntry:
    """One entry from a .bib file."""

    key: str
    entry_type: str  # @article, @misc, @inproceedings, ...
    fields: dict[str, str]  # author, title, year, doi, eprint, archivePrefix, url, ...
    raw: str  # the original BibTeX source for this entry

    @property
    def doi(self) -> str | None:
        return self.fields.get("doi") or None

    @property
    def arxiv_id(self) -> str | None:
        # arXiv conventions: archivePrefix=arXiv + eprint=<id>, OR
        # journal field with "arXiv:<id>" pattern.
        archive = self.fields.get("archivePrefix") or self.fields.get("archiveprefix")
        if archive and archive.lower() == "arxiv":
            eprint = self.fields.get("eprint")
            if eprint:
                return eprint
        # Fall back to scanning the URL field for an arxiv link
        url = self.fields.get("url") or ""
        if "arxiv.org/abs/" in url:
            return url.split("arxiv.org/abs/", 1)[1].rstrip("/").strip()
        return None

    @property
    def url(self) -> str | None:
        raw = self.fields.get("url") or self.fields.get("howpublished") or ""
        if not raw:
            return None
        # Strip a wrapping \url{...} macro if present (common in BibTeX
        # entries that use the url package).
        stripped = raw.strip()
        if stripped.startswith(r"\url{") and stripped.endswith("}"):
            stripped = stripped[len(r"\url{") : -1]
        # Sanity: must look like a URL with a scheme.
        if "://" not in stripped:
            return None
        return stripped


def parse_bib(text: str) -> list[BibEntry]:
    """Parse a .bib file string into BibEntry objects.

    Uses bibtexparser v1.x API (``loads`` -> BibDatabase with .entries
    as list[dict]).
    """
    if not text.strip():
        return []
    db = bibtexparser.loads(text)
    entries: list[BibEntry] = []
    for entry in db.entries:
        key = entry.get("ID", "")
        entry_type = entry.get("ENTRYTYPE", "").lower()
        fields_dict: dict[str, str] = {}
        for field_key, value in entry.items():
            if field_key in ("ID", "ENTRYTYPE"):
                continue
            if value is None:
                continue
            fields_dict[field_key.lower()] = str(value).strip()
        # Reconstruct a raw entry string by re-emitting (bibtexparser
        # doesn't preserve the original; for traceability we synthesise).
        body_lines = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields_dict.items())
        raw = f"@{entry_type}{{{key},\n{body_lines}\n}}"
        entries.append(
            BibEntry(
                key=key,
                entry_type=entry_type,
                fields=fields_dict,
                raw=raw,
            )
        )
    return entries


def parse_bib_file(path: Path | str) -> list[BibEntry]:
    """Read and parse a .bib file from disk."""
    return parse_bib(Path(path).read_text(encoding="utf-8"))


# ---- thebibliography fallback ----

_RE_THEBIB = re.compile(
    r"\\begin\{thebibliography\}(?:\{[^{}]*\})?(.*?)\\end\{thebibliography\}",
    re.DOTALL,
)
# Match \bibitem[label]{key} or \bibitem{key}; capture key and (optional) label.
# The body of one entry runs until the next \bibitem or the end of the block.
_RE_BIBITEM = re.compile(
    r"\\bibitem\s*(?:\[([^\]]*)\])?\s*\{([^{}]+)\}(.*?)(?=\\bibitem|\Z)",
    re.DOTALL,
)
_RE_DOI_IN_TEXT = re.compile(r"10\.[0-9]{4,9}/[-._;()/:a-zA-Z0-9]+")
_RE_ARXIV_IN_TEXT = re.compile(r"arXiv\s*:?\s*([0-9]{4}\.[0-9]{4,5})", re.IGNORECASE)
_RE_URL_IN_TEXT = re.compile(r"https?://[^\s,)\\]+")


def _strip_trailing_punct(s: str) -> str:
    """Strip trailing punctuation introduced by the surrounding sentence."""
    return s.rstrip(".,;:)]}")


def parse_thebibliography(tex_source: str) -> list[BibEntry]:
    """Extract \\bibitem entries from any \\begin{thebibliography} block.

    Each entry's body becomes the ``note`` field. Heuristics extract DOIs,
    arXiv IDs, and URLs from the body text into typed fields so the build
    module can populate :class:`rrxiv.models.Citation` accurately.
    """
    entries: list[BibEntry] = []
    for block_match in _RE_THEBIB.finditer(tex_source):
        body = block_match.group(1)
        for item in _RE_BIBITEM.finditer(body):
            label = (item.group(1) or "").strip()
            key = item.group(2).strip()
            text = item.group(3).strip()

            fields: dict[str, str] = {}
            if label:
                fields["label"] = label
            if text:
                fields["note"] = text
            doi_m = _RE_DOI_IN_TEXT.search(text)
            if doi_m:
                fields["doi"] = _strip_trailing_punct(doi_m.group(0))
            arxiv_m = _RE_ARXIV_IN_TEXT.search(text)
            if arxiv_m:
                fields["eprint"] = arxiv_m.group(1)
                fields["archiveprefix"] = "arXiv"
            url_m = _RE_URL_IN_TEXT.search(text)
            if url_m:
                fields["url"] = _strip_trailing_punct(url_m.group(0))

            # Reconstruct a synthesised BibTeX-shaped raw entry for the
            # bibtex_entry field of the resulting Citation.
            body_lines = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields.items())
            raw = f"@misc{{{key},\n{body_lines}\n}}"
            entries.append(
                BibEntry(
                    key=key,
                    entry_type="misc",
                    fields=fields,
                    raw=raw,
                )
            )
    return entries
