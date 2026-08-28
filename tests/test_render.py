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
from semdoc.render.html import _fact_dimension_table, _per_fact_diagrams, _table_groups, render_guide

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


def test_table_focus_respects_label_hidden_columns(ir):
    # ClientKey is a hidden join column (see the business-variant leak test elsewhere in
    # this file) — the per-fact diagram must honor the same business/technical rule the
    # combined star_schema diagram already does, not silently ignore it.
    with_label = diagrams.table_focus(ir.model, "Service Fact", label_hidden_columns=True)
    without_label = diagrams.table_focus(ir.model, "Service Fact", label_hidden_columns=False)
    assert 'Client -- "ClientKey" --> Service_Fact' in with_label
    assert 'Client -- "ClientKey" --> Service_Fact' not in without_label
    assert "Client --> Service_Fact" in without_label


def test_lineage_focus_returns_none_for_unrelated_or_unknown_table(ir):
    assert diagrams.lineage_focus(ir, "Targets") is None
    assert diagrams.lineage_focus(ir, "Nope") is None


def test_lineage_focus_narrows_to_the_focused_tables_subset(ir):
    mermaid = diagrams.lineage_focus(ir, "Service Fact")
    assert 'wh_dbo_fact_service -- "DirectLake" --> sm_Service_Fact' in mermaid
    # Targets has no relationship to Service Fact, so it has no place in this subset —
    # not even as an unlinked node, the way it would in the full combined lineage diagram.
    assert "sm_Targets" not in mermaid


def test_fact_relationship_map_none_for_a_single_fact_model(ir):
    # Fixture has exactly one fact table — nothing for it to relate to.
    assert diagrams.fact_relationship_map(ir.model) is None


def test_fact_relationship_map_none_when_facts_share_nothing():
    # Two facts, each with its own private dimension — genuinely unrelated, not just
    # under-connected. An all-isolated-facts map would say nothing real.
    from semdoc.ir.schema import Column, Model, Relationship, Table, TableKind

    fact_a = Table(name="A_Fact", kind=TableKind.FACT, columns=[Column(name="XId")])
    fact_b = Table(name="B_Fact", kind=TableKind.FACT, columns=[Column(name="YId")])
    dim_x = Table(name="X_Dim", kind=TableKind.DIMENSION, columns=[Column(name="XId")])
    dim_y = Table(name="Y_Dim", kind=TableKind.DIMENSION, columns=[Column(name="YId")])
    model = Model(
        name="Silos",
        tables=[fact_a, fact_b, dim_x, dim_y],
        relationships=[
            Relationship(
                name="r1", from_table="A_Fact", from_column="XId", to_table="X_Dim", to_column="XId"
            ),
            Relationship(
                name="r2", from_table="B_Fact", from_column="YId", to_table="Y_Dim", to_column="YId"
            ),
        ],
    )
    assert diagrams.fact_relationship_map(model) is None


def test_fact_relationship_map_includes_only_dimensions_shared_by_two_or_more_facts():
    multi_ir = _multi_fact_ir()
    mermaid = diagrams.fact_relationship_map(multi_ir.model)
    assert mermaid is not None
    assert "Orders" in mermaid
    assert "Shipments" in mermaid
    assert "Customer" in mermaid


def test_fact_dimension_table_none_for_a_single_fact_model(ir):
    assert _fact_dimension_table(ir.model) is None


def test_fact_dimension_table_matches_the_shared_dimension_map():
    multi_ir = _multi_fact_ir()
    table = _fact_dimension_table(multi_ir.model)
    assert table == {
        "facts": ["Orders", "Shipments"],
        "dims": ["Customer"],
        "marks": {("Orders", "Customer"), ("Shipments", "Customer")},
    }


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


def test_table_groups_pins_a_disconnected_measures_container():
    # The real HMIS shape: *HMIS_Measures hosts 199 measures, one placeholder column,
    # and zero relationships — kind is DISCONNECTED precisely because it joins to
    # nothing. That combination, not merely "has measures," is what makes it a pure
    # measures container worth pulling out above the groups.
    from semdoc.ir.schema import Measure, Table, TableKind

    measures_home = Table(
        name="*HMIS_Measures",
        kind=TableKind.DISCONNECTED,
        measures=[Measure(name="M1", expression="1")],
    )
    prefixed_a = Table(name="hmis Affiliation")
    prefixed_b = Table(name="hmis Enrollment")
    no_prefix = Table(name="AgeAsOfLookup")

    pinned, groups = _table_groups([measures_home, prefixed_a, prefixed_b, no_prefix])

    assert pinned == [measures_home]
    assert dict(groups) == {
        "General": [no_prefix],
        "hmis": [prefixed_a, prefixed_b],
    }


