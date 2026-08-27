"""Tests for rendering.

These are mostly guards against the two failure modes that matter for a generated
document: a template that silently drops content, and a variant that leaks detail its
audience should not see.
"""

import json
import pathlib

import pytest

from semdoc.ir.build import tmsl_to_model
from semdoc.ir.schema import (
    AnsweredQuestion,
    MeasureNarrative,
    ModelIR,
    Narrative,
    Ref,
    ValidationReport,
)
from semdoc.render import diagrams
from semdoc.render.html import render_guide

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "sample_tmsl.json"


@pytest.fixture(scope="module")
def ir():
    tmsl = json.loads(FIXTURE.read_text(encoding="utf-8"))
    model = tmsl_to_model(tmsl, name="Case Services Analytics", workspace="Analytics")
    return ModelIR(model=model, generated_at="2026-08-27")


@pytest.fixture(scope="module")
def enriched_ir(ir):
    return ir.model_copy(
        update={
            "narrative": Narrative(
                model_purpose="Tracks service delivery against funded programs.",
                measures={
                    "Total Units": MeasureNarrative(
                        plain_english="Adds up billable units across the services in scope."
                    )
                },
                questions_answered=[
                    AnsweredQuestion(
                        question="How many units did each program deliver last quarter?",
                        approach="Put Program Name on rows and Total Units in values.",
                        fields=[Ref(table="Program", column="Program Name")],
                        dax="EVALUATE ROW ( \"u\", [Total Units] )",
                        dax_verified=True,
                    )
                ],
            ),
            "validation": ValidationReport(identifiers_checked=4, dax_snippets_checked=1),
        }
    )


# -- diagrams --------------------------------------------------------------------------


def test_star_schema_draws_filter_direction_not_key_direction(ir):
    mermaid = diagrams.star_schema(ir.model)
    # Filters flow from the dimension into the fact.
    assert 'Client -- "ClientKey" --> Service_Fact' in mermaid
    assert "Service_Fact -- " not in mermaid


def test_star_schema_marks_inactive_relationship_dashed(ir):
    mermaid = diagrams.star_schema(ir.model)
    assert 'Date -. "Enrollment Date (inactive)" .-> Service_Fact' in mermaid


def test_lineage_shows_mode_per_table(ir):
    mermaid = diagrams.warehouse_lineage(ir)
    assert 'wh_dbo_fact_service -- "DirectLake" --> sm_Service_Fact' in mermaid
    assert 'wh_dbo_dim_program -- "Import" --> sm_Program' in mermaid


def test_measure_dependency_diagram_omitted_when_flat():
    from semdoc.ir.schema import Measure, Model, Table

    flat = Model(
        name="Flat",
        tables=[Table(name="T", measures=[Measure(name="M", expression="1")])],
    )
    assert diagrams.measure_dependencies(flat) is None


def test_table_focus_returns_none_for_unrelated_table(ir):
    assert diagrams.table_focus(ir.model, "Targets") is None
    assert diagrams.table_focus(ir.model, "Nope") is None


# -- html ------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["technical", "business"])
def test_renders_without_unresolved_template_syntax(ir, variant):
    html = render_guide(ir, variant)
    assert "{{" not in html
    assert "{%" not in html
    assert "Undefined" not in html


def test_standalone_wraps_document_fragment_does_not(ir):
    assert render_guide(ir, "technical", standalone=True).startswith("<!doctype html>")

    fragment = render_guide(ir, "technical", standalone=False)
    assert "<!doctype" not in fragment.lower()
    assert "<body" not in fragment.lower()
    # The Artifact host reads the title from the fragment.
    assert "<title>Case Services Analytics</title>" in fragment


