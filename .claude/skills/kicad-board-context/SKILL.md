---
name: kicad-board-context
description: Build a trustworthy reasoning context from an EXISTING KiCad project (.kicad_sch / .kicad_pcb / BOM), then reason about the board — review it, debug it, explain it, pick replacement parts, or plan modifications. Use this skill whenever the user points at a board that already exists: "review my board", "why doesn't this circuit work", "look at this schematic", "check this KiCad project", "what does this board do", "help me debug", "replace this part on my board", "add X to my existing board". For designing a NEW board or schematic from a description, use kicad-schematic-gen instead — this skill can hand off to it once context is built.
---

# KiCad Board Context — reason about boards that already exist

The generator skill (`kicad-schematic-gen`) runs *forward*: intent → documents
→ artifacts, verifying each step. This skill runs the same loop *backwards*:
artifacts → extracted documents → reconstructed intent → reasoning. Both obey
the same design hierarchy, with the risk inverted:

> **Greenfield risk is authoring wrong facts. Brownfield risk is misreading
> the artifact. So: scripts do ALL the reading; Claude does ALL the
> interpreting. Never freehand-read a `.kicad_sch`/`.kicad_pcb` and reason
> from what you think you saw — extract with the scripts, reason over the
> extracted documents.**

The payoff of extracting into the **same data model** the generator uses
(Stage-5b netlist YAML, flat BOM markdown, fact cards, datasheet cache): every
existing analyzer, verifier, and subagent recipe works on an existing board
exactly as it does on a generated one, and any modification task can hand off
into the generator pipeline at the right stage with zero translation.

**Shared code lives in `../kicad-schematic-gen/scripts/`** (referenced below as
`{GEN}`). This skill's own ingest scripts live in `scripts/` here (`{CTX}`).
Never copy shared modules across; fix them in place so both skills benefit.

Read `../kicad-schematic-gen/preferences.yaml` before recommending parts or
changes — the user's footprint defaults, preferred parts, PCBway assembly
rubric, and design rules all still apply to brownfield work.

---

## The context pack

Everything derived from a board lives in a `claude_context/` directory next to
the user's KiCad files (which are **never modified** — see Rules):

```
{kicad_project}/
├── myboard.kicad_sch / .kicad_pcb / .kicad_pro   # user's files — read-only
└── claude_context/
    ├── 00_context_pack.md        # manifest + provenance (source hashes) — templates/00_context_pack.md
    ├── netlist.yaml              # extracted Stage-5b-format connectivity (+ class: tags from ENRICH)
    ├── bom_flat.md               # harvested from schematic fields — Stage-3 flat format
    ├── pcb_summary.yaml          # stackup, placements, routed-net stats
    ├── reconstructed_intent.md   # Claude-authored, user-corrected — templates/reconstructed_intent.md
    ├── datasheets/               # same cache convention as the generator:
    │                             #   {MPN}.pdf + {MPN}.facts.yaml + index.md
    └── findings/
        └── YYYY-MM-DD_<topic>.md # dated, accumulating — templates/findings.md
```

**Staleness rule:** `00_context_pack.md` records the sha256 of each source
file at extraction time. **Before any reasoning session, re-hash the sources**
(each extracted doc also carries its `source_sha256`). A mismatch means the
user edited the board since extraction — re-run ingest first. Reasoning over a
stale model of the board is the same defect class as an F8-less BOM upload.

If a context pack already exists and hashes match, **do not re-ingest** — load
the pack and continue. Prior findings in `findings/` are part of the context:
read them before re-deriving anything ("we ruled that out in June" is real
signal).

---

## Phase 1 — INGEST (always; cheap; deterministic)

Locate the project files (`.kicad_pro` names the project; there may be no PCB
yet, or no BOM doc — extract what exists). Then:

```bash
# connectivity → Stage-5b format (self-verifies round-trip against the source)
python {CTX}/extract_netlist.py {board}.kicad_sch -o claude_context/netlist.yaml

# BOM from symbol fields → Stage-3 flat format (reports missing MPN/mfr as findings)
python {CTX}/extract_bom.py {board}.kicad_sch -o claude_context/bom_flat.md

# board summary: stackup, placements, routed-net stats (only if a .kicad_pcb exists)
python {CTX}/summarize_pcb.py {board}.kicad_pcb -o claude_context/pcb_summary.yaml

# structural health of the schematic itself
python {GEN}/validate_kicad_sch.py {board}.kicad_sch --json

# proven-block recognition (W1c): which registry blocks live on this board, and
# where the board deviates from the validated design
python {CTX}/match_blocks.py claude_context/netlist.yaml --json
```

