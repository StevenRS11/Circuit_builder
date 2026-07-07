# Promotion — how bench results become permanent capability (W2)

The validation loop's contract: **every bench session deposits something.**
Mistakes become lessons encoded as gates/checks/tests; successes become
proven blocks, verified pinouts, and eval anchors. The ledger
(`validated_boards.yaml`) is the registry of record; `check_ledger.py`
mechanically verifies that every claimed deposit still exists — a lesson that
lives only in prose is not a lesson.

The ritual, after any bench session:

1. Complete the bringup checklist (`templates/10_bringup.md`, authored at
   delivery) or note the field observations.
2. Fill `templates/10_field_report.md` (~10 minutes).
3. Route each failure and each success through the tables below.
4. Update `validated_boards.yaml`; run
   `python scripts/check_ledger.py --strict` — it must pass.

## Failure routing — where each defect class gets encoded

Encode at the **cheapest layer that would have caught it**, exactly once,
with a test. The `encoded_in` ledger entry is `path::needle` so the checker
can prove the encoding still exists after refactors.

| Class | The defect was… | Encode it in |
|-------|-----------------|--------------|
| `pinout` | wrong pin number/name/function reached the netlist | fix + promote the `{MPN}.facts.yaml` → `pinouts/pinout_db.json`; if a *process* let it through, also tighten the Stage-4 re-derivation recipe (`references/subagents.md`) |
| `topology` | parts wired in a way that's electrically wrong though internally consistent | `generate_from_data.py` pre-flight gate (pattern is pin-type/net-shape detectable) or an `analyze_dc.py` / `analyze_analog.py` pattern + recipe; always with a regression test |
| `sourcing` | wrong physical part despite a well-formed BOM line | `check_pcbway.py` structural gate if offline-detectable; otherwise it's evidence for the answer-blind `bom_verify.py` coverage rules (e.g. widen the worklist) |
| `layout` | placement/routing defect on a correct schematic | `analyze_pcb_si.py` check if pattern-detectable today; else record in the W3 seed list (ROADMAP) — W3's check list is built from exactly these |
| `process` | a workflow/handling mistake (off-grid hand edit, skipped F8, stale cache) | the relevant SKILL.md step or reference doc (`ingest.md`, `layout_authoring.md`), phrased as a recognizable failure signature |

**Worked example (the canonical one):** battery_3s v3.0, CH224K pin 1 VDD
(internal LDO output) tied to VBUS. Class `topology` → became
`generate_from_data.py` pre-flight gate #5 (a device's `power_out` pin
sharing a net with its own `power_in` pin) + regression test
`test_regulator_output_shorted_to_supply`. The same session's four
wrong-distributor-code lines were class `sourcing` → already-existing
`bom_verify.py` answer-blind pass caught them; the ledger records it as the
evidence for why that pass is mandatory.

## Success routing

| Evidence | Promotion | Where |
|----------|-----------|-------|
| Bringup checklist passed on a physical board | ledger status → `validated` (or `validated_with_lessons`) | `validated_boards.yaml` |
| A subcircuit worked as designed (active silicon, defined function, beyond trivial) | **block extraction** — the W1a pipeline (`extract_block.py`); record under `blocks_extracted` | `blocks/{name}/` |
| A novel IC behaved per its fact card in silicon | promote the card into `pinouts/pinout_db.json` — bench-verified beats datasheet-verified | `pinout_db.json` |
| Board stable in real use | **Tier-3 eval anchor**: freeze inputs + golden, wire into `evals/` (regen byte-stable + all verifiers pass); record under `eval_anchor` | `evals/corpus/validated/{board}/` |

Promotion standards:
- `validated` requires a **passed bringup checklist on hardware**, not "it
  probably works." Rework counts (note it), simulation doesn't.
- A block is extracted from the **as-validated revision** — never from a rev
  that differs from what was on the bench.
- Ledger `bench_date`, board rev, and physical-unit identity must be real;
  when unknown, `null` + a comment beats a plausible guess.

## Retroactive seeding (the current fleet)

Sequencing per ROADMAP: infrastructure first, then **one end-to-end proof on
the validated NAU7802 block** (field report → ledger entry → W1a block
extraction → block passes `check_block.py`), then the bulk pass over the
remaining boards (CoF testers, deflection testers, scales) with the user at
the bench. battery_3s v3.0/v3.1 are already seeded in the ledger from the
2026-07-06 session.
