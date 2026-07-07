# Roadmap — from pipeline to accumulating system

Agreed 2026-07-06. The strategic diagnosis: the pipeline is process-complete but
doesn't *accumulate* — every board re-derives blocks the previous boards already
proved. These three workstreams (plus two small adjacencies) fix that. Track
record at time of writing: battery_3s (bench-validated, one failure → preflight
gate #5), ESP32-based CoF testers, deflection testers, ESP-based scales with
on-board ADCs.

Ordering: **A1 → A2 → W2 → W1a → W1b → W1c → W3.** The adjacencies are small and
unblock everything; the validation loop (W2) comes before the block library
because it *produces* the blocks; W3 (layout verify) is deliberately last and
designed after the block schema exists.

Status: **A1 DONE, A2 DONE, W2 infrastructure DONE, W1a DONE (2026-07-06),
W1b DONE (2026-07-07).** W1b shipped: layout YAML `blocks:` section
(`{instance: {block, x, y, port_map, refdes_base?}}` + optional `blocks_dir:`),
engine composition in `generate_from_data.py` (loads the registry bundle,
clones `sheet.kicad_sch` per instance with refs re-annotated into per-instance
ranges — U2→U102/U202 — and a fresh root uuid, emits the root `(sheet)` symbol
with pins from the block contract, wires each pin to its mapped board net,
self-verifies against the FLATTENED intended netlist + merged BOM, emits a
whole-board `{out}_bom_flat.md` for Stage 9, and removes clones on a failed
build), hierarchical loader (`load_kicad_sch` walks `(sheet)`→child files with
cycle detection; `extract_netlist` merges child netlists through sheet pins —
port nets join the parent net, power/global names merge globally, sheet-local
nets get `instance/` prefixes), validator recursion + the `sheet_integrity`
check (13th: pin↔hlabel parity, missing child file, duplicate sheet names,
unwired-pin warning) + hierarchy-wide duplicate-ref check, hierarchy-aware
`verify_netlist`/`cross_check_bom`/board-context `extract_netlist` (the
reviewer's flat-only limitation is GONE), and an NC'd-pin single_pin_net
false-positive fix. New gates: port_map↔contract parity, mapped-net declared,
rail-availability, refdes-range collision. 25 new tests (606 repo-wide);
byte-deterministic under `--uuid-seed` including clones.
W1a shipped: `HierarchicalLabel` support through the whole stack (builder
`hlabel_at_pin`/`add_hierarchical_label`, save-emission, loader parse, netlist
extraction, dangling-wire check), `blocks/` registry + `CircuitBlocks.pretty`
promotion, `extract_block.py` (port contract explicit; declared port overrides
internal classification; power nets can't be ports; self-verifies and removes
the bundle on failure), `check_block.py` (standalone validation, port/rail/BOM
parity, two-source dependency policy, TODO-judgment warnings).

**W1a close-out: NAU7802 proof DONE (2026-07-06).** `nau7802_dual_loadcell`
extracted from the real DualScale_Compact combined board: 25 components, 3
ports (SDA/SCL/DRDY), rails 3V3(+forced-rail flag, added for label-powered
donors)/GND, JST-PH footprint promoted into CircuitBlocks, contract fully
filled from the addon's verified DC analysis, `check_block --strict` clean,
ledger entry recorded. The proof shook out and fixed 4 real bugs (all with
regression tests): extract_netlist auto-name collision clobbering labeled
nets; stale property-cache refs fooling every regex field reader (now
instances-resolved via `resolve_active_reference`); control chars in
component values crashing YAML emission; source auto-net names traveling
into blocks (now renamed to IC-anchored `N_U2_x`). Pending from the user:
DualScale bench_date + retroactive field report. → Next: **W1b** (engine
composes sheets) then bulk fleet seeding. W2 shipped:
`templates/10_bringup.md` + `templates/10_field_report.md`,
`references/promotion.md`, `validated_boards.yaml` (seeded with battery_3s
v3.0 failure + lessons and v3.1 in-bringup), `scripts/check_ledger.py`
(mechanically verifies every lesson's `path::needle` encoding still exists),
SKILL.md Stage 10. Seeding order (user decision, 2026-07-06): **prove the
loop end-to-end once on the validated NAU7802 block** (needs W1a's
`extract_block.py`), then run the bulk retroactive pass over the rest of the
fleet as the last step. → Next up: **W1a** (block format + extraction).

Style note (user call): SKILL.md bloat is acceptable while features land —
capabilities must never silently vanish. Pare down only when feature-complete.

---

## A1 (small): Loader library-fallback — DONE 2026-07-06

`load_kicad_sch` resolves cache-missing lib_ids from installed/registered
libraries (`--project-dir` / `--sym-lib` / `--no-lib-fallback`), keeps real
pins/connectivity, and emits a `stale_lib_cache` warning (12th validator
check); unresolvable lib_ids tracked in `sch.unresolved_lib_ids` and still
error. `extract_netlist.py` surfaces both. Bonus fix:
`check_kicad_library._split_symbols` was tab-indentation-bound (space-indented
vendor `.kicad_sym` files returned zero symbols) — now depth-based.
Tests: `tests/test_loader_fallback.py` (false-cascade regression included).

## A2 (small): Firmware pinmap handoff — DONE 2026-07-06 — `generate_pinmap.py`

The boards are ESP32 test fixtures; the schematic isn't the product, the working
instrument is. The netlist YAML already knows every MCU pin ↔ net binding.
Deterministic script (fits the doctrine — pure transform of authored data):

- Input: `05b_netlist.yaml` + flat BOM (identify the MCU by lib_id/part).
- Output: `board_pins.h` (one `#define`/constexpr per named net on an MCU pin),
  plus a bringup-sketch skeleton (I²C scan with expected addresses from the
  fact cards, status-LED blink, per-rail ADC sanity reads where wired).
- Catches the classic "firmware GPIO ≠ schematic GPIO" defect class at the seam.
- Works for brownfield too: the board-context skill's extracted netlist feeds it
  directly.

## W2 (medium): The validation loop — every build deposits knowledge

**This is the block factory.** Mistakes become lessons/checks/gates; successes
become copy-paste functional blocks. Formalize what happened ad hoc with the
CH224K failure (bench failure → preflight gate #5, permanently).

1. **Bringup checklist at delivery** (`templates/10_bringup.md`, Claude-authored
   from the netlist + DC analysis, per doctrine): power-up order, expected
   voltage per rail/test point (numbers from `analyze_dc`), first-connect checks
   (USB enumeration, I²C scan expectations), smoke-test current estimate.
2. **Field report ritual** (`templates/field_report.md`): after bench time, the
   user reports pass/fail per checklist line + anomalies. Ten minutes, max.
3. **Promotion rules** (`references/promotion.md`) — every report ends in one or
   more explicit deposits:
   - **Failure →** classify and encode where it belongs: pinout error → fact
     card / `pinout_db.json` fix; topology class → engine preflight gate or
     analyzer check (CH224K precedent); sourcing/part-identity → `check_pcbway`
     / `bom_verify` gate; layout class → seed for W3's check list.
   - **Success (board stable in use) →** the board becomes a **Tier-3 eval
     anchor** (regen + full verifier suite against the validated artifact), its
     novel ICs get `pinout_verified` promotion, and its reusable subcircuits get
     extracted as **blocks** (W1).
4. **Validated-board ledger** (`validated_boards.yaml`): board → rev, bench
   date, status, failures found, where each lesson was encoded, blocks
   extracted. The durable registry the reviewer and Stage 2 can consult.
5. **Retroactive seeding — do this immediately:** run the ritual backwards over
   the existing fleet (battery_3s, CoF testers, deflection testers, scales).
   That yields the first ledger entries, the first Tier-3 anchors, and the
   first block candidates (NAU7802 front-end, S3 inverter blocks) with real
   provenance.

SKILL.md gains a **Stage 10: Bringup & Field Report** (non-gating, after
delivery) so the loop is part of the skill, not tribal knowledge.

## W1 (large, 3 phases): Proven-block library — hierarchical sheets

**A block = anything with active silicon and a defined, beyond-trivial function,
validated on a built board.** KiCad-native representation: a hierarchical child
sheet whose **hierarchical labels are the block's declared inputs/outputs**;
power rails connect globally via power symbols and are documented in the
contract, not pinned.

### W1a: Block format + extraction (do after W2 scaffolding exists)

Registry at `.claude/skills/kicad-schematic-gen/blocks/{block_name}/`:

```
blocks/nau7802_frontend/
├── block.yaml          # the contract (see below)
├── sheet.kicad_sch     # child sheet, hierarchical labels at ports
├── netlist.yaml        # 05b fragment: components, internal nets, ports marked
├── bom.md              # flat BOM lines for the block's parts
├── layout_intent.md    # reserved now, populated by W3 (schema field exists day 1)
└── facts/              # {MPN}.facts.yaml for the block's silicon
```

`block.yaml` — the "this works, and these are its inputs/outputs" document:
```yaml
name: nau7802_frontend
description: "Load-cell bridge front-end + NAU7802 24-bit ADC, ratiometric"
provenance: {validated_on: "DualScale_Compact rev3", bench_date: ..., field_report: ...}
ports:
  - {name: SDA,  dir: bidirectional, class: digital, note: "pull-ups live on parent"}
  - {name: DRDY, dir: output, class: digital}
  - {name: LC_P, dir: input, class: analog_differential, pair: LC, polarity: P}
rails:
  - {name: "+3V3", budget_ma: 2}
  - {name: GND}
constraints:
  - "I2C address 0x2A fixed — one block per bus"
```

**Dependency resolution (decided 2026-07-06)** — three asset types, three rules:

- **Symbols: embedded, always.** `sheet.kicad_sch` carries its full
  `lib_symbols` cache (KiCad-native), so blocks have zero symbol-library
  dependencies; original `lib_id` kept as provenance only. `extract_block.py`
  refuses a source schematic with a stale symbol cache (the A1 signature) —
  never freeze a broken cache into the library.
- **Footprints: exactly two allowed sources — KiCad built-ins or the single
  repo library `blocks/footprints/CircuitBlocks.pretty`** (one nickname,
  git-versioned). Extraction *copies* any non-builtin `.kicad_mod` from
  whatever fragmented personal lib it lived in into `CircuitBlocks` and
  rewrites the block's footprint fields; `copied_from` provenance recorded.
  `block.yaml` gains a `dependencies.footprints` manifest
  (`{ref, source: builtin|blocks, copied_from}`), enforced by
  `check_block.py`. One-time per-machine registration of `CircuitBlocks` in
  the global fp-lib-table (via a `${CIRCUIT_BUILDER}` path variable);
  generation pre-flights registration with `check_kicad_library.py
  --footprint` so a miss surfaces at generate time, not at F8.
- **3D models: best-effort.** Copy into `blocks/3dmodels/`, rewrite paths to
  the variable; a missing model is cosmetic and never fails `check_block`.

Consequences: blocks are reproducible from a fresh clone + one fp-lib-table
entry; the user's fragmented learning-era libraries are never referenced by
any block, and they decay naturally — canonical homes going forward are
`Custom` (kicad-import-lib vendor installs) and `CircuitBlocks` (promotion
from validated boards only). Eventual cleanup = delete what never earned
promotion; no block can break.

Tooling: `extract_block.py` (board-context skill synergy) — given a validated
board's schematic + a ref list + port→net mapping, carve out the block bundle
mechanically (netlist subset, BOM subset, child sheet with hierarchical labels
substituted at the cut nets, footprint promotion into `CircuitBlocks`).
`check_block.py` verifies bundle-internal consistency (ports ⊆ netlist nets,
every component in BOM, facts present, dependencies manifest satisfied).
**Seed blocks: NAU7802 front-end (DualScale) and the S3 board's inverter
blocks** — extracted, not hand-invented, so provenance is real.

### W1b: Engine + loader go hierarchical — DONE 2026-07-07

- Layout YAML gains `blocks:` — `{instance_name, block_id, x/y, port_map:
  {port → board_net}}`.
- `generate_from_data.py` emits child sheets + root `(sheet)` symbols with
  sheet pins + `sheet_instances`. **v1 refdes strategy: clone the child file
  per instance, re-annotated into per-instance ranges (U101…, U201…)** — true
  shared-file multi-instance annotation is a later refinement.
- `load_kicad_sch` / `extract_netlist` / validators traverse sheets (walk
  `(sheet)` → child files, resolve hierarchical labels through sheet pins).
  This **also removes the reviewer's flat-only limitation** — shared infra.
- Self-verify extends across the hierarchy (netlist verify sees through ports).

### W1c: Pipeline + reviewer integration

- **Stage 2 becomes block-first:** before the per-role candidate fan-out, check
  the registry — a proven block covering a role short-circuits sourcing for its
  parts ("validated on DualScale rev3" is stronger evidence than any datasheet
  citation). Rubric still re-checks *sourceability* (stock goes stale even when
  designs don't).
- Requirements traceability accepts `block:{name}` as `satisfied_by` evidence.
- **Reviewer block recognition:** on ingest, match netlist fragments against
  the registry — "U3/C11-C14/R7 is your NAU7802 block as validated on
  DualScale, except C12 is 10nF where the block says 100nF" is the highest-value
  sentence brownfield review can produce.

## W3 (design later — after W1a): Layout intent + verify_layout

Deliberately last (user call: needs more thinking). Constraints already fixed:
the `layout_intent.md` slot exists in the block schema from day 1 so blocks
carry placement/width/thermal intent when W3 defines the format, and TODO.md
items 6+7 (verify_layout.py + shared `load_kicad_pcb`) remain the skeleton.
Failure classes deposited by W2 field reports feed its check list. Design
session happens after W1a ships.

---

## Non-goals (unchanged)

Autorouting, SPICE, motherboard-class digital, speculative analyzer patterns.
Checks are earned from real failures (W2), not written on spec.
