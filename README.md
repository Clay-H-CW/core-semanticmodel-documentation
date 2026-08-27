# semdoc

Generates documentation, diagrams, and an HTML user guide for a Fabric/Power BI semantic
model and the warehouse behind it — so a report author can go from "here is a model" to
"I can build the report I need" without reverse-engineering the schema.

POC. See [`docs/design.md`](docs/design.md) for the architecture and the reasoning
behind each decision.

## Why it is built this way

Handing a model's metadata to an LLM and asking for documentation produces fluent,
confident, hallucinated output — column names that do not exist, DAX that does not run.
Analysts trust it and lose hours. So content is split into two lanes:

- **Extracted facts** — inventory, DAX definitions, relationships, RLS, warehouse
  lineage. Rendered straight from metadata, never LLM-authored.
- **Generated narrative** — what the model is for, plain-English measure meanings,
  starter DAX, gotchas. LLM-authored, then **verified**: every identifier must resolve
  against the model, and every generated DAX snippet must actually execute.

The guide marks which lane each block came from, and states what was checked up front.

## Pipeline

```
extract  ->  model-ir.json  ->  enrich  ->  validate  ->  render
(Fabric)     (the contract)     (Claude)    (identifiers  (HTML guides,
                                             + live DAX)   Mermaid diagrams)
```

Extraction is pure HTTPS — no XMLA, ADOMD.NET, or .NET runtime. `getDefinition` supplies
the structure as TMSL; `executeQueries` runs DAX for stats and verification.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env    # then fill it in
```

## Usage

```powershell
# Discover what you can reach
semdoc workspaces
semdoc models --workspace "Analytics"

# Extract a model to out/model-ir.json
semdoc extract --workspace "Analytics" --model "Case Services Analytics"

# Render guides from the stored IR (no Fabric access needed)
semdoc render

# Both in one pass
semdoc generate -w "Analytics" -m "Case Services Analytics"
```

`extract` and `render` are separate so templates and prompts can be iterated offline
against a stored IR. Add `--save-tmsl` to keep the raw `model.bim` for use as a fixture.

## Output

| File | Audience |
|---|---|
| `out/guide-technical.html` | Report builders and developers — full column inventory with data types, verbatim DAX, RLS filter expressions, warehouse lineage, relationship table |
| `out/guide-business.html` | End users — what the model answers and how to build it; internal keys, DAX bodies, and warehouse detail omitted |
| `out/guide-*.artifact.html` | Same content as a body fragment, for publishing as a Claude Artifact (Mermaid renders natively there) |
| `out/model-ir.json` | The intermediate representation everything downstream reads |

Diagrams are emitted as Mermaid source, which renders natively in Artifacts and in
GitHub/Azure DevOps markdown — no headless browser, and diagrams stay diffable in Git.

## Authentication

Interactive by default: leave `SEMDOC_CLIENT_SECRET` blank and you will be signed in via
browser (falling back to device code). Tokens cache under `~/.semdoc`.

Setting `SEMDOC_CLIENT_SECRET` switches to service principal auth for automation. That
additionally needs the tenant setting **Service principals can use Fabric APIs** plus
workspace access for the app registration.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Tests run entirely against `tests/fixtures/sample_tmsl.json` — no Fabric access required.
The fixture is a small star schema that deliberately includes the awkward cases:
DirectLake and Import tables side by side, an inactive relationship, a disconnected
table, a calculated table, a marked date table, and an RLS role.

## Status

Built and tested:

- [x] Pure-HTTP Fabric extraction (TMSL + DAX)
- [x] IR schema, with extracted facts and narrative kept separate
- [x] Deterministic table classification, measure dependency graph, warehouse lineage
- [x] Mermaid diagrams: star schema (drawn as filter flow), lineage, measure dependencies
- [x] Two-audience HTML guides
- [x] CLI

Next:

- [ ] Enrichment pass (Claude API, grounded strictly in the IR)
- [ ] Validation pass (identifier resolution + live DAX execution)
- [ ] Warehouse schema pass over the SQL analytics endpoint
- [ ] Column cardinality and row counts via `INFO.*` DAX
- [ ] Run against a real model
