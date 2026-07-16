"""Deterministic workflow state, migration, hash sync, and invalidation."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "references" / "stages" / "manifest.yaml"
ARTIFACT_PATTERNS = {
    "01": "*_01_specification.md", "02": "*_02_candidates.md",
    "03": "*_03_bom.md", "04": "*_04_implementation.md",
    "05": "*_04c_analysis.md", "05b": "*_05b_netlist.yaml",
    "06": "*.kicad_sch", "07": "*_07_review.md",
    "08": "*_08_layout_review.md", "09": "PCBway_uploads",
    "10": "*_10_bringup.md",
}


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha256(path):
    path = Path(path)
    h = hashlib.sha256()
    if path.is_dir():
        for child in sorted(p for p in path.rglob("*") if p.is_file()):
            h.update(child.relative_to(path).as_posix().encode())
            h.update(child.read_bytes())
    else:
        h.update(path.read_bytes())
    return h.hexdigest()


def _manifest(path=MANIFEST_PATH):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _read(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def _write(path, data):
    Path(path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def new_state(project, mode, output_dir):
    if mode not in {"explore", "draft", "production"}:
        raise ValueError(f"invalid mode: {mode}")
    return {"schema_version": 1, "project": project, "mode": mode,
            "created_utc": _now(), "updated_utc": _now(), "stages": {},
            "next_action": "01", "unresolved_questions": []}


def _record(path, artifact):
    p = Path(artifact).resolve()
    return {"path": str(p), "sha256": _sha256(p)}


def _required_stages(state, manifest):
    return manifest["modes"][state["mode"]]


def sync(state_path, manifest_path=MANIFEST_PATH):
    state = _read(state_path); manifest = _manifest(manifest_path)
    changed = []
    for stage, record in state.get("stages", {}).items():
        for artifact in record.get("artifacts", []):
            p = Path(artifact["path"])
            digest = _sha256(p) if p.exists() else None
            if digest != artifact.get("sha256"):
                artifact["sha256"] = digest
                record.update(status="invalid", approval="required", reason="artifact hash changed or file missing")
                changed.append(stage)
    if changed:
        downstream = _downstream(changed, manifest)
        for stage in downstream:
            rec = state.setdefault("stages", {}).setdefault(stage, {})
            if stage not in changed:
                rec.update(status="invalid", approval="required", reason=f"dependency changed: {', '.join(changed)}")
    state["next_action"] = _next_action(state, manifest)
    state["updated_utc"] = _now(); _write(state_path, state)
    return state


def _downstream(stages, manifest):
    affected = set(stages); progressed = True
    while progressed:
        progressed = False
        for stage, cfg in manifest["stages"].items():
            if stage not in affected and affected.intersection(cfg.get("dependencies", [])):
                affected.add(stage); progressed = True
    return sorted(affected, key=lambda x: list(manifest["stages"]).index(x))


def _next_action(state, manifest):
    for stage in _required_stages(state, manifest):
        rec = state.get("stages", {}).get(stage, {})
        if rec.get("status") != "complete" or rec.get("approval") in {"required", "confirmation_required"}:
            return stage
    return "complete"


def approve(state_path, stage, artifact_paths, manifest_path=MANIFEST_PATH):
    state = _read(state_path); manifest = _manifest(manifest_path)
    if stage not in manifest["stages"]:
        raise ValueError(f"unknown stage: {stage}")
    missing = [d for d in manifest["stages"][stage].get("dependencies", [])
               if state.get("stages", {}).get(d, {}).get("status") != "complete"]
    if missing:
        raise ValueError(f"cannot approve {stage}; incomplete dependencies: {', '.join(missing)}")
    artifacts = [_record(state_path, p) for p in artifact_paths]
    state.setdefault("stages", {})[stage] = {"status": "complete", "approval": "approved",
        "approved_utc": _now(), "artifacts": artifacts}
    state["next_action"] = _next_action(state, manifest); state["updated_utc"] = _now()
    _write(state_path, state); return state


def invalidate(state_path, change_class, manifest_path=MANIFEST_PATH):
    state = _read(state_path); manifest = _manifest(manifest_path)
    stages = manifest["invalidation"].get(change_class)
    if not stages:
        raise ValueError(f"unknown invalidation class: {change_class}")
    for stage in stages:
        state.setdefault("stages", {}).setdefault(stage, {}).update(
            status="invalid", approval="required", reason=f"change class: {change_class}")
    state["next_action"] = _next_action(state, manifest); state["updated_utc"] = _now()
    _write(state_path, state); return state


def migrate(project_dir, project, mode="production"):
    project_dir = Path(project_dir).resolve(); state = new_state(project, mode, project_dir)
    for stage, pattern in ARTIFACT_PATTERNS.items():
        matches = [p for p in project_dir.glob(pattern) if not p.name.endswith("_workflow.yaml")]
        if matches:
            state["stages"][stage] = {"status": "complete", "approval": "confirmation_required",
                "reason": "legacy artifact discovered; prior approval cannot be inferred",
                "artifacts": [_record(project_dir, p) for p in matches]}
    manifest = _manifest(); state["next_action"] = _next_action(state, manifest)
    out = project_dir / f"{project}_workflow.yaml"; _write(out, state); return out, state


def main(argv=None):
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("init"); q.add_argument("project"); q.add_argument("--mode", choices=["explore","draft","production"], required=True); q.add_argument("--output-dir", required=True)
    q = sub.add_parser("status"); q.add_argument("state"); q.add_argument("--json", action="store_true")
    q = sub.add_parser("sync"); q.add_argument("state"); q.add_argument("--json", action="store_true")
    q = sub.add_parser("approve"); q.add_argument("state"); q.add_argument("--stage", required=True); q.add_argument("--artifact", action="append", default=[])
    q = sub.add_parser("invalidate"); q.add_argument("state"); q.add_argument("--change-class", required=True)
    q = sub.add_parser("migrate"); q.add_argument("project_dir"); q.add_argument("--project", required=True); q.add_argument("--mode", default="production", choices=["explore","draft","production"])
    args = p.parse_args(argv)
    if args.command == "init":
        out = Path(args.output_dir) / f"{args.project}_workflow.yaml"; out.parent.mkdir(parents=True, exist_ok=True); data = new_state(args.project, args.mode, args.output_dir); _write(out, data)
    elif args.command == "status": out, data = Path(args.state), _read(args.state)
    elif args.command == "sync": out, data = Path(args.state), sync(args.state)
    elif args.command == "approve": out, data = Path(args.state), approve(args.state, args.stage, args.artifact)
    elif args.command == "invalidate": out, data = Path(args.state), invalidate(args.state, args.change_class)
    else: out, data = migrate(args.project_dir, args.project, args.mode)
    if getattr(args, "json", False): print(json.dumps(data, indent=2))
    else: print(out); print(f"next_action: {data['next_action']}")


if __name__ == "__main__":
    main()
