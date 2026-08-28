"""Tests for the reports module's pure functions.

Everything here is exercised against hand-built JSON matching the real legacy
`report.json` shapes confirmed live against the project's actual target tenant (see
`semdoc.reports`'s module docstring) — never invented shapes for a format this project
has not actually seen. The Fabric-calling orchestration (`extract_report_usage`'s use of
`FabricClient`) needs a live connection and isn't tested here, same split as
`test_warehouse.py` and `test_stats.py`.
"""

import json

from semdoc.ir.schema import Column, Measure, Model, Ref, Table
from semdoc.reports import _to_ref, parse_legacy_report_json

# -- parse_legacy_report_json -----------------------------------------------------------


def _visual_container(select: list[dict], from_: list[dict], filters: list[dict] | None = None) -> dict:
    config = {
        "singleVisual": {
            "visualType": "clusteredBarChart",
            "prototypeQuery": {"Version": 2, "From": from_, "Select": select},
        }
    }
    vc: dict = {"config": json.dumps(config)}
    if filters is not None:
        vc["filters"] = json.dumps(filters)
    return vc


def _column_select(alias: str, prop: str, name: str) -> dict:
    return {"Column": {"Expression": {"SourceRef": {"Source": alias}}, "Property": prop}, "Name": name}


def _measure_select(alias: str, prop: str, name: str) -> dict:
    return {"Measure": {"Expression": {"SourceRef": {"Source": alias}}, "Property": prop}, "Name": name}


def test_parses_a_column_and_measure_off_one_visual():
    report = {
        "sections": [
            {
                "displayName": "Page 1",
                "visualContainers": [
                    _visual_container(
                        select=[
                            _column_select("h1", "SchoolStatus", "hmis EmploymentEducation.SchoolStatus"),
                            _measure_select("*", "(CMF)Client", "*HMIS_Measures.(CMF)Client"),
                        ],
                        from_=[
                            {"Name": "h1", "Entity": "hmis EmploymentEducation", "Type": 0},
                            {"Name": "*", "Entity": "*HMIS_Measures", "Type": 0},
                        ],
                    )
                ],
            }
        ]
    }
    pages, fields = parse_legacy_report_json(json.dumps(report))
    assert pages == ["Page 1"]
    assert ("hmis EmploymentEducation", "SchoolStatus", False) in fields
    assert ("*HMIS_Measures", "(CMF)Client", True) in fields


def test_unwraps_a_column_wrapped_in_aggregation():
    select = [
        {
            "Aggregation": {
                "Expression": {"Column": {"Expression": {"SourceRef": {"Source": "h"}}, "Property": "AgeAtEnrollment"}},
                "Function": 1,
            },
            "Name": "Sum(hmis Enrollment.AgeAtEnrollment)",
        }
    ]
    report = {
        "sections": [
            {
                "displayName": "P",
                "visualContainers": [
                    _visual_container(select, from_=[{"Name": "h", "Entity": "hmis Enrollment", "Type": 0}])
                ],
            }
        ]
    }
    _, fields = parse_legacy_report_json(json.dumps(report))
    assert fields == [("hmis Enrollment", "AgeAtEnrollment", False)]


def test_report_level_filter_uses_entity_directly_not_an_alias():
    # Real quirk found live: a filter's own top-level `expression` names the table via
    # `SourceRef.Entity` directly, unlike a visual's `Select`, which names it via an
    # aliased `SourceRef.Source` resolved through the query's `From` list.
    report = {
        "filters": json.dumps(
            [{"name": "F1", "expression": {"Column": {"Expression": {"SourceRef": {"Entity": "hmis Project"}}, "Property": "DateDeleted"}}}]
        ),
        "sections": [],
    }
    _, fields = parse_legacy_report_json(json.dumps(report))
    assert fields == [("hmis Project", "DateDeleted", False)]


