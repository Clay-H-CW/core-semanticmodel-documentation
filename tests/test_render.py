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
    Gotcha,
    MeasureNarrative,
    ModelIR,
    Narrative,
    Ref,
    ReportRecipe,
    ValidationReport,
)
from semdoc.render import diagrams
from semdoc.render.html import _table_groups, render_guide

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
                        visual="Bar chart",
                        dax="EVALUATE ROW ( \"u\", [Total Units] )",
                        dax_verified=True,
                    )
                ],
                gotchas=[
                    Gotcha(
                        symptom="My total changes when I add Enrollment Date to a visual.",
                        cause="The relationship on Enrollment Date is inactive.",
                        fix="Use USERELATIONSHIP in a measure to activate it.",
                        affects=[Ref(table="Service Fact", column="Enrollment Date")],
                    )
                ],
                report_recipes=[
                    ReportRecipe(
                        requirement="Units delivered by program",
                        visual="Stacked bar",
                        measures=[Ref(measure="Total Units")],
                    )
                ],
                glossary={
                    "RLS": "Row-level security — restricts which rows a user can see.",
                },
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


def test_renders_a_complete_html_document(ir):
    html = render_guide(ir, "technical")
    assert html.startswith("<!doctype html>")
    assert "<title>Case Services Analytics</title>" in html
    assert html.rstrip().endswith("</html>")


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


def test_default_mermaid_mode_links_the_engine(ir):
    html = render_guide(ir, "technical")
    assert 'src="vendor/mermaid.min.js"' in html
    assert "mermaid.initialize" in html


def test_no_diagrams_mode_omits_the_engine(ir):
    html = render_guide(ir, "technical", mermaid_mode="none")
    assert "vendor/mermaid.min.js" not in html
    assert "mermaid.initialize" not in html


def test_guide_includes_chat_widget(ir):
    html = render_guide(ir, "technical")
    assert 'id="chat-widget"' in html
    assert "/api/chat" in html
    assert "Ask about Case Services Analytics" in html


def test_technical_variant_includes_dax_and_security(ir):
    html = render_guide(ir, "technical")
    assert "DISTINCTCOUNT" in html
    assert "USERPRINCIPALNAME" in html
    assert "Row-level security" in html
    assert "dbo.fact_service" in html


def test_technical_variant_shows_bpa_findings(ir):
    # The fixture's Units/Target Value columns are visible doubles with no format string
    # and no explicit SummarizeBy=None — real findings, not fabricated for the test.
    html = render_guide(ir, "technical")
    assert 'id="bpa"' in html
    assert "META_AVOID_FLOAT" in html
    assert "TabularEditor/BestPracticeRules" in html


def test_business_variant_omits_bpa_findings(ir):
    # Model-quality findings are a maintainer concern, not a report-building one.
    html = render_guide(ir, "business")
    assert 'id="bpa"' not in html
    assert "META_AVOID_FLOAT" not in html


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


def test_business_variant_hides_a_visible_key_column():
    # Found against the real HMIS model: a key column that is *not* marked hidden in the
    # model (is_key=True but is_hidden=False) still showed up in the business variant,
    # directly contradicting that section's own "internal key columns are omitted" text
    # — the filter only ever checked is_hidden, never is_key.
    from semdoc.ir.schema import Column, Model, Table

    model = Model(
        name="M",
        tables=[
            Table(
                name="T",
                columns=[
                    Column(name="VisibleKey", is_key=True, is_hidden=False),
                    Column(name="RealField", is_hidden=False),
                ],
            )
        ],
    )
    html = render_guide(ModelIR(model=model), "business")
    assert "VisibleKey" not in html
    assert "RealField" in html


def test_inactive_relationship_is_surfaced_in_both_variants(ir):
    for variant in ("technical", "business"):
        html = render_guide(ir, variant)
        assert "USERELATIONSHIP" in html, variant
        assert "Enrollment Date" in html, variant


def test_disconnected_table_is_called_out(ir):
    html = render_guide(ir, "business")
    assert "no relationships to anything else" in html


def test_table_groups_pins_tables_that_host_their_own_measures():
    from semdoc.ir.schema import Measure, Table

    measures_home = Table(name="*HMIS_Measures", measures=[Measure(name="M1", expression="1")])
    prefixed_a = Table(name="hmis Affiliation")
    prefixed_b = Table(name="hmis Enrollment")
    no_prefix = Table(name="AgeAsOfLookup")

    pinned, groups = _table_groups([measures_home, prefixed_a, prefixed_b, no_prefix])

    assert pinned == [measures_home]
    assert dict(groups) == {
        "General": [no_prefix],
        "hmis": [prefixed_a, prefixed_b],
    }


