"""Tests for narrative identifier validation.

This is the check that keeps generated narrative honest, regardless of who authored it
— it must catch a misnamed table/column/measure whether the mistake came from a hand-
written file, a Claude Code session, or a future automated enrichment pass.
"""

import json
import pathlib

import pytest

from semdoc.ir.build import tmsl_to_model
from semdoc.ir.schema import (
    AnsweredQuestion,
    Gotcha,
    MeasureNarrative,
    ModelIR,
    Narrative,
    Ref,
    ReportRecipe,
    TableNarrative,
)
from semdoc.validate import validate_identifiers

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "sample_tmsl.json"


@pytest.fixture(scope="module")
def model():
    tmsl = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return tmsl_to_model(tmsl, name="Case Services Analytics", workspace="Analytics")


def test_valid_narrative_passes_with_zero_failures(model):
    narrative = Narrative(
        model_purpose="Tracks services.",
        tables={"Service Fact": TableNarrative(business_description="Service events.")},
        measures={"Total Units": MeasureNarrative(plain_english="Sums Units.")},
        questions_answered=[
            AnsweredQuestion(
                question="How many units?",
                approach="Use Total Units.",
                fields=[Ref(measure="Total Units"), Ref(table="Client", column="Client Name")],
            )
        ],
        gotchas=[
            Gotcha(
                title="Inactive relationship",
                detail="Enrollment Date needs USERELATIONSHIP.",
                affects=[Ref(table="Service Fact", column="Enrollment Date")],
            )
        ],
        report_recipes=[
            ReportRecipe(
                requirement="Units by client",
                fields=[Ref(table="Client", column="Client Name")],
                measures=[Ref(measure="Total Units")],
            )
        ],
    )

    report = validate_identifiers(model, narrative)

    assert report.ok
    assert report.identifiers_failed == []
    assert report.identifiers_checked > 0


def test_unknown_table_key_is_caught(model):
    narrative = Narrative(tables={"Nonexistent Table": TableNarrative(business_description="x")})
    report = validate_identifiers(model, narrative)
    assert not report.ok
    assert any("Nonexistent Table" in f for f in report.identifiers_failed)


def test_unknown_measure_key_is_caught(model):
    narrative = Narrative(measures={"Made Up Measure": MeasureNarrative(plain_english="x")})
    report = validate_identifiers(model, narrative)
    assert not report.ok
    assert any("Made Up Measure" in f for f in report.identifiers_failed)


def test_unknown_column_in_a_ref_is_caught(model):
    narrative = Narrative(
        gotchas=[
            Gotcha(
                title="x",
                detail="x",
                affects=[Ref(table="Service Fact", column="NotARealColumn")],
            )
        ]
    )
    report = validate_identifiers(model, narrative)
    assert not report.ok
    assert any("NotARealColumn" in f for f in report.identifiers_failed)


def test_unknown_measure_in_a_ref_is_caught(model):
    narrative = Narrative(
        questions_answered=[
            AnsweredQuestion(question="x", approach="x", fields=[Ref(measure="Fake Measure")])
        ]
    )
    report = validate_identifiers(model, narrative)
    assert not report.ok


def test_table_only_ref_is_valid_without_a_column(model):
    # Gotchas often refer to a whole table (e.g. "no relationships"), not a column.
    narrative = Narrative(
        gotchas=[Gotcha(title="x", detail="x", affects=[Ref(table="Targets")])]
    )
    report = validate_identifiers(model, narrative)
    assert report.ok


def test_empty_narrative_has_nothing_to_check(model):
    report = validate_identifiers(model, Narrative())
    assert report.ok
    assert report.identifiers_checked == 0


def test_ir_round_trips_narrative_and_validation_through_json(model):
    narrative = Narrative(model_purpose="x")
    ir = ModelIR(model=model, narrative=narrative, validation=validate_identifiers(model, narrative))
    restored = ModelIR.model_validate_json(json.dumps(ir.model_dump(mode="json")))
    assert restored.narrative.model_purpose == "x"
    assert restored.validation.ok
