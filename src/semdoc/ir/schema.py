"""The semdoc intermediate representation.

This module is the contract between extraction and everything downstream. Extractors
produce a `ModelIR`; the enricher and all renderers read only a `ModelIR`. Nothing
downstream of `build` is allowed to call Fabric.

Two rules keep the output trustworthy:

1. Everything under `ModelIR.model` and `ModelIR.warehouse` is extracted fact. It is
   never written by an LLM.
2. Everything under `ModelIR.narrative` is LLM-authored. It is kept in a separate
   branch precisely so that (a) readers can tell which is which, (b) validation can
   walk it mechanically and check every identifier against the extracted facts, and
   (c) renderers can produce a facts-only document when enrichment is unavailable.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

IR_VERSION = "1"


class IRBase(BaseModel):
    """Common base for IR models.

    `protected_namespaces` is cleared because this domain legitimately uses names like
    `model_purpose` and `model_permission`; pydantic reserves the `model_` prefix for
    its own API by default.
    """

    model_config = ConfigDict(protected_namespaces=())


class StorageMode(StrEnum):
    IMPORT = "Import"
    DIRECT_LAKE = "DirectLake"
    DIRECT_QUERY = "DirectQuery"
    DUAL = "Dual"
    MIXED = "Mixed"
    UNKNOWN = "Unknown"


class TableKind(StrEnum):
    """Inferred from the relationship graph, not declared in the model.

    A table sitting on the "many" side of its relationships is a fact; a table on the
    "one" side is a dimension. Both means a bridge. This is a deterministic heuristic
    computed in `ir.build`, not an LLM guess.
    """

    FACT = "fact"
    DIMENSION = "dimension"
    BRIDGE = "bridge"
    CALCULATION_GROUP = "calculation_group"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


class SourceKind(StrEnum):
    DIRECT_LAKE = "directlake"
    M_QUERY = "m"
    CALCULATED = "calculated"
    UNKNOWN = "unknown"


class Ref(IRBase):
    """A reference to a model object, used by narrative content and validation.

    Any identifier an LLM mentions must be expressible as a `Ref` that resolves against
    the extracted model. `validate.identifiers` enforces this.
    """

    table: str | None = None
    column: str | None = None
    measure: str | None = None

    def __str__(self) -> str:
        if self.measure:
            return f"[{self.measure}]"
        if self.table and self.column:
            return f"'{self.table}'[{self.column}]"
        return f"'{self.table}'" if self.table else "<unresolved>"


# --------------------------------------------------------------------------------------
# Extracted facts
# --------------------------------------------------------------------------------------


class Column(IRBase):
    name: str
    data_type: str = "unknown"
    description: str | None = None
    is_hidden: bool = False
    is_key: bool = False
    format_string: str | None = None
    display_folder: str | None = None
    summarize_by: str | None = None
    sort_by_column: str | None = None
    # Name of the column in the underlying source (warehouse/lakehouse table).
    source_column: str | None = None
    # Populated by the optional stats pass. Cardinality drives prominence: a
    # 3-value column reads as a natural slicer, a 4-million-value one does not.
    cardinality: int | None = None
    sample_values: list[str] = Field(default_factory=list)


class Measure(IRBase):
    name: str
    expression: str
    description: str | None = None
    format_string: str | None = None
    display_folder: str | None = None
    is_hidden: bool = False
    # Resolved by `ir.build` from the DAX expression; drives the dependency diagram
    # and lets the renderer show base measures before derived ones.
    depends_on: list[Ref] = Field(default_factory=list)
    referenced_by: list[Ref] = Field(default_factory=list)


class Hierarchy(IRBase):
    name: str
    description: str | None = None
    is_hidden: bool = False
    levels: list[str] = Field(default_factory=list)


class TableSource(IRBase):
    """Where a model table's data actually comes from.

    This is what makes "understand the warehouse behind the model" possible. For
    DirectLake, `warehouse_table` is the delta table name; for Import, it is parsed
    out of the partition's M expression where we can manage it.
    """

    kind: SourceKind = SourceKind.UNKNOWN
    warehouse_schema: str | None = None
    warehouse_table: str | None = None
    expression: str | None = None


class Partition(IRBase):
    name: str
    mode: StorageMode = StorageMode.UNKNOWN
    source: TableSource = Field(default_factory=TableSource)


class Table(IRBase):
    name: str
    description: str | None = None
    is_hidden: bool = False
    is_date_table: bool = False
    kind: TableKind = TableKind.UNKNOWN
    storage_mode: StorageMode = StorageMode.UNKNOWN
    row_count: int | None = None
    source: TableSource = Field(default_factory=TableSource)
    columns: list[Column] = Field(default_factory=list)
    measures: list[Measure] = Field(default_factory=list)
    hierarchies: list[Hierarchy] = Field(default_factory=list)
    partitions: list[Partition] = Field(default_factory=list)

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


class Relationship(IRBase):
    name: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    from_cardinality: str = "many"
    to_cardinality: str = "one"
    is_active: bool = True
    cross_filter_direction: str = "OneDirection"
    # Inactive relationships are the single most common source of "why is my report
    # wrong" confusion, so the renderer calls them out explicitly.
    relies_on_userelationship: bool = False


class RoleTablePermission(IRBase):
    table: str
    filter_expression: str


class Role(IRBase):
    """Row-level security role. Analysts need to know what they will and will not see."""

    name: str
    description: str | None = None
    model_permission: str | None = None
    table_permissions: list[RoleTablePermission] = Field(default_factory=list)


class Model(IRBase):
    name: str
    workspace: str | None = None
    id: str | None = None
    description: str | None = None
    culture: str | None = None
    storage_mode: StorageMode = StorageMode.UNKNOWN
    tables: list[Table] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    roles: list[Role] = Field(default_factory=list)

    def table(self, name: str) -> Table | None:
        return next((t for t in self.tables if t.name == name), None)

    def measure(self, name: str) -> Measure | None:
        for t in self.tables:
            for m in t.measures:
                if m.name == name:
                    return m
        return None

    @property
    def all_measures(self) -> list[Measure]:
        return [m for t in self.tables for m in t.measures]


class WarehouseColumn(IRBase):
    name: str
    data_type: str
    is_nullable: bool = True
    description: str | None = None


class WarehouseForeignKey(IRBase):
    column: str
    references_schema: str
    references_table: str
    references_column: str


class WarehouseTable(IRBase):
    schema_name: str
    name: str
    description: str | None = None
    row_count: int | None = None
    is_view: bool = False
    # Verbatim SQL from INFORMATION_SCHEMA.VIEWS / sys.sql_modules, view objects only.
    # Shown to the reader as-is; never rewritten or summarized, for the same reason DAX
    # and M expressions elsewhere in the IR are kept verbatim.
    view_definition: str | None = None
    # Best-effort FROM/JOIN targets parsed out of `view_definition` — "schema.table"
    # strings, not resolved WarehouseTable references, since the referenced object may
    # not itself be one the model reads from (and so never gets extracted). Empty when
    # not a view, or when nothing could be confidently parsed out. Never guessed: a
    # missing entry here means "not extracted," not "reads from nothing."
    reads_from: list[str] = Field(default_factory=list)
    columns: list[WarehouseColumn] = Field(default_factory=list)
    foreign_keys: list[WarehouseForeignKey] = Field(default_factory=list)
    # Model tables that read from this warehouse table. Populated by the lineage pass.
    consumed_by: list[str] = Field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.schema_name}.{self.name}"


class Warehouse(IRBase):
    server: str | None = None
    database: str | None = None
    tables: list[WarehouseTable] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# LLM-authored narrative
# --------------------------------------------------------------------------------------


class TableNarrative(IRBase):
    business_description: str
    typical_use: str | None = None
    grain: str | None = None


class MeasureNarrative(IRBase):
    plain_english: str
    when_to_use: str | None = None


class AnsweredQuestion(IRBase):
    question: str
    approach: str
    fields: list[Ref] = Field(default_factory=list)
    dax: str | None = None
    # Set by `validate.dax` after actually running the snippet against the model.
    dax_verified: bool | None = None


class Gotcha(IRBase):
    title: str
    detail: str
    affects: list[Ref] = Field(default_factory=list)


class ReportRecipe(IRBase):
    requirement: str
    visual: str | None = None
    fields: list[Ref] = Field(default_factory=list)
    measures: list[Ref] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)


class Narrative(IRBase):
    """Everything in here was written by an LLM and verified against the model.

    Keyed by object name so renderers can look up narrative beside the extracted fact
    without the two ever being merged in storage.
    """

    model_purpose: str | None = None
    tables: dict[str, TableNarrative] = Field(default_factory=dict)
    measures: dict[str, MeasureNarrative] = Field(default_factory=dict)
    questions_answered: list[AnsweredQuestion] = Field(default_factory=list)
    gotchas: list[Gotcha] = Field(default_factory=list)
    report_recipes: list[ReportRecipe] = Field(default_factory=list)


class ValidationReport(IRBase):
    """What validation found. Rendered into the output so readers see the guarantees."""

    identifiers_checked: int = 0
    identifiers_failed: list[str] = Field(default_factory=list)
    dax_snippets_checked: int = 0
    dax_snippets_failed: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.identifiers_failed and not self.dax_snippets_failed


class ModelIR(IRBase):
    ir_version: str = IR_VERSION
    # Set by the caller after extraction; not generated inside the pipeline so that
    # re-rendering a stored IR does not churn the timestamp.
    generated_at: str | None = None
    source_tool_version: str | None = None

    model: Model
    warehouse: Warehouse | None = None
    narrative: Narrative | None = None
    validation: ValidationReport | None = None
