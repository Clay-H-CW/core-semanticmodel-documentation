"""SQL analytics endpoint access for the Fabric Warehouse behind a semantic model.

Deliberately scoped to tiers 1-3, not the whole warehouse:

1. Metadata  - INFORMATION_SCHEMA: is it a table or a view, its columns and SQL types.
2. Views     - the verbatim SQL from sys.sql_modules, shown as-is, never rewritten.
3. Lineage   - best-effort FROM/JOIN extraction out of that SQL.

No row counts, no data profiling — that has a real cost on Fabric capacity (CU
consumption) and should be a deliberate, separate decision, not a side effect of running
this. Also scoped to what the model actually reads: every table's `TableSource` already
names the warehouse object it comes from, so this only ever looks up objects the model
itself references, not every object in the warehouse.

This is the one place in semdoc that needs a native driver rather than pure HTTP — the
SQL analytics endpoint speaks TDS, and there is no REST equivalent for view definitions
on either a Warehouse or a Lakehouse (Lakehouse's REST API lists Delta table names only).
"""

from __future__ import annotations

import re
import struct

import pyodbc

from semdoc.auth import SQL_SCOPE, Credential
from semdoc.ir.schema import Model, Warehouse, WarehouseColumn, WarehouseForeignKey, WarehouseTable

# https://learn.microsoft.com/sql/connect/odbc/using-azure-active-directory - the
# pre-connection attribute that hands the driver an already-acquired AAD token, instead of
# having the driver try to acquire one itself interactively.
_SQL_COPT_SS_ACCESS_TOKEN = 1256

_ODBC_DRIVER = "ODBC Driver 18 for SQL Server"


class WarehouseError(RuntimeError):
    pass


def _encode_access_token(token: str) -> bytes:
    token_bytes = token.encode("utf-16-le")
    return struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)


def connect(warehouse: Warehouse, credential: Credential) -> pyodbc.Connection:
    if not (warehouse.server and warehouse.database):
        raise WarehouseError(
            "This IR has no warehouse server/database recorded. Re-run `semdoc extract` "
            "— that is what discovers it, from the model's own data source metadata."
        )

    token = credential.token(SQL_SCOPE)
    conn_str = (
        f"Driver={{{_ODBC_DRIVER}}};"
        f"Server=tcp:{warehouse.server},1433;"
        f"Database={warehouse.database};"
        "Encrypt=yes;TrustServerCertificate=no;"
    )
    try:
        return pyodbc.connect(
            conn_str,
            attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: _encode_access_token(token)},
            timeout=30,
        )
    except pyodbc.Error as exc:
        raise WarehouseError(
            f"Could not connect to {warehouse.database!r} on {warehouse.server!r}: {exc}"
        ) from exc


def referenced_objects(model: Model) -> list[tuple[str, str]]:
    """Distinct (schema, table) pairs the model's own table sources name.

    This — not a full warehouse listing — is what tiers 1-3 look up. A table whose
    lineage could not be resolved during extraction (no `warehouse_table` recovered from
    its M-query or DirectLake entity) simply contributes nothing here.
    """
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for table in model.tables:
        source = table.source
        if source.warehouse_schema and source.warehouse_table:
            key = (source.warehouse_schema, source.warehouse_table)
            if key not in seen:
                seen.add(key)
                ordered.append(key)
    return ordered


# -- tier 3: best-effort lineage out of a view's own SQL text --------------------------

_CTE_NAME = re.compile(r"\bWITH\s+(\w+)\s+AS\s*\(|,\s*(\w+)\s+AS\s*\(", re.IGNORECASE)
_TABLE_REF = re.compile(r"\b(?:FROM|JOIN)\s+(?:\[?(\w+)\]?\.)?\[?(\w+)\]?", re.IGNORECASE)


def _strip_sql_noise(sql: str) -> str:
    """Remove comments and string literals so they cannot masquerade as identifiers."""
    without_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    without_line = re.sub(r"--[^\n]*", " ", without_block)
    return re.sub(r"'(?:[^']|'')*'", "''", without_line)


def extract_referenced_tables(sql: str, default_schema: str) -> list[str]:
    """Best-effort FROM/JOIN target extraction from a view's SQL definition.

    A schema-qualified reference (`schema.table`) is used as-is. An unqualified one is
    resolved against `default_schema` — the view's own schema, the overwhelmingly common
    convention — unless it matches a CTE name defined earlier in the same statement, in
    which case it is a local alias, not a real table, and is dropped.

    This is a heuristic over SQL text, not a parser: it cannot track paren nesting, so a
    CTE defined inside a deeply nested subquery can be missed. Consistent with the rest of
    this tool's philosophy, this is why the verbatim SQL is always shown alongside
    whatever this extracts, rather than in place of it — a human can always check.
    """
    cleaned = _strip_sql_noise(sql)

    cte_names = {(m.group(1) or m.group(2)).casefold() for m in _CTE_NAME.finditer(cleaned)}

    seen: set[str] = set()
    ordered: list[str] = []
    for match in _TABLE_REF.finditer(cleaned):
        schema, name = match.group(1), match.group(2)
        if name.casefold() in cte_names:
            continue
        full = f"{schema}.{name}" if schema else f"{default_schema}.{name}"
        key = full.casefold()
        if key not in seen:
            seen.add(key)
            ordered.append(full)
    return ordered