**Block recognition is part of every ingest.** `match_blocks.py` matches
netlist fragments against the generator's `blocks/` registry (anchored on the
block's silicon, grown through net correspondence) and reports each recognized
instance with its port→net map and **every deviation from the validated
block** — "U3/C11-C14/R7 is your NAU7802 block as validated on DualScale,
except C12 is 10nF where the block says 100nF" is the highest-value sentence
brownfield review produces. The script only reads; *you* judge each deviation
(bug, improvement, or accepted change — ask the user) and check the reported
match against the block's `constraints:` (it surfaces them but cannot judge
them — e.g. two recognized NAU7802 blocks on one I2C bus is a constraints
violation the script won't call). Matches and deviations go in the findings
doc and `reconstructed_intent.md` (a recognized block is `[CONFIRMED]`-grade
structure: cite its ledger entry).

Then write `claude_context/00_context_pack.md` from the template: what was
extracted, from which files, at which hashes, with which warnings.

**Interpreting ingest output — read `references/ingest.md` first.** Two big
caveats live there: the **stale-lib_symbols-cache** loader limitation (a
hand-edited/pasted schematic whose embedded symbol cache is stale makes the
loader drop components and cascade false connectivity errors — recognize the
signature before reporting "14 errors" to the user), and what extraction can
and cannot see (extracted `netlist.yaml` is *what the wires say*, not what the
designer meant).

Extractor warnings (floating pins, blank MPN fields, unmatched NC markers) are
**findings, not extraction failures** — carry them into the findings doc, do
not "fix" them silently.

## Phase 2 — RECONCILE (whenever ≥2 artifacts exist)

A board's schematic, layout, and BOM drift apart in the real world, and that
drift *is* a large share of real defects. Make it visible before reasoning:

```bash
python {CTX}/reconcile.py {board}.kicad_sch --bom claude_context/bom_flat.md --pcb {board}.kicad_pcb --json
```

Checks refs / values / footprints between schematic and board, schematic and
BOM, and **MPN field propagation** (schematic field present but absent on the
board footprint = the "F8 never ran" failure that once uploaded a wrong BOM).
If the user hands you an *external* BOM (their own spreadsheet/markdown), pass
that as `--bom` too — drift between their BOM and their schematic is exactly
the kind of thing they need to know first.

Reconcile never decides which side is right. Present drift to the user; the
usual resolution is "schematic is the source of truth, the board/BOM is stale,"
but that is their call.

## Phase 3 — ENRICH (proportional to the task — this is judgment, not script)

Ingest gives structure; enrichment gives *meaning*. Do only what the task
needs:

- **Quick question** ("what does this pin connect to?") — no enrichment; the
  netlist answers it.
- **Debug / review / modify** — enrich the parts of the board the task
  touches:
  1. **Identify the ICs** from `bom_flat.md`. For each relevant IC, build the
     datasheet cache + `{MPN}.facts.yaml` fact card exactly as the generator's
     Stage 2/4 does — research subagent per part, answer-blind pinout
     re-derivation for anything unverified (recipes A and C in
     `../kicad-schematic-gen/references/subagents.md`). Check
     `{GEN}/lookup_pinout.py` and `check_kicad_library.py --lookup
     --project-dir {project_dir}` first — a symbol from the user's own library
     is an authoritative pinout, no re-derivation needed.
  2. **Tag net classes** by editing `claude_context/netlist.yaml` (`class:`
     analog / analog_differential / reference / rf / switching …, per the
     schema in `../kicad-schematic-gen/templates/05b_netlist.yaml`). This is
     what arms `analyze_analog` and `analyze_pcb_si`.
  3. **Author `reconstructed_intent.md`** (template in `templates/`): purpose,
     power tree, functional blocks, interfaces — every claim marked
     `[INFERRED]` until the user confirms it (`[CONFIRMED]`). **Show it to the
     user and ask what you got wrong.** This document is the brownfield
     equivalent of the Stage-1 spec: once corrected, it is the reference that
     later judgments are checked against.
