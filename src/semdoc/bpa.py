"""Best Practice Analyzer: deterministic model-quality checks.

Ports a subset of the community-maintained Tabular Editor Best Practice Rules
(github.com/TabularEditor/BestPracticeRules — the "standard" and "PowerBI" rule sets)
directly onto the semdoc IR. No live connection, no VertiPaq statistics — these rules are
themselves static-metadata checks in the original tool, so everything here runs on data
`ir.build` already extracted.

Coverage: 21 of the 30 unique rules across both source files. (Fabric's MCP server
description advertises "71 rules" for its own bundled analyzer; the actual open rule set
this ports from has 30 unique rules, not 71 — the discrepancy is worth knowing about
rather than silently target a number that doesn't match what was actually fetched.) The
other 9 are skipped, for two honest reasons:

- Perspectives and multi-culture translations (LAYOUT_ADD_TO_PERSPECTIVES, LAYOUT_LOCALIZE_DF,
  TRANSLATE_*, DISABLE_ATTRIBUTE_HIERACHIES) need model concepts this tool does not extract
  at all (perspectives, translated-name/description dictionaries, hierarchy variations).
  Rare in Power BI-authored models; not worth a schema extension for this pass.
- Legacy on-prem connection-string checks (SPECIFY_APPLICATION_NAME_IN_CONNECTION_STRING,
  USE_MSOLEDBSQL_PROVIDER) target classic SQLOLEDB/SQLNCLI providers. Fabric never
  generates these — Import-mode models use a structured "tds" data source, DirectLake
  models reference OneLake directly. Not applicable here.

This is a maintainer/developer concern, not a report-builder one — findings render in the
technical guide only, never the business one.

A note on fidelity: a few ported rules are deliberately simplified from the original TOM
scripting expression, either because the exact signal isn't in this IR (e.g.
APPLY_FORMAT_STRING_MEASURES drops the original's numeric-DataType qualifier, since this
tool does not infer a measure's DAX result type) or because the original inspects tokens
this tool would otherwise have to re-lex from scratch (DAX_DIVISION_COLUMNS is a regex
heuristic, not a real DAX tokenizer). Each such simplification is called out in that
rule's docstring.
"""

from __future__ import annotations

import re
from collections import Counter

from semdoc.dax_text import BARE_REF, QUALIFIED_REF, strip_dax_noise
from semdoc.ir.schema import BpaFinding, Model, TableKind

# The rule set's own severity scale, as documented alongside the JSON.
_SEVERITY = {1: "info", 2: "warning", 3: "error"}

_NUMERIC_TYPES = {"int64", "double", "decimal", "datetime"}
_SUMMARIZABLE_TYPES = {"int64", "double", "decimal"}


def _finding(rule_id: str, category: str, severity: int, message: str, object_type: str, object_name: str) -> BpaFinding:
    return BpaFinding(
        rule_id=rule_id,
        category=category,
        severity=_SEVERITY[severity],
        message=message,
        object_type=object_type,
        object_name=object_name,
    )


# -- DAX Expressions ---------------------------------------------------------------------


def _rule_dax_todo(model: Model) -> list[BpaFinding]:
    findings = []
    for table in model.tables:
        for measure in table.measures:
            if "todo" in measure.expression.casefold():
                findings.append(
                    _finding(
                        "DAX_TODO", "DAX Expressions", 1,
                        f"Measure '{measure.name}' contains \"TODO\" in its DAX definition.",
                        "measure", measure.name,
                    )
                )
        for partition in table.partitions:
            expr = partition.source.expression
            if expr and "todo" in expr.casefold():
                findings.append(
                    _finding(
                        "DAX_TODO", "DAX Expressions", 1,
                        f"Partition '{partition.name}' on '{table.name}' contains \"TODO\" "
                        "in its source expression.",
                        "partition", f"{table.name}.{partition.name}",
                    )
                )
    return findings


_BRACKET_CONTENT = re.compile(r"\[[^\]]*\]")


def _mask_bracket_refs(text: str) -> str:
    """Blank out `[...]` contents so a `/` inside a bracketed name (a real example from
    this model: `[(CMF)... (Adult/Child Household Filtered)]`) cannot masquerade as a
    division operator. Column/measure names routinely contain slashes; DAX operators
    never appear inside a bracket reference, so this is safe to do unconditionally."""
    return _BRACKET_CONTENT.sub(lambda m: "[" + "_" * (len(m.group(0)) - 2) + "]", text)


