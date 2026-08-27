"""Mermaid diagram generation from the IR.

We emit Mermaid *source* rather than rendered images. It renders natively in published
Artifacts, in GitHub/Azure DevOps markdown, and in most wikis, which means no headless
browser, no image pipeline, and diagrams that stay diffable in Git.

The star schema diagram deliberately draws arrows in the direction filters propagate
(dimension -> fact) rather than in foreign-key direction. Filter flow is what actually
determines whether a report gives the right answer, and it is the thing report authors
most often get wrong.
"""

from __future__ import annotations

import re

from semdoc.ir.schema import Model, ModelIR, SourceKind, TableKind

_KIND_LABEL = {
    TableKind.FACT: "fact",
    TableKind.DIMENSION: "dimension",
    TableKind.BRIDGE: "bridge",
    TableKind.CALCULATION_GROUP: "calc group",
    TableKind.DISCONNECTED: "disconnected",
    TableKind.UNKNOWN: "",
}


def _node_id(*parts: str) -> str:
    """Build a Mermaid-safe node id. Mermaid ids cannot contain spaces or punctuation."""
    raw = "_".join(parts)
    cleaned = re.sub(r"\W+", "_", raw).strip("_")
    return cleaned or "n"


def _escape(text: str) -> str:
    """Escape text used inside a Mermaid node label."""
    return text.replace('"', "&quot;").replace("\n", " ")


def star_schema(model: Model, *, label_hidden_columns: bool = True) -> str:
    """The data model as filter-propagation flow.

    Solid arrow = active relationship, dashed = inactive (needs USERELATIONSHIP),
    double-headed = bidirectional cross-filtering.

    `label_hidden_columns=False` drops join-column labels for columns the audience cannot
    see anyway. Surrogate keys are hidden in the model precisely because report authors
    have no use for them, so naming them on the diagram is noise for a business reader —
    and it keeps the diagram consistent with the field lists beside it.
    """
    lines = ["flowchart LR"]

    for table in model.tables:
        node = _node_id(table.name)
        kind = _KIND_LABEL.get(table.kind, "")
        marker = "date table" if table.is_date_table else kind
        label = _escape(table.name)
        if marker:
            label += f"<br/><small>{marker}</small>"

        # Shape carries meaning: stadium for facts, rounded for dimensions, plain for the
        # ones that sit outside the graph.
        if table.kind is TableKind.FACT:
            lines.append(f'    {node}(["{label}"])')
        elif table.kind is TableKind.DISCONNECTED:
            lines.append(f'    {node}["{label}"]')
        else:
            lines.append(f'    {node}("{label}")')

    for rel in model.relationships:
        src = _node_id(rel.to_table)  # the "one" side filters...
        dst = _node_id(rel.from_table)  # ...the "many" side
        bidirectional = rel.cross_filter_direction.casefold() == "bothdirections"

        from_table = model.table(rel.from_table)
        from_column = from_table.column(rel.from_column) if from_table else None
        column_hidden = bool(from_column and from_column.is_hidden)
        show_label = label_hidden_columns or not column_hidden
        label = _escape(rel.from_column) if show_label else ""

        if not rel.is_active:
            # Always state inactivity, label or not — it changes whether the join works.
            text = f"{label} (inactive)" if label else "inactive"
            lines.append(f'    {src} -. "{text}" .-> {dst}')
        elif bidirectional:
            lines.append(f'    {src} <== "{label or "both ways"}" ==> {dst}')
        elif label:
            lines.append(f'    {src} -- "{label}" --> {dst}')
        else:
            lines.append(f"    {src} --> {dst}")

    # Class-based styling keeps the diagram legible in both light and dark themes by
    # relying on Mermaid's own theme variables rather than hard-coded colors.
    lines += [
        "    classDef factNode stroke-width:2px",
        "    classDef orphanNode stroke-dasharray:4 3",
    ]
    facts = [_node_id(t.name) for t in model.tables if t.kind is TableKind.FACT]
    orphans = [_node_id(t.name) for t in model.tables if t.kind is TableKind.DISCONNECTED]
    if facts:
        lines.append(f"    class {','.join(facts)} factNode")
    if orphans:
        lines.append(f"    class {','.join(orphans)} orphanNode")

    return "\n".join(lines)


def warehouse_lineage(ir: ModelIR) -> str:
    """Model tables mapped back to the warehouse objects they read from.

    This is the "what is actually underneath this model" view. Tables with no resolvable
    source (calculated tables, hand-rolled M) are shown as such rather than guessed at.
    """
    model = ir.model
    lines = ["flowchart LR", "    subgraph WH[Warehouse]", "        direction TB"]

    seen_sources: dict[str, str] = {}
    for table in model.tables:
        source = table.source
        if not source.warehouse_table:
            continue
        full = (
            f"{source.warehouse_schema}.{source.warehouse_table}"
            if source.warehouse_schema
            else source.warehouse_table
        )
        if full not in seen_sources:
            node = _node_id("wh", full)
            seen_sources[full] = node
            lines.append(f'        {node}[("{_escape(full)}")]')
    lines.append("    end")

    lines += ["    subgraph SM[Semantic model]", "        direction TB"]
    for table in model.tables:
        lines.append(f'        {_node_id("sm", table.name)}("{_escape(table.name)}")')
    lines.append("    end")

    for table in model.tables:
        target = _node_id("sm", table.name)
        source = table.source
        if source.warehouse_table:
            full = (
                f"{source.warehouse_schema}.{source.warehouse_table}"
                if source.warehouse_schema
                else source.warehouse_table
            )
            mode = "DirectLake" if source.kind is SourceKind.DIRECT_LAKE else "Import"
            lines.append(f'    {seen_sources[full]} -- "{mode}" --> {target}')
        elif source.kind is SourceKind.CALCULATED:
            calc = _node_id("calc", table.name)
            lines.append(f'    {calc}["DAX expression"] --> {target}')

    return "\n".join(lines)


def measure_dependencies(model: Model) -> str | None:
    """Measure-to-measure dependency graph.

    Returns None when no measure builds on another, since an all-singletons diagram tells
    the reader nothing.
    """
    edges: list[tuple[str, str]] = []
    for measure in model.all_measures:
        for dep in measure.depends_on:
            if dep.measure:
                edges.append((measure.name, dep.measure))

    if not edges:
        return None

    involved = {name for edge in edges for name in edge}
    lines = ["flowchart TD"]
    for name in sorted(involved):
        lines.append(f'    {_node_id("m", name)}["{_escape(name)}"]')
    for parent, child in edges:
        lines.append(f'    {_node_id("m", parent)} --> {_node_id("m", child)}')

    # Base measures — depended upon but depending on no other measure — are the ones a
    # new author should learn first, so mark them.
    derived = {parent for parent, _ in edges}
    base = sorted(name for name in involved if name not in derived)
    if base:
        lines.append("    classDef baseMeasure stroke-width:2px")
        lines.append(f"    class {','.join(_node_id('m', n) for n in base)} baseMeasure")

    return "\n".join(lines)


def table_focus(model: Model, table_name: str) -> str | None:
    """A single fact table with just the dimensions that reach it.

    Large models produce an unreadable all-tables diagram; a per-fact view stays legible.
    """
    table = model.table(table_name)
    if table is None:
        return None

    related = [
        r for r in model.relationships if r.from_table == table_name or r.to_table == table_name
    ]
    if not related:
        return None

    names = {table_name} | {r.from_table for r in related} | {r.to_table for r in related}
    subset = Model(
        name=model.name,
        tables=[t for t in model.tables if t.name in names],
        relationships=related,
    )
    return star_schema(subset)
