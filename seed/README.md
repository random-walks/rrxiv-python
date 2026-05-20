# Seed corpus

A small set of canonical CIRs the deployed reference server bakes in at
build time. Loaded via:

```
rrxiv seed-store --store sqlite:///./rrxiv.db --from ./seed/
```

(See `Dockerfile` in the parent directory for the deployed invocation.)

## What's here

Eight hand-crafted CIRs that together exercise every scope and rollup
state defined in `paper_list_item.schema.json`:

| Filename | Title | Scope coverage |
|---|---|---|
| `rrxiv-whitepaper.cir.json` | rrxiv whitepaper itself | replicated, fresh |
| `claim-graph-first-class.cir.json` | Claim graph as first-class artifact | replicated, agent |
| `reproducibility-budgets.cir.json` | Reproducibility budgets for ML preprints | contested, human |
| `shrinkage-estimators.cir.json` | Negative result on shrinkage estimators | untested |
| `agents-as-editors.cir.json` | Editorial role of agents | replicated, agent |
| `citation-vs-knowledge-graphs.cir.json` | Citation graphs aren't knowledge graphs | preprint |
| `retraction-as-data.cir.json` | Retraction notices as first-class data | untested |
| `active-replication.cir.json` | Many claims being actively replicated | active |

Each CIR is a self-contained JSON document conforming to
`cir.schema.json`. IDs are stable across rebuilds so the seed is
idempotent.

## How to edit

These are hand-edited. If a schema field changes, sync the change
in here. Don't add real research papers — these are seed fixtures,
not the published corpus. Real submissions go through
`POST /api/v0/submissions`.

## How to regenerate

`rrxiv seed-store --from ./seed/ --store sqlite:///./tmp.db && diff` against
the prior state to confirm idempotency. No regen script — the JSONs
are the source.
