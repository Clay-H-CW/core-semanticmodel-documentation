"""Command line interface.

    semdoc workspaces                     list workspaces you can see
    semdoc models --workspace W           list semantic models in a workspace
    semdoc extract --workspace W --model M   pull the model into out/<slug>/model-ir.json
    semdoc render                         render guides for a stored IR (--model to pick
                                           one, when out/ holds more than one)
    semdoc generate --workspace W --model M  extract and render in one pass
    semdoc narrative apply FILE           validate narrative JSON against the model, attach it
    semdoc warehouse extract              schema, view SQL, and best-effort lineage for
                                           warehouse objects the model reads from
    semdoc stats extract                  column cardinality and sample values via live DAX
    semdoc reports extract                which fields/measures existing Power BI reports use
    semdoc serve                          serve guides locally, with the chat widget if
                                           ANTHROPIC_API_KEY is set

`extract` and `render` are separate on purpose: extraction needs Fabric access, while
rendering needs only the stored IR. That split makes it possible to iterate on templates
and prompts offline, and to check a reference IR into the repo as a test fixture.

`out/` can hold more than one extracted model side by side, one subdirectory per model
(named by `catalog.model_slug`) — extracting a new model never touches another one's
directory. `--ir` still accepts a direct path to any IR file (in or out of `out/`, e.g. a
fixture) for the commands below that read one; `--model` instead picks among whatever is
already sitting in `out/` by slug or display name, and is only needed once there is more
than one to choose from.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys

from semdoc import __version__, catalog
from semdoc.auth import credential_from_env
from semdoc.config import enrich_model, load_env
from semdoc.fabric import FabricClient, FabricError
from semdoc.ir.build import extract_onelake_reference, extract_warehouse_connection, tmsl_to_model
from semdoc.ir.schema import ModelIR, Narrative, Warehouse
from semdoc.render.assets import AssetError
from semdoc.render.html import write_guides
from semdoc.validate import validate_identifiers

DEFAULT_OUT = pathlib.Path("out")
IR_FILENAME = "model-ir.json"


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")


def _resolve(args: argparse.Namespace, key: str, env_var: str) -> str:
    value = getattr(args, key, None) or os.environ.get(env_var)
    if not value:
        raise SystemExit(
            f"Missing --{key.replace('_', '-')}. Pass it on the command line or set {env_var}."
        )
    return value


def _write_ir(ir: ModelIR, path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(ir.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _load_ir(path: pathlib.Path) -> ModelIR:
    if not path.exists():
        raise SystemExit(f"No IR at {path}. Run `semdoc extract` first.")
    return ModelIR.model_validate_json(path.read_text(encoding="utf-8"))


def _describe_models(models: list[dict]) -> str:
    return ", ".join(f"{m['name']!r} ({m['slug']})" for m in models)


def _match_model(models: list[dict], selector: str) -> dict | None:
    """Match `--model` against either a slug or a display name, case-insensitively."""
    lowered = selector.casefold()
    return next((m for m in models if m["slug"] == lowered or m["name"].casefold() == lowered), None)


def _resolve_ir_path(out_root: pathlib.Path, ir_arg: str | None, model_arg: str | None) -> pathlib.Path:
    """Find the IR to operate on, for every command except `extract` itself.

    `--ir` wins outright when given — this is also how a fixture outside `out/` entirely
    (e.g. for offline template iteration) gets used. Otherwise this looks at what has
    actually been extracted into `out_root`: exactly one model needs no `--model` at all
    (today's single-model setups keep working with zero flag changes); more than one
    requires `--model` to say which.
    """
    if ir_arg:
        return pathlib.Path(ir_arg)

    found = catalog.discover_models(out_root)
    if not found:
        raise SystemExit(f"No model extracted into {out_root}. Run `semdoc extract` first.")

    if model_arg:
        match = _match_model(found, model_arg)
        if match is None:
            raise SystemExit(
                f"No extracted model matches --model {model_arg!r}. Available: {_describe_models(found)}"
            )
        return out_root / match["slug"] / IR_FILENAME

    if len(found) == 1:
        return out_root / found[0]["slug"] / IR_FILENAME

    raise SystemExit(
        f"Multiple models extracted into {out_root} — pass --model to pick one. "
        f"Available: {_describe_models(found)}"
    )


# -- commands --------------------------------------------------------------------------


def cmd_workspaces(args: argparse.Namespace) -> int:
    with FabricClient(credential_from_env()) as client:
        workspaces = client.list_workspaces()
    if not workspaces:
        print("No workspaces visible to this identity.")
        return 1
    for ws in sorted(workspaces, key=lambda w: w.get("displayName", "")):
        print(f"{ws.get('displayName')}\t{ws.get('id')}")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    workspace = _resolve(args, "workspace", "SEMDOC_WORKSPACE")
    with FabricClient(credential_from_env()) as client:
        ws = client.find_workspace(workspace)
        models = client.list_semantic_models(ws["id"])
    if not models:
        print(f"No semantic models in {workspace!r}.")
        return 1
    for m in sorted(models, key=lambda x: x.get("displayName", "")):
        print(f"{m.get('displayName')}\t{m.get('id')}")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    workspace = _resolve(args, "workspace", "SEMDOC_WORKSPACE")
    model_name = _resolve(args, "model", "SEMDOC_SEMANTIC_MODEL")
    out_root = pathlib.Path(args.out)

    with FabricClient(credential_from_env()) as client:
        ws = client.find_workspace(workspace)
        sm = client.find_semantic_model(ws["id"], model_name)
        print(f"Extracting {sm['displayName']!r} from {ws['displayName']!r}…", file=sys.stderr)

        # Deterministic from workspace + model name: re-extracting this same model always
        # lands back in its own directory; a different workspace or model name always
        # gets a fresh sibling one. Nothing else in out/ is touched either way.
        model_dir = out_root / catalog.model_slug(ws["displayName"], sm["displayName"])

        tmsl = client.get_tmsl(ws["id"], sm["id"])

        if args.save_tmsl:
            model_dir.mkdir(parents=True, exist_ok=True)
            raw_path = model_dir / "model.bim"
            raw_path.write_text(json.dumps(tmsl, indent=2), encoding="utf-8")
            print(f"  raw TMSL -> {raw_path}", file=sys.stderr)

        # Import-mode models carry the connection directly in dataSources. DirectLake
        # models don't — they name OneLake by workspace/item GUID instead, which needs a
        # live Fabric lookup (still inside this `with` block) to resolve to something
        # `semdoc warehouse extract` can actually connect to.
        warehouse = extract_warehouse_connection(tmsl)
        if warehouse is None:
            onelake_ref = extract_onelake_reference(tmsl)
            if onelake_ref:
                resolved = client.resolve_sql_endpoint(*onelake_ref)
                if resolved:
                    server, database = resolved
                    warehouse = Warehouse(server=server, database=database)

    model = tmsl_to_model(
        tmsl,
        name=sm["displayName"],
        workspace=ws["displayName"],
        workspace_id=ws["id"],
        model_id=sm["id"],
    )
    ir = ModelIR(
        model=model, warehouse=warehouse, generated_at=_now(), source_tool_version=__version__
    )

    path = _write_ir(ir, model_dir / IR_FILENAME)
    if warehouse:
        print(f"  warehouse: {warehouse.database} on {warehouse.server}", file=sys.stderr)
    print(
        f"  {len(model.tables)} tables, {len(model.all_measures)} measures, "
        f"{len(model.relationships)} relationships, {len(model.roles)} RLS roles",
        file=sys.stderr,
    )
    print(path)
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    """Render one model's guides — and, when it lives in `out/`'s multi-model tree,
    quietly refresh every sibling model's guides too, so every dropdown lists the full,
    current roster rather than only whichever model was rendered most recently.
    """
    out_root = pathlib.Path(args.out)
    ir_path = _resolve_ir_path(out_root, args.ir, args.model)
    ir = _load_ir(ir_path)
    model_dir = ir_path.parent

    # An explicit --ir pointing outside out/ entirely (e.g. a checked-in fixture, for
    # offline template iteration) renders standalone: no sibling dropdown, no shared
    # vendor/ reused from a tree it isn't part of.
    in_catalog = out_root.exists() and model_dir.resolve().is_relative_to(out_root.resolve())
    all_models = catalog.discover_models(out_root) if in_catalog else []
    vendor_dir = out_root / "vendor" if in_catalog else model_dir / "vendor"

    written: dict[str, pathlib.Path] = {}
    try:
        written = write_guides(
            ir,
            model_dir,
            vendor_dir=vendor_dir,
            available_models=all_models,
            inline_assets=args.inline_assets,
            with_diagrams=not args.no_diagrams,
        )

        for entry in all_models:
            if entry["slug"] == model_dir.name:
                continue
            sibling_dir = out_root / entry["slug"]
            sibling_ir = _load_ir(sibling_dir / IR_FILENAME)
            write_guides(
                sibling_ir,
                sibling_dir,
                vendor_dir=vendor_dir,
                available_models=all_models,
                inline_assets=args.inline_assets,
                with_diagrams=not args.no_diagrams,
            )
            print(f"  also refreshed: {entry['slug']} (dropdown now lists every model)", file=sys.stderr)
    except AssetError as exc:
        print(f"{exc}\n\nRe-run with --no-diagrams to render without them.", file=sys.stderr)
        return 3

    for label, path in written.items():
        size = path.stat().st_size
        print(f"{label}\t{path}\t{size:,} bytes")
    return 0


def cmd_warehouse_extract(args: argparse.Namespace) -> int:
    """Pull warehouse metadata (tiers 1-3: schema, view SQL, best-effort lineage) for
    every warehouse object the model actually reads from, and attach it to the IR.
    """
    from semdoc import warehouse as warehouse_module

    out_root = pathlib.Path(args.out)
    ir_path = _resolve_ir_path(out_root, args.ir, args.model)
    ir = _load_ir(ir_path)

    if ir.warehouse is None:
        raise SystemExit(
            "This IR has no warehouse connection recorded. Re-run `semdoc extract` "
            "against the model — that is what discovers the server/database."
        )

    refs = warehouse_module.referenced_objects(ir.model)
    print(
        f"Looking up {len(refs)} warehouse object(s) referenced by the model "
        f"on {ir.warehouse.database} ({ir.warehouse.server})…",
        file=sys.stderr,
    )

    try:
        updated_warehouse, missing = warehouse_module.extract_warehouse(
            ir.model, ir.warehouse, credential_from_env()
        )
    except warehouse_module.WarehouseError as exc:
        print(f"Warehouse extraction failed: {exc}", file=sys.stderr)
        return 4

    views = sum(1 for t in updated_warehouse.tables if t.is_view)
    print(
        f"  {len(updated_warehouse.tables)} object(s) found ({views} views), "
        f"{len(missing)} referenced by the model but not found",
        file=sys.stderr,
    )
    for schema, name in missing:
        print(f"    MISSING: {schema}.{name}", file=sys.stderr)

    ir.warehouse = updated_warehouse
    _write_ir(ir, ir_path)
    print(f"Warehouse detail attached -> {ir_path}")
    return 0


def cmd_stats_extract(args: argparse.Namespace) -> int:
    """Populate column cardinality and sample values via live DAX, so the guide can tell
    a report author which columns are good slicers.
    """
    from semdoc import stats as stats_module

    out_root = pathlib.Path(args.out)
    ir_path = _resolve_ir_path(out_root, args.ir, args.model)
    ir = _load_ir(ir_path)

    profilable = sum(len(cols) for _, cols in stats_module.profilable_columns(ir.model))
    print(f"Profiling {profilable} visible column(s)…", file=sys.stderr)

    try:
        with FabricClient(credential_from_env()) as client:
            failed = stats_module.extract_column_stats(
                ir.model, client, sample_threshold=args.sample_threshold
            )
    except stats_module.StatsError as exc:
        print(f"Column stats extraction failed: {exc}", file=sys.stderr)
        return 5

    print(f"  {profilable - len(failed)} column(s) profiled, {len(failed)} failed", file=sys.stderr)
    if failed:
        # Grouped by reason, not printed one-by-one: a single bad query fails an entire
        # chunk with the same message repeated across every column in it.
        by_reason: dict[str, list[str]] = {}
        for name, reason in failed:
            by_reason.setdefault(reason, []).append(name)
        for reason, names in by_reason.items():
            print(f"    FAILED ({len(names)}): {reason[:300]}", file=sys.stderr)
            for name in names:
                print(f"      - {name}", file=sys.stderr)

    _write_ir(ir, ir_path)
    print(f"Column stats attached -> {ir_path}")
    return 0


def cmd_reports_extract(args: argparse.Namespace) -> int:
    """Find every Power BI report built on this model and what it actually uses."""
    from semdoc import reports as reports_module

    out_root = pathlib.Path(args.out)
    ir_path = _resolve_ir_path(out_root, args.ir, args.model)
    ir = _load_ir(ir_path)

    try:
        with FabricClient(credential_from_env()) as client:
            found, notes = reports_module.extract_report_usage(ir.model, client)
    except reports_module.ReportsError as exc:
        print(f"Report usage extraction failed: {exc}", file=sys.stderr)
        return 6

    total_fields = sum(len(r.used_fields) for r in found)
    print(
        f"  {len(found)} report(s) found on this model, {total_fields} field/measure "
        f"reference(s) resolved",
        file=sys.stderr,
    )
    for r in found:
        print(f"    {r.name}: {len(r.pages)} page(s), {len(r.used_fields)} field(s)/measure(s)", file=sys.stderr)
    for note in notes:
        print(f"    NOTE: {note}", file=sys.stderr)

    ir.reports = found
    _write_ir(ir, ir_path)
    print(f"Report usage attached -> {ir_path}")
    return 0


def cmd_narrative_apply(args: argparse.Namespace) -> int:
    """Attach narrative content to the stored IR, after checking it against the model.

    The narrative file is plain JSON matching the `Narrative` schema — hand-authored,
    written by a session working from the IR directly (no API call), or eventually the
    output of an automated `semdoc enrich` pass. All three go through the same check
    here, because all three can misname a field.
    """
    out_root = pathlib.Path(args.out)
    ir_path = _resolve_ir_path(out_root, args.ir, args.model)
    ir = _load_ir(ir_path)

    narrative_path = pathlib.Path(args.narrative_path)
    if not narrative_path.exists():
        raise SystemExit(f"{narrative_path} does not exist.")
    narrative = Narrative.model_validate_json(narrative_path.read_text(encoding="utf-8"))

    report = validate_identifiers(ir.model, narrative)
    print(f"Checked {report.identifiers_checked} identifiers.", file=sys.stderr)
    if not report.ok:
        for failure in report.identifiers_failed:
            print(f"  FAIL: {failure}", file=sys.stderr)
        if not args.force:
            print(
                f"\n{len(report.identifiers_failed)} identifier(s) do not resolve against "
                f"the model. Fix the narrative file, or pass --force to attach it anyway.",
                file=sys.stderr,
            )
            return 1

    ir.narrative = narrative
    ir.validation = report
    _write_ir(ir, ir_path)
    print(f"Narrative attached ({'PASS' if report.ok else 'FORCED with failures'}) -> {ir_path}")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    rc = cmd_extract(args)
    if rc != 0:
        return rc
    args.ir = None
    return cmd_render(args)


def cmd_serve(args: argparse.Namespace) -> int:
    """Serve the output directory over localhost.

    Opening the files directly with file:// works too. A server is offered because some
    browsers restrict local pages, because a URL is easier to reload and to share with
    someone sitting next to you than a file path, and because the chat widget needs a
    same-origin endpoint (/api/chat) to call — that only exists here, not over file://.
    """
    import http.server
    import threading
    import webbrowser

    import anthropic

    from semdoc import chat as chat_module

    out_dir = pathlib.Path(args.out).resolve()
    if not out_dir.exists():
        raise SystemExit(f"{out_dir} does not exist. Run `semdoc render` first.")

    all_models = catalog.discover_models(out_dir)
    if not all_models:
        raise SystemExit(f"No rendered model found under {out_dir}. Run `semdoc render` first.")

    if args.model:
        match = _match_model(all_models, args.model)
        if match is None:
            raise SystemExit(
                f"No model matches --model {args.model!r}. Available: {_describe_models(all_models)}"
            )
        landing_slug = match["slug"]
    elif len(all_models) == 1:
        landing_slug = all_models[0]["slug"]
    else:
        raise SystemExit(
            f"Multiple models rendered under {out_dir} — pass --model to pick a landing "
            f"page. Available: {_describe_models(all_models)}"
        )

    landing = f"{landing_slug}/guide-{args.variant}.html"
    if not (out_dir / landing).exists():
        raise SystemExit(f"{landing} not found in {out_dir}. Re-run `semdoc render`.")

    # Loaded once at startup, not per request: re-run `semdoc render` and restart the
    # server to pick up changed models. A model that fails to load here just has no
    # working chat — the guide itself still browses fine either way, same as before this
    # was ever a dict — and it does not stop the other models' chat from working.
    models_by_slug: dict[str, ModelIR] = {}
    for entry in all_models:
        ir_file = out_dir / entry["slug"] / IR_FILENAME
        try:
            models_by_slug[entry["slug"]] = ModelIR.model_validate_json(ir_file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - best-effort per model, never fatal to serve
            print(f"  WARNING: could not load {entry['slug']} for chat: {exc}", file=sys.stderr)

    # `Anthropic()` with no api_key resolves ANTHROPIC_API_KEY from the environment
    # (already loaded from .env by main()). Left unset when there is no key so chat
    # requests fail with a clear message instead of the SDK raising at request time.
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    anthropic_client = anthropic.Anthropic() if api_key else None

    def send_json(handler: http.server.BaseHTTPRequestHandler, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(out_dir), **kw)

        def do_POST(self) -> None:  # noqa: N802 - required name by http.server
            if self.path != "/api/chat":
                self.send_error(404)
                return

            if anthropic_client is None:
                send_json(
                    self,
                    503,
                    {"error": "ANTHROPIC_API_KEY is not set. Add it to .env and restart `semdoc serve`."},
                )
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                history = body.get("history")
                slug = body.get("slug")
                if not isinstance(history, list) or not history:
                    raise ValueError("history must be a non-empty list")
                if not isinstance(slug, str) or not slug:
                    raise ValueError("slug must be a non-empty string")
            except (ValueError, json.JSONDecodeError) as exc:
                send_json(self, 400, {"error": f"Bad request: {exc}"})
                return

            ir = models_by_slug.get(slug)
            if ir is None:
                send_json(self, 404, {"error": f"Unknown model {slug!r}."})
                return

            try:
                reply = chat_module.answer(ir, anthropic_client, history)
            except chat_module.ChatError as exc:
                send_json(self, 502, {"error": str(exc)})
                return
            except Exception as exc:  # last-resort guard: a bad turn must not kill the server
                send_json(self, 500, {"error": f"Unexpected error: {exc}"})
                return

            send_json(self, 200, {"reply": reply})

    # Bind to loopback only: this serves an unauthenticated directory that will contain
    # real model metadata, and it has no business being reachable from the network.
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        raise SystemExit(f"Could not bind 127.0.0.1:{args.port}: {exc}")

    url = f"http://127.0.0.1:{server.server_port}/{landing}"
    print(f"Serving {out_dir}", file=sys.stderr)
    print(f"  {url}", file=sys.stderr)
    if len(all_models) > 1:
        others = ", ".join(m["name"] for m in all_models if m["slug"] != landing_slug)
        print(f"  Also available via the dropdown: {others}", file=sys.stderr)
    if anthropic_client is None:
        print("  Chat disabled — set ANTHROPIC_API_KEY in .env to enable it.", file=sys.stderr)
    else:
        print(f"  Chat enabled (model: {enrich_model()})", file=sys.stderr)
    print("Press Ctrl+C to stop.", file=sys.stderr)

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    finally:
        server.server_close()
    return 0


# -- wiring ----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="semdoc",
        description="Generate documentation for a Fabric semantic model.",
    )
    parser.add_argument("--version", action="version", version=f"semdoc {__version__}")
    parser.add_argument(
        "--env",
        default=".env",
        help="Path to a .env file (default: .env; missing file is fine).",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help=f"Output directory (default: {DEFAULT_OUT}).",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("workspaces", help="List workspaces visible to this identity.").set_defaults(
        func=cmd_workspaces
    )

    p_models = sub.add_parser("models", help="List semantic models in a workspace.")
    p_models.add_argument("--workspace", "-w")
    p_models.set_defaults(func=cmd_models)

    def add_target(p: argparse.ArgumentParser) -> None:
        p.add_argument("--workspace", "-w", help="Workspace display name.")
        p.add_argument("--model", "-m", help="Semantic model display name.")
        p.add_argument(
            "--save-tmsl",
            action="store_true",
            help="Also write the raw model.bim, useful for building test fixtures.",
        )

    def add_model_selector(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--model",
            "-m",
            help="Which already-extracted model to use, by slug or display name — only "
            "needed when <out> holds more than one. Ignored if --ir is also given.",
        )

    def add_render_opts(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--inline-assets",
            action="store_true",
            help="Embed the Mermaid bundle in each guide for a single self-contained "
            "file (~3.5 MB larger each). Default links a shared vendor/ copy.",
        )
        p.add_argument(
            "--no-diagrams",
            action="store_true",
            help="Skip the Mermaid bundle. Guides then show diagram source as text.",
        )

    p_extract = sub.add_parser("extract", help="Extract a model into model-ir.json.")
    add_target(p_extract)
    p_extract.set_defaults(func=cmd_extract)

    p_render = sub.add_parser("render", help="Render guides from a stored IR.")
    p_render.add_argument("--ir", help="Path to an IR file (overrides --model entirely).")
    add_model_selector(p_render)
    add_render_opts(p_render)
    p_render.set_defaults(func=cmd_render)

    p_narrative = sub.add_parser(
        "narrative", help="Attach narrative content to a stored IR."
    )
    narrative_sub = p_narrative.add_subparsers(dest="narrative_command", required=True)
    p_narrative_apply = narrative_sub.add_parser(
        "apply", help="Validate a narrative JSON file against the model and attach it."
    )
    p_narrative_apply.add_argument("narrative_path", help="Path to a Narrative-schema JSON file.")
    p_narrative_apply.add_argument("--ir", help="Path to an IR file (overrides --model entirely).")
    add_model_selector(p_narrative_apply)
    p_narrative_apply.add_argument(
        "--force",
        action="store_true",
        help="Attach the narrative even if some identifiers fail to resolve.",
    )
    p_narrative_apply.set_defaults(func=cmd_narrative_apply)

    p_warehouse = sub.add_parser("warehouse", help="Look up detail on the connected warehouse.")
    warehouse_sub = p_warehouse.add_subparsers(dest="warehouse_command", required=True)
    p_warehouse_extract = warehouse_sub.add_parser(
        "extract",
        help="Pull schema, view SQL, and best-effort lineage for warehouse objects the "
        "model reads from.",
    )
    p_warehouse_extract.add_argument("--ir", help="Path to an IR file (overrides --model entirely).")
    add_model_selector(p_warehouse_extract)
    p_warehouse_extract.set_defaults(func=cmd_warehouse_extract)

    p_stats = sub.add_parser("stats", help="Column cardinality and sample values, via live DAX.")
    stats_sub = p_stats.add_subparsers(dest="stats_command", required=True)
    p_stats_extract = stats_sub.add_parser(
        "extract", help="Profile every visible column so the guide can flag good slicers."
    )
    p_stats_extract.add_argument("--ir", help="Path to an IR file (overrides --model entirely).")
    add_model_selector(p_stats_extract)
    p_stats_extract.add_argument(
        "--sample-threshold",
        type=int,
        default=50,
        help="Skip sample values above this many distinct values (default: 50). "
        "Cardinality is still recorded either way.",
    )
    p_stats_extract.set_defaults(func=cmd_stats_extract)

    p_reports = sub.add_parser("reports", help="Look up which existing reports use this model.")
    reports_sub = p_reports.add_subparsers(dest="reports_command", required=True)
    p_reports_extract = reports_sub.add_parser(
        "extract", help="Find every report built on this model and what it actually uses."
    )
    p_reports_extract.add_argument("--ir", help="Path to an IR file (overrides --model entirely).")
    add_model_selector(p_reports_extract)
    p_reports_extract.set_defaults(func=cmd_reports_extract)

    p_generate = sub.add_parser("generate", help="Extract and render in one pass.")
    add_target(p_generate)
    add_render_opts(p_generate)
    p_generate.set_defaults(func=cmd_generate)

    p_serve = sub.add_parser("serve", help="Serve the rendered guides on localhost.")
    p_serve.add_argument("--port", type=int, default=8000, help="Port (default: 8000).")
    add_model_selector(p_serve)
    p_serve.add_argument(
        "--variant",
        choices=("technical", "business"),
        default="technical",
        help="Which guide to open (default: technical).",
    )
    p_serve.add_argument("--no-browser", action="store_true", help="Do not open a browser.")
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_env(args.env)

    try:
        return args.func(args)
    except FabricError as exc:
        print(f"Fabric request failed: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