- **Full audit** — enrich everything above for every block.

Fact cards and net tags written here are real artifacts: a later session (or a
generator-pipeline re-entry) reuses them from the pack.

## Phase 4 — REASON (the point of all this — open-ended, not gated)

There is no stage sequence here. Pick the mode the user's ask calls for; each
mode says which machinery to reach for. Mix modes freely; write results to a
dated `findings/` doc (template in `templates/findings.md`).

### Mode: REVIEW ("look this over / audit my board")
Run the deterministic analyzers that apply, then the answer-blind review:
- `{GEN}/validate_kicad_sch.py` (already run at ingest) — structural health
- `{CTX}/match_blocks.py` (already run at ingest) — recognized proven blocks:
  review their *deviations* instead of re-reviewing their internals (the block
  is bench-validated; the deviations are where this board differs from what
  was validated), and check each block constraint against the board
- `{GEN}/analyze_analog.py claude_context/netlist.yaml` — front-end completeness (needs class tags)
- `{GEN}/analyze_pcb_si.py {board}.kicad_pcb --netlist claude_context/netlist.yaml` — routing vs sensitivity
- `{GEN}/check_pcbway.py claude_context/bom_flat.md --json` — sourceability/assembly, MPN hygiene
- DC analysis where it applies: author a design YAML for the subcircuits you
  can parameterize (rails, dividers, LEDs, pull-ups) from the netlist + fact
  cards, run `{GEN}/analyze_dc.py`. Skeptical defaults: unknown budgets are
  questions for the user, not guesses.
- **Structural design review as an answer-blind subagent** (recipe D in
  `../kicad-schematic-gen/references/subagents.md`): hand it the extracted
  netlist, the schematic, the cached datasheets, and
  `../kicad-schematic-gen/references/design_review.md`. A failure blocks progression;
  validate its frozen inputs, obtain a second answer-blind verdict if cited evidence
  conflicts, and surface unresolved disagreement.

### Mode: DEBUG ("it doesn't work / X misbehaves")
Hypothesis-driven, with the context pack as ground truth:
1. Get the symptom precisely (measured voltages, what works vs doesn't, when).
2. Trace the affected path in `netlist.yaml` — every net between stimulus and
   symptom, checked against the fact cards' pin functions (wrong-pin wiring is
   the top defect class; the CH224K VDD→VBUS short was exactly this).
3. Form ranked hypotheses; for each, identify what the *documents* can rule in
   or out (DC analysis of the subcircuit, drift report, datasheet limits from
   the cache) before asking for bench measurements.
4. Ask the user for the cheapest discriminating measurement; iterate.
5. Record every ruled-out hypothesis in the findings doc with the evidence —
   negative results are context for next time.
