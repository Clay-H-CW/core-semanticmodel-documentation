"""Tests for the warehouse module's pure functions.

These cover everything that doesn't need a live SQL connection: which objects the model
actually references, and the best-effort FROM/JOIN extraction out of view SQL. The
connection/query functions (`connect`, `_fetch_table`) are exercised only against a real
warehouse, not here — there is nothing pure-Python left to unit test once pyodbc is
involved.
"""

from semdoc.ir.schema import Model, SourceKind, Table, TableSource
from semdoc.warehouse import extract_referenced_tables, referenced_objects


def _table(name: str, schema: str | None = None, wh_table: str | None = None) -> Table:
    source = TableSource(kind=SourceKind.DIRECT_LAKE, warehouse_schema=schema, warehouse_table=wh_table)
    return Table(name=name, source=source)


def test_referenced_objects_collects_distinct_schema_table_pairs():
    model = Model(
        name="M",
        tables=[
            _table("A", "hmis", "vDM_A"),
            _table("B", "hmis", "vDM_B"),
            _table("C"),  # no lineage resolved - contributes nothing
        ],
    )
    assert referenced_objects(model) == [("hmis", "vDM_A"), ("hmis", "vDM_B")]


def test_referenced_objects_deduplicates_shared_sources():
    # Two model tables occasionally read the same warehouse object (e.g. one filtered).
    model = Model(name="M", tables=[_table("A", "hmis", "vDM_X"), _table("B", "hmis", "vDM_X")])
    assert referenced_objects(model) == [("hmis", "vDM_X")]


def test_referenced_objects_empty_model():
    assert referenced_objects(Model(name="M", tables=[])) == []


# -- extract_referenced_tables -----------------------------------------------------------


def test_extracts_schema_qualified_from_and_join():
    sql = """
    SELECT a.*, b.Name
    FROM hmis.Enrollment a
    JOIN hmis.Client b ON a.ClientKey = b.ClientKey
    """
    assert extract_referenced_tables(sql, default_schema="hmis") == ["hmis.Enrollment", "hmis.Client"]


def test_unqualified_reference_resolves_to_default_schema():
    sql = "SELECT * FROM Enrollment"
    assert extract_referenced_tables(sql, default_schema="hmis") == ["hmis.Enrollment"]


def test_bracketed_identifiers_are_handled():
    sql = "SELECT * FROM [hmis].[Enrollment] JOIN [hmis].[Client] ON 1 = 1"
    assert extract_referenced_tables(sql, default_schema="hmis") == ["hmis.Enrollment", "hmis.Client"]


def test_cte_names_are_excluded_from_results():
    sql = """
    WITH RecentEnrollments AS (
        SELECT * FROM hmis.Enrollment WHERE EntryDate > '2024-01-01'
    )
    SELECT * FROM RecentEnrollments JOIN hmis.Client c ON 1 = 1
    """
    result = extract_referenced_tables(sql, default_schema="hmis")
    assert "hmis.RecentEnrollments" not in result
    assert result == ["hmis.Enrollment", "hmis.Client"]


def test_multiple_ctes_are_all_excluded():
    sql = """
    WITH A AS (SELECT 1 AS x), B AS (SELECT 2 AS y)
    SELECT * FROM A JOIN B ON 1 = 1 JOIN hmis.Real r ON 1 = 1
    """
    result = extract_referenced_tables(sql, default_schema="hmis")
    assert result == ["hmis.Real"]


def test_line_comment_does_not_create_a_false_reference():
    sql = "SELECT * FROM hmis.Enrollment -- FROM hmis.FakeTable\n"
    assert extract_referenced_tables(sql, default_schema="hmis") == ["hmis.Enrollment"]


def test_block_comment_does_not_create_a_false_reference():
    sql = "SELECT * FROM hmis.Enrollment /* JOIN hmis.FakeTable */"
    assert extract_referenced_tables(sql, default_schema="hmis") == ["hmis.Enrollment"]


def test_string_literal_containing_keywords_is_not_matched():
    sql = "SELECT * FROM hmis.Enrollment WHERE Note = 'select x from y'"
    assert extract_referenced_tables(sql, default_schema="hmis") == ["hmis.Enrollment"]


def test_duplicate_references_are_deduplicated_case_insensitively():
    sql = "SELECT * FROM hmis.Enrollment e1 JOIN HMIS.ENROLLMENT e2 ON 1=1"
    assert extract_referenced_tables(sql, default_schema="hmis") == ["hmis.Enrollment"]


def test_no_from_or_join_returns_empty():
    assert extract_referenced_tables("SELECT 1", default_schema="hmis") == []
