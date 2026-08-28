"""Render a `ModelIR` to the HTML guide.

Two audience variants come out of one template (see D-audience in docs/design.md):

- `technical` — full column inventory with data types, verbatim DAX, RLS filter
  expressions, warehouse lineage, relationship table.
- `business`  — what the model answers and how to build it; hidden columns, key columns,
  and DAX bodies are left out.

Always a complete, self-contained HTML document, meant to be opened locally or served by
`semdoc serve` — this project keeps everything local rather than publishing anywhere.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

from semdoc import __version__
from semdoc.ir.schema import Measure, Model, ModelIR, ReportUsage, Table, TableKind
from semdoc.render import assets, diagrams

TEMPLATE_DIR = Path(__file__).parent / "templates"

VARIANTS = ("technical", "business")

# How the page gets a Mermaid renderer:
#   "none"   - the host renders Mermaid itself (published Artifacts)
#   "link"   - reference a sibling vendor/mermaid.min.js (local default; one shared copy)
#   "inline" - embed the ~3.5 MB bundle for a genuinely single-file document
MERMAID_MODES = ("none", "link", "inline")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-") or "x"


def _measure_groups(measures: list[Measure]) -> list[tuple[str, list[Measure]]]:
    """Group measures by their Power BI display folder, for the sidebar nav.

    Reuses structure the model author already built rather than inventing a new one — a
    field list with a real display folder is exactly the "how do I stop scrolling through
    199 measures" problem the folder already solves in Power BI Desktop. Order within a
    group is preserved from the model (an author's own field-list ordering), and a nested
    folder path (`Basic\\Detail`) is rendered with the same "›" separator used elsewhere
    for hierarchy levels.
    """
    groups: dict[str, list[Measure]] = {}
    for measure in measures:
        key = measure.display_folder.replace("\\", " › ").strip() if measure.display_folder else ""
        groups.setdefault(key or "Ungrouped", []).append(measure)

    ordered_keys = sorted(groups, key=lambda k: (k != "Ungrouped", k.casefold()))
    return [(key, groups[key]) for key in ordered_keys]


_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _bpa_by_category(model: Model) -> list[tuple[str, list]]:
    """Group BPA findings by category for the technical guide's findings section.

    Sorted by category name, and within a category by severity (error first) so the
    things most worth a maintainer's attention aren't buried under low-priority notes.
    """
    groups: dict[str, list] = {}
    for finding in model.bpa_findings:
        groups.setdefault(finding.category, []).append(finding)

    for findings in groups.values():
        findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.rule_id, f.object_name))

    return [(category, groups[category]) for category in sorted(groups)]


def _report_usage_indexes(reports: list[ReportUsage]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Reverse-index existing-report usage for template lookups.

    Two separate dicts, rather than one keyed by `Ref.__str__`, because the template
    looks measures up by name alone and columns up by (table, column) — building the
    exact lookup keys here keeps the template from having to reconstruct `Ref`'s string
    format itself.
    """
    measures: dict[str, list[str]] = {}
    columns: dict[str, list[str]] = {}
    for report in reports:
        for ref in report.used_fields:
            if ref.measure:
                measures.setdefault(ref.measure, []).append(report.name)
            elif ref.table and ref.column:
                columns.setdefault(f"{ref.table}.{ref.column}", []).append(report.name)
    return measures, columns


def _fact_dimension_table(model: Model) -> dict | None:
    """Facts x shared dimensions, as plain rows/columns/marks for an HTML table.

    Same underlying data as `diagrams.fact_relationship_map` (`diagrams.shared_dimension_map`
    — one computation, two presentations) — a scannable table for "which facts touch
    which shared dimension" reads better than a node-and-line diagram for what is
    fundamentally a small bipartite relationship, and costs nothing to lay out wide: an
    HTML table just flows, it isn't fighting a graph-layout algorithm for horizontal
    space the way the diagram version is. `None` under the same condition the diagram
    returns `None` under — fewer than two facts, or none sharing a dimension.
    """
    shared = diagrams.shared_dimension_map(model)
    if not shared:
        return None

    dims = sorted(shared)
    facts = sorted({f for reached_by in shared.values() for f in reached_by})
    marks = {(f, d) for d, reached_by in shared.items() for f in reached_by}
    return {"facts": facts, "dims": dims, "marks": marks}


_SUFFIX_LABEL_OVERRIDES = {"dim": "Dimension"}
_SUFFIX_PRIORITY = {"Fact": 0, "Dimension": 1, "Bridge": 2}


def _suffix_group_key(name: str) -> str:
    """The group a table's name puts it in, under the underscore-suffix convention.

    Whatever comes after the *last* underscore, verbatim — "Dim" is spelled out as
    "Dimension" (the one relabel worth doing for readability), everything else (`Fact`,
    `Bridge`, or some other structural role this project hasn't seen yet, e.g. `_Lookup`)
    passes through as-is, trusting the model's own naming rather than guessing at a fixed
    vocabulary. A name with no underscore at all falls to "General".
    """
    if "_" not in name:
        return "General"
    last = name.rsplit("_", 1)[-1].strip()
    return _SUFFIX_LABEL_OVERRIDES.get(last.casefold(), last)


def _per_fact_diagrams(
    visible_tables: list[Table], ir: ModelIR, variant: str
) -> tuple[bool, list[dict], list[dict]]:
    """Per-fact-table diagram data for the collapsible split (see `docs/design.md`).

    Splitting only kicks in once there is more than one fact table to separate — a
    single-fact model has nothing meaningful to split, and its one combined diagram is
    already legible. `label_hidden_columns` follows the same business/technical rule the
    combined `star_schema` diagram already uses; lineage entries are only computed for
    the technical variant, since the lineage section itself only ever renders there —
    computing it for business would just be thrown away.
    """
    fact_tables = [t for t in visible_tables if t.kind is TableKind.FACT]
    use_per_fact = len(fact_tables) > 1

    per_fact_star: list[dict] = []
    per_fact_lineage: list[dict] = []
    if use_per_fact:
        for table in fact_tables:
            # Same relationship count `diagrams._focus_subset` uses to build the subset
            # in the first place — "how many dimensions does this fact reach" is a far
            # more useful summary-line number than the fact table's own column count.
            related_count = sum(
                1 for r in ir.model.relationships if r.from_table == table.name or r.to_table == table.name
            )
            star = diagrams.table_focus(
                ir.model, table.name, label_hidden_columns=(variant == "technical")
            )
            if star:
                per_fact_star.append({"table": table, "diagram": star, "related_count": related_count})
            if variant == "technical":
                lineage = diagrams.lineage_focus(ir, table.name)
                if lineage:
                    per_fact_lineage.append(
                        {"table": table, "diagram": lineage, "related_count": related_count}
                    )

    return use_per_fact, per_fact_star, per_fact_lineage


def _table_groups(tables: list[Table]) -> tuple[list[Table], list[tuple[str, list[Table]]]]:
    """Split tables into pinned (measure-hosting) and grouped tables, for the sidebar.

    Two real naming conventions exist across the models this project has documented:
    a warehouse-style suffix ("Cwe_Enrollment_Fact", "Cwe_Client_Dim") and a prefix
    before the first space (HMIS's "hmis Enrollment", "hmis Exit"). Which one applies is
    decided once, for the model as a whole, by which convention actually explains most of
    its tables — not per table, and not by name-checking the model itself. A model
    genuinely on the suffix convention still has the odd exception (HMIS's own
    `*HMIS_Measures` aside, a model like ServTracker's `ST_Client_Attributes` ends in
    neither Fact nor Dim but is still grouped by its own last segment, "Attributes",
    since the *model* is on that convention); the reverse also holds — HMIS has a
    `Tool_YesNoOff` here and there, but three tables out of dozens doesn't make the whole
    model suffix-organized, so all of HMIS groups by prefix instead. Getting this
    per-model, majority-vote call wrong would fragment a mostly-prefix model into a
    handful of one-table suffix groups, or vice versa — worse than either convention
    applied consistently.

    A table that hosts measures *and* sits disconnected from the relationship graph
    (`kind is DISCONNECTED`) is pulled out and pinned above the groups instead of being
    grouped with the rest: that combination is what a modeler's dedicated, otherwise-
    empty measures container actually looks like (HMIS's `*HMIS_Measures` — 199 measures,
    one placeholder column, zero relationships), and it deserves its own top-level slot
    regardless of what its name happens to start or end with. Measures on a real fact or
    dimension table — the far more common pattern, seen in every one of this project's
    other real models, where a table hosts a handful of measures alongside dozens of its
    own real columns — stay with that table and get grouped normally; `_classify_tables`
    already gives every real table a relationship-derived kind (never a bare "has
    measures" guess would tell fact/dimension/bridge apart from a true measures home).
    """
    def is_measures_container(t: Table) -> bool:
        return bool(t.measures) and t.kind is TableKind.DISCONNECTED

    pinned = [t for t in tables if is_measures_container(t)]
    rest = [t for t in tables if not is_measures_container(t)]

    uses_suffix_convention = bool(rest) and sum(1 for t in rest if "_" in t.name) > len(rest) / 2

    groups: dict[str, list[Table]] = {}
    for table in rest:
        if uses_suffix_convention:
            key = _suffix_group_key(table.name)
        else:
            prefix = table.name.split(" ", 1)[0] if " " in table.name else ""
            key = prefix or "General"
        groups.setdefault(key, []).append(table)

    if uses_suffix_convention:

        def sort_key(name: str) -> tuple:
            if name == "General":
                return (2, "")
            if name in _SUFFIX_PRIORITY:
                return (0, _SUFFIX_PRIORITY[name])
            return (1, name.casefold())
    else:

        def sort_key(name: str) -> tuple:
            return (0, "") if name == "General" else (1, name.casefold())

    ordered_keys = sorted(groups, key=sort_key)
    return pinned, [(key, groups[key]) for key in ordered_keys]


def _ordered_measures(ir: ModelIR) -> list[Measure]:
    """Base measures first, then measures that build on them.

    A reader meeting a model for the first time needs `Total Units` before
    `Avg Units per Service`; presenting them alphabetically buries the foundations.
    """
    measures = [m for m in ir.model.all_measures if not m.is_hidden]

    def depth(measure: Measure, seen: frozenset[str] = frozenset()) -> int:
        if measure.name in seen:
            return 0  # circular reference; the model would not deploy, but do not hang
        parents = [d.measure for d in measure.depends_on if d.measure]
        if not parents:
            return 0
        child_depths = []
        for name in parents:
            child = ir.model.measure(name)
            if child is not None:
                child_depths.append(depth(child, seen | {measure.name}))
        return 1 + max(child_depths, default=0)

    return sorted(measures, key=lambda m: (depth(m), m.display_folder or "", m.name))


def _build_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "j2"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=False,
    )
    env.filters["slug"] = _slug
    return env