6. When a root cause is confirmed at the bench, it feeds the generator
   skill's **Stage 10 promotion ritual** (`../kicad-schematic-gen/references/
   promotion.md`): file a field report, encode the lesson (gate/check/test/
   doc), and update `../kicad-schematic-gen/validated_boards.yaml` — a
   brownfield debug session deposits knowledge exactly like a greenfield
   bringup does. Check the ledger *first*, too: the board (or its defect
   class) may already have an entry.

### Mode: EXPLAIN ("what does this board / block do?")
Walk the netlist + BOM + intent doc; answer at the altitude asked. If the
explanation surfaces something odd, note it as a finding — don't silently
absorb it. For MCU boards, the firmware pinmap is part of explaining/using the
board — generate it straight from the schematic:
```bash
python {GEN}/generate_pinmap.py {board}.kicad_sch -o claude_context/firmware --sketch
```
(`board_pins.h` + bringup sketch; also the right first artifact when the
user's actual question is "why doesn't my firmware see the sensor" — compare
its pin truth against their code.)

### Mode: RESELECT ("replace this part / it's EOL / find a cheaper one")
The generator's Stage-2 machinery, seeded from the pack instead of a spec:
derive the requirements the incumbent actually satisfies **from the fact card
+ how the board uses it** (pins used, rails, package constraints from
`pcb_summary.yaml` if the footprint must be reused), then run the per-candidate
research fan-out + rubric + [CRITICAL]-style verification (recipes A/B) and the
four-gate check from `../kicad-schematic-gen/SKILL.md` Stage 2. Drop-in
replacement claims get the answer-blind treatment — pin-compatibility is
verified against both datasheets, never assumed.

### Mode: BAKE ("fix the BOM identity fields on this board")
The one write this skill is allowed to make to a user's KiCad file (see Rule 1
for the conditions). For a board whose symbols lack the PCBWay identity fields
(blank MPN/Manufacturer in `extract_bom` output — common on boards generated
before the engine baked fields, or hand-edited since):
1. Get a correct Stage-3 BOM: start from `claude_context/bom_flat.md`, fill the
   gaps with the user (research per the generator's Stage 2/3 gates where parts
   are unidentified), and run `{GEN}/check_pcbway.py` — 0 blocking required.
2. With **explicit user approval and the file backed up / committed**, bake:
   ```bash
   python {GEN}/bake_bom_fields.py {bom.md} {board}.kicad_sch
   ```
   It reconciles (insert/update/**remove stale managed fields and shadow MPN
   aliases**), refuses files KiCad has locked (`~*.lck`), and never bakes a
   distributor code or description as an MPN. Field-name contract lives in
   `check_pcbway.py` (`CANONICAL_MPN_FIELD` etc.) — never hardcode names.
3. **Propagate via KiCad, not by baking the board:** user runs F8 ("Update PCB
   from Schematic"). Direct `.kicad_pcb` baking exists as belt-and-suspenders
   but F8 is the safe default.
4. Close the loop: re-ingest, then `reconcile.py --pcb` must come back clean
   (`field_not_propagated` gone). Well-formed ≠ correct part: if the board is
   heading to fab, the Stage-9 answer-blind live verification
   (`{GEN}/bom_verify.py`) is still the gate that catches a real-looking MPN
   that resolves to the wrong physical part.

### Mode: MODIFY ("add X / change Y on this board")
Build/refresh the context pack, get `reconstructed_intent.md` confirmed, then
check the recognition report first: a change *inside* a recognized block
(component values, wiring) invalidates its bench provenance — surface the
block's `constraints:` and ledger entry before planning it. Then
**hand off to `kicad-schematic-gen`** at the right stage, using the pack's
documents as the prior-stage inputs (they are format-identical):
- new functional block → enter at Stage 1/2 with the intent doc as the spec
  baseline; existing rails/nets are constraints.
- value/part changes only → enter at Stage 3/4.
- Note on placeable modules (W1b): if the addition matches a proven block in
  the generator's `blocks/` registry, the deliverable can be that block's
  `sheet.kicad_sch` — the user places it as a hierarchical sheet in KiCad
  ("Import Sheet Pins") per `blocks/README.md`. The engine also composes
  blocks automatically via the layout YAML `blocks:` section, but only into
  schematics it generates; auto-merging into the user's existing hand-drawn
  file stays off-limits (rule 1). For non-block modules the deliverable
  remains the designed module + explicit integration instructions (nets to
  tie, refdes ranges).

---

## Rules (non-negotiable)

1. **Never modify the user's KiCad files.** Everything this skill produces
   goes in `claude_context/`. If a fix to the board is agreed, the user makes
   it in KiCad (or the task hands off to the generator pipeline whose outputs
   the user reviews) — then re-ingest and confirm the fix took.
   *Single sanctioned exception:* `bake_bom_fields.py` in BAKE mode — a
   deterministic, property-only, lock-aware, tested script — and only with
   explicit user approval and a backup/commit first. Never hand-edit the file,
   and re-ingest immediately after.
2. **Scripts read, Claude interprets.** No conclusions from eyeballing raw
   S-expressions. If a script can't see something you need, that's a gap to
   note (or a script to propose), not a license to freehand.
3. **Stale context is no context.** Hash-check before reasoning; re-ingest on
   mismatch.
4. **Findings are durable.** Dated docs in `findings/`, including ruled-out
   hypotheses and drift the user chose to accept. Next session starts by
   reading them.
5. **Subagent failures block progression** — follow the generator conflict protocol
   (`../kicad-schematic-gen/references/subagents.md`): validate inputs, re-run, obtain
   a second independent verdict for conflicting evidence, or surface to the user.
6. **Enrichment is proportional.** Don't build fact cards for 40 parts to
   answer a question about one connector. The pack grows incrementally across
   sessions.
