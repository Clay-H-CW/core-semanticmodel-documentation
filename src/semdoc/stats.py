"""Column cardinality and sample values, via live DAX against the model.

Answers the one question the guide previously had no way to answer: "is this column a
good slicer?" A text column with 4 distinct values is; one with 40,000 is not — and
without this, a report author has no way to tell without opening Power BI Desktop and
trying it. `Column.cardinality` and `Column.sample_values` already existed in the IR
schema for exactly this; this module is what actually populates them.

Deliberately excludes row counts (a separate, real cost decision on Fabric capacity for
large fact tables — left out on purpose, same as the warehouse module's tiers). Cost here
is bounded a different way: one DAX query per table (not per column), and sample values
are only fetched for columns under a cardinality threshold — fetching three examples out
of 40,000 distinct values is not useful anyway, so there is no reason to pay for it.

Scoped to visible columns of visible, non-calculation-group tables — a report author
never sees anything else, so profiling it would just be wasted queries.
"""

from __future__ import annotations

from semdoc.fabric import FabricClient, FabricError
from semdoc.ir.schema import Column, Model, Table, TableKind

# Above this many distinct values, three sample values would not be representative of
# anything, and are not worth the query cost of fetching.
DEFAULT_SAMPLE_THRESHOLD = 50

# How many columns go into one DAX query. Keeps queries a reasonable size rather than one
# giant VAR block for a 154-column table like this project's own real-world HMIS Enrollment.
_CHUNK_SIZE = 25

_SAMPLE_DELIMITER = "|~|"


class StatsError(RuntimeError):
    pass


def profilable_columns(model: Model) -> list[tuple[Table, list[Column]]]:
    """(table, columns) pairs for every visible column a report author can actually see."""
    out = []
    for table in model.tables:
        if table.is_hidden or table.kind is TableKind.CALCULATION_GROUP:
            continue
        columns = [c for c in table.columns if not c.is_hidden]
        if columns:
            out.append((table, columns))
    return out


def _dax_string_literal(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _dax_column_ref(table_name: str, column_name: str) -> str:
    escaped_table = table_name.replace("'", "''")
    escaped_column = column_name.replace("]", "]]")
    return f"'{escaped_table}'[{escaped_column}]"


def _chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _build_query(table_name: str, columns: list[Column], sample_threshold: int) -> str:
    """One EVALUATE ROW(...) computing cardinality (and, below the threshold, a sample
    string) for every column in this chunk. A VAR per column means DISTINCTCOUNT is
    computed once and reused for both the cardinality value and the sample-size gate.
    """
    var_lines = []
    row_args = []
    for i, col in enumerate(columns):
        var_name = f"__c{i}"
        ref = _dax_column_ref(table_name, col.name)
        var_lines.append(f"VAR {var_name} = DISTINCTCOUNT({ref})")

        card_key = _dax_string_literal(f"{col.name}__card")
        sample_key = _dax_string_literal(f"{col.name}__sample")
        row_args.append(f"{card_key}, {var_name}")
        row_args.append(
            f"{sample_key}, IF({var_name} <= {sample_threshold}, "
            f'CONCATENATEX(VALUES({ref}), {ref}, "{_SAMPLE_DELIMITER}"))'
        )

    body = ",\n    ".join(row_args)
    return "EVALUATE\n" + "\n".join(var_lines) + f"\nRETURN\nROW(\n    {body}\n)"


def _row_value(row: dict, key: str):
    """executeQueries sometimes returns ROW()-produced column names wrapped in brackets
    (a real quirk of that endpoint, not documented consistently) — check both forms
    rather than assume one."""
    if key in row:
        return row[key]
    return row.get(f"[{key}]")


def _apply_result_row(columns: list[Column], row: dict) -> None:
    by_name = {c.name: c for c in columns}
    for col_name, col in by_name.items():
        cardinality = _row_value(row, f"{col_name}__card")
        if cardinality is not None:
            col.cardinality = int(cardinality)

        sample = _row_value(row, f"{col_name}__sample")
        col.sample_values = sample.split(_SAMPLE_DELIMITER) if sample else []


def extract_column_stats(
    model: Model,
    client: FabricClient,
    *,
    sample_threshold: int = DEFAULT_SAMPLE_THRESHOLD,
) -> list[str]:
    """Populate `cardinality`/`sample_values` on every profilable column, in place.

    Returns the list of "table.column" identifiers that could not be profiled (a query
    error on that chunk) — surfaced so the caller can report them rather than have a
    column's stats silently stay empty with no indication why.
    """
    if not (model.workspace_id and model.id):
        raise StatsError(
            "This IR has no workspace/model id recorded. Re-run `semdoc extract` — "
            "older IRs predate this and won't have it."
        )

    failed: list[str] = []
    for table, columns in profilable_columns(model):
        for chunk in _chunk(columns, _CHUNK_SIZE):
            query = _build_query(table.name, chunk, sample_threshold)
            try:
                rows = client.execute_dax(model.workspace_id, model.id, query)
            except FabricError:
                failed.extend(f"{table.name}.{c.name}" for c in chunk)
                continue
            if rows:
                _apply_result_row(chunk, rows[0])
            else:
                failed.extend(f"{table.name}.{c.name}" for c in chunk)

    return failed
