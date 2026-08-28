"""Tests for the model catalog: discovering what's already been extracted into `out/`.

Pure filesystem functions, no Fabric involved — same tmp_path-based style as
test_warehouse.py/test_stats.py.
"""

import json

from semdoc.catalog import discover_models, model_slug, slugify


def _write_ir(dir_path, *, name, workspace=None, generated_at="2026-08-28"):
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "model-ir.json").write_text(
        json.dumps({"model": {"name": name, "workspace": workspace}, "generated_at": generated_at}),
        encoding="utf-8",
    )


# -- slugify / model_slug ----------------------------------------------------------------


def test_slugify_lowercases_and_hyphenates():
    assert slugify("HMIS DirectLake") == "hmis-directlake"


def test_slugify_collapses_punctuation_runs():
    assert slugify("Case--Worthy!!  Gold") == "case-worthy-gold"


def test_slugify_empty_falls_back_to_x():
    assert slugify("") == "x"


def test_model_slug_combines_workspace_and_name():
    assert model_slug("Dev_Baseline", "CaseWorthy_Gold") == "dev-baseline-caseworthy-gold"


def test_model_slug_handles_missing_workspace():
    assert model_slug(None, "Standalone Model") == "standalone-model"


# -- discover_models ----------------------------------------------------------------------


def test_discover_models_on_missing_out_dir_returns_empty(tmp_path):
    assert discover_models(tmp_path / "does-not-exist") == []


def test_discover_models_on_empty_out_dir_returns_empty(tmp_path):
    assert discover_models(tmp_path) == []


def test_discover_models_finds_one(tmp_path):
    _write_ir(tmp_path / "hmis-directlake", name="HMIS DirectLake", workspace="CSMITH_Dev_Baseline")
    found = discover_models(tmp_path)
    assert found == [
        {
            "slug": "hmis-directlake",
            "name": "HMIS DirectLake",
            "workspace": "CSMITH_Dev_Baseline",
            "generated_at": "2026-08-28",
        }
    ]


def test_discover_models_finds_several_sorted_by_slug(tmp_path):
    _write_ir(tmp_path / "zzz-model", name="Z Model")
    _write_ir(tmp_path / "aaa-model", name="A Model")
    found = discover_models(tmp_path)
    assert [m["slug"] for m in found] == ["aaa-model", "zzz-model"]


def test_discover_models_skips_a_corrupt_sibling_without_failing(tmp_path):
    _write_ir(tmp_path / "good-model", name="Good Model")
    bad_dir = tmp_path / "bad-model"
    bad_dir.mkdir()
    (bad_dir / "model-ir.json").write_text("{not valid json", encoding="utf-8")

    found = discover_models(tmp_path)
    assert [m["slug"] for m in found] == ["good-model"]


def test_discover_models_skips_a_sibling_missing_the_model_key(tmp_path):
    good = tmp_path / "good-model"
    good.mkdir()
    (good / "model-ir.json").write_text(json.dumps({"not_model": {}}), encoding="utf-8")
    assert discover_models(tmp_path) == []


def test_discover_models_ignores_directories_without_a_model_ir_file(tmp_path):
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "mermaid.min.js").write_text("// not an IR", encoding="utf-8")
    assert discover_models(tmp_path) == []


def test_discover_models_ignores_loose_files_at_the_root(tmp_path):
    (tmp_path / "model.bim").write_text("{}", encoding="utf-8")
    assert discover_models(tmp_path) == []
