# `kicad-schematic-gen` eval suite

The scripts are unit-tested in `../tests/`. This suite measures the **other half** — the
part the design hierarchy leaves to the model: does Claude **route and select a proportional
mode**, **follow production gates**, **resolve verifier conflicts**, and **author good designs**? It is
organized in honest tiers matched to what is actually verifiable today.

> **Provenance is the load-bearing idea.** A fixture plays one of two roles, with different
> trust requirements. A *regression anchor* only needs self-consistent inputs. A *correctness
> anchor* — the thing you grade a fresh design against — must be a **physically built,
> bench-verified board**. No board is built yet, so today the suite is regression + process +
> triggering only; the judgment-quality tier waits for the first validated anchor (NAU7802).

## Layout

```
evals/
  graders/run_all_verifiers.py   one entry point: run every applicable verifier on a case dir
  corpus/
    synthetic/   self-consistent, UNBUILT cases (regression/process) — e.g. battery_3s_full
    validated/   BUILT + bench-verified boards (correctness ground truth) — EMPTY today
  regression/    frozen [CRITICAL] NEGATIVE cases — the gate must bite (e.g. critical_bq24650)
  triggering/    should / should-not prompt corpora
  behavioral/    gate-adherence prompt set + rubric + saved transcripts (manual protocol)
  test_evals.py  pytest entry for the deterministic tiers (0 / 0b / 1)
```

## Tiers

| Tier | What | How it runs |
|------|------|-------------|
| **0 — full-pipeline regression** | each `corpus/synthetic/*` case regenerates byte-deterministically (`uuid_seed=0`) and passes every applicable verifier | `pytest` (every commit) |
| **0b — `[CRITICAL]` negative** | each `regression/*` case MUST fail its gate (negative-space: proves the gate bites) | `pytest` |
| **1 — triggering** | should/should-not corpora are on/off-topic; SKILL.md description still advertises core capabilities (drift guard) | `pytest` |
| **2 — gate adherence** | does Claude stop at each gate, run scripts in order, honor `[CRITICAL]` verdicts | **manual protocol** (below) |
| **3 — judgment quality** | is the *fresh* design good (part choice, topology)? | **deferred** until a validated anchor exists |

## Running

```bash
# Deterministic tiers (0/0b/1)
python -m pytest .claude/skills/kicad-schematic-gen/evals/ -v

# Grade one corpus case with every verifier (human-readable)
python .claude/skills/kicad-schematic-gen/evals/graders/run_all_verifiers.py \
    .claude/skills/kicad-schematic-gen/evals/corpus/synthetic/battery_3s_full

# Refresh a golden after an INTENTIONAL generator/inputs change
python evals/graders/run_all_verifiers.py <case_dir> --update-golden
```

A `case.yaml` records the hard contract (every applicable grader passes + golden byte-matches)
and the *observed* warning/info profile (documented, not asserted — errors are the contract).

## Tier 2 — gate-adherence protocol (manual, periodic)

Tiers 0/0b/1 need no model. Tier 2 does, and **subagents distribute the trajectory across
multiple transcripts**, so it is a documented protocol rather than a pytest:

1. Run each prompt in `behavioral/prompts.md` in a fresh session against the skill.
2. Score the run against `behavioral/rubric.md` (stopped at each user gate; ran scripts in the
   right order — `analyze_dc` before generation, `check_requirements` at Stage 3 **and** 7;
   didn't skip stages; **did not override a `[CRITICAL]` `fails`** from a verifier subagent).
3. Save the transcript (main + stitched subagent transcripts) under `behavioral/runs/`.

This is the layer that catches a SKILL.md prose edit silently breaking a behavior. Run it on
releases / after meaningful SKILL.md changes, not every commit.

## Promotion path — first validated anchor (NAU7802 load-cell board)

When that board is **fabbed and bench-verified**, promote it from a design into the gold tier:

1. Freeze its full current-skill artifact set into `corpus/validated/nau7802_loadcell/`
   (same selective freeze as `battery_3s_full`: text artifacts + cards + `uuid_seed=0` golden;
   no PDFs/backups). Add a `case.yaml` with `provenance: validated`.
2. `run_all_verifiers.py <dir> --update-golden`, then open the golden in KiCad to confirm.
3. Promote its `pinout_verified` fact-card pinouts into `../pinouts/pinout_db.json` (flywheel).
4. Record provenance in `case.yaml` (fab date + exactly what was bench-verified).
5. It becomes the first **Tier-3** judgment-quality anchor and a **Tier-2** behavioral target.

## Adding a new synthetic case

Drop a case dir under `corpus/synthetic/<name>/` with the normalized artifacts
(`05b_netlist.yaml`, `03_bom_flat.md`, `06_layout.yaml` at minimum; add `03_bom.md`,
`01_specification.md` + `07_traceability.yaml`, `04b_design.yaml`, `datasheets/` to light up
more graders), run `run_all_verifiers.py <dir> --update-golden`, and write `case.yaml`. Tier 0
picks it up automatically.
