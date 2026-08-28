"""Which fields and measures existing Power BI reports actually use.

Answers a different question than the Best Practice Analyzer's "unused measure" check:
BPA only knows whether a measure is referenced by *other DAX* in the model, so a measure
built for exactly one report card visual still looks "used" to it, and a measure no
report has touched in years still looks fine too. This module reads the reports
themselves, so a report author can find an existing report doing something similar
before building from scratch, and a model maintainer can see which fields are actually
load-bearing.

Scoped to the legacy single-file `report.json` format, which is what this project's real
target tenant's reports are actually stored as — confirmed live: requesting the newer
split `definition/pages/.../visual.json` format via `getDefinition?format=PBIR` fails
outright for a report saved in the older format ("cannot be converted... using the API").
Reports in that newer format are skipped with a clear reason rather than guessed at,
since this module has never seen one to verify a parser against.

Field references are pulled from two places inside `report.json`, verbatim, both keyed by
Fabric's own real table/column/measure names:

- Each visual's `config` (itself a JSON-encoded string) has a `singleVisual.prototypeQuery`
  with a `Select` list — the field wells actually on the visual.
- `filters` arrays exist at report, page (section), and visual level; each entry's
  `expression` names one field being filtered on.

A `Select`/filter entry can wrap the field reference in `Aggregation` (e.g. "Average of
X"); that is unwrapped. Anything else unrecognized (hierarchy drill levels, binned
groups) is skipped rather than guessed, same policy as `semdoc.warehouse`'s lineage
parsing.
"""

from __future__ import annotations

import json

from semdoc.fabric import FabricClient
from semdoc.ir.schema import Column, Measure, Model, Ref, ReportUsage, Table


class ReportsError(RuntimeError):
    pass


def _alias_map(from_list: list[dict]) -> dict[str, str]:
    return {f["Name"]: f["Entity"] for f in from_list if "Name" in f and "Entity" in f}


def _source_entity(source_ref: dict, aliases: dict[str, str]) -> str | None:
    if "Entity" in source_ref:
        return source_ref["Entity"]
    if "Source" in source_ref:
        return aliases.get(source_ref["Source"])
    return None


def _resolve_field(expr: dict, aliases: dict[str, str]) -> tuple[str, str, bool] | None:
    """Best-effort resolve one query/filter field expression to (table, name, is_measure).

    Returns None for any shape this project has not seen verified against a real report
    (hierarchy drill levels, grouping bins, percentiles) — these do not map onto a single
    model field, and guessing would be worse than omitting them.
    """
    if "Aggregation" in expr:
        return _resolve_field(expr["Aggregation"].get("Expression", {}), aliases)

    for key, is_measure in (("Column", False), ("Measure", True)):
        if key in expr:
            inner = expr[key]
            entity = _source_entity(inner.get("Expression", {}).get("SourceRef", {}), aliases)
            prop = inner.get("Property")
            return (entity, prop, is_measure) if entity and prop else None

    return None


def _visual_select_fields(config_raw: str) -> list[tuple[str, str, bool]]:
    try:
        config = json.loads(config_raw)
    except json.JSONDecodeError:
        return []

    query = (config.get("singleVisual") or {}).get("prototypeQuery") or {}
    aliases = _alias_map(query.get("From", []))

    found = []
    for entry in query.get("Select", []):
        resolved = _resolve_field(entry, aliases)
        if resolved:
            found.append(resolved)
    return found


def _filter_fields(filters_raw: str | None) -> list[tuple[str, str, bool]]:
    if not filters_raw:
        return []
    try:
        filters = json.loads(filters_raw)
    except json.JSONDecodeError:
        return []

    found = []
    for f in filters:
        expr = f.get("expression")
        if expr:
            resolved = _resolve_field(expr, {})
            if resolved:
                found.append(resolved)
    return found