@pytest.mark.parametrize("variant", ["technical", "business"])
def test_inlined_css_is_not_html_escaped(ir, variant):
    """Autoescaping the stylesheet silently destroys it.

    `[data-theme="dark"]` escaped to `[data-theme=&quot;dark&quot;]` is an invalid
    selector, so the browser drops the whole rule — the dark theme and every quoted
    font-family vanish with no error anywhere. Caught only by looking at the page.
    """
    html = render_guide(ir, variant)

    assert ':root[data-theme="dark"]' in html
    assert ':root:not([data-theme="light"])' in html
    assert '"IBM Plex Sans"' in html
    assert "&quot;" not in html.split("</style>")[0]


def test_inline_mermaid_source_is_not_escaped(ir, monkeypatch):
    """Same hazard for the embedded bundle: escaping would corrupt the JavaScript."""
    monkeypatch.setattr(
        "semdoc.render.assets.fetch_mermaid",
        lambda **_: 'var x = "quoted" && 1 < 2;',
    )
    html = render_guide(ir, "technical", mermaid_mode="inline")
    assert 'var x = "quoted" && 1 < 2;' in html


def test_fragment_omits_mermaid_engine(ir):
    # Artifacts render Mermaid natively; shipping 3.5 MB of it would be waste.
    fragment = render_guide(ir, "technical", standalone=False)
    assert "vendor/mermaid.min.js" not in fragment
    assert "mermaid.initialize" not in fragment


def test_standalone_links_mermaid_engine(ir):
    html = render_guide(ir, "technical", standalone=True)
    assert 'src="vendor/mermaid.min.js"' in html
    assert "mermaid.initialize" in html


def test_fragment_omits_chat_widget(ir):
    # The widget calls a same-origin /api/chat that only `semdoc serve` provides. An
    # Artifact host has no such backend, so shipping the widget there would be dead UI.
    # (The stylesheet's #chat-widget CSS rule is inlined either way — harmless, since
    # the element it targets is never emitted — so this checks markup, not the ruleset.)
    fragment = render_guide(ir, "technical", standalone=False)
    assert 'id="chat-widget"' not in fragment
    assert "/api/chat" not in fragment


def test_standalone_includes_chat_widget(ir):
    html = render_guide(ir, "technical", standalone=True)
    assert 'id="chat-widget"' in html
    assert "/api/chat" in html
    assert "Ask about Case Services Analytics" in html


def test_technical_variant_includes_dax_and_security(ir):
    html = render_guide(ir, "technical")
    assert "DISTINCTCOUNT" in html
    assert "USERPRINCIPALNAME" in html
    assert "Row-level security" in html
    assert "dbo.fact_service" in html


def test_business_variant_withholds_implementation_detail(ir):
    html = render_guide(ir, "business")
    # No verbatim DAX bodies, no RLS expressions, no warehouse object names.
    assert "DISTINCTCOUNT" not in html
    assert "USERPRINCIPALNAME" not in html
    assert "dbo.fact_service" not in html


def test_business_variant_hides_internal_key_columns(ir):
    html = render_guide(ir, "business")
    # ClientKey is a hidden key column; Client Name is what an author actually uses.
    assert "Client Name" in html
    assert "ClientKey" not in html


def test_inactive_relationship_is_surfaced_in_both_variants(ir):
    for variant in ("technical", "business"):
        html = render_guide(ir, variant)
        assert "USERELATIONSHIP" in html, variant
        assert "Enrollment Date" in html, variant


def test_disconnected_table_is_called_out(ir):
    html = render_guide(ir, "business")
    assert "no relationships to anything else" in html


def test_sidebar_groups_measures_by_display_folder(ir):
    # Fixture measures: Service Count/Total Units/Avg Units per Service -> "Volume",
    # Units YoY % -> "Trend", Clients Served -> no folder -> "Ungrouped".
    html = render_guide(ir, "technical")
    assert '<span class="nav-folder-label">Ungrouped</span>' in html
    assert '<span class="nav-folder-label">Volume</span>' in html
    assert '<span class="nav-folder-label">Trend</span>' in html
    # Ungrouped sorts first, then alphabetically among real folder names.
    assert html.index("Ungrouped</span>") < html.index("Trend</span>") < html.index("Volume</span>")


