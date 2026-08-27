"""Command line interface.

    semdoc workspaces                     list workspaces you can see
    semdoc models --workspace W           list semantic models in a workspace
    semdoc extract --workspace W --model M   pull the model into out/model-ir.json
    semdoc render                         render guides from a stored IR
    semdoc generate --workspace W --model M  extract and render in one pass

`extract` and `render` are separate on purpose: extraction needs Fabric access, while
rendering needs only the stored IR. That split makes it possible to iterate on templates
and prompts offline, and to check a reference IR into the repo as a test fixture.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys

from semdoc import __version__
from semdoc.auth import credential_from_env
from semdoc.config import load_env
from semdoc.fabric import FabricClient, FabricError
from semdoc.ir.build import tmsl_to_model
from semdoc.ir.schema import ModelIR
from semdoc.render.html import write_guides

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


def _write_ir(ir: ModelIR, out_dir: pathlib.Path) -> pathlib.Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / IR_FILENAME
    path.write_text(
        json.dumps(ir.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _load_ir(path: pathlib.Path) -> ModelIR:
    if not path.exists():
        raise SystemExit(f"No IR at {path}. Run `semdoc extract` first.")
    return ModelIR.model_validate_json(path.read_text(encoding="utf-8"))


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
    out_dir = pathlib.Path(args.out)

    with FabricClient(credential_from_env()) as client:
        ws = client.find_workspace(workspace)
        sm = client.find_semantic_model(ws["id"], model_name)
        print(f"Extracting {sm['displayName']!r} from {ws['displayName']!r}…", file=sys.stderr)

        tmsl = client.get_tmsl(ws["id"], sm["id"])

        if args.save_tmsl:
            out_dir.mkdir(parents=True, exist_ok=True)
            raw_path = out_dir / "model.bim"
            raw_path.write_text(json.dumps(tmsl, indent=2), encoding="utf-8")
            print(f"  raw TMSL -> {raw_path}", file=sys.stderr)

    model = tmsl_to_model(
        tmsl,
        name=sm["displayName"],
        workspace=ws["displayName"],
        model_id=sm["id"],
    )
    ir = ModelIR(model=model, generated_at=_now(), source_tool_version=__version__)

    path = _write_ir(ir, out_dir)
    print(
        f"  {len(model.tables)} tables, {len(model.all_measures)} measures, "
        f"{len(model.relationships)} relationships, {len(model.roles)} RLS roles",
        file=sys.stderr,
    )
    print(path)
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    out_dir = pathlib.Path(args.out)
    ir = _load_ir(out_dir / IR_FILENAME if args.ir is None else pathlib.Path(args.ir))
    written = write_guides(ir, out_dir)
    for label, path in written.items():
        print(f"{label}\t{path}")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    rc = cmd_extract(args)
    if rc != 0:
        return rc
    args.ir = None
    return cmd_render(args)


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

    p_extract = sub.add_parser("extract", help="Extract a model into model-ir.json.")
    add_target(p_extract)
    p_extract.set_defaults(func=cmd_extract)

    p_render = sub.add_parser("render", help="Render guides from a stored IR.")
    p_render.add_argument("--ir", help=f"Path to an IR file (default: <out>/{IR_FILENAME}).")
    p_render.set_defaults(func=cmd_render)

    p_generate = sub.add_parser("generate", help="Extract and render in one pass.")
    add_target(p_generate)
    p_generate.set_defaults(func=cmd_generate)

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
