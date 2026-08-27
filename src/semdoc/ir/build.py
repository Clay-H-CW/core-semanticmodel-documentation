"""Normalize a TMSL (`model.bim`) document into the semdoc IR.

Everything in here is deterministic. Table classification, measure dependencies, and
warehouse lineage are all derived mechanically from the model definition — no LLM is
involved, because analysts need these to be exactly right.
"""

from __future__ import annotations

import re

from semdoc.ir.schema import (
    Column,
    Hierarchy,
    Measure,
    Model,
    Partition,
    Ref,
    Relationship,
    Role,
    RoleTablePermission,
    SourceKind,
    StorageMode,
    Table,
    TableKind,
    TableSource,
)

_PARTITION_MODES = {
    "import": StorageMode.IMPORT,
    "directlake": StorageMode.DIRECT_LAKE,
    "directquery": StorageMode.DIRECT_QUERY,
    "dual": StorageMode.DUAL,
}

# DAX reference patterns. Applied only after comments and string literals are stripped,
# so that text inside a quoted string cannot masquerade as an identifier.
_QUALIFIED_REF = re.compile(r"'([^']+)'\[([^\]]+)\]|(\b\w+)\[([^\]]+)\]")
_BARE_REF = re.compile(r"(?<![\w'\]])\[([^\]]+)\]")


def _strip_dax_noise(expression: str) -> str:
    """Remove comments and string literals so reference matching cannot false-positive."""
    without_block = re.sub(r"/\*.*?\*/", " ", expression, flags=re.S)
    without_line = re.sub(r"(--|//)[^\n]*", " ", without_block)
    return re.sub(r'"(?:[^"\\]|\\.)*"', '""', without_line)


def _partition_mode(raw: str | None) -> StorageMode:
    return _PARTITION_MODES.get((raw or "").replace(" ", "").casefold(), StorageMode.UNKNOWN)


def _partition_source(raw_source: dict) -> TableSource:
    """Resolve a TMSL partition source to the warehouse object behind it.

    DirectLake and DirectQuery partitions use `type: entity`, which names the warehouse
    schema and table directly — this is the clean lineage case. Import partitions carry
    an M expression, from which we recover the table name where the shape is recognizable.
    """
    source_type = (raw_source.get("type") or "").casefold()

    if source_type == "entity":
        return TableSource(
            kind=SourceKind.DIRECT_LAKE,
            warehouse_schema=raw_source.get("schemaName"),
            warehouse_table=raw_source.get("entityName"),
            expression=raw_source.get("expressionSource"),
        )

    if source_type == "calculated":
        return TableSource(kind=SourceKind.CALCULATED, expression=raw_source.get("expression"))

    if source_type == "m":
        expression = raw_source.get("expression")
        if isinstance(expression, list):
            expression = "\n".join(expression)
        return TableSource(
            kind=SourceKind.M_QUERY,
            expression=expression,
            **_warehouse_ref_from_m(expression or ""),
        )

    return TableSource(kind=SourceKind.UNKNOWN)


def _warehouse_ref_from_m(expression: str) -> dict[str, str | None]:
    """Best-effort recovery of schema/table from an M query.

    Covers the two shapes Fabric generates by default. Anything more hand-rolled is left
    unresolved rather than guessed at — a wrong lineage claim is worse than a missing one,
    and the full expression is retained on the source either way.
    """
    # Sql.Database(...) / Lakehouse navigation: {[Schema="dbo",Item="dim_client"]}
    nav = re.search(r'\[\s*Schema\s*=\s*"([^"]+)"\s*,\s*Item\s*=\s*"([^"]+)"\s*\]', expression)
    if nav:
        return {"warehouse_schema": nav.group(1), "warehouse_table": nav.group(2)}

    # Inline T-SQL: FROM dbo.dim_client
    frm = re.search(r'\bFROM\s+\[?(\w+)\]?\.\[?(\w+)\]?', expression, flags=re.I)
    if frm:
        return {"warehouse_schema": frm.group(1), "warehouse_table": frm.group(2)}

    return {"warehouse_schema": None, "warehouse_table": None}