def _rule_dax_division(model: Model) -> list[BpaFinding]:
    """DAX_DIVISION_COLUMNS — approximate: real rule tokenizes DAX properly and checks
    whether the divisor token is a numeric literal; this looks at the character(s)
    immediately following `/` instead. Good enough to catch the common case
    (`[A]/[B]` instead of `DIVIDE([A],[B])`), not immune to edge cases a real lexer
    would get right (e.g. a parenthesized or negative literal divisor).
    """
    findings = []
    divisor_pattern = re.compile(r"/\s*(-?\d)")
    slash_pattern = re.compile(r"/")
    for table in model.tables:
        for measure in table.measures:
            expr = _mask_bracket_refs(strip_dax_noise(measure.expression))
            for m in slash_pattern.finditer(expr):
                if divisor_pattern.match(expr, m.start()):
                    continue
                findings.append(
                    _finding(
                        "DAX_DIVISION_COLUMNS", "DAX Expressions", 3,
                        f"Measure '{measure.name}' divides using '/' with a non-constant "
                        "denominator — use DIVIDE(...) instead to avoid divide-by-zero errors.",
                        "measure", measure.name,
                    )
                )
                break  # one finding per measure is enough signal
    return findings


def _rule_dax_qualification(model: Model) -> list[BpaFinding]:
    """DAX_COLUMNS_FULLY_QUALIFIED + DAX_MEASURES_UNQUALIFIED.

    Both are about how a reference is *written*, not what it resolves to — a separate
    lightweight scan from `ir.build`'s dependency resolver, which deliberately discards
    qualification style once a reference is resolved into a canonical `Ref`.
    """
    findings = []
    measure_names = {m.name for m in model.all_measures}

    for table in model.tables:
        column_names = {c.name for c in table.columns}
        for measure in table.measures:
            expr = strip_dax_noise(measure.expression)

            unqualified_columns = {
                match.group(1)
                for match in BARE_REF.finditer(expr)
                if match.group(1) in column_names and match.group(1) not in measure_names
            }
            qualified_measures = {
                (match.group(2) or match.group(4))
                for match in QUALIFIED_REF.finditer(expr)
                if (match.group(2) or match.group(4)) in measure_names
            }

            if unqualified_columns:
                findings.append(
                    _finding(
                        "DAX_COLUMNS_FULLY_QUALIFIED", "DAX Expressions", 2,
                        f"Measure '{measure.name}' references column(s) "
                        f"{', '.join(sorted(unqualified_columns))} without a table qualifier.",
                        "measure", measure.name,
                    )
                )
            if qualified_measures:
                findings.append(
                    _finding(
                        "DAX_MEASURES_UNQUALIFIED", "DAX Expressions", 2,
                        f"Measure '{measure.name}' references measure(s) "
                        f"{', '.join(sorted(qualified_measures))} with an unnecessary table qualifier.",
                        "measure", measure.name,
                    )
                )
    return findings


# -- Formatting / Metadata ----------------------------------------------------------------


def _rule_format_string_columns(model: Model) -> list[BpaFinding]:
    findings = []
    for table in model.tables:
        for col in table.columns:
            if not col.is_hidden and not col.format_string and col.data_type.casefold() in _NUMERIC_TYPES:
                findings.append(
                    _finding(
                        "APPLY_FORMAT_STRING_COLUMNS", "Formatting", 2,
                        f"Visible column '{table.name}'[{col.name}] has no format string.",
                        "column", f"{table.name}[{col.name}]",
                    )
                )
    return findings


def _rule_format_string_measures(model: Model) -> list[BpaFinding]:
    """APPLY_FORMAT_STRING_MEASURES, simplified: the original also requires a numeric
    DAX result type, which this tool cannot infer without executing the measure. Checked
    against all visible measures instead — in practice nearly all of them are numeric."""
    findings = []
    for measure in model.all_measures:
        if not measure.is_hidden and not measure.format_string:
            findings.append(
                _finding(
                    "APPLY_FORMAT_STRING_MEASURES", "Formatting", 2,
                    f"Visible measure '{measure.name}' has no format string.",
                    "measure", measure.name,
                )
            )
    return findings


