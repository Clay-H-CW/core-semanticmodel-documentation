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

# View locally — serves out/ on loopback and opens a browser
semdoc serve
semdoc serve --variant business --port 8080
```

`extract` and `render` are separate so templates and prompts can be iterated offline
against a stored IR. Add `--save-tmsl` to keep the raw `model.bim` for use as a fixture.

### Optional passes

Each of these augments the stored IR in place; run `semdoc render` again afterwards to
pick up the result. All are safe to skip — the guide degrades gracefully without them.

```powershell
# Attach narrative content (hand-authored or session-authored JSON matching the
# Narrative schema), after checking every identifier it mentions against the model
semdoc narrative apply out/narrative.json

# Warehouse schema, view SQL, and best-effort lineage for objects the model reads from
semdoc warehouse extract

# Column cardinality and sample values via live DAX ("is this a good filter?")
semdoc stats extract --sample-threshold 50

# Which existing Power BI reports use this model, and which fields/measures each uses
semdoc reports extract
```

`narrative apply` is what gives narrative content its "verified" badge — everything else
above (warehouse lineage, column stats, report usage) is extracted fact, computed
straight from the model/warehouse/reports and never LLM-authored, so it carries no such
badge in the first place.

## Output

| File | Audience |
|---|---|
| `out/guide-technical.html` | Report builders and developers — full column inventory with data types, verbatim DAX, RLS filter expressions, warehouse lineage, relationship table |
| `out/guide-business.html` | End users — what the model answers and how to build it; internal keys, DAX bodies, and warehouse detail omitted |
| `out/vendor/mermaid.min.js` | Diagram renderer, shared by both guides |
| `out/model-ir.json` | The intermediate representation everything downstream reads |

Diagrams are authored as Mermaid source, so they also render natively in GitHub/Azure
DevOps markdown and stay diffable in Git — no headless browser or image pipeline.

The guides render diagrams locally by loading a vendored Mermaid bundle (downloaded once,
cached in `~/.semdoc/vendor`). Options:

- `--inline-assets` embeds the bundle in each guide for a genuinely single-file document,
  at ~3.5 MB extra per file.
- `--no-diagrams` skips it entirely; guides then display diagram source as text.

Diagrams follow the page theme, including redrawing when the viewer switches between
light and dark.

Everything stays local. `render` only writes to `out/`, and `serve` binds loopback only —
nothing is uploaded or published as a side effect of running the tool.

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

Built, tested, and run against a real model (a live Fabric DirectLake workspace, not
just the fixture):

- [x] Pure-HTTP Fabric extraction (TMSL + DAX), both Import and DirectLake storage modes
- [x] IR schema, with extracted facts and narrative kept separate
- [x] Deterministic table classification, measure dependency graph, warehouse lineage
- [x] Mermaid diagrams: star schema (drawn as filter flow), lineage, measure dependencies
- [x] Two-audience HTML guides (technical / business), with a chat widget in `serve`
      when `ANTHROPIC_API_KEY` is set
- [x] CLI
- [x] Narrative identifier validation (`narrative apply` — every identifier a narrative
      mentions is checked against the model before the guide calls it "verified")
- [x] Warehouse schema, view SQL, and best-effort FROM/JOIN lineage (`warehouse extract`)
- [x] Column cardinality and sample values via live DAX (`stats extract`)
- [x] Best Practice Analyzer findings — 21 of the 30 community Tabular Editor rules
- [x] Existing-report usage: which fields/measures each Power BI report on the model
      actually uses (`reports extract`), for the legacy single-file report format

Not yet built:

- [ ] An automated narrative-generation pass. Today, narrative content is authored
      outside the tool (by hand, or by a session working from the IR) and only
      validated/attached by `narrative apply` — there is no `semdoc enrich` that calls
      Claude directly to draft it.
- [ ] Automated DAX-snippet execution validation. `AnsweredQuestion.dax_verified` exists
      in the schema, but nothing currently runs a narrative's DAX and sets it
      automatically — whoever authors the narrative sets it by hand today.
- [ ] Report usage for the newer split `definition/pages/.../visual.json` report format
      (only the legacy single-file `report.json` format is parsed — see `docs/design.md`
      D8 for why).
- [ ] Row counts / table-size profiling (explicit cost decision, not an oversight).
- [ ] Service principal auth is implemented but has not been exercised against a live
      tenant — only interactive sign-in has been used so far.
