"""Tests for the Best Practice Analyzer port.

One positive and one negative case per rule where practical, using small isolated
models built directly from the schema classes rather than the shared fixture — these
tests are about a single rule's logic, not about normalization, so they should not
break every time the fixture changes for an unrelated reason.
"""

from semdoc.bpa import run_bpa
from semdoc.ir.schema import (
    Column,
    Hierarchy,
    Measure,
    Model,
    Partition,
    Ref,
    Relationship,
    Table,
    TableKind,
    TableSource,
)


def _ids(model: Model, rule_id: str | None = None) -> list[str]:
    findings = run_bpa(model)
    if rule_id is None:
        return [f.rule_id for f in findings]
    return [f.object_name for f in findings if f.rule_id == rule_id]


# -- DAX_TODO -----------------------------------------------------------------------------


def test_dax_todo_flags_measure_containing_todo():
    m = Model(name="M", tables=[Table(name="T", measures=[Measure(name="X", expression="1 -- TODO fix")])])
    assert "DAX_TODO" in _ids(m)


def test_dax_todo_ignores_clean_measure():
    m = Model(name="M", tables=[Table(name="T", measures=[Measure(name="X", expression="SUM(T[A])")])])
    assert "DAX_TODO" not in _ids(m)


# -- DAX_DIVISION_COLUMNS -------------------------------------------------------------------


def test_division_flags_non_constant_denominator():
    m = Model(name="M", tables=[Table(name="T", measures=[Measure(name="X", expression="[A]/[B]")])])
    assert "DAX_DIVISION_COLUMNS" in _ids(m)


def test_division_allows_divide_function():
    m = Model(name="M", tables=[Table(name="T", measures=[Measure(name="X", expression="DIVIDE([A],[B])")])])
    assert "DAX_DIVISION_COLUMNS" not in _ids(m)


def test_division_allows_constant_denominator():
    m = Model(name="M", tables=[Table(name="T", measures=[Measure(name="X", expression="[A]/100")])])
    assert "DAX_DIVISION_COLUMNS" not in _ids(m)


def test_division_ignores_a_slash_inside_a_bracketed_reference_name():
    # Real false positive found against the HMIS model: a measure named with a literal
    # "/" (e.g. "Adult/Child Household Filtered") that referenced it in brackets was
    # flagged, even though the measure's actual DAX used DIVIDE() correctly throughout.
    expr = "DIVIDE([Some Measure (Adult/Child Household Filtered)], [Other Measure])"
    m = Model(name="M", tables=[Table(name="T", measures=[Measure(name="X", expression=expr)])])
    assert "DAX_DIVISION_COLUMNS" not in _ids(m)


# -- DAX_COLUMNS_FULLY_QUALIFIED / DAX_MEASURES_UNQUALIFIED --------------------------------


def test_unqualified_column_reference_is_flagged():
    m = Model(
        name="M",
        tables=[Table(name="T", columns=[Column(name="A")], measures=[Measure(name="X", expression="SUM([A])")])],
    )
    assert "DAX_COLUMNS_FULLY_QUALIFIED" in _ids(m)


def test_qualified_column_reference_is_not_flagged():
    m = Model(
        name="M",
        tables=[
            Table(name="T", columns=[Column(name="A")], measures=[Measure(name="X", expression="SUM('T'[A])")])
        ],
    )
    assert "DAX_COLUMNS_FULLY_QUALIFIED" not in _ids(m)


def test_qualified_measure_reference_is_flagged():
    m = Model(
        name="M",
        tables=[
            Table(
                name="T",
                measures=[
                    Measure(name="Base", expression="1"),
                    Measure(name="X", expression="'T'[Base] + 1"),
                ],
            )
        ],
    )
    assert "DAX_MEASURES_UNQUALIFIED" in _ids(m)


def test_unqualified_measure_reference_is_not_flagged():
    m = Model(
        name="M",
        tables=[
            Table(
                name="T",
                measures=[
                    Measure(name="Base", expression="1"),
                    Measure(name="X", expression="[Base] + 1"),
                ],
            )
        ],
    )
    assert "DAX_MEASURES_UNQUALIFIED" not in _ids(m)


# -- Formatting / Metadata ------------------------------------------------------------------


def test_visible_numeric_column_without_format_string_is_flagged():
    m = Model(name="M", tables=[Table(name="T", columns=[Column(name="A", data_type="int64")])])
    assert "APPLY_FORMAT_STRING_COLUMNS" in _ids(m)


def test_column_with_format_string_is_not_flagged():
    m = Model(
        name="M",
        tables=[Table(name="T", columns=[Column(name="A", data_type="int64", format_string="#,0")])],
    )
    assert "APPLY_FORMAT_STRING_COLUMNS" not in _ids(m)


def test_hidden_column_without_format_string_is_not_flagged():
    m = Model(name="M", tables=[Table(name="T", columns=[Column(name="A", data_type="int64", is_hidden=True)])])
    assert "APPLY_FORMAT_STRING_COLUMNS" not in _ids(m)


