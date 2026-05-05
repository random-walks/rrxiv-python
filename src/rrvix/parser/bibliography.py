"""Bibliography (.bib) parser, used to populate Citation objects.

We use ``bibtexparser`` (a dev dep) for the heavy lifting. This module
provides the thin layer that turns ``bibtexparser`` records into
structured data the build module can hand to the Citation schema.
"""

from __future__ import annotations

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