def _context(
    ir: ModelIR,
    variant: str,
    mermaid_mode: str,
    mermaid_src_url: str,
    model_slug: str,
    available_models: list[dict],
) -> dict:
    model = ir.model

    visible_tables = [t for t in model.tables if not t.is_hidden]
    if variant == "business":
        # A calculation group is a modelling construct, not something to slice by.
        visible_tables = [t for t in visible_tables if t.kind is not TableKind.CALCULATION_GROUP]

    heading = (
        f"Building reports on {model.name}"
        if variant == "business"
        else f"{model.name} reference"
    )
    subtitle = (
        "What this model can answer, which fields to use, and how to avoid the "
        "traps that produce wrong numbers."
        if variant == "business"
        else "Complete structure, calculation definitions, security rules, and the "
        "warehouse objects behind each table."
    )

    if ir.validation is None:
        verification_state = "absent"
    elif ir.validation.ok:
        verification_state = "pass"
    else:
        verification_state = "fail"

    pinned_tables, table_groups = _table_groups(visible_tables)
    measure_report_usage, column_report_usage = _report_usage_indexes(ir.reports)
    use_per_fact_diagrams, per_fact_star, per_fact_lineage = _per_fact_diagrams(
        visible_tables, ir, variant
    )

    return {
        "model": model,
        "narrative": ir.narrative,
        "validation": ir.validation,
        "variant": variant,
        # The model's own name is distinctive and identifies the page in a gallery;
        # appending "Model Guide" would only add a generic explainer.
        "page_title": model.name,
        "heading": heading,
        "subtitle": subtitle,
        "verification_state": verification_state,
        "model_slug": model_slug,
        # Only worth showing a switcher once there is something to switch to; a
        # single-model out/ renders exactly as it always has.
        "available_models": available_models,
        "visible_tables": visible_tables,
        "pinned_tables": pinned_tables,
        "table_groups": table_groups,
        "visible_measures": (visible_measures := [m for m in model.all_measures if not m.is_hidden]),
        "measure_groups": _measure_groups(visible_measures),
        "ordered_measures": _ordered_measures(ir),
        "date_table": next((t for t in model.tables if t.is_date_table), None),
        "has_dax_snippets": bool(
            ir.narrative and any(q.dax for q in ir.narrative.questions_answered)
        ),
        "bpa_by_category": _bpa_by_category(model),
        "existing_reports": ir.reports,
        "measure_report_usage": measure_report_usage,
        "column_report_usage": column_report_usage,
        "disconnected_tables": [
            t for t in visible_tables if t.kind is TableKind.DISCONNECTED
        ],
        "inactive_relationships": [r for r in model.relationships if not r.is_active],
        "star_schema": diagrams.star_schema(
            model, label_hidden_columns=(variant == "technical")
        ),
        "warehouse_lineage": diagrams.warehouse_lineage(ir),
        "measure_dependencies": diagrams.measure_dependencies(model),
        "fact_relationship_map": diagrams.fact_relationship_map(
            model, label_hidden_columns=(variant == "technical")
        ),
        "fact_dimension_table": _fact_dimension_table(model),
        "use_per_fact_diagrams": use_per_fact_diagrams,
        "per_fact_star": per_fact_star,
        "per_fact_lineage": per_fact_lineage,
        # Markup, not a plain str: autoescaping would turn `[data-theme="dark"]` into
        # `[data-theme=&quot;dark&quot;]` — an invalid selector the browser silently drops,
        # taking the dark theme and every quoted font-family with it. Both of these are
        # our own files, never user data, so marking them safe is correct.
        "stylesheet": Markup((TEMPLATE_DIR / "style.css").read_text(encoding="utf-8")),
        "tool_version": __version__,
        "generated_at": ir.generated_at,
        "mermaid_mode": mermaid_mode,
        "mermaid_src_url": mermaid_src_url,
        # Only read the 3.5 MB bundle when it is actually going into the page.
        "mermaid_source": Markup(assets.fetch_mermaid()) if mermaid_mode == "inline" else Markup(""),
    }


