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


def _table_groups(tables: list[Table]) -> tuple[list[Table], list[tuple[str, list[Table]]]]:
    """Split tables into pinned (measure-hosting) and name-prefix groups, for the sidebar.

    Tables have no `display_folder` the way measures do, but a naming convention like
    "hmis Enrollment" / "hmis Exit" is common — this groups by the token before the first
    space, falling back to "General" for a name with no such prefix (e.g. "AgeAsOfLookup").

    A table that itself carries measures is pulled out and pinned above the groups
    instead of being alphabetized into one: it is architecturally a different kind of
    thing from a plain data table — typically a hidden container a modeler adds purely so
    calculated measures have somewhere to live — and deserves its own top-level slot
    regardless of what its name happens to start with.
    """
    pinned = [t for t in tables if t.measures]
    rest = [t for t in tables if not t.measures]

    groups: dict[str, list[Table]] = {}
    for table in rest:
        prefix = table.name.split(" ", 1)[0] if " " in table.name else ""
        groups.setdefault(prefix or "General", []).append(table)

    ordered_keys = sorted(groups, key=lambda k: (k != "General", k.casefold()))
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
) -> str:
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}, got {variant!r}")
    if mermaid_mode not in MERMAID_MODES:
        raise ValueError(f"mermaid_mode must be one of {MERMAID_MODES}, got {mermaid_mode!r}")

    env = _build_environment()
    template = env.get_template("guide.html.j2")
    return template.render(**_context(ir, variant, mermaid_mode, mermaid_src_url))


def write_guides(
    ir: ModelIR,
    out_dir: Path,
    *,
    inline_assets: bool = False,
    with_diagrams: bool = True,
) -> dict[str, Path]:
    """Write both audience variants into `out_dir`.

    `with_diagrams` installs the Mermaid bundle so the guides render diagrams when opened
    directly from disk. Set it False for an offline run with no cached bundle; the pages
    then show diagram source as text instead of failing.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    mermaid_mode = "none"
    if with_diagrams:
        mermaid_mode = "inline" if inline_assets else "link"
        if mermaid_mode == "link":
            written["mermaid"] = assets.install_mermaid(out_dir)

    for variant in VARIANTS:
        path = out_dir / f"guide-{variant}.html"
        path.write_text(render_guide(ir, variant, mermaid_mode=mermaid_mode), encoding="utf-8")
        written[variant] = path

    return written