def test_table_groups_keeps_a_real_fact_table_grouped_even_with_its_own_measures():
    # Found live against real data: CaseWorthy Enterprise and Coordinate Semantic Model
    # each put a handful of measures directly on the fact table they belong to (a real,
    # common modeling pattern, unlike HMIS's single dedicated measures container) — e.g.
    # Coordinate's real C8_Plan_Fact hosts 6 measures and 17 real columns, with kind
    # FACT, not DISCONNECTED. It must still land in the "Fact" group, not be pulled out
    # as if it were a measures-only container.
    from semdoc.ir.schema import Column, Measure, Table, TableKind

    plan_fact = Table(
        name="C8_Plan_Fact",
        kind=TableKind.FACT,
        columns=[Column(name="Plan_Id"), Column(name="Client_Id")],
        measures=[Measure(name="Count_Of_Active_Plans", expression="1")],
    )
    client_dim = Table(name="C8_Client_Dim", kind=TableKind.DIMENSION, columns=[Column(name="Client_Id")])

    pinned, groups = _table_groups([plan_fact, client_dim])

    assert pinned == []
    assert dict(groups) == {"Fact": [plan_fact], "Dimension": [client_dim]}


def test_table_groups_orders_general_before_named_prefixes():
    from semdoc.ir.schema import Table

    tables = [Table(name="hmis Project"), Table(name="AgeAsOfLookup"), Table(name="hmis CoC")]
    _, groups = _table_groups(tables)
    assert [key for key, _ in groups] == ["General", "hmis"]


def test_table_groups_empty_input():
    pinned, groups = _table_groups([])
    assert pinned == []
    assert groups == []


def test_table_groups_splits_by_last_underscore_word_when_that_convention_dominates():
    # Real naming convention seen across several models (e.g. Cwe_Enrollment_Fact,
    # Cwe_Client_Dim, Cwe_Service_Type_Category_Bridge, ST_Client_Attributes) — not the
    # space-prefix style HMIS uses. "Dim" is relabeled "Dimension"; any other suffix
    # (here "Attributes") passes through verbatim rather than being guessed at from a
    # fixed vocabulary.
    from semdoc.ir.schema import Table

    fact = Table(name="Cwe_Enrollment_Fact")
    dim = Table(name="Cwe_Client_Dim")
    bridge = Table(name="Cwe_Service_Type_Category_Bridge")
    attributes = Table(name="ST_Client_Attributes")

    _, groups = _table_groups([fact, dim, bridge, attributes])
    assert dict(groups) == {
        "Fact": [fact],
        "Dimension": [dim],
        "Bridge": [bridge],
        "Attributes": [attributes],
    }
    # Fact leads, then Dimension, then Bridge, then any other suffix group alphabetically.
    assert [key for key, _ in groups] == ["Fact", "Dimension", "Bridge", "Attributes"]


def test_table_groups_suffix_convention_needs_a_majority_not_one_table():
    # Three underscored outliers (HMIS's real Tool_YesNoOff, Tool_Percentages,
    # Tool_Integers) among mostly space-prefixed tables must not flip the whole model
    # into suffix mode and fragment it into one-table groups — the model as a whole
    # decides which convention applies, not each table in isolation.
    from semdoc.ir.schema import Table

    tables = [
        Table(name="hmis Enrollment"),
        Table(name="hmis Exit"),
        Table(name="hmis Project"),
        Table(name="Tool_YesNoOff"),
    ]
    _, groups = _table_groups(tables)
    assert dict(groups) == {
        "hmis": [tables[0], tables[1], tables[2]],
        "General": [tables[3]],
    }


def test_table_groups_no_underscore_falls_back_to_general_in_suffix_mode():
    # A table with no underscore at all, in a model that otherwise clearly uses the
    # suffix convention, has no "last word after _" to group by.
    from semdoc.ir.schema import Table

    fact = Table(name="Cwe_Enrollment_Fact")
    dim = Table(name="Cwe_Client_Dim")
    no_underscore = Table(name="Time Period")

    _, groups = _table_groups([fact, dim, no_underscore])
    assert dict(groups) == {"Fact": [fact], "Dimension": [dim], "General": [no_underscore]}
    # General sorts last among suffix groups — it's the leftover, not the headline.
    assert [key for key, _ in groups] == ["Fact", "Dimension", "General"]