def _rule_avoid_float(model: Model) -> list[BpaFinding]:
    findings = []
    for table in model.tables:
        for col in table.columns:
            if col.data_type.casefold() == "double":
                findings.append(
                    _finding(
                        "META_AVOID_FLOAT", "Metadata", 3,
                        f"'{table.name}'[{col.name}] is a floating-point column — values near "
                        "zero can compare unexpectedly. Prefer a fixed decimal type.",
                        "column", f"{table.name}[{col.name}]",
                    )
                )
    return findings


def _rule_summarize_none(model: Model) -> list[BpaFinding]:
    findings = []
    for table in model.tables:
        for col in table.columns:
            if (
                not col.is_hidden
                and col.data_type.casefold() in _SUMMARIZABLE_TYPES
                and (col.summarize_by or "").casefold() != "none"
            ):
                findings.append(
                    _finding(
                        "META_SUMMARIZE_NONE", "Metadata", 1,
                        f"Visible numeric column '{table.name}'[{col.name}] can be dragged "
                        "into a visual and auto-summed — set SummarizeBy to None and use a "
                        "measure instead if that is not intended.",
                        "column", f"{table.name}[{col.name}]",
                    )
                )
    return findings


# -- Model Layout --------------------------------------------------------------------------


def _rule_columns_hierarchies_display_folders(model: Model) -> list[BpaFinding]:
    findings = []
    for table in model.tables:
        missing = sum(1 for c in table.columns if not c.is_hidden and not c.display_folder)
        missing += sum(1 for h in table.hierarchies if not h.is_hidden and not h.display_folder)
        if missing > 10:
            findings.append(
                _finding(
                    "LAYOUT_COLUMNS_HIERARCHIES_DF", "Model Layout", 1,
                    f"'{table.name}' has {missing} visible columns/hierarchies with no "
                    "display folder — consider organizing them.",
                    "table", table.name,
                )
            )
    return findings


def _rule_hide_fk_columns(model: Model) -> list[BpaFinding]:
    findings = []
    many_side = {(r.from_table, r.from_column) for r in model.relationships}
    for table in model.tables:
        for col in table.columns:
            if not col.is_hidden and (table.name, col.name) in many_side:
                findings.append(
                    _finding(
                        "LAYOUT_HIDE_FK_COLUMNS", "Model Layout", 1,
                        f"'{table.name}'[{col.name}] is the many-side key of a relationship "
                        "and is visible — hide it; filter from the related table instead.",
                        "column", f"{table.name}[{col.name}]",
                    )
                )
    return findings


def _rule_measures_display_folders(model: Model) -> list[BpaFinding]:
    findings = []
    for table in model.tables:
        missing = sum(1 for m in table.measures if not m.is_hidden and not m.display_folder)
        if missing > 10:
            findings.append(
                _finding(
                    "LAYOUT_MEASURES_DF", "Model Layout", 1,
                    f"'{table.name}' has {missing} visible measures with no display "
                    "folder — consider organizing them.",
                    "table", table.name,
                )
            )
    return findings


def _rule_auto_date_time(model: Model) -> list[BpaFinding]:
    auto_tables = [t.name for t in model.tables if t.is_auto_date_table]
    if not auto_tables:
        return []
    return [
        _finding(
            "DIABLE_AUTO_DATE/TIME", "Model Layout", 3,
            f"{len(auto_tables)} table(s) were auto-generated by Power BI's 'Auto date/time' "
            f"feature ({', '.join(auto_tables)}) — disable it and use a shared Date table "
            "instead.",
            "model", model.name,
        )
    ]


# -- Naming Conventions ----------------------------------------------------------------

# Flags a name with more than one upper/lower-case "hump" (dimSales, mSalesAmount) while
# leaving a single leading-capital word (Sales) alone.
_CAMEL_CASE = re.compile(
    r"[A-Z]([A-Z0-9]*[a-z][a-z0-9]*[A-Z]|[a-z0-9]*[A-Z][A-Z0-9]*[a-z])[A-Za-z0-9]*"
)


def _is_camel_case(name: str) -> bool:
    return " " not in name and bool(_CAMEL_CASE.search(name))


