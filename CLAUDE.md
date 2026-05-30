# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **KiCad Schematic Generator** — a Python library that programmatically generates `.kicad_sch` files (S-expression format) compatible with KiCad 7/8. No KiCad installation is required.

The project also includes a `.skill` file (`kicad-schematic-gen.skill`) which bundles the generator script, a preferences/inventory config, and reference docs into a portable skill package.

**Design hierarchy (governs all skill changes):** the skill runs one loop — *Claude generates context → verifies that context → authors from it → verifies what it authored with scripts*. Scripts live at the verify steps (and as the deterministic assembler `generate_from_data.py`, driven by Claude-authored data); Claude lives at the generate/author steps. **Never script the judgment** (pin arrangement, placement, custom-symbol authoring, part selection) — a script that *checks* a decision is welcome, one that *makes* it is not. This works because circuits are well described in natural language and nearly every artifact is plain text. See the "Design hierarchy" section at the top of `SKILL.md`.

## Key Files

- `.claude/skills/kicad-schematic-gen/scripts/generate_kicad_sch.py` — Core library. `KicadSchematic` builder class and dataclasses. Supports `uuid_seed=` for reproducible (golden-testable) output via `seed_uuids()`.
- `.claude/skills/kicad-schematic-gen/scripts/generate_from_data.py` — **Stage 6 engine (primary generation path).** Builds a `.kicad_sch` from three data files — netlist YAML (connectivity) + flat BOM (value/footprint) + layout YAML (placement + IC pin-sides) — joined on reference designator. Pure function of its inputs (no KiCad install). Runs pre-flight join/pin-set gates and self-verifies (validate + verify_netlist + cross_check) before saving; refuses to emit a file with errors, and treats `missing_junction` as blocking (unintended wire collision / short class). Replaces per-project builder scripts.
- `.claude/skills/kicad-schematic-gen/scripts/validate_kicad_sch.py` — Schematic validator. Parses `.kicad_sch` files, extracts netlists, runs 11 connectivity checks.
- `.claude/skills/kicad-schematic-gen/scripts/analyze_dc.py` — DC analysis engine. Pattern-matches subcircuits (LDOs, dividers, LEDs, pull-ups) and validates electrical correctness before schematic generation.
- `.claude/skills/kicad-schematic-gen/scripts/verify_netlist.py` — Netlist verifier. Checks schematic connectivity against an intended netlist YAML (completeness, consistency, connectivity).
- `.claude/skills/kicad-schematic-gen/scripts/cross_check_bom.py` — BOM cross-checker. Verifies schematic matches BOM (references, values, footprints) with fuzzy value matching.
- `.claude/skills/kicad-schematic-gen/scripts/check_cards.py` — Fact-card cross-checker (Stage 6 pre-build gate). Deterministic, verify-only: joins the per-part `{MPN}.facts.yaml` fact cards against the Stage 6 layout YAML + Stage 3 flat BOM (on `lib_id`) and fails on drift between the verified card and the authored docs — BOM footprint vs `card.kicad.footprint`, each IC's `symbols:` pin number/name/type vs `card.pins`, and `pinout_verified` must be true. Warns on a `symbols:` IC with no card (`--strict` → error). The mechanical backstop for "copy, don't paraphrase"; complements the engine's `symbol pin-set == netlist pin-set` gate by adding name/type/footprint coverage. Makes no design decisions.
- `.claude/skills/kicad-schematic-gen/scripts/check_pcbway.py` — PCBway assembly checker. Scores a BOM for PCBway turnkey-assembly readiness (package assemblability, distributor PN presence, EOL/MSL/single-source flags) and emits a sourcing sheet. No network — live distributor stock is confirmed via web search in the workflow.
- `.claude/skills/kicad-schematic-gen/scripts/check_requirements.py` — Requirements traceability backstop (Stage 7). Deterministic, verify-only: parses the Stage-1 spec's numbered Requirements Checklist (`R#`, `[CRITICAL]` flags) and a Claude-authored traceability YAML (`R# → {satisfied_by: [refs], evidence}`), and checks the matrix is complete + consistent against the BOM — catching untraced requirements (dropped blocks), phantom refs, `[CRITICAL]` items lacking evidence, stale entries, and orphan ICs/connectors. It does **not** judge whether evidence is true (that stays Claude's job); `EXTERNAL` token covers off-board items. This is the structural enforcement of "verified docs are the test suite."
- `.claude/skills/kicad-schematic-gen/scripts/check_kicad_library.py` — KiCad library checker. Searches installed KiCad libraries for symbols, footprints, and pin data. Returns lib_id + footprint ready for schematic generation.
- `.claude/skills/kicad-schematic-gen/scripts/lookup_pinout.py` — Pinout database lookup. Queries verified IC pinouts to reduce hallucination risk in Stage 4.
- `.claude/skills/kicad-schematic-gen/scripts/analyze_analog.py` — Analog front-end completeness checker (Stage 5b). Operates on the netlist YAML + `analog_recipes.yaml` to verify sensitive analog chains have anti-alias/EMI input filtering, supply/reference decoupling, and a ratiometric reference *before* generation. Recipe-driven (knows a part's input/supply pins) and class-driven (net `class:` tags). Catches the "connector wired straight to ADC pin, no filter" defect.
- `.claude/skills/kicad-schematic-gen/scripts/analyze_pcb_si.py` — PCB signal-integrity / analog-noise layout backstop (Stage 8). Parses `.kicad_pcb` and checks differential-pair symmetry, reference-layer (sensitive net over a power vs GND plane), aggressor proximity (RF/switching parts), long runs, via-in-pad, and return vias. Source-impedance-aware (low-Z bridge → guarding is advisory, not required). Optional `--netlist` for authoritative net classes.
- `.claude/skills/kicad-schematic-gen/pinouts/pinout_db.json` — Verified IC pinout database (10 parts: AP2112K, AMS1117, TPS563200, CH340G, BME280, BMP390, FUSB302B, RP2040).
- `.claude/skills/kicad-schematic-gen/preferences.yaml` — User preferences: footprints, preferred/on-hand parts, power rails, connectors, PCBway assembly settings + selection rubric, design rules.
- `.claude/skills/kicad-schematic-gen/footprint_map.yaml` — Package-to-footprint lookup table. Maps ~380 common package names to exact KiCad footprint library IDs (includes transistor packages).
- `.claude/skills/kicad-schematic-gen/analog_recipes.yaml` — Front-end "recipe" knowledge base for sensitive analog ICs (differential input pins, expected input filter, supply/reference decoupling, ratiometric support). Drives `analyze_analog.py`. Ships with a NAU7802 recipe.
- `.claude/skills/kicad-schematic-gen/references/analog_layout.md` — Reasoning guide behind the analog-noise checks.
- `.claude/skills/kicad-schematic-gen/references/layout_authoring.md` — Stage 6 layout-authoring guide: pin-side semantic-grouping conventions, worked examples for a library IC (BQ24650) and a custom IC (NAU7802), placement/connector-rotation conventions (the J3 lesson), and the author→run-engine→fix loop. Layout authoring is **Claude-guided, not scripted** (no scaffolder) — the engine gates are the mechanical safety net. Impedance-aware prioritization (input filtering > guards), sigma-delta bandwidth, RF rectification, ratiometric references, reference planes, diff-pair symmetry. Read this to interpret analyzer severities.
- `.claude/skills/kicad-schematic-gen/references/subagents.md` — Subagent doctrine + per-stage recipes. The non-scriptable *verify* legs run as **answer-blind subagents** (isolated context = unbiased check, the mechanism behind "don't trust the current step's own reasoning"): Stage 2 per-candidate research fan-out + `[CRITICAL]` requirement verifier, Stage 4 independent pinout re-derivation, Stage 7 structural design review. Authoring legs and user gates stay on the main thread; deterministic scripts are never wrapped in a subagent. Defines the **datasheet cache** (`{outputs}/{project_name}_datasheets/{MPN}.pdf` + `index.md`) so every verifier reads the same frozen artifact instead of re-fetching, and the **`{MPN}.facts.yaml` fact cards** — structured *intrinsic* per-part facts (sourcing, `lib_id`, `footprint`, pinout number/name/type; never per-design connectivity) that the BOM/`symbols:` fields are copied from and verified back against (Stage 4 pinout re-derivation validates the card; Stage 7 review checks the schematic against it). Cards seed from / get promoted into `pinout_db.json`. The card↔doc drift is enforced mechanically by `check_cards.py`.
- `.claude/skills/kicad-schematic-gen/references/builder_api.md` — Stage 6 **fallback** path: the low-level `KicadSchematic` builder API (pre-flight checks, 7-step label-based wiring, API quick reference). The primary path is `generate_from_data.py`; use the builder directly only for custom symbol shapes the layout schema can't express.
- `.claude/skills/kicad-schematic-gen/references/design_review.md` — Stage 7 structural design-review checklist (Power/Signal Integrity, Connectivity, Component Correctness, Footprint & BOM); the checklist the Stage 7 review subagent walks.
- `.claude/skills/kicad-schematic-gen/templates/` — Stage document templates (spec, candidates, BOM, implementation reference, design analysis YAML, analysis report, netlist YAML w/ net-class schema, design review checklist).
- `.claude/skills/kicad-schematic-gen/tests/` — 409 pytest tests across 15 test files. Includes `test_generate_from_data.py` (engine unit tests) and `test_integration_battery3s.py` (end-to-end + byte-stable golden snapshot, with the J3-short regression guard). Golden + frozen inputs live in `tests/fixtures/battery_3s/`; refresh the golden with `UPDATE_GOLDEN=1`.
- `kicad-schematic-gen.skill` — Original packaged skill archive (binary zip).

## Running

```bash
# Generate a schematic from data (Stage 6 engine — primary path)
python .claude/skills/kicad-schematic-gen/scripts/generate_from_data.py <netlist.yaml> <bom_flat.md> <layout.yaml> -o <out.kicad_sch>
python .claude/skills/kicad-schematic-gen/scripts/generate_from_data.py <netlist.yaml> <bom_flat.md> <layout.yaml> -o <out.kicad_sch> --json --strict --uuid-seed 0

# Generate a test schematic (low-level builder demo)
python .claude/skills/kicad-schematic-gen/scripts/generate_kicad_sch.py

# Validate a .kicad_sch file
python .claude/skills/kicad-schematic-gen/scripts/validate_kicad_sch.py <file.kicad_sch>
python .claude/skills/kicad-schematic-gen/scripts/validate_kicad_sch.py <file.kicad_sch> --json
python .claude/skills/kicad-schematic-gen/scripts/validate_kicad_sch.py <file.kicad_sch> --json --netlist

# Run DC analysis on a design file
python .claude/skills/kicad-schematic-gen/scripts/analyze_dc.py <design.yaml>
python .claude/skills/kicad-schematic-gen/scripts/analyze_dc.py <design.yaml> --json
python .claude/skills/kicad-schematic-gen/scripts/analyze_dc.py <design.yaml> --strict

# Verify schematic against intended netlist
python .claude/skills/kicad-schematic-gen/scripts/verify_netlist.py <netlist.yaml> <file.kicad_sch>
python .claude/skills/kicad-schematic-gen/scripts/verify_netlist.py <netlist.yaml> <file.kicad_sch> --json

# Cross-check BOM against schematic
python .claude/skills/kicad-schematic-gen/scripts/cross_check_bom.py <bom.md> <file.kicad_sch>
python .claude/skills/kicad-schematic-gen/scripts/cross_check_bom.py <bom.md> <file.kicad_sch> --json

# Cross-check per-part fact cards against the layout YAML + flat BOM (Stage 6 pre-build gate)
python .claude/skills/kicad-schematic-gen/scripts/check_cards.py <layout.yaml> <bom_flat.md> --cards-dir <datasheets_dir>
python .claude/skills/kicad-schematic-gen/scripts/check_cards.py <layout.yaml> <bom_flat.md> --cards-dir <dir> --json --strict

# Check BOM for PCBway turnkey-assembly readiness
python .claude/skills/kicad-schematic-gen/scripts/check_pcbway.py <bom.md>
python .claude/skills/kicad-schematic-gen/scripts/check_pcbway.py <bom.md> --json
python .claude/skills/kicad-schematic-gen/scripts/check_pcbway.py <bom.md> --strict
python .claude/skills/kicad-schematic-gen/scripts/check_pcbway.py <bom.md> --sourcing-sheet -o <sourcing.md>

# Check KiCad library for a part (symbol + footprint + pins)
python .claude/skills/kicad-schematic-gen/scripts/check_kicad_library.py <part_number> --lookup
python .claude/skills/kicad-schematic-gen/scripts/check_kicad_library.py <part_number> --lookup --json
python .claude/skills/kicad-schematic-gen/scripts/check_kicad_library.py --footprint "Library:Footprint"
python .claude/skills/kicad-schematic-gen/scripts/check_kicad_library.py --footprint "search_term"

# Check requirements traceability (Stage 7 backstop)
python .claude/skills/kicad-schematic-gen/scripts/check_requirements.py <spec.md> <traceability.yaml> <bom_flat.md>
python .claude/skills/kicad-schematic-gen/scripts/check_requirements.py <spec.md> <traceability.yaml> <bom_flat.md> --json --strict

# Look up IC pinouts from verified database
python .claude/skills/kicad-schematic-gen/scripts/lookup_pinout.py <part_number>
python .claude/skills/kicad-schematic-gen/scripts/lookup_pinout.py <part_number> --json
python .claude/skills/kicad-schematic-gen/scripts/lookup_pinout.py <part_number> --generator

# Analog front-end completeness check (Stage 5b, runs on the netlist YAML)
python .claude/skills/kicad-schematic-gen/scripts/analyze_analog.py <netlist.yaml>
python .claude/skills/kicad-schematic-gen/scripts/analyze_analog.py <netlist.yaml> --json
python .claude/skills/kicad-schematic-gen/scripts/analyze_analog.py <netlist.yaml> --strict

# Analog-noise / signal-integrity layout check (Stage 8, runs on the .kicad_pcb)
python .claude/skills/kicad-schematic-gen/scripts/analyze_pcb_si.py <file.kicad_pcb>
python .claude/skills/kicad-schematic-gen/scripts/analyze_pcb_si.py <file.kicad_pcb> --netlist <netlist.yaml>
python .claude/skills/kicad-schematic-gen/scripts/analyze_pcb_si.py <file.kicad_pcb> --json

# Run tests
python -m pytest .claude/skills/kicad-schematic-gen/tests/ -v
```

## Architecture

The generator follows a **builder pattern**:

1. Create a `KicadSchematic` instance
2. Register library symbol definitions (`add_lib_symbol_*` methods) — resistor, capacitor, LED, diode, inductor, power symbols, connectors, custom ICs
3. Place component instances (`place_component`, `place_power_symbol`)
4. Add connectivity via **netlist-driven label-based wiring** (preferred) or manual wiring
5. Run pre-save audits (`ensure_all_pins_assigned`, `ensure_footprints`)
6. Call `save()` to serialize everything to KiCad S-expression format

**Coordinate system**: All coordinates use KiCad's 1.27mm grid snapping (via `snap_to_grid`). Y-axis is inverted between symbol space (Y+ up) and schematic space (Y+ down) — the `get_pin_position` method handles this transform.

**Netlist-driven wiring (Stage 6)**: The preferred approach uses labels for ALL connectivity. Each pin gets a short wire stub + label via `label_at_pin(ref, pin, net_name)` (auto-detects stub direction), `gnd_at_pin(ref, pin)` for GND connections, `power_at_pin(ref, pin, power_name)` for other power symbols, and `nc_at_pin(ref, pin)` for no-connects. `get_pin_stub_direction(ref, pin)` returns the outward `(dx, dy)` vector for any pin. This approach makes connectivity correct by construction — if the label matches the netlist YAML, the net is right.

**Pin positions**: `get_pin_position(reference, pin_number)` computes absolute schematic coordinates for a component's pin, accounting for placement position and rotation. `wire_between(ref1, pin1, ref2, pin2)` uses this to auto-route L-shaped wires (available for manual wiring when needed).

## Preferences System

`preferences.yaml` defines:
- Default passive package sizes and footprints (0805 default)
- IC package preference order (SOT-23-5 first)
- Standard component values (decoupling: 100nF, bulk: 10uF, pull-ups: 4.7k/10k)
- Available power rails and preferred regulators
- Connector inventory (JST-SH, JST-PH, USB-C)
- Parts on hand (resistor/capacitor values, LEDs)
- Design rules (power symbols vs labels, auto-decoupling, power LED)

## Footprint Map

`footprint_map.yaml` is a lookup table that maps common package designators to their exact KiCad footprint library IDs. It covers passives (0201–2512), IC packages (SOT, SOIC, TSSOP, MSOP, QFN, DFN, LQFP, BGA), diodes, through-hole, connectors, tantalum/electrolytic caps, crystals, test points, and mounting holes.

**Usage in the skill workflow:**
1. **BOM stage (Stage 3)** — look up each part's package in `footprint_map.yaml` to fill the `Footprint (KiCad)` column with the verified string. For passives, use the component-type-specific section (e.g., `capacitors["0805"]` not `resistors["0805"]`). For ICs, use the `aliases` section for quick lookup by package name (e.g., `aliases["SOT-23-5"]`), or the detailed section (e.g., `qfn["QFN-32_5x5"]`) when body size matters.
2. **Schematic generation** — pass the footprint string from the BOM into each `place_component(footprint=...)` call.
3. **Inventory parts** in `preferences.yaml` already have footprints — use those directly. The map handles standard/generic packages.

## Validator

The validator (`validate_kicad_sch.py`) works two ways:

**CLI** — point it at any `.kicad_sch` file. Uses `--json` for machine-readable output that Claude Code can parse and act on. Exit code 0 = pass, 1 = errors found.

**Python API** — call `validate(sch)` on an in-memory `KicadSchematic` before/after `save()`. Also provides `assert_connected(sch, ref1, pin1, ref2, pin2)` and `assert_net_contains(sch, net_name, pins)` for scripted assertions.

**Connectivity model**: Pins connect to wires anywhere along their length (not just at endpoints), matching KiCad's actual behavior. Labels and power symbols with the same name are unified into a single net.

**Reference resolution**: `load_kicad_sch` resolves each symbol's reference from the `(instances ...)` block matching the root sheet path (`_resolve_active_reference`), NOT the `(property "Reference")` cache. KiCad 8 leaves stale instance records (often under an empty project name `""`) after re-annotation/merges/renames; reading the property cache yields wrong, apparently-duplicate refs that cascade false failures into every downstream tool. When any KiCad-parsing tool reports surprising duplicate/odd references, suspect stale instances first.

**Checks** (11 total): `floating_pin`, `dangling_wire`, `disconnected_label`, `duplicate_reference`, `missing_lib_symbol`, `single_pin_net`, `missing_junction`, `overlapping_components`, `no_connect_conflict`, `missing_power_source`, `similar_net_names`.

**Skill integration**: After generating a schematic, the skill should run:
```python
result = validate(sch)
# or from CLI: python validate_kicad_sch.py output.kicad_sch --json
```

## DC Analyzer

The DC analyzer (`analyze_dc.py`) validates electrical correctness *before* schematic generation by pattern-matching known subcircuit topologies from a design YAML file.

**Supported patterns** (7 total): `voltage_divider`, `ldo_regulator`, `led_circuit`, `pullup_network`, `current_budget`, `cap_sizing`, `feedback_divider`.

**What it catches**: wrong divider ratios, LDO dropout/thermal violations, LED overcurrent, I2C rise time violations, rail overcurrent, undersized caps, MLCC voltage derating issues.

**Skill integration**: Before generating, the skill builds a design YAML from the implementation reference and runs:
```python
from analyze_dc import load_design_from_string, analyze
result = analyze(load_design_from_string(yaml_text))
# or from CLI: python analyze_dc.py design.yaml --json
```

## KiCad Library Checker

The library checker (`check_kicad_library.py`) searches the locally installed KiCad libraries to verify that a part has a symbol and footprint available before it's selected for a design. It reads directly from KiCad's on-disk library files — no static list to maintain.

**What it returns**: For any part number, it provides the exact `lib_id` (e.g., `Regulator_Linear:AP2112K-3.3`), the default footprint (e.g., `Package_TO_SOT_SMD:SOT-23-5`), whether that footprint file exists, pin data, and alternative variants.

**Three modes**:
- `--lookup <part>`: Full lookup — symbol + footprint verification + pins. Main entry point for the skill.
- `<part>`: Symbol search only — find all matching symbols.
- `--footprint <id>`: Verify a specific footprint exists, or search by substring.

**Skill integration**: During Stage 2 (Component Sourcing), run `lookup_part()` for each candidate IC. If it returns `found: false`, the part needs a symbol from SnapEDA/manufacturer. The `lib_id` and `footprint` from the result go directly into the BOM and schematic generation — no manual lookup needed.

```python
from check_kicad_library import lookup_part
result = lookup_part("AP2112K-3.3")
# result["lib_id"] → "Regulator_Linear:AP2112K-3.3"
# result["footprint"] → "Package_TO_SOT_SMD:SOT-23-5"
# result["footprint_exists"] → True
# result["pins"] → [{number: "1", name: "VIN", type: "power_in"}, ...]
# or from CLI: python check_kicad_library.py AP2112K-3.3 --lookup --json
```

**Note**: Passives (resistors, capacitors, inductors) use generic KiCad symbols (`Device:R`, `Device:C`, etc.) and don't need symbol lookup. Their footprints come from `footprint_map.yaml`. The library checker is for ICs, sensors, connectors, and other specific parts.

## Netlist Verifier

The netlist verifier (`verify_netlist.py`) checks that a generated schematic's connectivity matches a pin-level intended netlist YAML document. It bridges the gap between design intent and generated geometry.

**Three checks**: `completeness` (every pin accounted for in exactly one net or no_connects), `consistency` (no phantom refs/pins), `connectivity` (pins on same intended net are actually connected in schematic).

**CLI**: `python verify_netlist.py <netlist.yaml> <schematic.kicad_sch> [--json]`. Exit code 0 = pass, 1 = errors found.

**Python API**: `verify(intended, sch)` returns a `VerificationResult` with `passed`, `issues`, `errors`, `warnings`.

**Skill integration**: After generating, verify the schematic matches the Stage 5b netlist:
```python
from verify_netlist import verify, load_intended_netlist_from_string
result = verify(load_intended_netlist_from_string(yaml_text), sch)
# or from CLI: python verify_netlist.py netlist.yaml output.kicad_sch --json
```

## Analog Noise Handling (3 layers)

Sensitive analog designs (ADC front-ends, bridge/load-cell sensing) are handled
across three layers so noise is addressed where it's cheapest to fix. The guiding
principle, documented in `references/analog_layout.md`: **most analog-noise
problems are schematic defects, not layout defects** — a missing input filter cap
cannot be added during routing — and mitigations are weighted accordingly
(input filtering ≫ guard pours; guarding is *source-impedance dependent*).

**Layer 1 — Net classification (Stage 5b).** The netlist YAML carries a `class:`
per net (`analog`, `analog_differential` with `pair`/`polarity`/`source_z`,
`analog_supply`, `reference`, `high_impedance`, `rf`, `switching`). This seeds
both checkers. Parsed by `verify_netlist.py`'s loader into `IntendedNet`. Schema
is documented in `templates/05b_netlist.yaml`.

**Layer 2 — Schematic-level completeness (`analyze_analog.py`, Stage 5b gate).**
Reads the netlist + `analog_recipes.yaml`. Recipe-driven (knows a part's
differential-input/supply/reference pins, e.g. NAU7802) *and* class-driven (works
off `class:` tags for parts without a recipe). Flags bare differential inputs
(no anti-alias/EMI filter — the canonical defect), `Cdiff < ~10×Ccm` imbalance,
missing supply/reference/bandgap decoupling, and a ratiometric reference not tied
to the excitation. Errors send you back to Stage 4 to add components to the impl
reference + BOM. Reuses `analyze_dc`'s `AnalysisIssue`/`AnalysisResult`/`_parse_value`.
```python
from analyze_analog import analyze_netlist_from_string
result = analyze_netlist_from_string(yaml_text)   # result.passed False if bare inputs
# or CLI: python analyze_analog.py netlist.yaml --json
```

**Layer 3 — Layout backstop (`analyze_pcb_si.py`, Stage 8).** Parses `.kicad_pcb`.
Checks diff-pair symmetry (layers/length/vias), reference-layer (sensitive net
over power vs GND plane — derived from the stackup), aggressor proximity (RF/
switching footprints, segment-through-bbox distance), long runs, via-in-pad, and
return vias near sensitive layer transitions. Conservative severities; the guard
advisory is source-impedance aware (low-Z bridge → guarding is *info*, high-Z →
*warning*). Sensitivity comes from `--netlist` classes (authoritative) or net-name
heuristics (fallback).
```python
from analyze_pcb_si import analyze_pcb_file
result = analyze_pcb_file("board.kicad_pcb", netlist_path="netlist.yaml")
# or CLI: python analyze_pcb_si.py board.kicad_pcb --netlist netlist.yaml --json
```

## PCBway Assembly Checker

Boards are ordered as **assembled boards from PCBway turnkey** — PCBway sources every part from authorized distributors (LCSC → DigiKey → Mouser) and populates the board. There is no in-house inventory and, unlike JLCPCB, **no public PCBway parts API to query**. So part selection has two halves:

1. **Selection rubric (Stage 2, in SKILL.md)** — choose parts PCBway can source/assemble cleanly: standard SMT packages, in-stock at a PCBway distributor, active and ideally multi-source.
2. **BOM verification (Stage 3)** — `check_pcbway.py` runs the deterministic, offline half of the rubric over the Stage 3 BOM markdown.

The checker (`check_pcbway.py`) does **no network access** (so it can't silently break). It checks:
- **Package assemblability** via `classify_package()` — flags 01005 (block), 0201/BGA/THT (caution), bare die (block), unrecognized packages (review).
- **Sourcing data** — non-passive lines with no MPN/distributor PN are blocking (PCBway needs an exact part to source); generic passives without a PN are a caution.
- **Notes-field flags** — obsolete/EOL/NRND, single-source, MSL 3+, long-lead.

It also emits a **sourcing sheet** (`--sourcing-sheet`) — the BOM augmented with assembly ratings and the distributor-PN columns to submit to PCBway. Live distributor stock is confirmed separately via web search in the workflow (PCBway has no API).

```python
from check_pcbway import check_bom, load_bom_for_pcbway, build_sourcing_sheet
result = check_bom(load_bom_for_pcbway(bom_md_text))   # result.passed False if any block
sheet = build_sourcing_sheet(result, "My Board")
# or from CLI: python check_pcbway.py bom.md --json   /   --sourcing-sheet -o sourcing.md
```

The assembler, distributor order, and rubric thresholds live in `preferences.yaml` under `assembly:`.