def parse_legacy_report_json(text: str) -> tuple[list[str], list[tuple[str, str, bool]]]:
    """Parse a legacy-format `report.json` into (page names, raw field references).

    Raw references are (entity, property, is_measure) triples, not yet checked against
    a model — `extract_report_usage` does that, since this function has no model to
    check against and is exercised directly in tests with hand-built fixtures.
    """
    data = json.loads(text)

    pages = [section.get("displayName", "") for section in data.get("sections", [])]

    fields: list[tuple[str, str, bool]] = []
    fields.extend(_filter_fields(data.get("filters")))
    for section in data.get("sections", []):
        fields.extend(_filter_fields(section.get("filters")))
        for vc in section.get("visualContainers", []):
            config_raw = vc.get("config")
            if config_raw:
                fields.extend(_visual_select_fields(config_raw))
            fields.extend(_filter_fields(vc.get("filters")))

    return pages, fields


def _find_table_casefold(model: Model, name: str) -> Table | None:
    exact = model.table(name)
    if exact is not None:
        return exact
    lowered = name.casefold()
    return next((t for t in model.tables if t.name.casefold() == lowered), None)


def _find_column_casefold(table: Table, name: str) -> Column | None:
    exact = table.column(name)
    if exact is not None:
        return exact
    lowered = name.casefold()
    return next((c for c in table.columns if c.name.casefold() == lowered), None)


def _find_measure_casefold(model: Model, name: str) -> Measure | None:
    exact = model.measure(name)
    if exact is not None:
        return exact
    lowered = name.casefold()
    for t in model.tables:
        for m in t.measures:
            if m.name.casefold() == lowered:
                return m
    return None


def _to_ref(model: Model, table: str, name: str, is_measure: bool) -> Ref | None:
    """Only keep a reference that still resolves against the current model.

    A report can reference a field that has since been renamed or removed — dropping it
    silently here (rather than guessing at a rename) is the same "missing means not
    extracted" policy `semdoc.warehouse` uses for lineage it cannot confidently parse.

    Resolution is case-insensitive, falling back to a casefold match and returning the
    model's own canonical casing — found live against the real HMIS model, where a report
    referenced `hmis Enrollment.CurrentYearActiveEnrollment` but the column is actually
    named `CurrentyearActiveEnrollment` (lowercase y). Same drift this project already
    hit once in measure-dependency resolution (`ir.build`), for the same reason: a report
    or a DAX expression is a point-in-time copy of a name that a later model edit can
    silently drift away from without breaking anything until you go looking for an exact
    string match.
    """
    if is_measure:
        measure = _find_measure_casefold(model, name)
        return Ref(measure=measure.name) if measure else None

    t = _find_table_casefold(model, table)
    if t is None:
        return None
    column = _find_column_casefold(t, name)
    return Ref(table=t.name, column=column.name) if column else None


def extract_report_usage(
    model: Model, client: FabricClient
) -> tuple[list[ReportUsage], list[str]]:
    """Find every report built on `model` and what it actually uses.

    Returns (reports found, diagnostic notes) — notes cover both reports skipped
    entirely (unsupported format, unparseable JSON) and are meant to be printed to the
    operator, not acted on programmatically.
    """
    if not (model.workspace_id and model.id):
        raise ReportsError(
            "This IR has no workspace/model id recorded. Re-run `semdoc extract` — "
            "older IRs predate this and won't have it."
        )

    all_reports = client.list_reports(model.workspace_id)
    matching = [r for r in all_reports if r.get("datasetId") == model.id]

    results: list[ReportUsage] = []
    notes: list[str] = []

    for r in matching:
        name = r.get("name") or r.get("id")
        parts = client.get_report_definition(model.workspace_id, r["id"])
        report_json = parts.get("report.json")
        if report_json is None:
            notes.append(
                f"{name}: skipped — not in the supported legacy report.json format "
                f"(parts seen: {', '.join(sorted(parts)) or 'none'})"
            )
            continue

        try:
            pages, raw_fields = parse_legacy_report_json(report_json)
        except json.JSONDecodeError as exc:
            notes.append(f"{name}: skipped — could not parse report.json ({exc})")
            continue

        refs: dict[str, Ref] = {}
        unresolved = 0
        for table, field_name, is_measure in raw_fields:
            ref = _to_ref(model, table, field_name, is_measure)
            if ref is not None:
                refs[str(ref)] = ref
            else:
                unresolved += 1

        if unresolved:
            notes.append(
                f"{name}: {unresolved} field reference(s) did not resolve against the "
                f"current model (renamed or removed since the report was last saved)"
            )

        results.append(ReportUsage(name=name, id=r["id"], pages=pages, used_fields=list(refs.values())))

    return results, notes
