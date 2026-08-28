"""Tests for the column stats module's pure functions.

The DAX-executing parts need a live connection and aren't tested here — same split as
test_warehouse.py: query construction and result parsing are pure functions, fully
testable without Fabric.
"""

from semdoc.ir.schema import Column, Model, Table, TableKind
from semdoc.stats import (
    _apply_result_row,
    _build_query,
    _chunk,
    _dax_column_ref,
    _dax_string_literal,
    profilable_columns,
)


def test_profilable_columns_skips_hidden_tables():
    model = Model(name="M", tables=[Table(name="T", is_hidden=True, columns=[Column(name="A")])])
    assert profilable_columns(model) == []


def test_profilable_columns_skips_calculation_group_tables():
    model = Model(
        name="M",
        tables=[Table(name="T", kind=TableKind.CALCULATION_GROUP, columns=[Column(name="A")])],
    )
    assert profilable_columns(model) == []


def test_profilable_columns_skips_hidden_columns_but_keeps_visible_ones():
    table = Table(name="T", columns=[Column(name="Visible"), Column(name="Hidden", is_hidden=True)])
    model = Model(name="M", tables=[table])
    [(returned_table, columns)] = profilable_columns(model)
    assert returned_table is table
    assert [c.name for c in columns] == ["Visible"]


def test_profilable_columns_omits_table_with_no_visible_columns():
    table = Table(name="T", columns=[Column(name="Hidden", is_hidden=True)])
    model = Model(name="M", tables=[table])
    assert profilable_columns(model) == []


def test_chunk_splits_into_requested_size():
    assert _chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


def test_chunk_empty_list():
    assert _chunk([], 10) == []


# -- DAX construction -----------------------------------------------------------------


def test_dax_string_literal_escapes_quotes():
    assert _dax_string_literal('Say "hi"') == '"Say ""hi"""'


def test_dax_column_ref_escapes_table_quote_and_column_bracket():
    assert _dax_column_ref("O'Brien", "A]B") == "'O''Brien'[A]]B]"


def test_build_query_includes_a_var_and_row_entry_per_column():
    cols = [Column(name="County"), Column(name="Program")]
    query = _build_query("Client", cols, sample_threshold=50)
    assert "VAR __c0 = DISTINCTCOUNT('Client'[County])" in query
    assert "VAR __c1 = DISTINCTCOUNT('Client'[Program])" in query
    assert '"County__card", __c0' in query
    assert '"Program__card", __c1' in query
    assert "CONCATENATEX(VALUES('Client'[County])" in query
    assert query.startswith("EVALUATE")


def test_build_query_uses_the_given_sample_threshold():
    query = _build_query("T", [Column(name="A")], sample_threshold=7)
    assert "__c0 <= 7" in query


# -- result parsing --------------------------------------------------------------------


def test_apply_result_row_sets_cardinality_and_samples():
    columns = [Column(name="County"), Column(name="Program")]
    row = {
        "County__card": 3,
        "County__sample": "Alameda|~|Contra Costa|~|Marin",
        "Program__card": 500,
        "Program__sample": None,
    }
    _apply_result_row(columns, row)
    assert columns[0].cardinality == 3
    assert columns[0].sample_values == ["Alameda", "Contra Costa", "Marin"]
    assert columns[1].cardinality == 500
    assert columns[1].sample_values == []


def test_apply_result_row_handles_bracket_wrapped_keys():
    # A real quirk of the executeQueries endpoint: ROW()-produced names sometimes come
    # back wrapped in brackets rather than bare.
    columns = [Column(name="County")]
    row = {"[County__card]": 3, "[County__sample]": "A|~|B"}
    _apply_result_row(columns, row)
    assert columns[0].cardinality == 3
    assert columns[0].sample_values == ["A", "B"]


def test_apply_result_row_missing_key_leaves_column_unset():
    columns = [Column(name="County")]
    _apply_result_row(columns, {})
    assert columns[0].cardinality is None
    assert columns[0].sample_values == []
