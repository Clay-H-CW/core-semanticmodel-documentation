# Design: Semantic Model Documentation Generator

POC for a tool that, on demand, generates documentation, diagrams, and an HTML user
guide for a Fabric/Power BI semantic model and the warehouse behind it — so a report
author can go from "here is a model" to "I can build the report I need" quickly.

## Goals

- Point the tool at a semantic model, get back a complete, **trustworthy** doc set.
- Two audiences from one extraction: a technical reference and a business-facing guide.
- Reproducible: same model in, same docs out. No hand-editing required.
- Documentation that includes the **underlying warehouse** — model table to warehouse
  table lineage, not just the model in isolation.

## Non-goals (for the POC)

- Editing or improving the semantic model itself (that is what the Best Practice
  Analyzer is for).
- Continuous/scheduled publishing. Design for it, do not build it yet.
- Documenting more than one model at a time.

## The core design problem

The naive approach — hand a model's metadata to an LLM and ask for documentation —
produces confident, fluent, **hallucinated** output: column names that do not exist,
measures that were never defined, DAX that does not run. That is worse than no
documentation, because analysts will trust it and waste hours on it.

So content is split into two lanes with different rules.

### Deterministic lane — never LLM-authored

Rendered directly from extracted metadata. Must be 100% accurate.

- Table, column, and measure inventory
- Measure DAX definitions (verbatim)
- Relationship graph: cardinality, direction, active/inactive
- Table grain and row counts
- RLS roles and their filter expressions
- Storage mode (Import / DirectLake / DirectQuery)
- Model table to warehouse table/view lineage

### Interpretive lane — LLM-authored, then verified

Where an LLM genuinely adds value over a metadata dump.

- What this model is for, in business terms
- Plain-English meaning of each measure
- "Questions this model can answer"
- Starter DAX recipes
- Gotchas (e.g. "`Client` has an inactive relationship to `Date` — use `USERELATIONSHIP`")
- Suggested report builds for common requirements

## Pipeline

```
1. EXTRACT    Fabric REST (TMSL + DAX) + warehouse T-SQL
                 |
2. NORMALIZE     +--> model-ir.json     <-- the contract
                 |                           everything downstream reads ONLY this
3. ENRICH     LLM narrative, grounded strictly in the IR
                 |
4. VALIDATE   - every identifier the LLM emitted must exist in the IR
              - every generated DAX snippet must actually execute
                 |
5. RENDER     - self-contained searchable HTML guide (technical + business)
              - Mermaid diagrams (star schema, warehouse lineage, measure deps)
              - markdown for Git/wiki
```

**Step 4 is the differentiator.** Validating every identifier against the IR, and
*executing* generated DAX against the live model, turns "plausible documentation" into
"verified documentation". It is cheap to build and it is the reason to trust the output.

## Key architecture decisions

### D1: The IR is the contract

A single versioned `model-ir.json` sits between extraction and everything downstream.
Renderers and the enricher never touch Fabric directly.

Why: lets us develop renderers and prompts against a checked-in fixture with no Fabric
access, makes output diffable, and means a second extraction backend (a Fabric notebook
using `sempy`) can be added later without touching the rest of the pipeline.

### D2: Pure-HTTP extraction — no XMLA/ADOMD/.NET

The usual way to read a semantic model's metadata from Python is XMLA via `pyadomd`,
which drags in ADOMD.NET and the .NET runtime. That is painful to install, hostile to
CI, and has no Python 3.14 story.

Everything we need is reachable over plain HTTPS instead:

| Need | Endpoint |
|---|---|
| Complete model structure | Fabric REST `getDefinition?format=TMSL` -> `model.bim` (JSON) |
| DAX execution (stats + validation) | Power BI REST `executeQueries` |
| Column cardinality, table row counts | `INFO.*` DAX functions via `executeQueries` |
| Warehouse schema | SQL analytics endpoint, `INFORMATION_SCHEMA` / `sys.*` |

Dependencies collapse to `msal` + `httpx` + `pydantic` + `jinja2` + `anthropic`.
`pyodbc` is optional and only needed for the warehouse pass.

Tradeoff: `executeQueries` caps result rows and allows one query per call, so it is not
suitable for bulk data extraction. We only need it for metadata and validation, so this
does not bite.

### D3: TMSL (JSON), not TMDL

`getDefinition` can return either. TMDL is an indentation-based format designed for
human-readable Git diffs; parsing it robustly is real work. TMSL is JSON and is the
right choice for programmatic consumption.

TMDL may still be fetched later for display purposes (showing an author a readable
snippet), but it is not the parsing target.

