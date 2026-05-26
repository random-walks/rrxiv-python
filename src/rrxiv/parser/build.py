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


def _build_label_resolver(
    tex_envs: tuple[TexEnvironment, ...],
    paper_id: str,
) -> dict[str, str]:
    """Map every labelled TeX env to its canonical paper-prefixed id.

    Authors write edges with whatever short form they used in ``\\label{}``
    — e.g. ``\\dependson{I.1}{post:1}`` where the source ``I.1`` is the
    *suffix* of a labelled claim (``\\label{prop:I.1}``) and the target
    ``post:1`` is the full label of a scope. The resolver maps both
    forms (bare suffix + full label) to the canonical
    ``<paper_id>:<label>`` claim/scope id so edge endpoints can be
    rewritten consistently.

    Disambiguation rule: if two envs share a suffix (e.g. ``prop:I.1``
    and ``def:I.1``), the *claim* env wins — the convention in Euclid
    and similar corpora is that bare ``I.1`` refers to the proposition,
    with definitions / postulates / common notions always written with
    their explicit prefix.
    """
    by_full: dict[str, str] = {}
    by_suffix_claims: dict[str, str] = {}
    by_suffix_other: dict[str, str] = {}
    for env in tex_envs:
        if not env.label:
            continue
        canonical = f"{paper_id}:{env.label}"
        by_full[env.label] = canonical
        suffix = env.label.split(":", 1)[1] if ":" in env.label else env.label
        if env.name == "claim":
            by_suffix_claims[suffix] = canonical
        else:
            # Don't overwrite if a non-claim already won (deterministic
            # by document order, but unlikely to collide in practice).
            by_suffix_other.setdefault(suffix, canonical)
    # Suffix lookup: claim wins over scope/remark.
    by_suffix: dict[str, str] = {**by_suffix_other, **by_suffix_claims}
    return {**by_suffix, **by_full}


def _resolve_edge_endpoint(
    raw: str, *, paper_id: str, resolver: dict[str, str]
) -> str:
    """Resolve a raw edge endpoint string against the label map.

    Order:
      1. Already-qualified ``<paper_id>:...`` → return unchanged.
      2. Label match (full or suffix) → canonical paper-prefixed id.
      3. Cross-paper id (``other-paper:...``) → return unchanged.
      4. Bare token → fall back to ``<paper_id>:<raw>`` so the wire
         format stays stable.
    """
    prefix = f"{paper_id}:"
    if raw.startswith(prefix):
        return raw
    if raw in resolver:
        return resolver[raw]
    # Looks like another paper's already-qualified id.
    if ":" in raw and raw.split(":", 1)[0] != paper_id:
        return raw
    return f"{paper_id}:{raw}"