def _starts_lowercase(name: str) -> bool:
    return bool(name) and name[0].islower()


def _rule_naming_conventions(model: Model) -> list[BpaFinding]:
    findings = []
    for table in model.tables:
        for name, obj_type, obj_name in [(table.name, "table", table.name)]:
            if _is_camel_case(name):
                findings.append(
                    _finding(
                        "NO_CAMELCASE_MEASURES_TABLES", "Naming Conventions", 2,
                        f"Table '{name}' uses CamelCase naming.", obj_type, obj_name,
                    )
                )
            if _starts_lowercase(name):
                findings.append(
                    _finding(
                        "UPPERCASE_FIRST_LETTER_MEASURES_TABLES", "Naming Conventions", 2,
                        f"Table '{name}' starts with a lowercase letter.", obj_type, obj_name,
                    )
                )

        for col in table.columns:
            if not col.is_hidden and _is_camel_case(col.name):
                findings.append(
                    _finding(
                        "NO_CAMELCASE_COLUMNS_HIERARCHIES", "Naming Conventions", 2,
                        f"Column '{table.name}'[{col.name}] uses CamelCase naming.",
                        "column", f"{table.name}[{col.name}]",
                    )
                )
            if not col.is_hidden and _starts_lowercase(col.name):
                findings.append(
                    _finding(
                        "UPPERCASE_FIRST_LETTER_COLUMNS_HIERARCHIES", "Naming Conventions", 2,
                        f"Column '{table.name}'[{col.name}] starts with a lowercase letter.",
                        "column", f"{table.name}[{col.name}]",
                    )
                )

        for hier in table.hierarchies:
            if not hier.is_hidden and _is_camel_case(hier.name):
                findings.append(
                    _finding(
                        "NO_CAMELCASE_COLUMNS_HIERARCHIES", "Naming Conventions", 2,
                        f"Hierarchy '{table.name}'[{hier.name}] uses CamelCase naming.",
                        "hierarchy", f"{table.name}[{hier.name}]",
                    )
                )

        for measure in table.measures:
            if not measure.is_hidden and _is_camel_case(measure.name):
                findings.append(
                    _finding(
                        "NO_CAMELCASE_MEASURES_TABLES", "Naming Conventions", 2,
                        f"Measure '{measure.name}' uses CamelCase naming.",
                        "measure", measure.name,
                    )
                )
            if not measure.is_hidden and _starts_lowercase(measure.name):
                findings.append(
                    _finding(
                        "UPPERCASE_FIRST_LETTER_MEASURES_TABLES", "Naming Conventions", 2,
                        f"Measure '{measure.name}' starts with a lowercase letter.",
                        "measure", measure.name,
                    )
                )
    return findings


def _rule_partition_names(model: Model) -> list[BpaFinding]:
    findings = []
    for table in model.tables:
        if table.kind is TableKind.CALCULATION_GROUP:
            continue
        partitions = table.partitions
        if len(partitions) == 1 and partitions[0].name != table.name:
            findings.append(
                _finding(
                    "PARTITION_NAMES_SHOULD_MATCH_TABLE_NAMES", "Naming Conventions", 1,
                    f"Table '{table.name}' has a single partition named "
                    f"'{partitions[0].name}' — it should match the table name.",
                    "partition", f"{table.name}.{partitions[0].name}",
                )
            )
        elif len(partitions) > 1:
            for partition in partitions:
                if not partition.name.startswith(table.name):
                    findings.append(
                        _finding(
                            "PARTITION_NAMES_SHOULD_MATCH_TABLE_NAMES", "Naming Conventions", 1,
                            f"Partition '{partition.name}' on '{table.name}' does not "
                            "start with the table name.",
                            "partition", f"{table.name}.{partition.name}",
                        )
                    )
    return findings