def test_sidebar_folder_group_has_a_correct_count(ir):
    html = render_guide(ir, "technical")
    volume_summary = html[html.index('nav-folder-label">Volume') : html.index("</summary>", html.index('nav-folder-label">Volume'))]
    assert '<span class="nav-count">3</span>' in volume_summary


def test_base_measures_render_before_derived_ones(ir):
    html = render_guide(ir, "technical")
    assert html.index('id="measure-total-units"') < html.index(
        'id="measure-avg-units-per-service"'
    )


def test_verification_banner_reports_absent_narrative(ir):
    html = render_guide(ir, "technical")
    assert 'class="verification absent"' in html
    assert "extracted facts only" in html


def test_footer_legend_omits_narrative_marker_when_there_is_no_narrative(ir):
    html = render_guide(ir, "technical")
    assert "Narrative sections are marked" not in html


def test_verification_banner_reports_checks_when_validated(enriched_ir):
    html = render_guide(enriched_ir, "technical")
    assert 'class="verification pass"' in html
    assert "Identifiers checked" in html


def test_generated_provenance_badge_uses_the_ok_color_not_the_accent():
    # The badge needs to read as a distinct signal while scanning, not just another use
    # of the page's teal accent — it should share styling with the green "ok" tokens
    # used elsewhere (verification banner, date-table chip).
    css_path = pathlib.Path(__file__).parent.parent / "src" / "semdoc" / "render" / "templates" / "style.css"
    css = css_path.read_text(encoding="utf-8")
    start = css.index(".provenance.generated")
    generated_block = css[start : css.index("}", start)]
    assert "var(--ok)" in generated_block
    assert "var(--accent)" not in generated_block


def test_narrative_is_marked_as_generated(enriched_ir):
    html = render_guide(enriched_ir, "business")
    assert "Tracks service delivery against funded programs." in html
    assert "generated &amp; verified" in html


def test_narrative_without_a_validation_pass_is_not_claimed_verified(ir):
    # Narrative can be attached without validation ever running (e.g. a hand-edited IR
    # that bypassed `semdoc narrative apply`). The badge must not claim "verified" for
    # a check that never happened.
    narrative = Narrative(model_purpose="Tracks services.")
    ir_unchecked = ir.model_copy(update={"narrative": narrative, "validation": None})

    html = render_guide(ir_unchecked, "business")

    assert "generated &amp; verified" not in html
    assert "generated — not yet verified" in html
    assert 'class="provenance unverified"' in html


def test_narrative_with_failed_validation_is_flagged_not_verified(ir):
    # If identifiers failed and the narrative was attached anyway (--force), the badge
    # must say so rather than silently claiming success.
    narrative = Narrative(model_purpose="Tracks services.")
    failed_validation = ValidationReport(
        identifiers_checked=3, identifiers_failed=["unresolved reference: [Fake Measure]"]
    )
    ir_failed = ir.model_copy(update={"narrative": narrative, "validation": failed_validation})

    html = render_guide(ir_failed, "business")

    assert "generated &amp; verified" not in html
    assert "generated — unverified" in html
    assert 'class="provenance unverified"' in html


def test_generated_dax_shows_execution_result(enriched_ir):
    html = render_guide(enriched_ir, "business")
    assert "executed successfully" in html


def test_questions_section_does_not_overclaim_dax_execution_when_none_exists(ir):
    # A narrative can legitimately answer questions with field guidance and no DAX at
    # all (safer than fabricating an unverified snippet). The section blurb must not
    # then claim "every DAX snippet was executed" — there are none to have executed.
    narrative_no_dax = Narrative(
        questions_answered=[
            AnsweredQuestion(question="How many clients?", approach="Use the Client measure.")
        ]
    )
    ir_no_dax = ir.model_copy(
        update={
            "narrative": narrative_no_dax,
            "validation": ValidationReport(identifiers_checked=1),
        }
    )
    html = render_guide(ir_no_dax, "business")
    assert "was executed against it" not in html
    assert "checked against the model" in html
