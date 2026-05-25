"""Combine sidecar + TeX + bibliography into a CIR.

This is the entry point used by the CLI (``rrxiv parse <file>``).

The function ``build_cir(tex_path)`` does the following:

1. Read the LaTeX source and walk it (``parser.tex.parse_tex_file``).
2. Locate and read the ``*.rrxiv.aux`` sidecar (``parser.sidecar.parse_sidecar_file``).
3. If the .tex declares a bibliography, read the corresponding .bib file
   and parse it (``parser.bibliography.parse_bib_file``).
4. Pair each LaTeX semantic environment with its sidecar marker by
   ordinal position (the v0.1 sidecar emits markers in document order).
5. Build a CIR object via the pydantic models, which validates against
   ``cir.schema.json`` on construction.

The result is a fully-validated ``rrxiv.models.CIR`` instance.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rrxiv.models import (
    CIR,
)
from rrxiv.parser.bibliography import BibEntry, parse_bib_file, parse_thebibliography
from rrxiv.parser.clean import tex_to_proof_text, tex_to_text
from rrxiv.parser.sidecar import (
    EdgeMarker,
    EnvMarker,
    Sidecar,
    parse_sidecar_file,
)
from rrxiv.parser.source_map import SourceMap
from rrxiv.parser.tex import (
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


def _label_suffix(label: str | None) -> str | None:
    """Tail after the first ``:`` of a LaTeX label.

    Convention: claims label as ``prop:X.Y``, evidence as ``ev:X.Y``.
    The shared tail (``X.Y``) is the pairing key. For labels without a
    ``:`` (e.g. ``claim:fixture`` vs ``ev:ci-test`` from older fixtures
    where the prefix differs but only the suffix is shared in practice),
    the parser falls back to the full label.
    """
    if not label:
        return None
    idx = label.find(":")
    if idx == -1:
        return label
    return label[idx + 1 :]


def _pair_claims_with_evidence(
    envs: tuple[TexEnvironment, ...],
) -> dict[TexEnvironment, TexEnvironment]:
    """Pair each ``claim`` env with its companion ``evidence`` env.

    Pairing rule (in priority order):

    1. **Shared label suffix**: a claim labelled ``\\label{prop:I.10}``
       pairs with an evidence block labelled ``\\label{ev:I.10}`` because
       the suffix ``I.10`` matches. This is the recommended convention
       documented in PUBLISHING.md.
    2. **Positional fallback**: when an evidence block has no label (or
       its suffix doesn't match any unpaired claim), it attaches to the
       most-recently-opened unpaired claim that appears before it in
       document order. This is the guardrail for legacy / single-claim
       papers that don't bother labelling evidence.

    Returns a dict from claim env → evidence env. Claims without any
    paired evidence are absent from the result (the caller treats
    absence as "no proof attached").
    """
    pairs: dict[TexEnvironment, TexEnvironment] = {}
    # Pre-compute suffix → list[evidence] for the label-based lookup.
    evidence_by_suffix: dict[str, list[TexEnvironment]] = defaultdict(list)
    for env in envs:
        if env.name == "evidence":
            suffix = _label_suffix(env.label)
            if suffix is not None:
                evidence_by_suffix[suffix].append(env)

    # Track which evidence envs we've already paired so they don't get
    # double-attached by the positional fallback.
    used_evidence: set[int] = set()  # id() of envs

    # Iterate claims in document order.
    claim_envs = [e for e in envs if e.name == "claim"]
    for claim in claim_envs:
        # 1. Try label-suffix pairing.
        claim_suffix = _label_suffix(claim.label)
        if claim_suffix is not None:
            for candidate in evidence_by_suffix.get(claim_suffix, []):
                if id(candidate) in used_evidence:
                    continue
                # Evidence should appear *after* the claim in source
                # order; reject earlier evidence (very rare in practice).
                if candidate.char_offset < claim.char_offset:
                    continue
                pairs[claim] = candidate
                used_evidence.add(id(candidate))
                break
            else:
                pass
        if claim in pairs:
            continue

        # 2. Positional fallback. Find the next unpaired evidence env
        # whose char_offset is after this claim's and before the next
        # claim's. This handles papers that don't label evidence.
        next_claim_offset = None
        for other in claim_envs:
            if other is claim:
                continue
            if other.char_offset > claim.char_offset:
                next_claim_offset = other.char_offset
                break
        for candidate in envs:
            if candidate.name != "evidence":
                continue
            if id(candidate) in used_evidence:
                continue
            if candidate.char_offset <= claim.char_offset:
                continue
            if next_claim_offset is not None and candidate.char_offset >= next_claim_offset:
                continue
            pairs[claim] = candidate
            used_evidence.add(id(candidate))
            break

    return pairs


_CAPTION_RE = re.compile(r"\\caption\s*\{((?:[^{}]|\{[^{}]*\})*)\}")


def _extract_figure_caption(figure_path: Path) -> str | None:
    """Read a figure .tex file and pull out the ``\\caption{...}`` text.

    Single-pass regex; deliberately tolerant — figure files are short
    and rarely contain pathological caption nesting. If the file can't
    be read or has no caption, returns ``None``.
    """
    try:
        text = figure_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    m = _CAPTION_RE.search(text)
    if m is None:
        return None
    return tex_to_text(m.group(1).strip()) or None


def _build_claim_figures(
    evidence: TexEnvironment | None,
    tex_dir: Path,
) -> list[dict[str, Any]] | None:
    """Build the ``figures[]`` entries for a claim from its evidence
    block's ``\\input{...}`` references.

    Path resolution: ``\\input{figures/fig-i-10}`` is stored as
    ``figures/fig-i-10.tex`` (LaTeX's implicit ``.tex`` extension is
    applied if the literal path doesn't already include one). The path
    is reported *relative to the source archive root* — the parser
    resolves the file on disk to read its caption, but the emitted path
    stays relative so it survives tarball round-tripping.
    """
    if evidence is None or not evidence.inputs:
        return None
    out: list[dict[str, Any]] = []
    for ref in evidence.inputs:
        # Skip references that don't look like figures (e.g. \input{postulates})
        # — only entries starting with "figures/" or ending in ".tex"
        # under a figures-named directory go in here. The conservative
        # rule: include if the reference's path component starts with
        # "figures/" or contains "/figures/". This matches Euclid + the
        # paper template; broader matchers can be added later.
        if not (
            ref.startswith("figures/")
            or "/figures/" in ref
            or (ref.endswith(".tex") and "figure" in ref.lower())
        ):
            continue
        # Resolve the figure path: try the literal ref, then ref + ".tex".
        rel_path = ref if ref.endswith(".tex") else f"{ref}.tex"
        abs_candidate = (tex_dir / rel_path).resolve()
        entry: dict[str, Any] = {"path": rel_path}
        caption = _extract_figure_caption(abs_candidate)
        if caption:
            entry["caption"] = caption
        out.append(entry)
    return out or None


def _build_source_location(
    claim: TexEnvironment,
    evidence: TexEnvironment | None,
    source_map: SourceMap,
) -> dict[str, Any] | None:
    """Build the ``source_location`` projection for a claim.

    Spans the ``\\begin{claim}`` line through the ``\\end{evidence}``
    line (or ``\\end{claim}`` when no evidence is paired). ``file`` is
    resolved via the SourceMap, which uses flatten-tex marker comments
    to translate flattened-source positions back to the original file's
    relative path.
    """
    start_file, start_line = source_map.locate(claim.char_offset)
    end_offset = evidence.char_end - 1 if evidence is not None else claim.char_end - 1
    end_file, end_line = source_map.locate(end_offset)
    # If start and end resolve to different files (shouldn't happen for
    # a single claim/evidence pair, but possible if flatten-tex inlined
    # the evidence from a different file than the claim), prefer the
    # start's file and clamp the end line so consumers don't see a
    # weird cross-file span.
    if start_file != end_file:
        end_line = start_line
    return {
        "file": start_file,
        "line_start": max(1, start_line),
        "line_end": max(start_line, end_line),
    }


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
    *,
    source_map: SourceMap,
    tex_dir: Path,
) -> list[dict[str, Any]]:
    pairs = _pair_envs_with_sidecar(tex.environments, sidecar.envs)
    evidence_pairs = _pair_claims_with_evidence(tex.environments)

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

        # RRP-0015 meaty-claim fields.
        evidence = evidence_pairs.get(env)
        if evidence is not None:
            proof_body = evidence.body.strip()
            if evidence.label:
                proof_body = proof_body.replace(
                    f"\\label{{{evidence.label}}}", ""
                ).strip()
            proof = tex_to_proof_text(proof_body)
            if proof:
                claim["proof"] = proof

        figures = _build_claim_figures(evidence, tex_dir)
        if figures:
            claim["figures"] = figures

        source_loc = _build_source_location(env, evidence, source_map)
        if source_loc:
            claim["source_location"] = source_loc

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


_AND_SPLIT_RE = re.compile(r"\s*\\and\s+")


def _build_authors(tex: TexDocument) -> list[dict[str, Any]]:
    authors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in tex.authors:
        # Strip LaTeX wrapping macros (\thanks{}, \texttt{}, etc.) from
        # the raw \author{...} group contents. Without this, names that
        # carry footnote-style annotations (``Alice\thanks{a@x.org}``)
        # round-trip through the CIR with the latex still attached and
        # show up as distinct authors on read paths.
        clean_name = tex_to_text(a.name).strip()
        if not clean_name:
            continue
        # ``\author{A \and B \and C}`` is the standard single-group form
        # for multi-author papers (the demo papers in Sprint 14 used it).
        # tex_to_text doesn't recognise \and (it's a zero-arg separator,
        # not a wrapping macro), so the literal string survives. Split
        # here so each piece becomes its own entry.
        for piece in _AND_SPLIT_RE.split(clean_name):
            piece = piece.strip()
            if not piece:
                continue
            # Dedup within a single paper, in case the LaTeX repeats \author.
            if piece in seen:
                continue
            seen.add(piece)
            authors.append({"name": piece})
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
        sidecar_path: Path to the *.rrxiv.aux sidecar. Defaults to the
            same basename as the .tex with ``.rrxiv.aux`` extension in
            the same directory.
        bib_path: Path to the .bib file. Defaults to looking for the
            file referenced in ``\\bibliography{...}`` in the same
            directory as the .tex.
    """
    tex_path = Path(tex_path)

    if sidecar_path is None:
        # Default to the new .rrxiv.aux extension; fall back to the legacy
        # .rrvix.aux if the rrxiv-suffix file isn't present (papers compiled
        # with the pre-rename rrvix.cls).
        candidate = tex_path.with_suffix(".rrxiv.aux")
        if not candidate.is_file():
            legacy = tex_path.with_suffix(".rrvix.aux")
            if legacy.is_file():
                candidate = legacy
        sidecar_path = candidate
    sidecar_path = Path(sidecar_path)

    tex = parse_tex_file(tex_path)
    sidecar = parse_sidecar_file(sidecar_path)
    meta = sidecar.meta_dict()
    source_map = SourceMap.from_flat_file(tex_path)

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
    # papers (the rrxiv whitepaper among them) inline their bibliography
    # rather than ship a .bib. Merge by key, preferring the .bib entry
    # if both are present.
    inline_entries = parse_thebibliography(tex_path.read_text(encoding="utf-8"))
    seen_keys = {e.key for e in bib_entries}
    for ie in inline_entries:
        if ie.key not in seen_keys:
            bib_entries.append(ie)
            seen_keys.add(ie.key)

    paper_id = meta.get("id") or tex.metadata.rrxiv_id or tex_path.stem

    cir_dict: dict[str, Any] = {
        "rrxiv_version": meta.get("protocol")
        or tex.metadata.rrxiv_protocol_version
        or "0.1.0",
        "id": paper_id,
        "version": meta.get("version") or tex.metadata.rrxiv_version or "v1",
        "title": tex_to_text(tex.title) if tex.title else "Untitled",
        "authors": _build_authors(tex),
        "abstract": tex_to_text(tex.abstract) if tex.abstract else "",
        "submitted_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "license": meta.get("license") or tex.metadata.rrxiv_license or "CC-BY-4.0",
        "source": {
            "format": "latex",
            "uri": tex_path.absolute().as_uri(),
        },
    }

    topics_str = meta.get("topics") or ""
    if topics_str:
        cir_dict["topics"] = [t.strip() for t in topics_str.split(",") if t.strip()]
    elif tex.metadata.rrxiv_topics:
        cir_dict["topics"] = list(tex.metadata.rrxiv_topics)

    sections = _build_sections(tex)
    if sections:
        cir_dict["sections"] = sections

    claims = _build_claims(
        tex,
        sidecar,
        paper_id,
        source_map=source_map,
        tex_dir=tex_path.parent,
    )
    if claims:
        cir_dict["claims"] = claims

    citations = _build_citations(tex, bib_entries, paper_id)
    if citations:
        cir_dict["citations"] = citations

    cir_dict["annotations"] = []  # populated lazily by the server

    return CIR.model_validate(cir_dict)
