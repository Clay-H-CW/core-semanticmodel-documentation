"""Validate narrative content against the extracted IR.

This is the check that turns "plausible narrative" into "verified narrative": every
table, column, and measure a narrative mentions must resolve against a real model
object. It runs regardless of who or what authored the narrative — a human, a session
like this one working from the IR directly, or a future automated `semdoc enrich` pass
calling the API. All of them can misname a field; only checking against the model
catches it.

DAX *execution* is a separate, Fabric-dependent check (it needs a live connection to run
`executeQueries`) and is not performed here — this module only checks that identifiers
exist, which needs nothing but the IR already on disk.
"""

from __future__ import annotations

from collections.abc import Iterator

from semdoc.ir.schema import Model, Narrative, Ref, ValidationReport


def _ref_resolves(model: Model, ref: Ref) -> bool:
    if ref.measure is not None:
        return model.measure(ref.measure) is not None
    if ref.table is not None:
        table = model.table(ref.table)
        if table is None:
            return False
        return ref.column is None or table.column(ref.column) is not None
    return False


def _iter_refs(narrative: Narrative) -> Iterator[Ref]:
    for question in narrative.questions_answered:
        yield from question.fields
    for gotcha in narrative.gotchas:
        yield from gotcha.affects
    for recipe in narrative.report_recipes:
        yield from recipe.fields
        yield from recipe.measures


def validate_identifiers(model: Model, narrative: Narrative) -> ValidationReport:
    """Check every identifier a narrative mentions against the model.

    Covers: the table/measure names narrative content is keyed by, and every `Ref` inside
    questions, gotchas, and recipes. Does not modify the narrative — a caller who wants to
    reject bad content on failure should check `report.ok`.
    """
    checked = 0
    failed: list[str] = []

    for table_name in narrative.tables:
        checked += 1
        if model.table(table_name) is None:
            failed.append(f"table narrative key {table_name!r} does not exist in the model")

    for measure_name in narrative.measures:
        checked += 1
        if model.measure(measure_name) is None:
            failed.append(f"measure narrative key {measure_name!r} does not exist in the model")

    for ref in _iter_refs(narrative):
        checked += 1
        if not _ref_resolves(model, ref):
            failed.append(f"unresolved reference: {ref}")

    return ValidationReport(identifiers_checked=checked, identifiers_failed=failed)