# -- queries -----------------------------------------------------------------------------

_TABLE_TYPE_SQL = "SELECT TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?"

_COLUMNS_SQL = (
    "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
    "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION"
)

# sys.sql_modules.definition is nvarchar(max) - unlike INFORMATION_SCHEMA.VIEWS, it is not
# subject to the 4000-character truncation some SQL Server-compatible engines apply there.
_VIEW_DEFINITION_SQL = """
SELECT m.definition
FROM sys.sql_modules m
JOIN sys.views v ON v.object_id = m.object_id
JOIN sys.schemas s ON s.schema_id = v.schema_id
WHERE s.name = ? AND v.name = ?
"""

# Fabric Warehouse supports declaring (non-enforced) foreign keys for the query optimizer
# and BI tools even though it does not enforce referential integrity — worth surfacing
# when present; an empty result here just means none were declared.
_FOREIGN_KEYS_SQL = """
SELECT c1.name AS column_name, s2.name AS ref_schema, t2.name AS ref_table, c2.name AS ref_column
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
JOIN sys.tables t1 ON t1.object_id = fk.parent_object_id
JOIN sys.schemas s1 ON s1.schema_id = t1.schema_id
JOIN sys.columns c1 ON c1.object_id = fkc.parent_object_id AND c1.column_id = fkc.parent_column_id
JOIN sys.tables t2 ON t2.object_id = fk.referenced_object_id
JOIN sys.schemas s2 ON s2.schema_id = t2.schema_id
JOIN sys.columns c2 ON c2.object_id = fkc.referenced_object_id AND c2.column_id = fkc.referenced_column_id
WHERE s1.name = ? AND t1.name = ?
"""


def _fetch_table(cursor, schema: str, name: str) -> WarehouseTable | None:
    cursor.execute(_TABLE_TYPE_SQL, schema, name)
    row = cursor.fetchone()
    if row is None:
        return None  # named by the model's own M-query, but not found under this name

    is_view = row[0] == "VIEW"

    cursor.execute(_COLUMNS_SQL, schema, name)
    columns = [
        WarehouseColumn(name=r.COLUMN_NAME, data_type=r.DATA_TYPE, is_nullable=(r.IS_NULLABLE == "YES"))
        for r in cursor.fetchall()
    ]

    foreign_keys: list[WarehouseForeignKey] = []
    view_definition: str | None = None
    reads_from: list[str] = []

    if is_view:
        cursor.execute(_VIEW_DEFINITION_SQL, schema, name)
        def_row = cursor.fetchone()
        view_definition = def_row[0] if def_row else None
        if view_definition:
            reads_from = extract_referenced_tables(view_definition, schema)
    else:
        cursor.execute(_FOREIGN_KEYS_SQL, schema, name)
        foreign_keys = [
            WarehouseForeignKey(
                column=r.column_name,
                references_schema=r.ref_schema,
                references_table=r.ref_table,
                references_column=r.ref_column,
            )
            for r in cursor.fetchall()
        ]

    return WarehouseTable(
        schema_name=schema,
        name=name,
        is_view=is_view,
        columns=columns,
        foreign_keys=foreign_keys,
        view_definition=view_definition,
        reads_from=reads_from,
    )


def extract_warehouse(
    model: Model, warehouse: Warehouse, credential: Credential
) -> tuple[Warehouse, list[tuple[str, str]]]:
    """Populate `warehouse.tables` for every warehouse object the model reads from.

    Returns the updated `Warehouse` and the list of (schema, table) pairs the model named
    that could not be found — surfaced so the caller can report them rather than have them
    silently vanish (a wrong or stale M-query lineage guess is a real failure mode worth
    seeing, not hiding).
    """
    refs = referenced_objects(model)
    if not refs:
        return warehouse, []

    tables: list[WarehouseTable] = []
    missing: list[tuple[str, str]] = []

    with connect(warehouse, credential) as conn:
        cursor = conn.cursor()
        for schema, name in refs:
            table = _fetch_table(cursor, schema, name)
            if table is None:
                missing.append((schema, name))
            else:
                tables.append(table)

    for table in tables:
        table.consumed_by = [
            t.name
            for t in model.tables
            if t.source.warehouse_schema == table.schema_name and t.source.warehouse_table == table.name
        ]

    return warehouse.model_copy(update={"tables": tables}), missing