def _build_column(raw: dict) -> Column:
    sort_by = raw.get("sortByColumn")
    return Column(
        name=raw.get("name", "?"),
        data_type=raw.get("dataType", "unknown"),
        description=_joined(raw.get("description")),
        is_hidden=bool(raw.get("isHidden", False)),
        is_key=bool(raw.get("isKey", False)),
        format_string=raw.get("formatString"),
        display_folder=raw.get("displayFolder"),
        summarize_by=raw.get("summarizeBy"),
        sort_by_column=sort_by if isinstance(sort_by, str) else None,
        source_column=raw.get("sourceColumn"),
    )


def _build_measure(raw: dict) -> Measure:
    return Measure(
        name=raw.get("name", "?"),
        expression=_joined(raw.get("expression")) or "",
        description=_joined(raw.get("description")),
        format_string=raw.get("formatString"),
        display_folder=raw.get("displayFolder"),
        is_hidden=bool(raw.get("isHidden", False)),
    )


def _joined(value: object) -> str | None:
    """TMSL stores multi-line strings as arrays of lines."""
    if value is None:
        return None
    if isinstance(value, list):
        return "\n".join(str(v) for v in value)
    return str(value)


def _build_table(raw: dict) -> Table:
    partitions = [
        Partition(
            name=p.get("name", "?"),
            mode=_partition_mode(p.get("mode")),
            source=_partition_source(p.get("source") or {}),
        )
        for p in raw.get("partitions", [])
    ]

    modes = {p.mode for p in partitions if p.mode is not StorageMode.UNKNOWN}
    if len(modes) == 1:
        storage_mode = next(iter(modes))
    elif modes:
        storage_mode = StorageMode.MIXED
    else:
        storage_mode = StorageMode.UNKNOWN

    hierarchies = [
        Hierarchy(
            name=h.get("name", "?"),
            description=_joined(h.get("description")),
            is_hidden=bool(h.get("isHidden", False)),
            levels=[lvl.get("column", lvl.get("name", "?")) for lvl in h.get("levels", [])],
        )
        for h in raw.get("hierarchies", [])
    ]

    kind = TableKind.CALCULATION_GROUP if "calculationGroup" in raw else TableKind.UNKNOWN

    return Table(
        name=raw.get("name", "?"),
        description=_joined(raw.get("description")),
        is_hidden=bool(raw.get("isHidden", False)),
        # `dataCategory: Time` is how a model marks its official date table. Report
        # authors need this called out: it is what makes time intelligence work.
        is_date_table=(raw.get("dataCategory") == "Time"),
        kind=kind,
        storage_mode=storage_mode,
        source=partitions[0].source if partitions else TableSource(),
        columns=[_build_column(c) for c in raw.get("columns", [])],
        measures=[_build_measure(m) for m in raw.get("measures", [])],
        hierarchies=hierarchies,
        partitions=partitions,
    )


def _build_relationship(raw: dict) -> Relationship:
    # TMSL omits these when they hold the default value.
    is_active = bool(raw.get("isActive", True))
    return Relationship(
        name=raw.get("name", "?"),
        from_table=raw.get("fromTable", "?"),
        from_column=raw.get("fromColumn", "?"),
        to_table=raw.get("toTable", "?"),
        to_column=raw.get("toColumn", "?"),
        from_cardinality=raw.get("fromCardinality", "many"),
        to_cardinality=raw.get("toCardinality", "one"),
        is_active=is_active,
        cross_filter_direction=raw.get("crossFilteringBehavior", "oneDirection"),
        relies_on_userelationship=not is_active,
    )


