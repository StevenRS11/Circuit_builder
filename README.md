# KiCad Schematic Generator

Generate production-ready KiCad `.kicad_sch` schematics from natural-language board
descriptions — no KiCad installation required for generation. Pure-Python S-expression
authoring, packaged as a Claude Code skill that runs a gated, verify-at-every-step
design workflow from spec to PCBway upload package.

> ⚠️ AI-generated schematics. Always verify pin assignments against datasheets and run
> KiCad's ERC before committing to a PCB layout.

## What it does

You describe a board ("a USB-C powered 3.3 V sensor breakout around a BME280"); the skill
walks an **8-stage collaborative workflow**, producing a reviewable document at each gate
and ending with a valid `.kicad_sch` (plus BOM, sourcing sheet, and fab-ready upload
package). Each stage re-checks its output against the approved spec, so errors surface
early — before they're baked into copper.

## Design philosophy

The whole system is one loop:

> **Claude generates context → verifies that context → authors from it → verifies what it authored with scripts.**

- **Scripts live at the *verify* steps** (and as the deterministic assembler,
  `generate_from_data.py`, which only emits a file after its gates pass).
- **Claude lives at the *generate/author* steps** — research, topology, part selection,
  netlist, and layout.
- **Never script the judgment.** A script that *checks* a decision is welcome; one that
  *makes* it is not. This works because circuits are well-described in natural language
  and nearly every artifact is plain text (Markdown, YAML, S-expressions).

The approved spec's **Requirements Checklist is the test suite** for the build, and
`[CRITICAL]` requirements are hard gates. Non-scriptable verification legs run as
**answer-blind subagents** — isolated context means an unbiased check.

## The workflow

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

## Quick start

```bash
# Generate a schematic from data (Stage 6 engine — primary path)
python .claude/skills/kicad-schematic-gen/scripts/generate_from_data.py \
    netlist.yaml bom_flat.md layout.yaml -o board.kicad_sch

# Validate a .kicad_sch file
python .claude/skills/kicad-schematic-gen/scripts/validate_kicad_sch.py board.kicad_sch --json

# Verify connectivity against the intended netlist
python .claude/skills/kicad-schematic-gen/scripts/verify_netlist.py netlist.yaml board.kicad_sch

# Cross-check the BOM against the schematic
python .claude/skills/kicad-schematic-gen/scripts/cross_check_bom.py bom.md board.kicad_sch
```

Used as a Claude Code skill, you don't call these directly — describe the board and the
skill drives the stages. The CLIs exist so every step is independently inspectable.

## Tooling

**Generators**
- `generate_from_data.py` — Stage 6 engine. Joins netlist (connectivity) + flat BOM
  (value/footprint) + layout YAML (placement + IC pin-sides) into a `.kicad_sch`. Runs
  pre-flight join/pin-set gates and self-verifies before saving; refuses to emit a file
  with errors.
- `generate_kicad_sch.py` — Core `KicadSchematic` builder library (fallback / custom symbols).
- `generate_pcbway_bom.py` / `generate_fab_outputs.py` — Stage 9 PCBway BOM (9-column xlsx)
  and DRC-gated gerbers + drill + centroid.

**Verifiers & analyzers**
- `validate_kicad_sch.py` — 11 connectivity checks (floating pins, dangling wires,
  duplicate refs, missing junctions, …).
- `verify_netlist.py` — schematic connectivity vs. intended netlist (completeness,
  consistency, connectivity).
- `cross_check_bom.py` — schematic vs. BOM (refs, values, footprints; fuzzy value match).
- `analyze_dc.py` — DC correctness: dividers, LDO dropout/thermal, LED current, I²C rise
  time, current budgets, cap sizing.
- `analyze_analog.py` — analog front-end completeness (anti-alias/EMI filters, decoupling,
  ratiometric reference) for sensitive designs.
- `analyze_pcb_si.py` — PCB signal-integrity backstop (diff-pair symmetry, reference
  layers, aggressor proximity, return vias).
- `check_requirements.py` / `check_cards.py` / `check_pcbway.py` / `bom_verify.py` —
  traceability, fact-card drift, PCBway assemblability, and answer-blind BOM verification.
- `check_kicad_library.py` / `lookup_pinout.py` — resolve real `lib_id` + footprint + pins
  from installed (built-in **and** user) libraries; verified-pinout lookup.

**Data**
- `preferences.yaml` — footprints, preferred/on-hand parts, power rails, connectors,
  assembly settings + selection rubric, design rules.
- `footprint_map.yaml` — ~380 package names → exact KiCad footprint IDs.
- `analog_recipes.yaml` / `pinouts/pinout_db.json` — analog front-end knowledge + verified
  IC pinout database.
- `templates/` · `references/` — stage document templates and authoring/review guides.

## Architecture notes

- **Builder pattern** — register lib symbols → place components → wire via labels → audit
  → `save()` to S-expression.
- **Netlist-driven wiring** — every pin gets a stub + label; if the label matches the
  netlist YAML, the net is correct by construction.
- **Coordinates** — 1.27 mm grid snapping; Y-axis inverted between symbol and schematic
  space (handled by `get_pin_position`).
- **Reference resolution** — refs come from the `(instances …)` block for the root sheet,
  not the (often stale) property cache.
- Targets KiCad 7/8 file format (version 20230121).

## Testing

```bash
# Unit + integration tests (includes byte-stable golden snapshot)
python -m pytest .claude/skills/kicad-schematic-gen/tests/ -v

# Skill-level eval suite (deterministic tiers 0/0b/1)
python -m pytest .claude/skills/kicad-schematic-gen/evals/ -v
```

## License

See repository for license details.
