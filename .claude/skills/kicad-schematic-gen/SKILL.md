---
name: kicad-schematic-gen
description: Create a NEW KiCad schematic or PCB design artifact from a user's requirements, including breakout boards, sensor boards, power supply circuits, chargers, and circuits built around specific ICs. Use only when the user wants a new design artifact. For an existing .kicad_sch/.kicad_pcb project use kicad-board-context; for a downloaded vendor library archive use kicad-import-lib; answer conceptual electronics questions directly without this skill.
---

# KiCad schematic generator

Route first, then load only the context required by the selected mode and stage.

## Route and select a mode

1. If an existing KiCad artifact is the subject, stop and use `kicad-board-context`.
2. If the task is installing a vendor symbol/footprint bundle, stop and use `kicad-import-lib`.
3. If no new artifact is requested, answer the electronics question directly.
4. Otherwise infer one mode. Ask only when fabrication intent or expected fidelity is ambiguous:
   - **Explore** — requirements, feasibility, topology, or candidate comparison; no schematic claim.
   - **Draft** — preliminary schematic with structural verification; no sourcing/fab/bringup claim.
   - **Production** — fabrication-intended ten-stage workflow and every safety gate.
   - **Resume** — continue a stateful run after checking hashes and invalidations.

`Answer` is the direct-answer route above and creates no workflow state. Never relabel Explore or Draft output as production-ready. Promotion to Production runs every missing production gate.

## Start or resume

Read `references/stages/manifest.yaml`, then use `scripts/workflow_state.py`:

```bash
python scripts/workflow_state.py init PROJECT --mode draft --output-dir OUTPUTS
python scripts/workflow_state.py status OUTPUTS/PROJECT_workflow.yaml --json
python scripts/workflow_state.py sync OUTPUTS/PROJECT_workflow.yaml --json
```

For a legacy run, use `migrate`; discovered user-gated artifacts are always `confirmation_required`, never silently approved. Before Resume, run `sync` and start at the earliest invalid, incomplete, or confirmation-required stage.

## Load proportional context

- Read the selected mode file under `references/stages/`.
- For Production, read `references/stages/production-workflow.md` only after mode selection. Within it, jump to the active stage; do not load unrelated stage references.
- Load preferences with `scripts/load_preferences.py --stage STAGE`. Do not read the complete preference store unless the command reports that the stage needs all sections.
- Read `references/subagents.md` only for research or non-scriptable verification.
- Read KiCad format, builder, analog, layout, promotion, or PCBWay references only when the stage manifest names them.

## Invariants

- Treat the approved requirements checklist as the design test suite. `[CRITICAL]` requirements block Production progression.
- Let Claude author judgment; use scripts to assemble, inspect, and verify it.
- Keep authoring and user approvals on the main thread. Run deterministic scripts directly.
- Give answer-blind verifiers frozen inputs without the hoped-for answer.
- A verifier failure blocks automatic progression. First verify its inputs. If evidence still conflicts, run a second independent verifier, persist both cited verdicts, and ask the user if unresolved.
- Author source data and regenerate; never repair generated schematic or fabrication outputs by hand.
- Record approval only after explicit user sign-off:

```bash
python scripts/workflow_state.py approve STATE --stage 01 --artifact SPEC
```

## Completion

Run every validator required by the active stage manifest, then `sync`. Report mode, completed artifacts, remaining invalidations, warnings, and the next user decision. Production delivery must retain the human datasheet/ERC review warning.
