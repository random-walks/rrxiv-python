"""Combine sidecar + TeX + bibliography into a CIR.

This is the entry point used by the CLI (``rrvix parse <file>``).

The function ``build_cir(tex_path)`` does the following:

1. Read the LaTeX source and walk it (``parser.tex.parse_tex_file``).
2. Locate and read the ``*.rrvix.aux`` sidecar (``parser.sidecar.parse_sidecar_file``).
3. If the .tex declares a bibliography, read the corresponding .bib file
   and parse it (``parser.bibliography.parse_bib_file``).
4. Pair each LaTeX semantic environment with its sidecar marker by
   ordinal position (the v0.1 sidecar emits markers in document order).
5. Build a CIR object via the pydantic models, which validates against
   ``cir.schema.json`` on construction.

The result is a fully-validated ``rrvix.models.CIR`` instance.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rrvix.models import (
    CIR,
)
from rrvix.parser.bibliography import BibEntry, parse_bib_file, parse_thebibliography
from rrvix.parser.clean import tex_to_text
from rrvix.parser.sidecar import (
    EdgeMarker,
    EnvMarker,
    Sidecar,
    parse_sidecar_file,
)
from rrvix.parser.tex import (
    TexDocument,
    TexEnvironment,
    parse_tex_file,
    tex_env_to_sidecar_kind,
)


def _claim_id(paper_id: str, env: TexEnvironment, fallback_index: str) -> str:
    """Build the canonical claim ID for an environment.

    Uses the user-provided ``\\label{...}`` if present, falls back to
    ``<paper_id>:<env_name>:<sidecar_index>``.
    """
    if env.label:
        return f"{paper_id}:{env.label}"
    return f"{paper_id}:{env.name}:{fallback_index}"


def _pair_envs_with_sidecar(
    tex_envs: tuple[TexEnvironment, ...],
    sidecar_envs: tuple[EnvMarker, ...],
) -> dict[TexEnvironment, EnvMarker]:
    """Pair TeX environment blocks with sidecar markers.

    The v0.1 sidecar emits one marker per ``\\begin{...}`` of a tracked
    environment, in document order. Within a kind (e.g. ``claim``), the
    Nth TeX block pairs with the Nth sidecar marker of that kind.
    """
    by_kind_tex: dict[str, list[TexEnvironment]] = defaultdict(list)
    by_kind_side: dict[str, list[EnvMarker]] = defaultdict(list)
    for env in tex_envs:
        by_kind_tex[tex_env_to_sidecar_kind(env.name)].append(env)
    for marker in sidecar_envs:
        by_kind_side[marker.kind].append(marker)

    pairs: dict[TexEnvironment, EnvMarker] = {}
    for kind, envs in by_kind_tex.items():
        markers = by_kind_side.get(kind, [])
        for i, env in enumerate(envs):
            if i < len(markers):
                pairs[env] = markers[i]
    return pairs


def _claim_ids_from_edge(
    edge: EdgeMarker, paper_id: str
) -> tuple[str, str]:
    """Heuristic mapping from a sidecar edge's source/target strings to
    canonical claim IDs.

    Edges in the sidecar reference user-written IDs from ``\\dependson``
    etc., which already follow the ``<paper_id>:<label>`` convention.
    We pass them through unchanged. Cross-paper edges (different
    paper_id prefix) work too because the convention is the same.
    """
    return edge.source, edge.target


def _build_claims(
    tex: TexDocument,
    sidecar: Sidecar,
    paper_id: str,
) -> list[dict[str, Any]]:
    pairs = _pair_envs_with_sidecar(tex.environments, sidecar.envs)

    # Group edges by source for fast lookup
    edges_from: dict[str, list[EdgeMarker]] = defaultdict(list)
    for edge in sidecar.edges:
        edges_from[edge.source].append(edge)

    claims: list[dict[str, Any]] = []
    claim_envs = [e for e in tex.environments if e.name == "claim"]
    for env in claim_envs:
        marker = pairs.get(env)
        index = marker.index if marker else f"{len(claims) + 1}"
        cid = _claim_id(paper_id, env, index)

        # Pull edges that reference this claim ID as source
        depends_on: list[str] = []
        supports: list[str] = []
        contradicts: list[str] = []
        extends: list[str] = []
        for edge in edges_from.get(cid, []):
            if edge.edge_type == "depends_on":
                depends_on.append(edge.target)
            elif edge.edge_type == "supports":
                supports.append(edge.target)
            elif edge.edge_type == "contradicts":
                contradicts.append(edge.target)
            elif edge.edge_type == "extends":
                extends.append(edge.target)

        statement = env.body.strip()
        # Strip out the \label{...} call so the statement is just the body.
        if env.label:
            statement = statement.replace(f"\\label{{{env.label}}}", "").strip()
        # Run through the TeX-to-text cleaner so the CIR carries plain
        # prose rather than raw cosmetic macros (\texttt, \emph, etc.).
        statement = tex_to_text(statement)

        claim: dict[str, Any] = {
            "id": cid,
            "paper_id": paper_id,
            "statement": statement,
            "claim_type": "theoretical",  # v0.1 default; spec/0003 will refine
            "evidence_type": "argument",  # v0.1 default
            "extracted_by": "author",
            "canonical": True,
        }
        if depends_on:
            claim["depends_on"] = depends_on
        if supports:
            claim["supports"] = supports
        if contradicts:
            claim["contradicts"] = contradicts
        if extends:
            claim["extends"] = extends
        claims.append(claim)
    return claims


def _build_citations(
    tex: TexDocument,
    bib_entries: list[BibEntry],
    paper_id: str,
) -> list[dict[str, Any]]:
    """Emit Citation records.

    Two sources are merged:

    - Keys explicitly cited via ``\\cite{...}`` in the body. These get
      Citation records whether or not a .bib entry resolves them — a
      dangling cite is preserved so the reference isn't lost.
    - Keys that appear in any bibliography entry (.bib OR
      ``\\bibitem`` inside a ``thebibliography`` block) but aren't
      explicitly cited. These are emitted too: the bibliography listing
      counts as an implicit citation.
    """
    by_key = {e.key: e for e in bib_entries}
    cited_keys: set[str] = set()
    for cite in tex.citations:
        cited_keys.update(cite.keys)

    # Union: explicit cites + everything in the bibliography.
    all_keys = cited_keys | set(by_key.keys())

    citations: list[dict[str, Any]] = []
    for key in sorted(all_keys):
        entry = by_key.get(key)
        if entry is None:
            # \cite key with no resolving entry — emit a minimal citation
            # so we don't lose the reference.
            citations.append(
                {
                    "id": f"cite-{paper_id}:{key}",
                    "key": key,
                    "bibtex_entry": f"% no .bib entry for {key}",
                }
            )
            continue
        cit: dict[str, Any] = {
            "id": f"cite-{paper_id}:{key}",
            "key": key,
            "bibtex_entry": entry.raw,
        }
        if entry.doi:
            cit["target_doi"] = entry.doi
        if entry.arxiv_id:
            cit["target_arxiv_id"] = entry.arxiv_id
        if entry.url:
            cit["target_url"] = entry.url
        citations.append(cit)
    return citations


_LEVEL_TO_TYPE: dict[int, str] = {
    1: "section",
    2: "subsection",
    3: "subsubsection",
    4: "paragraph",
}


def _build_sections(tex: TexDocument) -> list[dict[str, Any]]:
    """Map TeX section headings to CIR sections."""
    sections: list[dict[str, Any]] = []
    for i, sec in enumerate(tex.sections):
        sections.append(
            {
                "id": sec.label or f"sec:{i}",
                "type": _LEVEL_TO_TYPE.get(sec.level, "section"),
                "title": sec.title,
                "order": i,
            }
        )
    return sections


def _build_authors(tex: TexDocument) -> list[dict[str, Any]]:
    authors: list[dict[str, Any]] = []
    for a in tex.authors:
        authors.append({"name": a.name})
    if not authors:
        # Required field: minItems=1. If the .tex didn't declare authors,
        # use a placeholder so the CIR is at least constructable.
        authors.append({"name": "Unknown"})
    return authors


def build_cir(
    tex_path: Path | str,
    sidecar_path: Path | str | None = None,
    bib_path: Path | str | None = None,
) -> CIR:
    """Build a CIR from a .tex source.

    Args:
        tex_path: Path to the .tex file.
        sidecar_path: Path to the *.rrvix.aux sidecar. Defaults to the
            same basename as the .tex with ``.rrvix.aux`` extension in
            the same directory.
        bib_path: Path to the .bib file. Defaults to looking for the
            file referenced in ``\\bibliography{...}`` in the same
            directory as the .tex.
    """
    tex_path = Path(tex_path)

    if sidecar_path is None:
        sidecar_path = tex_path.with_suffix(".rrvix.aux")
    sidecar_path = Path(sidecar_path)

    tex = parse_tex_file(tex_path)
    sidecar = parse_sidecar_file(sidecar_path)
    meta = sidecar.meta_dict()

    # Bibliography: prefer explicit path, else look up the first
    # \bibliography{NAME} reference and resolve it as ./NAME.bib, else
    # fall back to inline \begin{thebibliography} blocks in the source.
    bib_entries: list[BibEntry] = []
    if bib_path is not None:
        bib_entries = parse_bib_file(bib_path)
    elif tex.bibliography_files:
        bib_candidate = tex_path.parent / f"{tex.bibliography_files[0]}.bib"
        if bib_candidate.is_file():
            bib_entries = parse_bib_file(bib_candidate)

    # Always also scan the .tex for inline thebibliography entries; some
    # papers (the rrvix whitepaper among them) inline their bibliography
    # rather than ship a .bib. Merge by key, preferring the .bib entry
    # if both are present.
    inline_entries = parse_thebibliography(tex_path.read_text(encoding="utf-8"))
    seen_keys = {e.key for e in bib_entries}
    for ie in inline_entries:
        if ie.key not in seen_keys:
            bib_entries.append(ie)
            seen_keys.add(ie.key)

    paper_id = meta.get("id") or tex.metadata.rrvix_id or tex_path.stem

    cir_dict: dict[str, Any] = {
        "rrvix_version": meta.get("protocol")
        or tex.metadata.rrvix_protocol_version
        or "0.1.0",
        "id": paper_id,
        "version": meta.get("version") or tex.metadata.rrvix_version or "v1",
        "title": tex_to_text(tex.title) if tex.title else "Untitled",
        "authors": _build_authors(tex),
        "abstract": tex_to_text(tex.abstract) if tex.abstract else "",
        "submitted_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "license": meta.get("license") or tex.metadata.rrvix_license or "CC-BY-4.0",
        "source": {
            "format": "latex",
            "uri": tex_path.absolute().as_uri(),
        },
    }

    topics_str = meta.get("topics") or ""
    if topics_str:
        cir_dict["topics"] = [t.strip() for t in topics_str.split(",") if t.strip()]
    elif tex.metadata.rrvix_topics:
        cir_dict["topics"] = list(tex.metadata.rrvix_topics)

    sections = _build_sections(tex)
    if sections:
        cir_dict["sections"] = sections

    claims = _build_claims(tex, sidecar, paper_id)
    if claims:
        cir_dict["claims"] = claims

    citations = _build_citations(tex, bib_entries, paper_id)
    if citations:
        cir_dict["citations"] = citations

    cir_dict["annotations"] = []  # populated lazily by the server

    return CIR.model_validate(cir_dict)
