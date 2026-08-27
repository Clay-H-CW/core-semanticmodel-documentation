"""Interactive Q&A over a semantic model, powered by Claude.

Grounds every answer in the extracted IR via tool calls rather than a giant context
dump: the system prompt carries only a compact index of table and measure *names* (kept
small and cheap to cache), and Claude fetches exact detail — columns, DAX, relationships
— through `lookup_table` / `lookup_measure` / `search_schema`, all resolved straight from
the IR, never from the model's memory. Same anti-hallucination principle as the narrative
lane elsewhere in this tool, applied to a live conversation instead of a batch pass.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from semdoc.config import enrich_model
from semdoc.ir.schema import Model, ModelIR

MAX_TOOL_ROUNDS = 4
# Bounds request size for a long-running chat session; older turns just age out.
MAX_HISTORY_MESSAGES = 16
# Medium, not the default high/xhigh: this is a synchronous, interactive request with
# someone waiting on the other end, and schema lookup + explanation rarely needs deep
# reasoning. Left at the default adaptive `thinking` rather than disabling it outright —
# disabling thinking has its own failure modes (tool calls leaking into visible text).
CHAT_EFFORT = "medium"


class ChatError(RuntimeError):
    """Raised for anything that should surface as a chat-widget error, not a crash."""


def _schema_index(model: Model) -> str:
    lines = [f"Storage mode: {model.storage_mode.value}", ""]
    if model.description:
        lines.append(f"Description: {model.description}")
        lines.append("")

    lines.append("TABLES")
    for table in model.tables:
        if table.is_hidden:
            continue
        desc = f" — {table.description}" if table.description else ""
        marker = " (date table)" if table.is_date_table else ""
        lines.append(f"- {table.name} [{table.kind.value}]{marker}{desc}")

    lines.append("")
    lines.append("MEASURES (by table)")
    for table in model.tables:
        visible = [m for m in table.measures if not m.is_hidden]
        if not visible:
            continue
        lines.append(f"[{table.name}]")
        for measure in visible:
            desc = f" — {measure.description}" if measure.description else ""
            lines.append(f"  - {measure.name}{desc}")

    return "\n".join(lines)


def _system_prompt(model: Model) -> str:
    return (
        f"You are embedded in a documentation guide for the Fabric semantic model "
        f"'{model.name}'. You help someone build a Power BI report using this model.\n\n"
        "Rules:\n"
        "- Never invent a table, column, or measure name. Use search_schema when you "
        "are not sure of the exact name, and lookup_table / lookup_measure before "
        "citing a field or writing DAX, so what you say matches the real model.\n"
        "- Prefer an existing measure over building a calculation from raw columns.\n"
        "- If what the user wants is not in the model, say so plainly and suggest the "
        "closest real alternative rather than guessing.\n"
        "- Call out inactive relationships (they need USERELATIONSHIP to work) when "
        "they affect the answer.\n"
        "- Be concise and concrete: exact field names to drag into a visual, or a short "
        "DAX snippet. Skip preamble.\n\n"
        f"{_schema_index(model)}"
    )


TOOLS: list[dict[str, Any]] = [
    {
        "name": "lookup_table",
        "description": (
            "Full detail for one table: description, storage mode, every column with "
            "its data type and description, and the relationships that touch it. Call "
            "this before telling the user which exact field to use."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Exact table name"}},
            "required": ["name"],
        },
    },
    {
        "name": "lookup_measure",
        "description": (
            "Full DAX definition, description, format string, and dependencies for one "
            "measure. Call this before writing DAX or explaining what a measure computes "
            "— never recall a measure's DAX from the name alone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Exact measure name"}},
            "required": ["name"],
        },
    },
    {
        "name": "search_schema",
        "description": (
            "Case-insensitive substring search across table, column, and measure names. "
            "Use this when you are not sure of the exact name to look up."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]


def _find_table_ci(model: Model, name: str):
    lowered = name.casefold()
    return next((t for t in model.tables if t.name.casefold() == lowered), None)


def _find_measure_ci(model: Model, name: str):
    lowered = name.casefold()
    return next((m for m in model.all_measures if m.name.casefold() == lowered), None)


def run_tool(model: Model, name: str, tool_input: dict) -> dict:
    """Resolve one tool call against the IR. Deterministic — no model calls in here."""
    if name == "lookup_table":
        table = _find_table_ci(model, tool_input.get("name", ""))
        if table is None:
            return {"error": f"No table named {tool_input.get('name')!r}."}
        related = [
            r
            for r in model.relationships
            if r.from_table == table.name or r.to_table == table.name
        ]
        return {
            "name": table.name,
            "kind": table.kind.value,
            "description": table.description,
            "storage_mode": table.storage_mode.value,
            "is_date_table": table.is_date_table,
            "warehouse_table": (
                f"{table.source.warehouse_schema}.{table.source.warehouse_table}"
                if table.source.warehouse_table
                else None
            ),
            "columns": [
                {
                    "name": c.name,
                    "data_type": c.data_type,
                    "description": c.description,
                    "hidden": c.is_hidden,
                }
                for c in table.columns
            ],
            "relationships": [
                {
                    "from": f"{r.from_table}[{r.from_column}]",
                    "to": f"{r.to_table}[{r.to_column}]",
                    "active": r.is_active,
                    "cross_filter": r.cross_filter_direction,
                }
                for r in related
            ],
        }

    if name == "lookup_measure":
        measure = _find_measure_ci(model, tool_input.get("name", ""))
        if measure is None:
            return {"error": f"No measure named {tool_input.get('name')!r}."}
        return {
            "name": measure.name,
            "expression": measure.expression,
            "description": measure.description,
            "format_string": measure.format_string,
            "depends_on": [str(r) for r in measure.depends_on],
            "referenced_by": [str(r) for r in measure.referenced_by],
        }

    if name == "search_schema":
        query = tool_input.get("query", "").casefold()
        if not query:
            return {"error": "query must not be empty"}
        tables = [t.name for t in model.tables if query in t.name.casefold()]
        measures = [m.name for m in model.all_measures if query in m.name.casefold()]
        columns = [
            f"{t.name}[{c.name}]"
            for t in model.tables
            for c in t.columns
            if query in c.name.casefold()
        ]
        return {"tables": tables[:20], "measures": measures[:20], "columns": columns[:20]}

    return {"error": f"Unknown tool {name!r}"}


def _create(client: anthropic.Anthropic, **kwargs):
    try:
        return client.messages.create(**kwargs)
    except anthropic.AuthenticationError as exc:
        raise ChatError("Anthropic authentication failed — check ANTHROPIC_API_KEY.") from exc
    except anthropic.RateLimitError as exc:
        raise ChatError("Rate limited by the Anthropic API. Try again in a moment.") from exc
    except anthropic.APIStatusError as exc:
        raise ChatError(f"Anthropic API error ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ChatError("Could not reach the Anthropic API. Check your network connection.") from exc


def answer(
    ir: ModelIR,
    client: anthropic.Anthropic,
    history: list[dict[str, Any]],
    *,
    model_id: str | None = None,
) -> str:
    """Answer the latest turn in `history`, resolving any tool calls against the IR.

    `history` is the conversation so far as plain {role, content-string} turns — the
    caller (the browser widget) owns persistence; this call is stateless. Tool-use
    round-trips happen entirely inside this call and are not exposed back to the caller,
    so the client-side history stays small and simple.
    """
    if not history or history[-1].get("role") != "user":
        raise ChatError("history must end with a user turn")

    messages: list[Any] = list(history[-MAX_HISTORY_MESSAGES:])
    system = _system_prompt(ir.model)

    for _ in range(MAX_TOOL_ROUNDS):
        response = _create(
            client,
            model=model_id or enrich_model(),
            max_tokens=4096,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            output_config={"effort": CHAT_EFFORT},
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            return "".join(b.text for b in response.content if b.type == "text").strip()

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = run_tool(ir.model, block.name, block.input)
            tool_results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)}
            )
        messages.append({"role": "user", "content": tool_results})

    return (
        "I wasn't able to finish looking that up within a reasonable number of steps. "
        "Try asking a more specific question."
    )
