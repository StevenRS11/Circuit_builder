# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **KiCad Schematic Generator** — a Python library that programmatically generates `.kicad_sch` files (S-expression format) compatible with KiCad 7/8. No KiCad installation is required.

The repo ships two sibling skills over one shared script library:

- **`kicad-schematic-gen`** — the forward (greenfield) pipeline: natural-language description → 9 gated stages → verified `.kicad_sch` + PCBway upload package.
- **`kicad-board-context`** — the reverse (brownfield) skill: an **existing** KiCad project → extracted "context pack" (netlist YAML, flat BOM, PCB summary, drift report) → open-ended reasoning (review / debug / explain / reselect parts / plan modifications). See "kicad-board-context skill" below.

The project also includes a `.skill` file (`kicad-schematic-gen.skill`) which bundles the generator script, a preferences/inventory config, and reference docs into a portable skill package.

**Design hierarchy (governs all skill changes):** the skill runs one loop — *Claude generates context → verifies that context → authors from it → verifies what it authored with scripts*. Scripts live at the verify steps (and as the deterministic assembler `generate_from_data.py`, driven by Claude-authored data); Claude lives at the generate/author steps. **Never script the judgment** (pin arrangement, placement, custom-symbol authoring, part selection) — a script that *checks* a decision is welcome, one that *makes* it is not. This works because circuits are well described in natural language and nearly every artifact is plain text. See the "Design hierarchy" section at the top of `SKILL.md`.

## Key Files