def _rule_relationship_column_names(model: Model) -> list[BpaFinding]:
    findings = []
    pair_counts = Counter((r.from_table, r.to_table) for r in model.relationships)
    for rel in model.relationships:
        count = pair_counts[(rel.from_table, rel.to_table)]
        if count == 1 and rel.from_column != rel.to_column:
            findings.append(
                _finding(
                    "RELATIONSHIP_COLUMN_NAMES", "Naming Conventions", 2,
                    f"The only relationship between '{rel.from_table}' and '{rel.to_table}' "
                    f"joins differently-named columns ({rel.from_column} -> {rel.to_column}).",
                    "relationship", rel.name,
                )
            )
        elif count > 1 and not rel.from_column.endswith(rel.to_column):
            findings.append(
                _finding(
                    "RELATIONSHIP_COLUMN_NAMES", "Naming Conventions", 2,
                    f"Of the {count} relationships between '{rel.from_table}' and "
                    f"'{rel.to_table}', '{rel.from_column}' does not end with "
                    f"'{rel.to_column}' — a consistent suffix convention makes multiple "
                    "relationships between the same two tables easier to tell apart.",
                    "relationship", rel.name,
                )
            )
    return findings


# -- Performance --------------------------------------------------------------------------


def _rule_avoid_single_attribute_dimensions(model: Model) -> list[BpaFinding]:
    used_in_relationships = {(r.from_table, r.from_column) for r in model.relationships}
    used_in_relationships |= {(r.to_table, r.to_column) for r in model.relationships}
    incoming_counts = Counter(r.to_table for r in model.relationships)

    findings = []
    for table in model.tables:
        unrelated_visible = [
            c for c in table.columns if not c.is_hidden and (table.name, c.name) not in used_in_relationships
        ]
        if len(unrelated_visible) <= 1 and incoming_counts[table.name] == 1:
            findings.append(
                _finding(
                    "AVOID_SINGLE_ATTRIBUTE_DIMENSIONS", "Performance", 2,
                    f"'{table.name}' has only one real attribute and a single incoming "
                    "relationship — consider moving that attribute onto the fact table "
                    "instead of keeping a separate dimension.",
                    "table", table.name,
                )
            )
    return findings


def _rule_unused_measures(model: Model) -> list[BpaFinding]:
    return [
        _finding(
            "PERF_UNUSED_MEASURES", "Performance", 1,
            f"Hidden measure '{measure.name}' is not referenced by any other measure — "
            "consider removing it.",
            "measure", measure.name,
        )
        for measure in model.all_measures
        if measure.is_hidden and not measure.referenced_by
    ]


def _rule_unused_columns(model: Model) -> list[BpaFinding]:
    referenced = {(d.table, d.column) for m in model.all_measures for d in m.depends_on if d.column}
    used_in_relationships = {(r.from_table, r.from_column) for r in model.relationships}
    used_in_relationships |= {(r.to_table, r.to_column) for r in model.relationships}
    sort_by_targets = {
        (table.name, col.sort_by_column) for table in model.tables for col in table.columns if col.sort_by_column
    }
    hierarchy_levels = {
        (table.name, level) for table in model.tables for hier in table.hierarchies for level in hier.levels
    }

    findings = []
    for table in model.tables:
        for col in table.columns:
            key = (table.name, col.name)
            if (
                col.is_hidden
                and key not in referenced
                and key not in used_in_relationships
                and key not in sort_by_targets
                and key not in hierarchy_levels
            ):
                findings.append(
                    _finding(
                        "PERF_UNUSED_COLUMNS", "Performance", 2,
                        f"Hidden column '{table.name}'[{col.name}] has no DAX references, "
                        "relationships, sort-by usage, or hierarchy usage — likely safe to "
                        "remove.",
                        "column", f"{table.name}[{col.name}]",
                    )
                )
    return findings


_RULES = [
    _rule_dax_todo,
    _rule_dax_division,
    _rule_dax_qualification,
    _rule_format_string_columns,
    _rule_format_string_measures,
    _rule_avoid_float,
    _rule_summarize_none,
    _rule_columns_hierarchies_display_folders,
    _rule_hide_fk_columns,
    _rule_measures_display_folders,
    _rule_auto_date_time,
    _rule_naming_conventions,
    _rule_partition_names,
    _rule_relationship_column_names,
    _rule_avoid_single_attribute_dimensions,
    _rule_unused_measures,
    _rule_unused_columns,
]


def run_bpa(model: Model) -> list[BpaFinding]:
    """Run every implemented rule and return the combined, unsorted findings list."""
    findings: list[BpaFinding] = []
    for rule in _RULES:
        findings.extend(rule(model))
    return findings
