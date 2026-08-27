"""Tests for TMSL -> IR normalization.

These guard the deterministic lane. If any of these regress, the generated
documentation starts telling analysts things that are not true.
"""

import json
import pathlib

import pytest

from semdoc.ir.build import tmsl_to_model
from semdoc.ir.schema import SourceKind, StorageMode, TableKind

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "sample_tmsl.json"


@pytest.fixture(scope="module")
def model():
    tmsl = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return tmsl_to_model(tmsl, name="Case Services Analytics", workspace="Analytics")


def test_model_level_metadata(model):
    assert model.name == "Case Services Analytics"
    assert model.workspace == "Analytics"
    assert model.culture == "en-US"
    assert model.description == "Service delivery and client enrollment reporting."
    # DirectLake tables plus imported ones.
    assert model.storage_mode is StorageMode.MIXED
    assert len(model.tables) == 5


def test_multiline_description_is_joined(model):
    fact = model.table("Service Fact")
    assert fact.description == (
        "One row per service event delivered to a client.\nGrain: service event."
    )


def test_table_classification_from_relationship_graph(model):
    assert model.table("Service Fact").kind is TableKind.FACT
    assert model.table("Client").kind is TableKind.DIMENSION
    assert model.table("Program").kind is TableKind.DIMENSION
    assert model.table("Date").kind is TableKind.DIMENSION
    # Nothing points at Targets, so it is disconnected — worth flagging to authors.
    assert model.table("Targets").kind is TableKind.DISCONNECTED


def test_date_table_is_detected(model):
    assert model.table("Date").is_date_table is True
    assert model.table("Client").is_date_table is False


def test_directlake_lineage_to_warehouse(model):
    source = model.table("Service Fact").source
    assert source.kind is SourceKind.DIRECT_LAKE
    assert source.warehouse_schema == "dbo"
    assert source.warehouse_table == "fact_service"


def test_import_lineage_recovered_from_m_expression(model):
    source = model.table("Program").source
    assert source.kind is SourceKind.M_QUERY
    assert source.warehouse_schema == "dbo"
    assert source.warehouse_table == "dim_program"


def test_calculated_table_has_no_warehouse_lineage(model):
    source = model.table("Targets").source
    assert source.kind is SourceKind.CALCULATED
    assert source.warehouse_table is None


def test_calculated_table_expression_lines_are_joined(model):
    # TMSL returns multi-line calculated-table expressions as an array of lines, the
    # same shape as descriptions and measure DAX. A real HMIS model hit this; the
    # fixture didn't, because it originally used a single-line expression string.
    source = model.table("Targets").source
    assert source.expression == 'DATATABLE (\n    "Metric", STRING, "Target Value", DOUBLE,\n    { { "Units", 1000.0 } }\n)'


def test_storage_mode_per_table(model):
    assert model.table("Service Fact").storage_mode is StorageMode.DIRECT_LAKE
    assert model.table("Program").storage_mode is StorageMode.IMPORT


def test_inactive_relationship_is_flagged(model):
    inactive = [r for r in model.relationships if not r.is_active]
    assert len(inactive) == 1
    assert inactive[0].from_column == "Enrollment Date"
    assert inactive[0].relies_on_userelationship is True

    # Relationships omit isActive when true; make sure we default correctly.
    active = [r for r in model.relationships if r.is_active]
    assert len(active) == 3


def test_relationship_cardinality_defaults(model):
    rel = next(r for r in model.relationships if r.from_column == "ClientKey")
    assert rel.from_cardinality == "many"
    assert rel.to_cardinality == "one"


def test_measure_dependencies_resolve_measure_references(model):
    avg = model.measure("Avg Units per Service")
    depends = {str(d) for d in avg.depends_on}
    assert depends == {"[Total Units]", "[Service Count]"}


def test_measure_dependencies_resolve_column_references(model):
    total = model.measure("Total Units")
    assert {str(d) for d in total.depends_on} == {"'Service Fact'[Units]"}


def test_measure_dependencies_span_tables(model):
    yoy = model.measure("Units YoY %")
    depends = {str(d) for d in yoy.depends_on}
    assert "[Total Units]" in depends
    assert "'Date'[Date]" in depends


def test_measure_dependencies_match_dax_case_insensitively():
    # DAX identifiers are case-insensitive in the engine — a measure referencing
    # 'service fact'[units] (lowercase) against a column actually named
    # 'Service Fact'[Units] still deploys and runs. A real HMIS model shipped exactly
    # this shape and a case-sensitive matcher silently dropped the dependency.
    # Canonical casing from the model, not the DAX author's typing, must show up in
    # the resolved Ref. Uses its own model, not the shared fixture, since resolving
    # dependencies mutates measures in place.
    from semdoc.ir.build import _resolve_measure_dependencies
    from semdoc.ir.schema import Column, Measure, Model, Table

    isolated = Model(
        name="Case",
        tables=[
            Table(
                name="Service Fact",
                columns=[Column(name="Units")],
                measures=[
                    Measure(name="Total Units", expression="SUM('Service Fact'[Units])"),
                    Measure(
                        name="Total Units Lower",
                        expression="SUM('service fact'[units])",
                    ),
                ],
            )
        ],
    )
    _resolve_measure_dependencies(isolated)

    measure = isolated.measure("Total Units Lower")
    assert [str(d) for d in measure.depends_on] == ["'Service Fact'[Units]"]


def test_dax_comments_do_not_create_references(model):
    # "Avg Units per Service" has a trailing `--` comment and a `//` line comment.
    # Neither should contribute references.
    avg = model.measure("Avg Units per Service")
    assert all(d.measure in {"Total Units", "Service Count"} for d in avg.depends_on)


def test_reverse_dependency_graph(model):
    total = model.measure("Total Units")
    referenced_by = {str(r) for r in total.referenced_by}
    assert referenced_by == {"[Avg Units per Service]", "[Units YoY %]"}

    count = model.measure("Service Count")
    assert {str(r) for r in count.referenced_by} == {"[Avg Units per Service]"}


def test_hierarchy_levels(model):
    calendar = model.table("Date").hierarchies[0]
    assert calendar.name == "Calendar"
    assert calendar.levels == ["Year", "Month Name"]


def test_sort_by_column_captured(model):
    age_band = model.table("Client").column("Age Band")
    assert age_band.sort_by_column == "Age Band Sort"


def test_rls_role_extracted(model):
    assert len(model.roles) == 1
    role = model.roles[0]
    assert role.name == "County Staff"
    assert role.model_permission == "read"
    assert len(role.table_permissions) == 1
    assert role.table_permissions[0].table == "Client"
    assert "USERPRINCIPALNAME" in role.table_permissions[0].filter_expression
