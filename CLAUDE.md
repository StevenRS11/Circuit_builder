# Circuit Builder repository guidance

This repository contains three KiCad skills sharing one deterministic Python toolchain.

## Route work

- Use `kicad-schematic-gen` only when the user requests a new KiCad design artifact.
- Use `kicad-board-context` whenever an existing schematic, PCB, BOM, or project is the subject.
- Use `kicad-import-lib` for a downloaded vendor symbol/footprint/3D-model archive.
- Answer conceptual electronics questions directly when no artifact is requested.

The selected skill is authoritative for its workflow. Do not preload sibling skills or detailed stage references.

## Architecture invariants

- Claude researches and authors design judgment; scripts assemble, extract, analyze, and verify it.
- The approved requirements checklist is the production design's test suite. `[CRITICAL]` requirements block progression.
- Use the data-driven generator for normal schematic creation. Edit source YAML/Markdown and regenerate; do not hand-edit generated outputs.
- Existing KiCad artifacts are read through the board-context extraction scripts, not by freehand S-expression interpretation.
- Use answer-blind subagents only for research and non-scriptable verification. Run deterministic scripts directly.
- A verifier failure blocks automatic progression. Validate its inputs, obtain a second independent verdict when evidence conflicts, and escalate unresolved conflicts.
- Preserve user changes and avoid edits to generated project artifacts unless the selected workflow explicitly authorizes them.

## Important locations

- `.claude/skills/kicad-schematic-gen/SKILL.md` — greenfield router/controller.
- `.claude/skills/kicad-schematic-gen/references/stages/manifest.yaml` — modes, dependencies, preference concerns, validators, and invalidation classes.
- `.claude/skills/kicad-schematic-gen/scripts/workflow_state.py` — init, resume, migration, approval, hashing, and invalidation.
- `.claude/skills/kicad-board-context/SKILL.md` — brownfield context extraction and reasoning modes.
- `.claude/skills/kicad-import-lib/SKILL.md` — vendor library installation.

## Verification

```powershell
python -m pytest .claude/skills/ -q
python C:/Users/steve/.codex/skills/.system/skill-creator/scripts/quick_validate.py .claude/skills/kicad-schematic-gen
```

Run targeted tests while developing, then the full suite. Do not update golden files unless generator behavior intentionally changed and the regenerated schematic was reviewed.