def test_table_groups_bare_words_with_no_underscore_are_not_suffix_matches():
    # "the last word after _" requires an actual underscore — a table bare-named "Fact"
    # has no underscore at all, so it is not a suffix match on its own; with no other
    # signal either, all three land together in the ordinary General fallback.
    from semdoc.ir.schema import Table

    tables = [Table(name="Fact"), Table(name="Dim"), Table(name="Bridge")]
    _, groups = _table_groups(tables)
    assert dict(groups) == {"General": tables}


def test_sidebar_groups_a_real_fact_tables_own_measures_normally(ir):
    # Fixture's "Service Fact" is a real fact table (6 columns, wired into
    # relationships) that also hosts its own measures — the common pattern, not a
    # dedicated measures-only container — so it must NOT be pinned; it groups like any
    # other table. The fixture doesn't use the underscore-suffix convention, so it falls
    # to the prefix-before-first-space rule and lands in its own "Service" folder.
    # Client, Program, Date, and Targets have no space in their names -> "General".
    html = render_guide(ir, "technical")

    service_link = '<a href="#table-service-fact" data-nav-item="service fact">'
    general_summary = html.index('<span class="nav-folder-label">General</span>')
    service_summary = html.index('<span class="nav-folder-label">Service</span>')

    assert service_link in html
    # General sorts before named-prefix folders like Service; the link itself sits
    # after its own folder's summary, not pinned above everything.
    assert general_summary < service_summary < html.index(service_link)
    assert 'href="#table-client"' in html[general_summary:service_summary]
    assert 'href="#table-targets"' in html[general_summary:service_summary]


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


# -- existing report usage ---------------------------------------------------------------


@pytest.fixture(scope="module")
def reports_ir(ir):
    from semdoc.ir.schema import ReportUsage

    return ir.model_copy(
        update={
            "reports": [
                ReportUsage(
                    name="Program Delivery",
                    id="11111111-1111-1111-1111-111111111111",
                    pages=["Overview", "Detail"],
                    used_fields=[
                        Ref(measure="Total Units"),
                        Ref(table="Program", column="Program Name"),
                    ],
                )
            ]
        }
    )


def test_existing_reports_section_lists_the_report_and_its_fields(reports_ir):
    html = render_guide(reports_ir, "business")
    assert 'id="existing-reports"' in html
    assert "Program Delivery" in html
    assert "Overview, Detail" in html
    assert "[Total Units]" in html
    assert "&#39;Program&#39;[Program Name]" in html


def test_existing_reports_section_absent_when_no_reports(ir):
    html = render_guide(ir, "business")
    assert 'id="existing-reports"' not in html
    assert "Program Delivery" not in html


def test_measure_and_column_show_report_usage_badges(reports_ir):
    html = render_guide(reports_ir, "technical")
    measure_start = html.index('id="measure-total-units"')
    measure_card = html[measure_start : measure_start + 1500]
    assert "in 1 report" in measure_card

    table_start = html.index('id="table-program"')
    table_card = html[table_start : table_start + 3000]
    assert "in 1 report" in table_card


def test_report_usage_badge_absent_for_unused_measure(reports_ir):
    html = render_guide(reports_ir, "technical")
    # "Service Count" is a real measure in the fixture that no report references. Slice
    # only up to the next measure card's id, not a fixed length — the cards are short
    # enough that a fixed window bleeds into a neighboring card's own badge.
    measure_start = html.index('id="measure-service-count"')
    next_card = html.index('id="measure-', measure_start + 1)
    measure_card = html[measure_start:next_card]
    assert "in 1 report" not in measure_card
    assert "report-usage" not in measure_card


# -- multi-model switcher ------------------------------------------------------------------


def test_model_switcher_absent_with_no_available_models(ir):
    html = render_guide(ir, "technical")
    assert 'id="model-switch"' not in html


def test_model_switcher_absent_with_only_one_model(ir):
    html = render_guide(ir, "technical", available_models=[{"slug": "x", "name": "X", "workspace": None}])
    assert 'id="model-switch"' not in html


