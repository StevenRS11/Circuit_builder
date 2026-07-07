# Circuit Builder — KiCad design & review skills for Claude Code

Two Claude Code skills over one shared Python toolchain (pure S-expression parsing —
no KiCad installation required except for final fab exports):

- **[`kicad-schematic-gen`](#designing-a-new-board--kicad-schematic-gen)** — the
  **designer**: natural-language description → gated, verify-at-every-step workflow →
  production-ready `.kicad_sch` + BOM + PCBway upload package.
- **[`kicad-board-context`](#reviewing-an-existing-board--kicad-board-context)** — the
  **reviewer**: an existing KiCad project → extracted, verified "context pack" → review,
  debug, explain, part reselection, or modification planning.

> ⚠️ AI-generated/AI-reviewed schematics. Always verify pin assignments against
> datasheets and run KiCad's ERC before committing to a PCB layout.

## What these are good at (and not)

Both skills share a sweet spot: **small-to-medium, flat, single-sheet boards** — the
kind where correctness lives in part choice, pinouts, and a few dozen nets.

| Works well | Out of scope |
|------------|--------------|
| Breakout boards | Motherboard-class designs (SoC + DDR fanout, length-tuned buses) |
| Sensor boards (I²C/SPI front-ends, ADCs, load cells) | Multi-hundred-net hierarchical / multi-sheet projects |
| Analog signal chains (filters, references, bridges) | FPGA/high-speed digital where SI simulation is the design work |
| Power supplies (LDO, buck/boost, battery charging, USB-PD) | Anything needing SPICE-level simulation (checks are pattern-based DC/completeness, not simulation) |

The reviewer will *ingest* a big board without complaint, but its analyzers and the
reconstructed-intent reasoning are built for the left column. On the right column you'll
get honest structural checks and not much judgment. Additional reviewer caveats: KiCad
7/8 format, flat schematics (hierarchical sheets aren't traversed), and hand-built
schematics with a stale embedded symbol cache can over-report connectivity errors until
they're re-saved in KiCad (recognition guidance ships in the skill's
`references/ingest.md`).

## Design philosophy

The whole system is one loop:

> **Claude generates context → verifies that context → authors from it → verifies what it authored with scripts.**

- **Scripts live at the *verify* steps** (and as the deterministic assembler,
  `generate_from_data.py`, which only emits a file after its gates pass).
- **Claude lives at the *generate/author* steps** — research, topology, part selection,
  netlist, layout, interpretation.
- **Never script the judgment.** A script that *checks* a decision is welcome; one that
  *makes* it is not.

The reviewer runs the same loop **backwards**, with the risk inverted: greenfield risk
is authoring wrong facts; brownfield risk is *misreading* the artifact. So scripts do
all the reading, Claude does all the interpreting — no conclusions from eyeballing raw
S-expressions. Non-scriptable verification legs in both skills run as **answer-blind
subagents**: isolated context means an unbiased check.

---

## Designing a new board — `kicad-schematic-gen`

Describe a board ("a USB-C powered 3.3 V sensor breakout around a BME280") and the skill
walks a **gated collaborative workflow**, producing a reviewable document at each stage.
The approved spec's **Requirements Checklist is the test suite** for the build;
`[CRITICAL]` requirements are hard gates no part choice may violate.

| Stage | Output | Gate |
|------:|--------|------|
| 1 · Specification | Formal spec + numbered requirements checklist | User signs off |
| 2 · Component Sourcing | Candidate parts w/ tradeoffs, sourcing, KiCad libs | User picks winners |
| 3 · BOM | Final parts list + PCBway sourcing + traceability | `check_pcbway` 0 blocks, `check_requirements` 0 errors |
| 4 · Implementation Reference | Per-IC pinouts, app circuits, passive sizing | User verifies pinouts |
| 5 · DC Analysis | Dividers, LDO thermal, LED current, budgets | `analyze_dc` passes |
| 5b · Netlist | Pin-level connectivity YAML + analog front-end check | `analyze_analog` passes |
| 6 · Schematic Generation | `.kicad_sch` from netlist + BOM + layout YAML | Engine self-verifies |
| 7 · Verification | Validator + netlist verify + BOM cross-check + design review | All checks clean |
| 8 · PCB Layout Review | Placement / trace-width / thermal / signal-integrity review | User fixes before fab |
| 9 · PCBway Upload Package | BOM xlsx + gerbers + centroid, answer-blind BOM verify | 0 mismatches, DRC clean |

```bash
# Stage 6 engine — the primary generation path
python .claude/skills/kicad-schematic-gen/scripts/generate_from_data.py \
    netlist.yaml bom_flat.md layout.yaml -o board.kicad_sch

# Verify any .kicad_sch: structure, intent, BOM parity
python .claude/skills/kicad-schematic-gen/scripts/validate_kicad_sch.py board.kicad_sch --json
python .claude/skills/kicad-schematic-gen/scripts/verify_netlist.py netlist.yaml board.kicad_sch
python .claude/skills/kicad-schematic-gen/scripts/cross_check_bom.py bom.md board.kicad_sch
```

Used as a skill you don't call these directly — describe the board and Claude drives the
stages. The CLIs exist so every step is independently inspectable.

---

## Reviewing an existing board — `kicad-board-context`

Point Claude at a KiCad project you already have — *"review this board"*, *"why doesn't
the charger start?"*, *"what does this thing even do?"*, *"the ADC is EOL, find a
replacement"* — and the skill first builds a **context pack**, then reasons over it.

**How to use it:** just ask, with the project directory in reach. There's no stage
gauntlet. The skill:

1. **Ingests** (deterministic): decompiles the schematic into the same pin-level netlist
   YAML and flat BOM the designer skill uses, summarizes the `.kicad_pcb`, and runs the
   structural validator. Everything lands in `claude_context/` **next to your KiCad
   files, which are never modified** — with source-file hashes so a stale context is
   detected and re-ingested, never reasoned over.
2. **Reconciles**: cross-checks schematic ↔ board ↔ BOM and reports drift (values changed
   on one side, footprints swapped on the board, symbol fields that never reached the
   board because F8 wasn't run). Drift *is* a large share of real defects — it surfaces
   before any reasoning starts.
3. **Enriches** (proportional to the task): datasheets + verified fact cards for the ICs
   that matter, net classifications, and a *reconstructed design intent* doc you correct —
   every claim marked `[INFERRED]` until you confirm it.
4. **Reasons** in whatever mode the question calls for: **REVIEW** (full analyzer suite +
   answer-blind design review), **DEBUG** (hypothesis-driven, asks for the cheapest
   discriminating bench measurement), **EXPLAIN**, **RESELECT** (replacement-part search
   with pin-compatibility verified against both datasheets), **BAKE** (repair the PCBWay
   BOM-identity fields on the schematic — the one sanctioned, user-approved write), or
   **MODIFY** (plans the change, then hands off into the designer pipeline — the context
   pack is format-identical to its stage documents).

Findings accumulate in dated files, including ruled-out hypotheses — a later session
picks up where the last one stopped instead of re-deriving everything.

```bash
CTX=.claude/skills/kicad-board-context/scripts

# Ingest: schematic → netlist YAML + flat BOM; board → summary
python $CTX/extract_netlist.py board.kicad_sch -o claude_context/netlist.yaml
python $CTX/extract_bom.py     board.kicad_sch -o claude_context/bom_flat.md
python $CTX/summarize_pcb.py   board.kicad_pcb -o claude_context/pcb_summary.yaml

# Reconcile: drift report across schematic / BOM / board (exit 1 on drift)
python $CTX/reconcile.py board.kicad_sch --bom claude_context/bom_flat.md --pcb board.kicad_pcb
```

---

## Shared tooling

**Generators & fixers**
- `generate_from_data.py` — Stage 6 engine. Joins netlist + flat BOM + layout YAML into a
  `.kicad_sch`; pre-flight gates + self-verification; refuses to emit a file with errors.
- `generate_kicad_sch.py` — core `KicadSchematic` builder library (fallback / custom symbols).
- `generate_pcbway_bom.py` / `generate_fab_outputs.py` — PCBway BOM xlsx and DRC-gated
  gerbers + drill + centroid.
- `bake_bom_fields.py` — retrofit/repair PCBWay identity fields on an existing board:
  reconciling (removes stale fields and shadow MPN aliases), lock-aware, idempotent.

**Verifiers & analyzers** (all direction-agnostic — they run on generated *and* existing boards)
- `validate_kicad_sch.py` — 11 structural connectivity checks.
- `verify_netlist.py` — schematic connectivity vs. intended netlist.
- `cross_check_bom.py` — schematic vs. BOM (refs, values, footprints; fuzzy value match).
- `analyze_dc.py` — DC correctness: dividers, LDO dropout/thermal, LED current, I²C rise
  time, current budgets, cap sizing.
- `analyze_analog.py` — analog front-end completeness (filters, decoupling, ratiometric refs).
- `analyze_pcb_si.py` — PCB signal-integrity backstop (diff pairs, reference layers,
  aggressors, return vias).
- `check_requirements.py` / `check_cards.py` / `check_pcbway.py` / `bom_verify.py` —
  traceability, fact-card drift, PCBway assemblability, answer-blind BOM verification.
- `check_kicad_library.py` / `lookup_pinout.py` — real `lib_id` + footprint + pins from
  installed (built-in **and** user) libraries; verified-pinout lookup.

**Data**
- `preferences.yaml` — footprints, preferred parts, power rails, connectors, assembly
  rubric, design rules.
- `footprint_map.yaml` — ~380 package names → exact KiCad footprint IDs.
- `analog_recipes.yaml` / `pinouts/pinout_db.json` — analog front-end knowledge +
  verified IC pinout database.
- `templates/` · `references/` in each skill — stage/context document templates and
  authoring, review, and ingest guides.

## Architecture notes

- **Builder pattern** — register lib symbols → place components → wire via labels → audit
  → `save()` to S-expression.
- **Netlist-driven wiring** — every pin gets a stub + label; if the label matches the
  netlist YAML, the net is correct by construction.
- **One data model, both directions** — the designer authors netlist/BOM/layout docs and
  compiles them to a schematic; the reviewer decompiles a schematic back into the same
  docs. That symmetry is what lets one analyzer suite serve both.
- **Coordinates** — 1.27 mm grid snapping; Y-axis inverted between symbol and schematic
  space (handled by `get_pin_position`).
- **Reference resolution** — refs come from the `(instances …)` block for the root sheet,
  not the (often stale) property cache.
- Targets KiCad 7/8 file format (version 20230121).

## Testing

```bash
# Everything (both skills + evals) — 534 tests
python -m pytest .claude/skills/ -v

# Designer: unit + integration (byte-stable golden snapshot) and eval tiers 0/0b/1
python -m pytest .claude/skills/kicad-schematic-gen/tests/ -v
python -m pytest .claude/skills/kicad-schematic-gen/evals/ -v

# Reviewer: ingest round-trip + drift-detection tests
python -m pytest .claude/skills/kicad-board-context/tests/ -v
```

## License

See repository for license details.
