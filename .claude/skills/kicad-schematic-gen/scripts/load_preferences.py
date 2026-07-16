"""Load only the preference concerns needed by a workflow stage."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "preferences" / "manifest.yaml"
STAGES = ROOT / "references" / "stages" / "manifest.yaml"


def load_preferences(*, concerns=(), stage=None, root=ROOT):
    root = Path(root)
    manifest = yaml.safe_load((root / "preferences" / "manifest.yaml").read_text(encoding="utf-8"))
    requested = list(concerns)
    if stage:
        stages = yaml.safe_load((root / "references" / "stages" / "manifest.yaml").read_text(encoding="utf-8"))
        requested.extend(stages["stages"][str(stage)].get("preferences", []))
    requested = list(dict.fromkeys(requested))
    unknown = sorted(set(requested) - set(manifest["concerns"]))
    if unknown:
        raise ValueError(f"unknown preference concerns: {', '.join(unknown)}")
    source = (root / "preferences" / manifest["legacy_source"]).resolve()
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    result = {}
    for concern in requested:
        for key in manifest["concerns"][concern]:
            if key not in raw:
                raise ValueError(f"preference section {key!r} required by {concern!r} is missing")
            result[key] = raw[key]
    return result


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--stage")
    p.add_argument("--concern", action="append", default=[])
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    data = load_preferences(concerns=args.concern, stage=args.stage)
    print(json.dumps(data, indent=2) if args.json else yaml.safe_dump(data, sort_keys=False))


if __name__ == "__main__":
    main()