def test_model_switcher_present_with_two_models_and_marks_current(ir):
    models = [
        {"slug": "model-a", "name": "Model A", "workspace": None},
        {"slug": "model-b", "name": "Model B", "workspace": None},
    ]
    html = render_guide(ir, "business", model_slug="model-a", available_models=models)
    assert 'id="model-switch"' in html
    assert '<option value="model-a" selected>Model A</option>' in html
    assert '<option value="model-b" selected>Model B</option>' not in html
    assert '<option value="model-b" >Model B</option>' in html
    # Switching must preserve the variant currently being viewed.
    assert "/guide-business.html" in html


def test_chat_request_carries_the_current_model_slug(ir):
    html = render_guide(ir, "technical", model_slug="hmis-directlake")
    assert 'slug: "hmis-directlake"' in html


# -- per-fact diagram split --------------------------------------------------------------


def _multi_fact_ir():
    from semdoc.ir.schema import Column, Model, Relationship, Table, TableKind

    orders = Table(name="Orders", kind=TableKind.FACT, columns=[Column(name="CustomerId")])
    shipments = Table(name="Shipments", kind=TableKind.FACT, columns=[Column(name="CustomerId")])
    customer = Table(name="Customer", kind=TableKind.DIMENSION, columns=[Column(name="CustomerId")])
    model = Model(
        name="Multi",
        tables=[orders, shipments, customer],
        relationships=[
            Relationship(
                name="r1", from_table="Orders", from_column="CustomerId",
                to_table="Customer", to_column="CustomerId",
            ),
            Relationship(
                name="r2", from_table="Shipments", from_column="CustomerId",
                to_table="Customer", to_column="CustomerId",
            ),
        ],
    )
    return ModelIR(model=model)


def _visible(ir_obj):
    return [t for t in ir_obj.model.tables if not t.is_hidden]


def test_per_fact_diagrams_off_for_a_single_fact_model(ir):
    # Fixture has exactly one fact table (Service Fact) — nothing to split.
    use_split, star, lineage = _per_fact_diagrams(_visible(ir), ir, "technical")
    assert use_split is False
    assert star == []
    assert lineage == []


def test_per_fact_diagrams_on_for_a_multi_fact_model():
    multi_ir = _multi_fact_ir()
    use_split, star, lineage = _per_fact_diagrams(_visible(multi_ir), multi_ir, "technical")
    assert use_split is True
    assert {e["table"].name for e in star} == {"Orders", "Shipments"}
    assert all(e["related_count"] == 1 for e in star)


def test_per_fact_diagrams_skip_lineage_for_business_variant():
    multi_ir = _multi_fact_ir()
    use_split, star, lineage = _per_fact_diagrams(_visible(multi_ir), multi_ir, "business")
    assert use_split is True
    assert star  # relationship diagrams still apply to business
    assert lineage == []  # the lineage section itself never renders for business


def test_guide_shows_per_fact_diagrams_alongside_the_combined_one():
    multi_ir = _multi_fact_ir()
    html = render_guide(multi_ir, "technical")
    # The combined whole-model diagram stays — per-fact is a supplement, not a
    # replacement — plus one collapsible entry per fact table in each section.
    assert "Or focus on one fact table at a time" in html
    assert html.count('class="focus-group"') >= 2
    assert "Orders" in html
    assert "Shipments" in html


def test_guide_shows_the_fact_relationship_map_when_facts_share_a_dimension():
    multi_ir = _multi_fact_ir()
    html = render_guide(multi_ir, "technical")
    # Collapsed by default, same mechanism as the per-fact entries.
    assert "How the fact tables connect through shared dimensions" in html
    start = html.index("How the fact tables connect through shared dimensions")
    details_open = html.rindex("<details", 0, start)
    assert "<details class=\"focus-group\">" in html[details_open : details_open + 40]


def test_guide_shows_the_fact_dimension_matrix_table_alongside_the_diagram():
    multi_ir = _multi_fact_ir()
    html = render_guide(multi_ir, "technical")
    assert 'class="matrix-table"' in html
    # Header names the shared dimension; a mark sits in both facts' rows for it.
    table_start = html.index('class="matrix-table"')
    table_html = html[table_start : table_start + 2000]
    assert "<th>Customer</th>" in table_html
    assert table_html.count('class="matrix-mark">✓') == 2


def test_guide_omits_the_fact_relationship_map_for_a_single_fact_model(ir):
    html = render_guide(ir, "technical")
    assert "How the fact tables connect through shared dimensions" not in html


def test_guide_has_no_per_fact_supplement_for_a_single_fact_model(ir):
    html = render_guide(ir, "technical")
    assert 'class="focus-group"' not in html
    assert "Or focus on one fact table at a time" not in html