### D4: Interactive auth now, service principal ready

Auth sits behind one interface with two implementations. Interactive device-code login
unblocks the POC without needing a tenant admin. A service principal — which requires
the tenant's "Service principals can use Fabric APIs" setting plus workspace access —
can be swapped in for automation without touching callers.

### D5: Local CLI now, Fabric notebook as a later target

A local Python CLI iterates fast, is testable, and costs no capacity. The extraction
layer is kept behind an interface (see D1) so a `sempy`-based notebook backend can be
added as a second deployment target for scheduled runs.

### D6: Provider-agnostic enrichment

Enrichment calls go through one interface. Claude API directly for the POC; an
Azure-hosted or gateway provider can be substituted if governance requires it. Only
model *metadata* is sent for enrichment — never customer data.

Default model is `claude-sonnet-5` (`SEMDOC_ENRICH_MODEL` to override). Enrichment is a
high-volume, well-constrained job — one bounded prompt per table and per measure, grounded
in the IR and verified afterwards — which is the shape where Sonnet's price and latency pay
off. The validation pass is what protects quality here, not model tier.

### D7: Output stays local

Nothing is published anywhere as a side effect of running the tool. `render` writes files
to `out/`, and `serve` binds loopback only. Generated guides will contain real table names,
measure DAX, and RLS filter expressions, so publishing is an explicit, separate act.

### D8: Report usage — legacy `report.json` only, verified live

`semdoc reports extract` reads which fields/measures existing Power BI reports actually
use, straight from each report's own definition (`getDefinition` on the Report item,
same LRO pattern as the model). Two formats exist for a report's definition: the newer
split `definition/pages/.../visual.json` layout, and the older single `report.json` file
with each visual's config JSON-encoded as a string inside it.

Only the legacy format is parsed. Checked live against this project's actual target
tenant: every real report there is stored in the legacy format, and asking the API to
convert one via `getDefinition?format=PBIR` fails outright ("Report is using format
'[CurrentFormat]' and cannot be converted... using the API"). A report in the split
format is skipped with a clear reason rather than guessed at from documentation alone —
this project has never seen one to verify a parser against, and the DateTime-cardinality
bug earlier in this project is exactly the cost of shipping an unverified assumption.

Field references are resolved back against the current model and dropped (not guessed
at) when they no longer match — the same "missing means not extracted" policy as
`WarehouseTable.reads_from`.

### D9: Multiple models — the directory tree is the catalog, not a manifest

`out/` holds one subdirectory per extracted model (`out/<slug>/model-ir.json` +
`guide-*.html`), slug derived deterministically from `f"{workspace} {model_name}"`
(`catalog.model_slug`). Re-extracting the same workspace/model always resolves to the
same directory and overwrites only that; a different workspace or model always gets a
fresh sibling — extracting a second model can never clobber a first one by construction,
not by convention.

Considered and rejected: a separate `registry.json` tracking what's been extracted.
Rejected because it is a second source of truth that can drift from reality (a manually
deleted model directory would leave a dangling registry entry, producing a dead link in
every other guide's dropdown) — see `docs/design.md`'s own running theme of preferring
"can't drift" over "must remember to keep in sync" (same reasoning as D1's single IR
contract). Instead, `catalog.discover_models` scans `out/*/model-ir.json` directly, on
every `render`/`serve` call. It reads only the handful of fields a dropdown needs
straight out of the raw JSON — not a full `ModelIR.model_validate_json` — so a sibling
model's IR from an older schema still lists correctly (if not necessarily usably) even
though this project has, more than once, changed the IR schema out from under an
already-extracted model.

The dropdown in each guide is baked in at render time, not fetched at runtime: guides
are meant to open directly via `file://` with no server at all, and `fetch()` against
`file://` is blocked by browser CORS policy anyway. The consequence is that `render`
re-renders every sibling model's guides whenever one changes, not just the target model
— cheap, since it only needs each sibling's already-stored IR, no Fabric access — so
every dropdown always reflects the full, current roster rather than whichever model was
rendered most recently.

## Extraction extras worth grabbing

- **Existing descriptions.** Many models already have some. These are ground truth: the
  enricher fills gaps, it never overwrites a human-authored description.
- **Report usage signals.** Which measures and columns existing reports actually
  reference (from PBIR report JSON). This drives prominence — document what people use
  up front, bury the rest in an appendix.

## Open questions

- Target semantic model: workspace, model name, and storage mode. Needed to validate
  extraction against a real specimen.
- Whether the tenant permits service principal access to Fabric APIs.
- Where generated docs should be published (in-repo, Artifact, SharePoint, Fabric).
