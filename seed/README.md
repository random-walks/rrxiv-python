# Seed corpus (demo fixtures)

A small set of synthetic CIRs that together exercise every scope and
rollup state in `paper_list_item.schema.json`. They exist so a fresh
`rrxiv serve` instance has visible content to navigate without
operators having to populate their own corpus first.

**These are demo fixtures, not real papers.** Every author is named
`Example Author A/B/C…` (and the lone agent is `Example Agent`) so it
is unambiguous that the data is illustrative. Don't read the abstracts
as factual claims; don't cite them. The deployed canonical instance
at `rrxiv.com` carries a different (real) corpus — see
[the project README](../README.md#deployment) for how operators
substitute their own seed dir.

## Load it

```sh
rrxiv seed-store --store sqlite:///./rrxiv.db --from ./seed/
```

(See the parent directory's `Dockerfile` for the deployed invocation.)

## What's here

| Filename | Title | Scope coverage |
|---|---|---|
| `rrxiv-whitepaper.cir.json` | rrxiv whitepaper (placeholder demo body, CIR only — no PDF) | replicated, fresh |
| `claim-graph-first-class.cir.json` | Claim graph as first-class artifact | replicated, agent |
| `reproducibility-budgets.cir.json` | Reproducibility budgets for ML preprints | contested, human |
| `shrinkage-estimators.cir.json` | Negative result on shrinkage estimators | untested |
| `agents-as-editors.cir.json` | Editorial role of agents | replicated, agent |
| `citation-vs-knowledge-graphs.cir.json` | Citation graphs aren't knowledge graphs | preprint |
| `retraction-as-data.cir.json` | Retraction notices as first-class data | untested |
| `active-replication.cir.json` | Many claims being actively replicated | active |

Each CIR is a self-contained JSON document conforming to
`cir.schema.json`. IDs are stable across rebuilds so the seed is
idempotent — re-running `seed-store` over the same dir is a no-op
beyond `INSERT OR REPLACE`.

## How to edit

The CIRs are hand-edited and check into git. If a schema field
changes, sync the change here too. Don't add real research papers —
real submissions go through `POST /api/v0/submissions`. Keep
authors as `Example Author …` placeholders; ship realism through your
own instance overlay, not the public reference library.

## How to regenerate the PDFs + tarballs

`scripts/build-seed-pdfs.py` reads each CIR, emits a synthetic LaTeX
file, builds it with `tectonic`, and packs a tarball. The whitepaper
is skipped — its real source lives in
[random-walks/rrxiv-whitepaper](https://github.com/random-walks/rrxiv-whitepaper)
and the public seed ships only the CIR (no PDF) to keep the demo
honest about the divergence between this corpus and the canonical one.

```sh
uv run python scripts/build-seed-pdfs.py --force
```