def render_guide(
    ir: ModelIR,
    variant: str = "technical",
    *,
    mermaid_mode: str = "link",
    mermaid_src_url: str = "vendor/mermaid.min.js",
    model_slug: str = "",
    available_models: list[dict] | None = None,
) -> str:
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}, got {variant!r}")
    if mermaid_mode not in MERMAID_MODES:
        raise ValueError(f"mermaid_mode must be one of {MERMAID_MODES}, got {mermaid_mode!r}")

    env = _build_environment()
    template = env.get_template("guide.html.j2")
    context = _context(ir, variant, mermaid_mode, mermaid_src_url, model_slug, available_models or [])
    return template.render(**context)


def write_guides(
    ir: ModelIR,
    model_dir: Path,
    *,
    vendor_dir: Path | None = None,
    available_models: list[dict] | None = None,
    inline_assets: bool = False,
    with_diagrams: bool = True,
) -> dict[str, Path]:
    """Write both audience variants into `model_dir`.

    `model_dir`'s own name is this model's slug — the multi-model layout has no separate
    slug parameter to pass, because the directory name *is* the source of truth
    `catalog.discover_models` reads back later; keeping one spelling avoids the two ever
    disagreeing. `vendor_dir` defaults to a `vendor/` subdirectory of `model_dir` itself
    (a standalone single-model output, as this always was before multi-model support);
    pass the shared `out/vendor` root explicitly to reuse one Mermaid copy across models.

    `with_diagrams` installs the Mermaid bundle so the guides render diagrams when opened
    directly from disk. Set it False for an offline run with no cached bundle; the pages
    then show diagram source as text instead of failing.
    """
    model_dir.mkdir(parents=True, exist_ok=True)
    vendor_dir = vendor_dir or (model_dir / "vendor")
    slug = model_dir.name
    written: dict[str, Path] = {}

    mermaid_mode = "none"
    mermaid_src_url = ""
    if with_diagrams:
        mermaid_mode = "inline" if inline_assets else "link"
        if mermaid_mode == "link":
            # install_mermaid(parent) creates parent/vendor/mermaid.min.js — pass
            # vendor_dir's *parent* so the file lands at vendor_dir itself, not at
            # vendor_dir/vendor.
            installed = assets.install_mermaid(vendor_dir.parent)
            written["mermaid"] = installed
            # Posix-style even on Windows: this is a browser src attribute, not a
            # filesystem path — a backslash there is silently treated as a literal
            # character, not a separator, and the diagram fails to load.
            mermaid_src_url = Path(os.path.relpath(installed, model_dir)).as_posix()

    for variant in VARIANTS:
        path = model_dir / f"guide-{variant}.html"
        html = render_guide(
            ir,
            variant,
            mermaid_mode=mermaid_mode,
            mermaid_src_url=mermaid_src_url,
            model_slug=slug,
            available_models=available_models,
        )
        path.write_text(html, encoding="utf-8")
        written[variant] = path

    return written