- `.claude/skills/kicad-schematic-gen/scripts/generate_kicad_sch.py` — Core library. `KicadSchematic` builder class and dataclasses. Supports `uuid_seed=` for reproducible (golden-testable) output via `seed_uuids()`.
- `.claude/skills/kicad-schematic-gen/scripts/generate_from_data.py` — **Stage 6 engine (primary generation path).** Builds a `.kicad_sch` from three data files — netlist YAML (connectivity) + flat BOM (value/footprint) + layout YAML (placement + IC pin-sides) — joined on reference designator. Pure function of its inputs (no KiCad install). Runs pre-flight join/pin-set gates and self-verifies (validate + verify_netlist + cross_check) before saving; refuses to emit a file with errors, and treats `missing_junction` as blocking (unintended wire collision / short class). Replaces per-project builder scripts. **Bakes each part's PCBWay identity fields (`MPN`/`Manufacturer`/`Package`/`Description`) onto its symbol** (canonical key `MPN`, the one the PCBWay KiCad plugin reads) so the plugin auto-populates the BOM after an F8 sync — every fitted line needs a real MPN (passives included), enforced by the Stage-9 `critical_schematic_mpn_present` gate. **Composes proven blocks (W1b):** a layout-YAML `blocks:` section (`{instance: {block, x, y, port_map, refdes_base?}}`, optional `blocks_dir:`) makes the engine clone each registry block's `sheet.kicad_sch` next to the output with refs re-annotated into per-instance ranges (U2→U102/U202), emit the root `(sheet)` symbol with contract-port pins wired to the mapped board nets, self-verify against the FLATTENED netlist + merged BOM, and write a whole-board `{out}_bom_flat.md` for Stage 9. Gates: port_map↔contract parity, mapped nets declared, block rails present in `power_nets`, refdes-range collisions. Block `constraints:` stay judgment — read block.yaml before mapping. Clones are removed on a failed build.
- `.claude/skills/kicad-schematic-gen/scripts/validate_kicad_sch.py` — Schematic validator. Parses `.kicad_sch` files, extracts netlists, runs 13 connectivity checks. **Hierarchical (roadmap W1b):** `load_kicad_sch` walks `(sheet)` nodes into child files (cycle-safe; `load_children=False` restores flat parse), `extract_netlist` merges child netlists through sheet pins (port nets join the parent net, power/global labels merge globally, sheet-local nets get `instance/` prefixes), `validate` recurses with `[sheet name]`-prefixed issues, checks duplicate refs across the whole hierarchy, and runs the `sheet_integrity` check (pin↔hierarchical-label parity, missing child file, unwired-pin warning). `iter_all_components()` is the shared hierarchy-aware component iteration (used by verify_netlist / cross_check_bom / board-context extraction). **Loader library-fallback (roadmap A1):** a placed symbol whose `lib_id` is missing from the file's embedded `lib_symbols` cache is resolved from installed/registered libraries like KiCad does (`--project-dir` / `--sym-lib` / `--no-lib-fallback`), keeping real pins/connectivity and emitting a `stale_lib_cache` *warning* instead of a false dangling/floating-error cascade; lib_ids that resolve nowhere land in `sch.unresolved_lib_ids` and still error.
- `.claude/skills/kicad-schematic-gen/scripts/analyze_dc.py` — DC analysis engine. Pattern-matches subcircuits (LDOs, dividers, LEDs, pull-ups) and validates electrical correctness before schematic generation.
- `.claude/skills/kicad-schematic-gen/scripts/verify_netlist.py` — Netlist verifier. Checks schematic connectivity against an intended netlist YAML (completeness, consistency, connectivity).
- `.claude/skills/kicad-schematic-gen/scripts/cross_check_bom.py` — BOM cross-checker. Verifies schematic matches BOM (references, values, footprints) with fuzzy value matching.
- `.claude/skills/kicad-schematic-gen/scripts/check_cards.py` — Fact-card cross-checker (Stage 6 pre-build gate). Deterministic, verify-only: joins the per-part `{MPN}.facts.yaml` fact cards against the Stage 6 layout YAML + Stage 3 flat BOM (on `lib_id`) and fails on drift between the verified card and the authored docs — BOM footprint vs `card.kicad.footprint`, each IC's `symbols:` pin number/name/type vs `card.pins`, and `pinout_verified` must be true. Warns on a `symbols:` IC with no card (`--strict` → error). The mechanical backstop for "copy, don't paraphrase"; complements the engine's `symbol pin-set == netlist pin-set` gate by adding name/type/footprint coverage. Makes no design decisions.
- `.claude/skills/kicad-schematic-gen/scripts/check_pcbway.py` — PCBway assembly checker + **Stage 9 structural BOM gate**. Scores a BOM for PCBway turnkey-assembly readiness (package assemblability, distributor PN presence, EOL/MSL/single-source flags) and emits a sourcing sheet. Also runs the deterministic per-line hygiene gates that catch the defects seen in the field: `distributor_code_as_mpn` (an LCSC `C#####` in the Mfg Part # column → block), `mpn_not_real` (a description where an MPN belongs → caution), `package_mismatch` (package field vs footprint size token → block), `missing_manufacturer` (PCBway's form requires one). Carries `manufacturer`/`description` BOM columns. `verification_worklist()` emits the lines (non-passives + flagged) that need the live check. No network — part-identity is confirmed live by `bom_verify.py`'s answer-blind agents. Also hosts the **[CRITICAL] schematic-MPN gate** (`check_schematic_mpns` / `--schematic <file.kicad_sch>`; BOM-level mirror `check_bom_mpn_ready`): asserts every fitted line — passives included — carries exactly one clean, plugin-readable MPN (catches missing / empty-shadow / `Mfg Part #`-trap / distributor-code MPNs). Owns the PCBWay plugin field-name constants (`PLUGIN_MPN_KEYS`, `CANONICAL_MPN_FIELD`, `FORBIDDEN_MPN_FIELD`) and the shared BOM column parser `parse_bom_records` (used by `cross_check_bom` + the xlsx generator so all consumers read columns identically).
- `.claude/skills/kicad-schematic-gen/scripts/bom_verify.py` — **Stage 9 answer-blind BOM verifier (bookkeeping half).** `--worklist` parses the BOM, runs the structural gate, and emits each line that needs a live check reduced to the bare CLAIM `{manufacturer, MPN, value, package, distributor code}`. The judgment half is an **answer-blind subagent per line** (recipe E in `references/subagents.md`) that web-verifies the claim blind to the main thread's reasoning. `--report` joins the agents' verdict JSON back to the BOM and writes `verification_report.md`, exiting 1 on any `mismatch` (a confirmed wrong part — the `C914291`=Zener / `C13564`=wrong-package class) so it gates the upload. Makes no correctness call itself; no network.
- `.claude/skills/kicad-schematic-gen/scripts/generate_pcbway_bom.py` — **Stage 9 BOM-format generator.** Pure transform: internal Stage-3 `bom.md` → PCBway's 9-column upload `.xlsx` (Item#, *Designator, *Qty, Manufacturer, *Mfg Part #, Description/Value, *Package/Footprint, Type, Notes). Groups identical parts (designator list + qty), puts the LCSC/distributor code in the Notes column, derives Mounting Type from the footprint. Every line carries a real Manufacturer + MPN (passives included) — fix data in `bom.md`, never in the spreadsheet. Reuses `check_pcbway.load_bom_for_pcbway`.
- `.claude/skills/kicad-schematic-gen/scripts/bake_bom_fields.py` — **After-the-fact PCBWay-field bake.** Injects the same identity field set the Stage-6 engine bakes at generation time (`_pcbway_symbol_props` + the `check_pcbway` field-name contract — never hardcode field names) onto an already-built / hand-edited `.kicad_sch` (and, belt-and-suspenders, `.kicad_pcb`). **Reconciling**: updates in place, inserts missing, and *removes* stale managed fields + shadow MPN-family aliases + the forbidden `Mfg Part #`, so exactly one plugin-readable MPN key remains. Lock-aware (refuses `~*.lck`-locked files; `--force` overrides); board-side managed set excludes the native footprint `Description`. Preferred propagation is schematic-bake → KiCad F8; verify with `crosscheck_pcbway_plugin_bom.py` / the board-context skill's `reconcile.py`. This is the bake path the `kicad-board-context` BAKE mode uses. Cannot catch an MPN that resolves to the wrong physical part — that stays with Stage-9's answer-blind `bom_verify.py`.
- `.claude/skills/kicad-schematic-gen/scripts/generate_fab_outputs.py` — **Stage 9 fab-output generator (kicad-cli wrapper).** Locates `kicad-cli`, auto-detects the copper stack (2/4/N-layer by board index), runs a **DRC gate** (refuses to emit fab files from a board with violations unless `--no-drc`), then exports gerbers + Excellon drill + centroid CSV and zips the gerbers — into `{pcb_dir}/PCBway_uploads/`. Pure script; the only stage that needs the KiCad binary installed.
- `.claude/skills/kicad-schematic-gen/scripts/check_requirements.py` — Requirements traceability backstop (Stage 7). Deterministic, verify-only: parses the Stage-1 spec's numbered Requirements Checklist (`R#`, `[CRITICAL]` flags) and a Claude-authored traceability YAML (`R# → {satisfied_by: [refs], evidence}`), and checks the matrix is complete + consistent against the BOM — catching untraced requirements (dropped blocks), phantom refs, `[CRITICAL]` items lacking evidence, stale entries, and orphan ICs/connectors. It does **not** judge whether evidence is true (that stays Claude's job); `EXTERNAL` token covers off-board items, and **`block:{name}` (W1c) cites a composed proven block** — verified against the `blocks/` registry (`unknown_block` error otherwise, `--blocks-dir` overrides), with the instance's re-annotated refs (U2→U102/U202, +k·100) counted as cited for the orphan check. This is the structural enforcement of "verified docs are the test suite."
- `.claude/skills/kicad-schematic-gen/scripts/check_kicad_library.py` — KiCad library checker. Searches installed KiCad libraries for symbols, footprints, and pin data. Returns lib_id + footprint ready for schematic generation. Symbol-file splitting is depth-based (indentation-agnostic — handles space-indented vendor/hand-written `.kicad_sym` files, not just KiCad's tabs).
- `.claude/skills/kicad-schematic-gen/scripts/generate_pinmap.py` — **Firmware pinmap handoff (roadmap A2).** Pure transform: `.kicad_sch` → `board_pins.h` (one `#define PIN_<NET> <gpio>` per MCU GPIO on a named net, byte-deterministic, with a skipped-pins audit trail) + optional `bringup.ino` skeleton (I²C scan on the board's SDA/SCL, status-LED blink). MCU auto-detected by part name (ESP32/RP2040/STM32/…) or `--mcu <ref>`; ambiguity errors, never guesses. Kills the "firmware GPIO ≠ schematic GPIO" defect class; works identically on brownfield boards via the board-context skill's extracted schematic.
- `.claude/skills/kicad-schematic-gen/scripts/lookup_pinout.py` — Pinout database lookup. Queries verified IC pinouts to reduce hallucination risk in Stage 4.
- `.claude/skills/kicad-schematic-gen/scripts/analyze_analog.py` — Analog front-end completeness checker (Stage 5b). Operates on the netlist YAML + `analog_recipes.yaml` to verify sensitive analog chains have anti-alias/EMI input filtering, supply/reference decoupling, and a ratiometric reference *before* generation. Recipe-driven (knows a part's input/supply pins) and class-driven (net `class:` tags). Catches the "connector wired straight to ADC pin, no filter" defect.
- `.claude/skills/kicad-schematic-gen/scripts/analyze_pcb_si.py` — PCB signal-integrity / analog-noise layout backstop (Stage 8). Parses `.kicad_pcb` and checks differential-pair symmetry, reference-layer (sensitive net over a power vs GND plane), aggressor proximity (RF/switching parts), long runs, via-in-pad, and return vias. Source-impedance-aware (low-Z bridge → guarding is advisory, not required). Optional `--netlist` for authoritative net classes.
- `.claude/skills/kicad-schematic-gen/pinouts/pinout_db.json` — Verified IC pinout database (10 parts: AP2112K, AMS1117, TPS563200, CH340G, BME280, BMP390, FUSB302B, RP2040).
- `.claude/skills/kicad-schematic-gen/scripts/extract_block.py` — **W1a block extraction.** Carves a proven subcircuit out of a validated board into a self-contained bundle under `blocks/{name}/`: a hierarchical child `sheet.kicad_sch` (symbols embedded **verbatim** from the source, internal nets as local labels, rails as power symbols, every declared port as a **hierarchical label** → sheet pin on a parent), a Stage-5b `netlist.yaml` fragment, `bom.md` subset, and the `block.yaml` contract (ports/rails/provenance/dependencies; judgment fields emitted as TODO). Port contract is explicit — unmapped boundary nets are an error, a declared port overrides internal classification, power nets can't be ports. Footprints are promoted into `blocks/footprints/CircuitBlocks.pretty` (two-source policy: builtin | CircuitBlocks — blocks never reference personal/fragmented libraries). Self-verifies (sheet validates + matches the fragment) and removes the bundle on failure.
- `.claude/skills/kicad-schematic-gen/scripts/check_block.py` — **W1a block bundle gate.** Verify-only: sheet validates standalone (no library fallback — embedded symbols required), netlist round-trip, contract↔sheet↔netlist port parity, rail parity, BOM cross-check, dependency two-source policy (every sheet footprint in the manifest as builtin|blocks, CircuitBlocks copies present). TODO judgment fields warn (`--strict` fails them — a block isn't ready for reuse until they're filled).
- `.claude/skills/kicad-schematic-gen/blocks/` — **The proven-block registry (W1).** `blocks/{name}/` bundles + `footprints/CircuitBlocks.pretty/` (the single non-builtin footprint library blocks may use, git-versioned, populated by extraction-time promotion). The engine composes blocks from the layout YAML `blocks:` section (W1b); `blocks/README.md` documents both that and manual composition in KiCad (place sheet → Import Sheet Pins).
- `.claude/skills/kicad-schematic-gen/scripts/check_ledger.py` — **Stage 10 / W2 bench-truth ledger checker.** Deterministic, verify-only: validates `validated_boards.yaml` — schema/enums/unique (board, rev), and crucially that **every lesson's `encoded_in` (`path::needle`) still resolves** (the gate/test/doc encoding a bench lesson exists and contains the needle), plus claimed blocks/eval-anchors/field-reports exist. A refactor that orphans a bench lesson fails the suite instead of silently forgetting it.
- `.claude/skills/kicad-schematic-gen/validated_boards.yaml` — **The bench-truth ledger (W2).** One entry per (board, rev) that met reality: status, bench date, summary, lessons (each with class + where it's encoded), blocks extracted, eval anchor, field reports. Seeded with battery_3s v3.0 (CH224K VDD→VBUS failure → pre-flight gate #5; 4 wrong-distributor-code lines → bom_verify evidence; off-grid hand-edit lesson) and v3.1 (in bringup). The promotion ritual (`references/promotion.md`) and Stage 10 write to it; the reviewer and Stage 2 consult it.
- `.claude/skills/kicad-schematic-gen/references/promotion.md` — **The promotion ritual (W2).** Failure-routing table (defect class → the cheapest layer that would have caught it: pinout→fact card/pinout DB, topology→pre-flight gate/analyzer+test, sourcing→check_pcbway/bom_verify, layout→analyze_pcb_si/W3 seed list, process→SKILL/reference failure signature) and success routing (validated status, block extraction, pinout promotion, Tier-3 eval anchor). Contract: every bench session deposits something; a lesson that lives only in prose is not a lesson.
- `.claude/skills/kicad-schematic-gen/templates/10_bringup.md` / `templates/10_field_report.md` — Stage 10 documents: bringup checklist (expected values cited from the verified artifacts — DC analysis, netlist, fact cards, board_pins.h) and the ~10-minute post-bench field report that feeds the promotion ritual.
- `.claude/skills/kicad-schematic-gen/preferences.yaml` — User preferences: footprints, preferred/on-hand parts, power rails, connectors, PCBway assembly settings + selection rubric, design rules.
- `.claude/skills/kicad-schematic-gen/footprint_map.yaml` — Package-to-footprint lookup table. Maps ~380 common package names to exact KiCad footprint library IDs (includes transistor packages).
- `.claude/skills/kicad-schematic-gen/analog_recipes.yaml` — Front-end "recipe" knowledge base for sensitive analog ICs (differential input pins, expected input filter, supply/reference decoupling, ratiometric support). Drives `analyze_analog.py`. Ships with a NAU7802 recipe.
- `.claude/skills/kicad-schematic-gen/references/analog_layout.md` — Reasoning guide behind the analog-noise checks.
- `.claude/skills/kicad-schematic-gen/references/layout_authoring.md` — Stage 6 layout-authoring guide: pin-side semantic-grouping conventions, worked examples for a library IC (BQ24650) and a custom IC (NAU7802), placement/connector-rotation conventions (the J3 lesson), and the author→run-engine→fix loop. Layout authoring is **Claude-guided, not scripted** (no scaffolder) — the engine gates are the mechanical safety net. Impedance-aware prioritization (input filtering > guards), sigma-delta bandwidth, RF rectification, ratiometric references, reference planes, diff-pair symmetry. Read this to interpret analyzer severities.
- `.claude/skills/kicad-schematic-gen/references/subagents.md` — Subagent doctrine + per-stage recipes. The non-scriptable *verify* legs run as **answer-blind subagents** (isolated context = unbiased check, the mechanism behind "don't trust the current step's own reasoning"): Stage 2 per-candidate research fan-out + `[CRITICAL]` requirement verifier, Stage 4 independent pinout re-derivation, Stage 7 structural design review. Authoring legs and user gates stay on the main thread; deterministic scripts are never wrapped in a subagent. Defines the **datasheet cache** (`{outputs}/{project_name}_datasheets/{MPN}.pdf` + `index.md`) so every verifier reads the same frozen artifact instead of re-fetching, and the **`{MPN}.facts.yaml` fact cards** — structured *intrinsic* per-part facts (sourcing, `lib_id`, `footprint`, pinout number/name/type; never per-design connectivity) that the BOM/`symbols:` fields are copied from and verified back against (Stage 4 pinout re-derivation validates the card; Stage 7 review checks the schematic against it). Cards seed from / get promoted into `pinout_db.json`. The card↔doc drift is enforced mechanically by `check_cards.py`.
- `.claude/skills/kicad-schematic-gen/references/builder_api.md` — Stage 6 **fallback** path: the low-level `KicadSchematic` builder API (pre-flight checks, 7-step label-based wiring, API quick reference). The primary path is `generate_from_data.py`; use the builder directly only for custom symbol shapes the layout schema can't express.
- `.claude/skills/kicad-schematic-gen/references/design_review.md` — Stage 7 structural design-review checklist (Power/Signal Integrity, Connectivity, Component Correctness, Footprint & BOM); the checklist the Stage 7 review subagent walks.
- `.claude/skills/kicad-schematic-gen/templates/` — Stage document templates (spec, candidates, BOM, implementation reference, design analysis YAML, analysis report, netlist YAML w/ net-class schema, design review checklist).
- `.claude/skills/kicad-schematic-gen/tests/` — 578 pytest tests across 27 test files. Includes `test_generate_from_data.py` (engine unit tests), `test_hierarchy.py` (W1b: sheet emission/loading, netlist merge through ports, sheet_integrity, engine block composition + gates + determinism), `test_pcbway_stage9.py` (Stage 9: structural gates, BOM generator, `bom_verify`, fab-output layer detection), and `test_integration_battery3s.py` (end-to-end + byte-stable golden snapshot, with the J3-short regression guard). Golden + frozen inputs live in `tests/fixtures/battery_3s/`; refresh the golden with `UPDATE_GOLDEN=1`.
- `.claude/skills/kicad-schematic-gen/evals/` — **Skill-level eval suite** measuring the model half the unit tests can't: does Claude trigger the skill, follow the 8 gated stages, honor subagent verdicts, author good designs. Honest tiers by what's verifiable today (no board is built yet, so judgment-quality is deferred). **Tier 0** full-pipeline regression: each `corpus/synthetic/*` case (e.g. `battery_3s_full`, the current-skill regeneration) regenerates byte-deterministically and passes every verifier via `graders/run_all_verifiers.py`. **Tier 0b** `[CRITICAL]` *negative* regression (`regression/critical_bq24650/`): the gate must bite. **Tier 1** triggering corpora + SKILL.md description drift guard. **Tier 2** gate-adherence is a manual prompt-set + rubric (`behavioral/`, model-in-loop). **Tier 3** judgment-quality is deferred to the first **validated** anchor (built + bench-verified NAU7802 board) — see `evals/README.md`. 9 deterministic tests (Tiers 0/0b/1) in `test_evals.py`; full skill suite is 587 (606 repo-wide with kicad-board-context's 19). *Fixture provenance matters:* `synthetic/` cases are regression/process anchors only, never correctness ground truth.
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

# Stage 9 — PCBway upload package (BOM xlsx + answer-blind verify + gerbers/centroid)
# 1. structural gate (0 blocking) — includes distributor-code-in-MPN, package-vs-footprint, missing-manufacturer
python .claude/skills/kicad-schematic-gen/scripts/check_pcbway.py <bom.md> --json
# 2. live answer-blind BOM verification: emit worklist, run a subagent per line, aggregate verdicts
python .claude/skills/kicad-schematic-gen/scripts/bom_verify.py <bom.md> --worklist
python .claude/skills/kicad-schematic-gen/scripts/bom_verify.py <bom.md> --worklist --all      # include clean passives
python .claude/skills/kicad-schematic-gen/scripts/bom_verify.py <bom.md> <verdicts.json> --report -o <verification_report.md>
# 3. generate the PCBway 9-column upload BOM from bom.md
python .claude/skills/kicad-schematic-gen/scripts/generate_pcbway_bom.py <bom.md> --output-dir <project>/PCBway_uploads
# 4. generate gerbers + drill + centroid (DRC-gated) from the routed board
python .claude/skills/kicad-schematic-gen/scripts/generate_fab_outputs.py <board.kicad_pcb> --output-dir <project>/PCBway_uploads
python .claude/skills/kicad-schematic-gen/scripts/generate_fab_outputs.py <board.kicad_pcb> --kicad-cli "<path>" --json

# Check KiCad library for a part (symbol + footprint + pins)
python .claude/skills/kicad-schematic-gen/scripts/check_kicad_library.py <part_number> --lookup
python .claude/skills/kicad-schematic-gen/scripts/check_kicad_library.py <part_number> --lookup --json
python .claude/skills/kicad-schematic-gen/scripts/check_kicad_library.py --footprint "Library:Footprint"
python .claude/skills/kicad-schematic-gen/scripts/check_kicad_library.py --footprint "search_term"

# Check requirements traceability (Stage 7 backstop)
python .claude/skills/kicad-schematic-gen/scripts/check_requirements.py <spec.md> <traceability.yaml> <bom_flat.md>
python .claude/skills/kicad-schematic-gen/scripts/check_requirements.py <spec.md> <traceability.yaml> <bom_flat.md> --json --strict

# Extract a proven subcircuit from a validated board into the block registry (W1a)
python .claude/skills/kicad-schematic-gen/scripts/extract_block.py <board.kicad_sch> --name <block> --refs U3,C11,R7 --port SDA=I2C_SDA:bidirectional --port DRDY=NAU_DRDY:output --validated-on "<board rev>" [--grid-layout] [--fp-lib NICK=PATH]
python .claude/skills/kicad-schematic-gen/scripts/check_block.py .claude/skills/kicad-schematic-gen/blocks/<block> [--json] [--strict]

# Check the bench-truth ledger (Stage 10 — every lesson's encoding must still exist)
python .claude/skills/kicad-schematic-gen/scripts/check_ledger.py
python .claude/skills/kicad-schematic-gen/scripts/check_ledger.py --json --strict

# Generate the firmware pinmap handoff (board_pins.h + bringup sketch) from a schematic
python .claude/skills/kicad-schematic-gen/scripts/generate_pinmap.py <board.kicad_sch> -o <out_dir> --sketch
python .claude/skills/kicad-schematic-gen/scripts/generate_pinmap.py <board.kicad_sch> --mcu U1 --json

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

# Run the skill-level eval suite (Tiers 0/0b/1 — deterministic)
python -m pytest .claude/skills/kicad-schematic-gen/evals/ -v

# Grade one eval corpus case with every verifier
python .claude/skills/kicad-schematic-gen/evals/graders/run_all_verifiers.py <case_dir>
python .claude/skills/kicad-schematic-gen/evals/graders/run_all_verifiers.py <case_dir> --update-golden
```

## kicad-board-context skill (brownfield reviewer)

`.claude/skills/kicad-board-context/` runs the design-hierarchy loop **backwards**: existing artifacts → extracted documents → reconstructed intent → reasoning. The inverted risk rule: greenfield risk is *authoring* wrong facts, brownfield risk is *misreading* the artifact — so **scripts do all the reading, Claude does all the interpreting** (never freehand-read a `.kicad_sch`/`.kicad_pcb`).

It extracts into the **same data model** the generator uses (Stage-5b netlist YAML, Stage-3 flat BOM, fact cards, datasheet cache), so every existing analyzer/verifier/subagent recipe works on an existing board unchanged, and modification tasks can re-enter the generator pipeline at the right stage with zero translation. Everything derived lives in a `claude_context/` directory next to the user's KiCad files (which are **never modified**), with sha256 provenance for staleness detection and a dated, accumulating `findings/` log.

**Shared code stays in `kicad-schematic-gen/scripts/`** — the new skill's scripts import it via `scripts/_paths.py`; never fork shared modules (shared fixes, e.g. to the S-expression parsers, land in the generator's scripts where both skills get them).

Ingest scripts (all deterministic, extraction/verify-only):

- `scripts/extract_netlist.py` — decompiles a `.kicad_sch` into Stage-5b netlist YAML via the validator's `extract_netlist()`. **Hierarchical boards ingest whole (W1b):** child-sheet components join the manifest, port nets arrive merged through the sheet pins, sheet-local nets carry `instance/` prefixes, and the summary lists `sheets` + `missing_sheet_files`. Self-verifies round-trip with `verify_netlist`. Floating pins become loudly-tagged `EXTRACTED-FLOATING` no-connects (findings, not failures); unlabeled nets get deterministic `N$<ref>_<pin>` names; power symbols become `power_symbols:`, never components.
- `scripts/extract_bom.py` — harvests a flat BOM (`bom_flat.md`, Stage-3 format) from schematic symbol fields via `crosscheck_pcbway_plugin_bom`'s field readers (same MPN aliases as the PCBWay plugin). Verbatim copy — blank MPN/Manufacturer cells are findings about the schematic. Output loads in `cross_check_bom` and `check_pcbway`.
- `scripts/summarize_pcb.py` — distills a `.kicad_pcb` (via `analyze_pcb_si`'s parser) into `pcb_summary.yaml`: stackup, Edge.Cuts outline, footprint placements, per-net routing stats, zones/vias. Context only — `analyze_pcb_si.py` remains the analyzer.
- `scripts/reconcile.py` — cross-artifact drift report (SCH↔BOM via `cross_check_bom`, SCH↔PCB refs/values/footprints, and **MPN field propagation** — catches the "F8 never ran" wrong-uploaded-BOM class). Reports drift; the user arbitrates which side is truth.
- `scripts/match_blocks.py` — **reviewer block recognition (W1c).** Deterministic, read-only: matches fragments of the ingested netlist YAML against the generator's `blocks/` registry — anchored on each block's silicon (fuzzy part-name match), grown through net correspondence (2-pin passives orientation-free; rail-only decoupling matched by value+rails and flagged as inherently ambiguous) — and reports each recognized instance: component/port/rail maps, bench provenance, and **every deviation from the validated block** (`value_mismatch`, `missing_component`, `nc_violated`, `extra_attachment` on internal nets, `nets_merged_on_board`, `connectivity_mismatch`), with the block's `constraints:` surfaced verbatim for Claude to judge (the script can't call a constraints violation — e.g. two recognized instances on one I2C bus). Runs at every ingest; not a gate (always exits 0).

Workflow (SKILL.md): **INGEST** (always, cheap) → **RECONCILE** (drift = first-class findings) → **ENRICH** (proportional Claude judgment: fact cards, net `class:` tags, `reconstructed_intent.md` with `[INFERRED]`/`[CONFIRMED]` marks) → **REASON** (modes: REVIEW / DEBUG / EXPLAIN / RESELECT / MODIFY — not gated stages). `references/ingest.md` documents the trust rules and failure signatures (notably the stale-lib_symbols-cache cascade: 1 stale symbol → ~14 false connectivity errors; recognize it before reporting findings).

```bash
# Ingest an existing board into a context pack
python .claude/skills/kicad-board-context/scripts/extract_netlist.py <board.kicad_sch> -o claude_context/netlist.yaml [--json] [--strict]
python .claude/skills/kicad-board-context/scripts/extract_bom.py <board.kicad_sch> -o claude_context/bom_flat.md [--json]
python .claude/skills/kicad-board-context/scripts/summarize_pcb.py <board.kicad_pcb> -o claude_context/pcb_summary.yaml [--json]

# Cross-artifact drift report (exit 1 on drift errors)
python .claude/skills/kicad-board-context/scripts/reconcile.py <board.kicad_sch> --bom claude_context/bom_flat.md --pcb <board.kicad_pcb> [--json]

# Proven-block recognition (W1c): registry blocks on this board + deviations from the validated design
python .claude/skills/kicad-board-context/scripts/match_blocks.py claude_context/netlist.yaml [--blocks-dir DIR] [--min-match 0.5] [--json]

# BAKE mode: fix PCBWay identity fields on an existing board (user-approved write; then F8 + re-reconcile)
python .claude/skills/kicad-schematic-gen/scripts/bake_bom_fields.py <bom.md> <board.kicad_sch> [--json] [--force]

# Run the skill's tests (also included in the full sweep)
python -m pytest .claude/skills/kicad-board-context/tests/ -v
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

**Checks** (13 total): `floating_pin`, `dangling_wire`, `disconnected_label`, `duplicate_reference` (hierarchy-wide), `missing_lib_symbol`, `stale_lib_cache` (warning — symbol resolved from installed libraries because the file's embedded cache is missing it), `single_pin_net` (skips explicitly NC'd pins), `missing_junction`, `overlapping_components`, `no_connect_conflict`, `missing_power_source`, `similar_net_names`, `sheet_integrity` (W1b — sheet pin ↔ child hierarchical-label parity, missing/duplicate sheet files and pin names, unwired-pin warning). On a hierarchical schematic, `validate` recurses into loaded child sheets, prefixing their issues with `[sheet name]`.

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