def _claim_ids_from_edge(
    edge: EdgeMarker, paper_id: str
) -> tuple[str, str]:
    """Heuristic mapping from a sidecar edge's source/target strings to
    canonical claim IDs.

    Legacy entry point — retained for backward compatibility. The new
    pipeline uses ``_resolve_edge_endpoint`` with a per-paper label
    resolver so suffix-form references (``I.1``) reach the canonical
    claim id (``rrxiv-paper-euclid-elements:prop:I.1``).
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

    # RRP-0001 / Sprint 26.L: edges in the sidecar carry the raw
    # ``\dependson{src}{tgt}`` strings, which use the author's local
    # label shorthand (often the suffix-only form, e.g. ``I.1``
    # referring to a claim labelled ``prop:I.1``). Resolve through a
    # label map so both ends become canonical ``<paper_id>:<label>``
    # ids; this is what unlocks the knowledge-graph DAG for multi-
    # prefix corpora like Euclid (claims ``prop:*`` depending on
    # scopes ``post:*`` / ``def:*`` / ``cn:*``).
    label_resolver = _build_label_resolver(tex.environments, paper_id)

    # Group edges by resolved source id for fast lookup.
    edges_from: dict[str, list[EdgeMarker]] = defaultdict(list)
    for edge in sidecar.edges:
        resolved_source = _resolve_edge_endpoint(
            edge.source, paper_id=paper_id, resolver=label_resolver
        )
        edges_from[resolved_source].append(edge)

    claims: list[dict[str, Any]] = []
    claim_envs = [e for e in tex.environments if e.name == "claim"]
    for env in claim_envs:
        marker = pairs.get(env)
        index = marker.index if marker else f"{len(claims) + 1}"
        cid = _claim_id(paper_id, env, index)

        # Pull edges whose resolved source matches this claim's
        # canonical id, then resolve each target the same way.
        depends_on: list[str] = []
        supports: list[str] = []
        contradicts: list[str] = []
        extends: list[str] = []
        for edge in edges_from.get(cid, []):
            target = _resolve_edge_endpoint(
                edge.target, paper_id=paper_id, resolver=label_resolver
            )
            if edge.edge_type == "depends_on":
                depends_on.append(target)
            elif edge.edge_type == "supports":
                supports.append(target)
            elif edge.edge_type == "contradicts":
                contradicts.append(target)
            elif edge.edge_type == "extends":
                extends.append(target)

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

    # Sprint 26.M: materialize scope + remark envs as foundational
    # claims so every node in the knowledge graph has a detail page.
    # Without this, Euclid's postulates / definitions / common notions
    # show up as edges' endpoints but have no /claims/{id} route, so
    # users clicking them from the DAG get a 404.
    foundational = _build_foundational_claims(
        tex,
        paper_id=paper_id,
        edges_from=edges_from,
        label_resolver=label_resolver,
        source_map=source_map,
    )
    claims.extend(foundational)
    return claims


# Title-prefix → (claim_type, evidence_type) discriminator. Order
# matters — first match wins. Anything not matched falls back to the
# env-name default below.
_FOUNDATIONAL_TITLE_RULES: tuple[tuple[str, tuple[str, str]], ...] = (
    ("definition", ("definitional", "definition")),
    ("postulate", ("theoretical", "convention")),
    ("common notion", ("theoretical", "convention")),
    ("axiom", ("theoretical", "convention")),
    ("lemma", ("theoretical", "argument")),
    ("corollary", ("theoretical", "argument")),
    ("remark", ("theoretical", "argument")),
)

# Fallback by env name (scope vs remark).
_FOUNDATIONAL_ENV_DEFAULT: dict[str, tuple[str, str]] = {
    "scope": ("definitional", "definition"),
    "remark": ("theoretical", "convention"),
}


def _classify_foundational(
    env: TexEnvironment, *, title_override: str | None = None,
) -> tuple[str, str]:
    """Map a scope/remark env to (claim_type, evidence_type).

    Uses the env's title (`\\begin{scope}[Definition I.1: point]`)
    when present — the title prefix is the most reliable signal. Falls
    back to env-name defaults when title is absent or unrecognised.
    """
    title = (title_override or env.title or "").strip().lower()
    for prefix, kinds in _FOUNDATIONAL_TITLE_RULES:
        if title.startswith(prefix):
            return kinds
    kind = tex_env_to_sidecar_kind(env.name)
    return _FOUNDATIONAL_ENV_DEFAULT.get(kind, ("theoretical", "convention"))


def _build_foundational_claims(
    tex: TexDocument,
    *,
    paper_id: str,
    edges_from: dict[str, list[EdgeMarker]],
    label_resolver: dict[str, str],
    source_map: SourceMap,
) -> list[dict[str, Any]]:
    """Emit Claim records for scope + remark envs that carry a label.

    Postulates, definitions, and common notions in Euclid are encoded
    as ``\\begin{scope}`` / ``\\begin{rrxivremark}`` envs because they
    aren't *checkable* assertions in the replication sense. But they
    are first-class nodes in the knowledge graph (claims depend on
    them), so they need ``/claims/{id}`` routes. This walker
    materialises them as Claim records with the foundational
    ``claim_type``/``evidence_type`` pair so the UI can render them
    correctly while the schema stays unchanged (no new enum values).
    """
    out: list[dict[str, Any]] = []
    for env in tex.environments:
        # The LaTeX class uses ``\\begin{rrxivremark}`` for remarks
        # (postulates + common notions in Euclid); the AST keeps the
        # raw env name. Normalise via sidecar_kind so both spellings
        # ("rrxivremark", "remark") match.
        kind = tex_env_to_sidecar_kind(env.name)
        if kind not in {"scope", "remark"}:
            continue
        if not env.label:
            # Unlabelled scopes (e.g. an inline ``\begin{scope}``
            # used for figure framing) aren't addressable; skip.
            continue
        cid = f"{paper_id}:{env.label}"
        # _classify_foundational uses the title for its prefix rules;
        # for remark envs the title lives inline in the body and we
        # extract it below — peek now so the classifier doesn't fall
        # to the env-name default and miscategorise postulates as
        # "theoretical, convention" when they should pick up the
        # postulate rule.
        body_for_title = env.body.strip()
        title_peek = env.title
        if title_peek is None:
            m = re.match(r"^\[([^\[\]\n]+)\]\s*", body_for_title)
            if m:
                title_peek = m.group(1)
        claim_type, evidence_type = _classify_foundational(env, title_override=title_peek)

        # The pylatexenc walker doesn't register ``rrxivremark`` as a
        # title-bearing macro, so `\begin{rrxivremark}[Postulate~1]`
        # lands the optional arg INLINE at the top of env.body
        # (`[Postulate~1]\n\label{post:1}\n...`). Pull that bracket
        # prefix off the body before tex-to-text cleaning so it
        # becomes the env's effective title instead of leaking into
        # the statement.
        raw_body = env.body.strip()
        effective_title: str | None = env.title
        if effective_title is None:
            bracket_match = re.match(r"^\[([^\[\]\n]+)\]\s*", raw_body)
            if bracket_match:
                effective_title = bracket_match.group(1).strip()
                raw_body = raw_body[bracket_match.end() :]

        if env.label:
            raw_body = raw_body.replace(f"\\label{{{env.label}}}", "").strip()
        statement = tex_to_text(raw_body)
        if not statement:
            continue

        # Edges *from* a foundational node are rare but possible
        # (e.g. one definition extending another). Same resolution
        # path as claim envs.
        depends_on: list[str] = []
        supports: list[str] = []
        contradicts: list[str] = []
        extends: list[str] = []
        for edge in edges_from.get(cid, []):
            target = _resolve_edge_endpoint(
                edge.target, paper_id=paper_id, resolver=label_resolver
            )
            if edge.edge_type == "depends_on":
                depends_on.append(target)
            elif edge.edge_type == "supports":
                supports.append(target)
            elif edge.edge_type == "contradicts":
                contradicts.append(target)
            elif edge.edge_type == "extends":
                extends.append(target)

        record: dict[str, Any] = {
            "id": cid,
            "paper_id": paper_id,
            "statement": statement,
            "claim_type": claim_type,
            "evidence_type": evidence_type,
            "extracted_by": "author",
            "canonical": True,
        }
        if effective_title:
            # Normalise `~` (LaTeX non-breaking space) to a regular
            # space so the wire payload is plain prose.
            record["title"] = tex_to_text(effective_title)
        if depends_on:
            record["depends_on"] = depends_on
        if supports:
            record["supports"] = supports
        if contradicts:
            record["contradicts"] = contradicts
        if extends:
            record["extends"] = extends

        # No evidence pairing for foundational nodes — they don't
        # ship with a separate proof block. Source-location still
        # comes from the env span itself.
        source_loc = _build_source_location(env, None, source_map)
        if source_loc:
            record["source_location"] = source_loc

        out.append(record)
    return out


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


_AUTHOR_SIDECAR_FIELDS: tuple[str, ...] = (
    "name",
    "orcid",
    "role",
    "handle",
    "is_agent",
    "affiliation",
    "email",
    # RRP-0026 ModelDescriptor fields (cls v0.7+):
    "model_name",
    "model_vendor",
    "model_family",
    "model_series",
    "model_version",
    "model_release_pin",
    "model_release_date",
    # RRP-0025 flat aliases (cls v0.6, deprecated — lifted into
    # models[0] downstream):
    "model_slug",
    # Inference-time:
    "inference_environment",
)


# RRP-0026: ModelDescriptor sub-fields, used to lift sidecar/flat
# entries into a structured models[] array.
_MODEL_DESCRIPTOR_FIELDS: tuple[str, ...] = (
    "name",
    "vendor",
    "family",
    "series",
    "version",
    "release_pin",
    "release_date",
    "context_window_tokens",
    "inference_provider",
)


def _provenance_from_sidecar(sa: dict[str, str]) -> dict[str, Any] | None:
    """Build a RRP-0026 provenance block from a single sidecar author
    record. Returns None if no model fields are present.

    Single-model: cls v0.7's \\rrxivauthor only emits one model per call,
    so we always produce ``models=[{...}]`` with one entry. Multi-model
    authors are declared via rrxiv-meta.json instead.
    """
    descriptor: dict[str, Any] = {}
    # cls v0.7 fields first.
    if "model_name" in sa:
        descriptor["name"] = sa["model_name"]
    if "model_vendor" in sa:
        descriptor["vendor"] = sa["model_vendor"]
    if "model_family" in sa:
        descriptor["family"] = sa["model_family"]
    if "model_series" in sa:
        descriptor["series"] = sa["model_series"]
    if "model_version" in sa:
        descriptor["version"] = sa["model_version"]
    if "model_release_pin" in sa:
        descriptor["release_pin"] = sa["model_release_pin"]
    elif "model_slug" in sa:
        # v0.6 deprecated alias lift.
        descriptor["release_pin"] = sa["model_slug"]
        if "name" not in descriptor:
            # Lifted-from-flat case: use the slug as a fallback name so
            # the required ModelDescriptor.name still gets populated.
            descriptor["name"] = sa["model_slug"]
    if "model_release_date" in sa:
        descriptor["release_date"] = sa["model_release_date"]

    if not descriptor:
        return None

    # Ensure `name` is set — required field.
    if "name" not in descriptor:
        descriptor["name"] = descriptor.get("release_pin") or "unknown-model"

    prov: dict[str, Any] = {"models": [descriptor]}
    if "inference_environment" in sa:
        prov["inference_environment"] = sa["inference_environment"]
    return prov


def _lift_flat_provenance(prov: dict[str, Any]) -> dict[str, Any]:
    """RRP-0026 compat: if a provenance block has flat RRP-0025 fields
    (model_slug, model_family, model_release_date, context_window_tokens)
    but no `models[]` array, lift them into a single-element models[].

    Idempotent: if `models[]` is already present, return unchanged.
    """
    if "models" in prov and isinstance(prov.get("models"), list) and prov["models"]:
        return prov  # already canonical shape

    flat_pin = prov.get("model_slug")
    flat_family = prov.get("model_family")
    flat_date = prov.get("model_release_date")
    flat_ctx = prov.get("context_window_tokens")
    if not (flat_pin or flat_family or flat_date or flat_ctx):
        return prov  # no flat fields either — nothing to lift

    descriptor: dict[str, Any] = {}
    if flat_pin:
        descriptor["release_pin"] = flat_pin
        descriptor["name"] = flat_pin  # best-effort fallback; meta-author
        # entries that want a proper marketing name SHOULD set
        # models[0].name explicitly instead of relying on this lift.
    if flat_family:
        descriptor["family"] = flat_family
    if flat_date:
        descriptor["release_date"] = flat_date
    if isinstance(flat_ctx, int):
        descriptor["context_window_tokens"] = flat_ctx
    if "name" not in descriptor:
        descriptor["name"] = "unknown-model"

    lifted = dict(prov)
    lifted["models"] = [descriptor]
    # Drop the now-redundant flat fields so downstream consumers don't
    # have to reconcile two shapes.
    for k in ("model_slug", "model_family", "model_release_date", "context_window_tokens"):
        lifted.pop(k, None)
    return lifted


def _parse_sidecar_author_line(line: str) -> dict[str, str] | None:
    """Parse a ``RRXIV:author:<n>|k1=v1|k2=v2|...`` line emitted by
    ``rrxiv.cls`` v0.6's ``\\rrxivauthor`` macro into a flat dict.

    Empty values are dropped. Returns ``None`` if the line doesn't
    match the expected shape.
    """
    if not line.startswith("RRXIV:author:"):
        return None
    rest = line[len("RRXIV:author:") :]
    # rest looks like "1|name=...|orcid=...|..."
    parts = rest.split("|")
    if not parts or "=" in parts[0]:
        return None
    # parts[0] is the author index; ignored — caller's iteration order
    # is the source of truth for ordering.
    out: dict[str, str] = {}
    for p in parts[1:]:
        if "=" not in p:
            continue
        key, _, value = p.partition("=")
        key = key.strip()
        value = value.strip()
        if key and value and key in _AUTHOR_SIDECAR_FIELDS:
            out[key] = value
    return out


def _parse_meta_json_authors(meta_path: Path | None) -> list[dict[str, Any]]:
    """Read ``rrxiv-meta.json`` (or the project-root meta) and return
    its ``authors`` array, or [] if missing / malformed."""
    if meta_path is None or not meta_path.is_file():
        return []
    try:
        import json

        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = data.get("authors")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict) and "name" in item:
            out.append(item)
    return out


def _coerce_author_record(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise a meta-json author dict into the CIR Author shape.

    - ``orcid`` becomes the field name expected by the schema.
    - ``is_agent`` coerces to bool.
    - ``provenance`` passes through as a dict; RRP-0025-shaped flat
      fields (model_slug etc.) are lifted into models[0] per RRP-0026.
    - Drop unknown keys to avoid schema noise.
    """
    cir_keys = {
        "name",
        "orcid",
        "affiliation",
        "email",
        "is_agent",
        "agent_handle",
        "role",
        "provenance",
    }
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k not in cir_keys:
            continue
        if k == "is_agent":
            if isinstance(v, str):
                v = v.lower() in ("true", "1", "yes")
            else:
                v = bool(v) if v is not None else False
        if v is None and k in ("orcid", "affiliation", "email", "agent_handle"):
            continue  # drop nulls so schema-required fields don't see them
        if k == "provenance" and isinstance(v, dict):
            v = _lift_flat_provenance(v)
        out[k] = v
    return out