def test_visible_measure_without_format_string_is_flagged():
    m = Model(name="M", tables=[Table(name="T", measures=[Measure(name="X", expression="1")])])
    assert "APPLY_FORMAT_STRING_MEASURES" in _ids(m)


def test_double_column_triggers_avoid_float():
    m = Model(name="M", tables=[Table(name="T", columns=[Column(name="A", data_type="double")])])
    assert "META_AVOID_FLOAT" in _ids(m)


def test_decimal_column_does_not_trigger_avoid_float():
    m = Model(name="M", tables=[Table(name="T", columns=[Column(name="A", data_type="decimal")])])
    assert "META_AVOID_FLOAT" not in _ids(m)


def test_visible_numeric_column_summarized_is_flagged():
    m = Model(
        name="M",
        tables=[Table(name="T", columns=[Column(name="A", data_type="int64", summarize_by="sum")])],
    )
    assert "META_SUMMARIZE_NONE" in _ids(m)


def test_visible_numeric_column_summarize_none_is_not_flagged():
    m = Model(
        name="M",
        tables=[Table(name="T", columns=[Column(name="A", data_type="int64", summarize_by="none")])],
    )
    assert "META_SUMMARIZE_NONE" not in _ids(m)


# -- Model Layout ---------------------------------------------------------------------------


def test_more_than_ten_ungrouped_visible_columns_is_flagged():
    cols = [Column(name=f"C{i}") for i in range(11)]
    m = Model(name="M", tables=[Table(name="T", columns=cols)])
    assert "LAYOUT_COLUMNS_HIERARCHIES_DF" in _ids(m)


def test_ten_or_fewer_ungrouped_visible_columns_is_not_flagged():
    cols = [Column(name=f"C{i}") for i in range(10)]
    m = Model(name="M", tables=[Table(name="T", columns=cols)])
    assert "LAYOUT_COLUMNS_HIERARCHIES_DF" not in _ids(m)


def test_visible_many_side_relationship_column_is_flagged():
    m = Model(
        name="M",
        tables=[
            Table(name="Fact", columns=[Column(name="DimKey")]),
            Table(name="Dim", columns=[Column(name="DimKey")]),
        ],
        relationships=[Relationship(name="r", from_table="Fact", from_column="DimKey", to_table="Dim", to_column="DimKey")],
    )
    assert "LAYOUT_HIDE_FK_COLUMNS" in _ids(m)


def test_hidden_many_side_relationship_column_is_not_flagged():
    m = Model(
        name="M",
        tables=[
            Table(name="Fact", columns=[Column(name="DimKey", is_hidden=True)]),
            Table(name="Dim", columns=[Column(name="DimKey")]),
        ],
        relationships=[Relationship(name="r", from_table="Fact", from_column="DimKey", to_table="Dim", to_column="DimKey")],
    )
    assert "LAYOUT_HIDE_FK_COLUMNS" not in _ids(m)


def test_more_than_ten_ungrouped_visible_measures_is_flagged():
    measures = [Measure(name=f"M{i}", expression="1") for i in range(11)]
    m = Model(name="M", tables=[Table(name="T", measures=measures)])
    assert "LAYOUT_MEASURES_DF" in _ids(m)


def test_auto_date_table_is_flagged():
    m = Model(name="M", tables=[Table(name="LocalDateTable_abc", is_auto_date_table=True)])
    assert "DIABLE_AUTO_DATE/TIME" in _ids(m)


def test_no_auto_date_table_is_not_flagged():
    m = Model(name="M", tables=[Table(name="Date")])
    assert "DIABLE_AUTO_DATE/TIME" not in _ids(m)


# -- Naming Conventions -----------------------------------------------------------------


def test_camelcase_table_name_is_flagged():
    # The rule's own regex only matches a name with an internal "hump" starting from an
    # uppercase letter (SalesAmount, HRDepartment) — not a lowercase-prefixed name like
    # "dimSales", which the separate UPPERCASE_FIRST_LETTER rule below is meant to catch.
    # Verified against the literal regex from the source rule before writing this test.
    m = Model(name="M", tables=[Table(name="SalesAmount")])
    assert "NO_CAMELCASE_MEASURES_TABLES" in _ids(m)


def test_single_capitalized_word_table_name_is_not_camelcase():
    m = Model(name="M", tables=[Table(name="Sales")])
    assert "NO_CAMELCASE_MEASURES_TABLES" not in _ids(m)


def test_name_with_space_is_not_flagged_as_camelcase():
    # "Service Fact" contains internal caps but has a space, which the rule exempts.
    m = Model(name="M", tables=[Table(name="Service Fact")])
    assert "NO_CAMELCASE_MEASURES_TABLES" not in _ids(m)


def test_lowercase_first_letter_table_is_flagged():
    m = Model(name="M", tables=[Table(name="sales")])
    assert "UPPERCASE_FIRST_LETTER_MEASURES_TABLES" in _ids(m)


def test_single_partition_matching_table_name_is_not_flagged():
    m = Model(name="M", tables=[Table(name="T", partitions=[Partition(name="T")])])
    assert "PARTITION_NAMES_SHOULD_MATCH_TABLE_NAMES" not in _ids(m)