def test_page_and_visual_level_filters_are_both_collected():
    report = {
        "sections": [
            {
                "displayName": "P",
                "filters": json.dumps(
                    [{"name": "F", "expression": {"Column": {"Expression": {"SourceRef": {"Entity": "hmis Enrollment"}}, "Property": "AgeGroup"}}}]
                ),
                "visualContainers": [
                    _visual_container(
                        select=[],
                        from_=[],
                        filters=[
                            {
                                "name": "F2",
                                "expression": {"Measure": {"Expression": {"SourceRef": {"Entity": "*HMIS_Measures"}}, "Property": "Enrollments"}},
                            }
                        ],
                    )
                ],
            }
        ]
    }
    _, fields = parse_legacy_report_json(json.dumps(report))
    assert ("hmis Enrollment", "AgeGroup", False) in fields
    assert ("*HMIS_Measures", "Enrollments", True) in fields


def test_unrecognized_field_shape_is_skipped_not_guessed():
    select = [{"HierarchyLevel": {"Expression": {}}, "Name": "Date Hierarchy.Year"}]
    report = {"sections": [{"displayName": "P", "visualContainers": [_visual_container(select, from_=[])]}]}
    _, fields = parse_legacy_report_json(json.dumps(report))
    assert fields == []


def test_malformed_visual_config_is_skipped():
    report = {
        "sections": [
            {"displayName": "P", "visualContainers": [{"config": "not json"}]},
        ]
    }
    pages, fields = parse_legacy_report_json(json.dumps(report))
    assert pages == ["P"]
    assert fields == []


def test_no_filters_key_is_fine():
    report = {"sections": [{"displayName": "P", "visualContainers": []}]}
    pages, fields = parse_legacy_report_json(json.dumps(report))
    assert pages == ["P"]
    assert fields == []


# -- _to_ref ------------------------------------------------------------------------------


def _model() -> Model:
    return Model(
        name="M",
        tables=[Table(name="hmis Enrollment", columns=[Column(name="AgeGroup")], measures=[Measure(name="Enrollments", expression="1")])],
    )


def test_to_ref_resolves_an_existing_column():
    assert _to_ref(_model(), "hmis Enrollment", "AgeGroup", False) == Ref(table="hmis Enrollment", column="AgeGroup")


def test_to_ref_resolves_an_existing_measure_ignoring_its_reported_table():
    # A measure's reported "table" in a report is whichever table it happened to be
    # queried through (often a bare measures-holder table) — irrelevant to resolution,
    # since `Model.measure` looks a measure up by name across every table.
    assert _to_ref(_model(), "*HMIS_Measures", "Enrollments", True) == Ref(measure="Enrollments")


def test_to_ref_drops_a_renamed_or_removed_column():
    assert _to_ref(_model(), "hmis Enrollment", "NoLongerExists", False) is None


def test_to_ref_drops_a_reference_to_an_unknown_table():
    assert _to_ref(_model(), "hmis NoSuchTable", "AgeGroup", False) is None


def test_to_ref_drops_a_renamed_or_removed_measure():
    assert _to_ref(_model(), "*HMIS_Measures", "NoLongerExists", True) is None


def test_to_ref_resolves_a_column_that_only_differs_by_case():
    # Real drift found live: a report named `hmis Enrollment.CurrentYearActiveEnrollment`,
    # the model's actual column is `CurrentyearActiveEnrollment` (lowercase y).
    assert _to_ref(_model(), "hmis Enrollment", "agegroup", False) == Ref(table="hmis Enrollment", column="AgeGroup")


def test_to_ref_resolves_a_measure_that_only_differs_by_case():
    assert _to_ref(_model(), "*HMIS_Measures", "enrollments", True) == Ref(measure="Enrollments")


def test_to_ref_resolves_a_table_that_only_differs_by_case():
    assert _to_ref(_model(), "HMIS ENROLLMENT", "AgeGroup", False) == Ref(table="hmis Enrollment", column="AgeGroup")