def _merge_meta_onto_authors(
    parsed: list[dict[str, Any]],
    meta_authors: list[dict[str, Any]],
    *,
    warn: list[str],
) -> list[dict[str, Any]]:
    """Match meta-authors to parsed-tex authors by name and overlay the
    structured fields. Authors present in meta but not in the parsed
    set are appended; authors present in parsed but not in meta keep
    their bare ``{name: ...}`` shape.

    The match is exact-string on ``name`` after stripping. Authors
    with no match get a warning recorded into ``warn``.
    """
    if not meta_authors:
        return parsed

    by_parsed_name: dict[str, dict[str, Any]] = {a["name"].strip(): a for a in parsed}

    used_meta: set[int] = set()
    for parsed_author in parsed:
        target = parsed_author["name"].strip()
        match_idx: int | None = None
        for i, m in enumerate(meta_authors):
            if i in used_meta:
                continue
            if str(m.get("name", "")).strip() == target:
                match_idx = i
                break
        if match_idx is None:
            warn.append(
                f"parsed author {target!r} has no name-match in rrxiv-meta.json"
            )
            continue
        used_meta.add(match_idx)
        # Overlay; meta wins for any field it sets, except for `name`
        # which we keep from the parsed source so a typo in meta doesn't
        # corrupt the canonical display name.
        merged = dict(parsed_author)
        for k, v in _coerce_author_record(meta_authors[match_idx]).items():
            if k == "name":
                continue
            merged[k] = v
        parsed_author.clear()
        parsed_author.update(merged)

    # Append meta-authors that had no parsed counterpart (e.g. Heath as
    # translator on Euclid — not in \author{} but real per RRP-0021).
    for i, m in enumerate(meta_authors):
        if i in used_meta:
            continue
        coerced = _coerce_author_record(m)
        if "name" in coerced:
            parsed.append(coerced)
            warn.append(
                f"meta author {coerced['name']!r} appended without a "
                f"parsed-tex counterpart"
            )

    return parsed


