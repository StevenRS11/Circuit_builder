from pathlib import Path
import importlib.util

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_module(name):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ws = load_module("workflow_state")
lp = load_module("load_preferences")


def test_init_and_approve_dependency_gate(tmp_path):
    state_path = tmp_path / "demo_workflow.yaml"
    ws._write(state_path, ws.new_state("demo", "draft", tmp_path))
    artifact = tmp_path / "demo_01_specification.md"
    artifact.write_text("R1: test", encoding="utf-8")
    ws.approve(state_path, "01", [artifact])
    state = ws._read(state_path)
    assert state["stages"]["01"]["approval"] == "approved"
    assert state["next_action"] == "02"


def test_hash_change_invalidates_downstream(tmp_path):
    state_path = tmp_path / "demo_workflow.yaml"
    ws._write(state_path, ws.new_state("demo", "production", tmp_path))
    artifact = tmp_path / "demo_01_specification.md"
    artifact.write_text("v1", encoding="utf-8")
    ws.approve(state_path, "01", [artifact])
    artifact.write_text("v2", encoding="utf-8")
    state = ws.sync(state_path)
    assert state["stages"]["01"]["status"] == "invalid"
    assert state["stages"]["10"]["status"] == "invalid"


def test_change_class_has_narrow_invalidation(tmp_path):
    state_path = tmp_path / "demo_workflow.yaml"
    ws._write(state_path, ws.new_state("demo", "production", tmp_path))
    state = ws.invalidate(state_path, "board")
    assert set(state["stages"]) == {"08", "09", "10"}


def test_migration_requires_reconfirmation(tmp_path):
    (tmp_path / "legacy_01_specification.md").write_text("spec", encoding="utf-8")
    out, state = ws.migrate(tmp_path, "legacy")
    assert out.exists()
    assert state["stages"]["01"]["approval"] == "confirmation_required"
    assert state["next_action"] == "01"


def test_stage_scoped_preferences():
    stage_one = lp.load_preferences(stage="01")
    assert "defaults" in stage_one and "assembly" in stage_one
    assert "connectors" not in stage_one
    stage_two = lp.load_preferences(stage="02")
    assert "connectors" in stage_two and "assembly" in stage_two