def test_single_partition_not_matching_table_name_is_flagged():
    m = Model(name="M", tables=[Table(name="T", partitions=[Partition(name="Other")])])
    assert "PARTITION_NAMES_SHOULD_MATCH_TABLE_NAMES" in _ids(m)


def test_single_relationship_between_tables_with_matching_columns_is_not_flagged():
    m = Model(
        name="M",
        tables=[Table(name="A"), Table(name="B")],
        relationships=[Relationship(name="r", from_table="A", from_column="Key", to_table="B", to_column="Key")],
    )
    assert "RELATIONSHIP_COLUMN_NAMES" not in _ids(m)


def test_single_relationship_with_mismatched_column_names_is_flagged():
    m = Model(
        name="M",
        tables=[Table(name="A"), Table(name="B")],
        relationships=[Relationship(name="r", from_table="A", from_column="Foo", to_table="B", to_column="Bar")],
    )
    assert "RELATIONSHIP_COLUMN_NAMES" in _ids(m)


# -- Performance ---------------------------------------------------------------------------


def test_single_attribute_dimension_is_flagged():
    m = Model(
        name="M",
        tables=[
            Table(name="Fact", columns=[Column(name="DimKey")]),
            Table(name="Dim", columns=[Column(name="DimKey"), Column(name="OnlyAttribute")]),
        ],
        relationships=[Relationship(name="r", from_table="Fact", from_column="DimKey", to_table="Dim", to_column="DimKey")],
    )
    assert "AVOID_SINGLE_ATTRIBUTE_DIMENSIONS" in _ids(m)


def test_hidden_unreferenced_measure_is_flagged_unused():
    m = Model(name="M", tables=[Table(name="T", measures=[Measure(name="X", expression="1", is_hidden=True)])])
    assert "PERF_UNUSED_MEASURES" in _ids(m)


def test_visible_measure_is_not_flagged_unused():
    m = Model(name="M", tables=[Table(name="T", measures=[Measure(name="X", expression="1")])])
    assert "PERF_UNUSED_MEASURES" not in _ids(m)


def test_hidden_referenced_measure_is_not_flagged_unused():
    m = Model(
        name="M",
        tables=[
            Table(
                name="T",
                measures=[
                    Measure(name="Base", expression="1", is_hidden=True, referenced_by=[Ref(measure="X")]),
                    Measure(name="X", expression="[Base] + 1"),
                ],
            )
        ],
    )
    assert "PERF_UNUSED_MEASURES" not in _ids(m)


def test_hidden_unreferenced_column_is_flagged_unused():
    m = Model(name="M", tables=[Table(name="T", columns=[Column(name="A", is_hidden=True)])])
    assert "PERF_UNUSED_COLUMNS" in _ids(m)


def test_hidden_column_used_in_relationship_is_not_flagged_unused():
    m = Model(
        name="M",
        tables=[Table(name="A", columns=[Column(name="Key", is_hidden=True)]), Table(name="B", columns=[Column(name="Key")])],
        relationships=[Relationship(name="r", from_table="A", from_column="Key", to_table="B", to_column="Key")],
    )
    assert "PERF_UNUSED_COLUMNS" not in _ids(m)


def test_hidden_column_used_in_hierarchy_is_not_flagged_unused():
    m = Model(
        name="M",
        tables=[
            Table(
                name="T",
                columns=[Column(name="MonthNum", is_hidden=True)],
                hierarchies=[Hierarchy(name="Cal", levels=["MonthNum"])],
            )
        ],
    )
    assert "PERF_UNUSED_COLUMNS" not in _ids(m)


# -- skipped rules are genuinely absent, not silently mis-triggered ------------------------


def test_no_findings_for_perspectives_or_translation_rules():
    # These are explicitly out of scope. Confirm none of their rule IDs ever appear.
    m = Model(name="M", tables=[Table(name="T", columns=[Column(name="A")])])
    ids = set(_ids(m))
    for skipped in [
        "LAYOUT_ADD_TO_PERSPECTIVES",
        "LAYOUT_LOCALIZE_DF",
        "TRANSLATE_DESCRIPTIONS",
        "TRANSLATE_HIDEABLE_OBJECT_NAMES",
        "TRANSLATE_HIERARCHY_LEVEL_NAMES",
        "TRANSLATE_OTHER_NAMES",
        "DISABLE_ATTRIBUTE_HIERACHIES",
        "SPECIFY_APPLICATION_NAME_IN_CONNECTION_STRING",
        "USE_MSOLEDBSQL_PROVIDER",
    ]:
        assert skipped not in ids


def test_run_bpa_on_empty_model_returns_no_findings():
    assert run_bpa(Model(name="M")) == []


def test_calculation_group_table_is_exempt_from_partition_naming():
    m = Model(
        name="M",
        tables=[Table(name="T", kind=TableKind.CALCULATION_GROUP, partitions=[Partition(name="Whatever")])],
    )
    assert "PARTITION_NAMES_SHOULD_MATCH_TABLE_NAMES" not in _ids(m)