def _build_authors(
    tex: TexDocument,
    sidecar_authors: list[dict[str, str]] | None = None,
    meta_authors: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
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

    # Layer 2: sidecar RRXIV:author records from cls v0.6/v0.7
    # \rrxivauthor macro. The cls falls through to authblk's \author{},
    # so the names should already appear in `authors` — we just enrich
    # the existing entry with the structured fields.
    if sidecar_authors:
        for sa in sidecar_authors:
            name = sa.get("name", "").strip()
            if not name:
                continue
            # Locate the matching parsed entry; append if missing.
            target: dict[str, Any] | None = None
            for entry in authors:
                if entry.get("name", "").strip() == name:
                    target = entry
                    break
            if target is None:
                target = {"name": name}
                authors.append(target)
            # Overlay structured fields.
            if "orcid" in sa:
                target["orcid"] = sa["orcid"]
            if "role" in sa:
                target["role"] = sa["role"]
            if "handle" in sa:
                target["agent_handle"] = sa["handle"]
            if "is_agent" in sa:
                target["is_agent"] = sa["is_agent"].lower() in ("true", "1", "yes")
            if "affiliation" in sa:
                target["affiliation"] = sa["affiliation"]
            if "email" in sa:
                target["email"] = sa["email"]
            # RRP-0026: build a structured provenance block with models[]
            # from the sidecar's model_* fields. Single-model only at the
            # cls level; multi-model is via rrxiv-meta.json.
            prov = _provenance_from_sidecar(sa)
            if prov is not None:
                target["provenance"] = prov

    # Layer 3: rrxiv-meta.json (canonical source per RRP-0021 / RRP-0025).
    # Wins over both LaTeX-parsed bare names and sidecar markers.
    if meta_authors:
        warn: list[str] = []
        authors = _merge_meta_onto_authors(authors, meta_authors, warn=warn)
        # Warnings are advisory; if the build is silent the parser's
        # CLI surface emits them as stderr notices.
        for w in warn:
            import sys

            print(f"rrxiv parse: WARN {w}", file=sys.stderr)

    if not authors:
        # Required field: minItems=1. If the .tex didn't declare authors,
        # use a placeholder so the CIR is at least constructable.
        authors.append({"name": "Unknown"})
    return authors


def build_cir(
    tex_path: Path | str,
    sidecar_path: Path | str | None = None,
    bib_path: Path | str | None = None,
    meta_path: Path | str | None = None,
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
        meta_path: Path to ``rrxiv-meta.json``. When present, its
            ``authors`` array enriches the parsed bare ``\\author{}``
            names with role / is_agent / agent_handle / orcid /
            provenance fields by name-match (RRP-0021 + RRP-0025).
            Defaults to auto-detect at ``<tex_root>/../rrxiv-meta.json``
            — the paper-repo layout convention where ``paper/main.tex``
            sits next to ``rrxiv-meta.json`` at the repo root.
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

    # Auto-detect rrxiv-meta.json at the paper-repo root if not supplied.
    if meta_path is None:
        # Standard layout: paper/main.tex + rrxiv-meta.json at repo root.
        candidate_meta = tex_path.parent.parent / "rrxiv-meta.json"
        if candidate_meta.is_file():
            meta_path = candidate_meta
        else:
            # Fallback: a meta.json sibling of the .tex (less common).
            sibling_meta = tex_path.parent / "rrxiv-meta.json"
            if sibling_meta.is_file():
                meta_path = sibling_meta
    if meta_path is not None:
        meta_path = Path(meta_path)

    tex = parse_tex_file(tex_path)
    sidecar = parse_sidecar_file(sidecar_path)
    meta = sidecar.meta_dict()
    source_map = SourceMap.from_flat_file(tex_path)

    # Layer in RRXIV:author records from cls v0.6's \rrxivauthor (sidecar)
    # + rrxiv-meta.json (canonical authors[] per RRP-0021).
    sidecar_authors = [dict(a.fields) for a in sidecar.authors]
    meta_authors = _parse_meta_json_authors(meta_path)

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
        "authors": _build_authors(
            tex, sidecar_authors=sidecar_authors, meta_authors=meta_authors
        ),
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