def _build_role(raw: dict) -> Role:
    return Role(
        name=raw.get("name", "?"),
        description=_joined(raw.get("description")),
        model_permission=raw.get("modelPermission"),
        table_permissions=[
            RoleTablePermission(
                table=tp.get("name", "?"),
                filter_expression=_joined(tp.get("filterExpression")) or "",
            )
            for tp in raw.get("tablePermissions", [])
        ],
    )


def _classify_tables(model: Model) -> None:
    """Label each table fact / dimension / bridge from the relationship graph.

    A table on the "many" end of its relationships carries the events being measured; a
    table on the "one" end supplies the attributes you slice by. Both ends means a bridge.
    """
    for table in model.tables:
        if table.kind is TableKind.CALCULATION_GROUP:
            continue

        many_side = any(
            r.from_table == table.name and r.from_cardinality.casefold() == "many"
            for r in model.relationships
        )
        one_side = any(
            r.to_table == table.name and r.to_cardinality.casefold() == "one"
            for r in model.relationships
        )

        if many_side and one_side:
            table.kind = TableKind.BRIDGE
        elif many_side:
            table.kind = TableKind.FACT
        elif one_side:
            table.kind = TableKind.DIMENSION
        else:
            table.kind = TableKind.DISCONNECTED


def _resolve_measure_dependencies(model: Model) -> None:
    """Fill in `depends_on` / `referenced_by` across all measures.

    Powers the dependency diagram, and lets the guide present base measures before the
    ones built on top of them.
    """
    measure_names = {m.name for m in model.all_measures}
    columns_by_table = {t.name: {c.name for c in t.columns} for t in model.tables}

    for table in model.tables:
        for measure in table.measures:
            expression = _strip_dax_noise(measure.expression)
            refs: list[Ref] = []
            seen: set[tuple[str | None, str | None, str | None]] = set()

            def add(ref: Ref) -> None:
                key = (ref.table, ref.column, ref.measure)
                if key not in seen:
                    seen.add(key)
                    refs.append(ref)

            for match in _QUALIFIED_REF.finditer(expression):
                table_name = match.group(1) or match.group(3)
                column_name = match.group(2) or match.group(4)
                if table_name in columns_by_table and column_name in columns_by_table[table_name]:
                    add(Ref(table=table_name, column=column_name))

            for match in _BARE_REF.finditer(expression):
                name = match.group(1)
                if name in measure_names:
                    add(Ref(measure=name))
                elif name in columns_by_table.get(table.name, set()):
                    # An unqualified reference resolves against the measure's own table.
                    add(Ref(table=table.name, column=name))

            measure.depends_on = refs

    # Invert the graph.
    for table in model.tables:
        for measure in table.measures:
            measure.referenced_by = [
                Ref(measure=other.name)
                for other in model.all_measures
                if any(d.measure == measure.name for d in other.depends_on)
            ]


def tmsl_to_model(tmsl: dict, *, name: str, workspace: str | None = None, model_id: str | None = None) -> Model:
    """Convert a parsed `model.bim` document into the IR `Model`."""
    body = tmsl.get("model", tmsl)

    tables = [_build_table(t) for t in body.get("tables", [])]
    relationships = [_build_relationship(r) for r in body.get("relationships", [])]
    roles = [_build_role(r) for r in body.get("roles", [])]

    table_modes = {t.storage_mode for t in tables if t.storage_mode is not StorageMode.UNKNOWN}
    if len(table_modes) == 1:
        storage_mode = next(iter(table_modes))
    elif table_modes:
        storage_mode = StorageMode.MIXED
    else:
        storage_mode = StorageMode.UNKNOWN

    model = Model(
        name=name,
        workspace=workspace,
        id=model_id,
        description=_joined(body.get("description")),
        culture=body.get("culture"),
        storage_mode=storage_mode,
        tables=tables,
        relationships=relationships,
        roles=roles,
    )

    _classify_tables(model)
    _resolve_measure_dependencies(model)
    return model
