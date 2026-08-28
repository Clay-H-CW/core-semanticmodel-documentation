"""Discovering which models have already been extracted into `out/`.

One model per subdirectory (`out/<slug>/model-ir.json`), slug derived deterministically
from the model's own workspace and name so re-extracting the same model always lands back
in the same place, and a different model always gets its own sibling directory. Nothing
is stored in a separate index — the directory listing on disk *is* the catalog, so there
is no manifest that can drift out of sync with what is actually there.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

MODEL_IR_FILENAME = "model-ir.json"


def slugify(name: str) -> str:
    """Same rule as the guide template's own anchor slugs (`render.html._slug`), kept as
    a separate copy on purpose — that one names anchors within a page, this one names
    directories on disk, and the two namespaces have no reason to stay coupled."""
    return re.sub(r"[^a-z0-9]+", "-", str(name).casefold()).strip("-") or "x"


def model_slug(workspace: str | None, name: str) -> str:
    return slugify(f"{workspace or ''} {name}")


def discover_models(out_root: Path) -> list[dict]:
    """[{"slug", "name", "workspace", "generated_at"}, ...] for every `out/*/model-ir.json`.

    A sibling directory whose IR fails to parse — an older schema, a partially written
    file — is skipped rather than raised: one bad neighbor must never block rendering or
    serving the model actually being worked on. Only the handful of fields a dropdown or
    a landing page needs are read directly out of the raw JSON, not a full
    `ModelIR.model_validate_json` — that is also what lets an older-schema sibling still
    list correctly even though it cannot be loaded for real use.
    """
    if not out_root.is_dir():
        return []

    found = []
    for child in sorted(out_root.iterdir()):
        ir_path = child / MODEL_IR_FILENAME
        if not child.is_dir() or not ir_path.exists():
            continue
        try:
            data = json.loads(ir_path.read_text(encoding="utf-8"))
            model = data["model"]
            found.append(
                {
                    "slug": child.name,
                    "name": model["name"],
                    "workspace": model.get("workspace"),
                    "generated_at": data.get("generated_at"),
                }
            )
        except (json.JSONDecodeError, KeyError, OSError):
            continue

    return found