def test_table_groups_orders_general_before_named_prefixes():
    from semdoc.ir.schema import Table

    tables = [Table(name="hmis Project"), Table(name="AgeAsOfLookup"), Table(name="hmis CoC")]
    _, groups = _table_groups(tables)
    assert [key for key, _ in groups] == ["General", "hmis"]


def test_table_groups_empty_input():
    pinned, groups = _table_groups([])
    assert pinned == []
    assert groups == []


def test_sidebar_pins_measure_hosting_table_and_groups_the_rest_by_prefix(ir):
    # Fixture tables: Service Fact carries its own measures -> pinned. Client, Program,
    # Date, and Targets have no space in their names -> all fall into "General".
    html = render_guide(ir, "technical")

    pinned_link = '<a href="#table-service-fact" data-nav-item="service fact">'
    general_summary = html.index('<span class="nav-folder-label">General</span>')

    assert pinned_link in html
    # The pinned link must appear before the General folder, and outside any <details>.
    assert html.index(pinned_link) < general_summary
    assert 'href="#table-client"' in html[general_summary:]
    assert 'href="#table-targets"' in html[general_summary:]


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


def test_gotcha_renders_symptom_cause_and_fix_as_separate_labeled_parts(enriched_ir):
    html = render_guide(enriched_ir, "business")
    assert "My total changes when I add Enrollment Date to a visual." in html
    assert "The relationship on Enrollment Date is inactive." in html
    assert "Use USERELATIONSHIP in a measure to activate it." in html
    assert ">Cause<" in html
    assert ">Fix<" in html


def test_answered_question_shows_visual_hint(enriched_ir):
    html = render_guide(enriched_ir, "business")
    assert 'class="chip visual-hint">Bar chart</span>' in html


def test_report_recipe_shows_visual_hint(enriched_ir):
    html = render_guide(enriched_ir, "business")
    assert 'class="chip visual-hint">Stacked bar</span>' in html


def test_glossary_renders_sorted_terms(enriched_ir):
    html = render_guide(enriched_ir, "business")
    assert '<dt>RLS</dt>' in html
    assert "restricts which rows a user can see" in html


def test_glossary_omitted_when_narrative_has_none(ir):
    html = render_guide(ir, "business")
    assert 'id="glossary"' not in html


def test_data_model_section_includes_chip_legend_in_both_variants(ir):
    for variant in ("technical", "business"):
        html = render_guide(ir, variant)
        assert "chip-legend" in html
        assert "the thing being measured" in html


def test_low_cardinality_column_shown_as_good_filter(ir):
    stats_ir = ir.model_copy(deep=True)
    stats_ir.model.table("Client").column("County").cardinality = 12
    stats_ir.model.table("Client").column("County").sample_values = ["Alameda", "Contra Costa", "Marin"]
    html = render_guide(stats_ir, "business")
    assert 'class="chip cardinality-good"' in html
    assert "12 values" in html
    assert "Alameda, Contra Costa, Marin" in html


def test_high_cardinality_column_shown_as_too_many_to_filter(ir):
    stats_ir = ir.model_copy(deep=True)
    stats_ir.model.table("Service Fact").column("Units").cardinality = 84213
    html = render_guide(stats_ir, "business")
    assert 'class="chip cardinality-high"' in html
    assert "84,213 values" in html


def test_values_column_omitted_when_table_has_no_profiled_columns(ir):
    # None of the fixture's columns have cardinality set by default - the "Values"
    # column header should not appear at all rather than show as empty everywhere.
    html = render_guide(ir, "business")
    assert "<th>Values</th>" not in html


def test_unprofiled_column_shows_no_cardinality_badge_even_when_table_has_stats(ir):
    stats_ir = ir.model_copy(deep=True)
    stats_ir.model.table("Client").column("County").cardinality = 12
    html = render_guide(stats_ir, "business")
    # Client has other visible columns (e.g. Client Name) that were never profiled -
    # those rows must not show a stray badge.
    client_card_start = html.index('id="table-client"')
    client_card = html[client_card_start : client_card_start + 4000]
    assert client_card.count('class="chip cardinality-good"') == 1


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
