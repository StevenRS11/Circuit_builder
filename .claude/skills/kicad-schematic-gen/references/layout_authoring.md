# Layout authoring guide (Stage 6)

This is the reasoning behind authoring `{project}_06_layout.yaml`. Authoring the layout is a
**judgment step** — Claude's job, not a script's (see the "Design hierarchy" section in
`SKILL.md`). The engine `generate_from_data.py` then *verifies* what you authored. Author with
the conventions below, run the engine, fix what its gates report.

The layout YAML owns only **geometry**: `power_nets`, `placements` (ref → lib_id + x/y/rotation),
and `symbols` (IC pin-side maps). Connectivity comes from the Stage 5b netlist; value/footprint
from the Stage 3 BOM. Don't restate those here.

---

## The `symbols:` block — where each field comes from

Each pin is `[number, name, type, side, index]`. The three sources, and which is yours to decide:

| Field | Source | Yours to decide? |
|-------|--------|------------------|
| `number` | the netlist component's `pins` list — **must match exactly** | no — copy it |
| `name`, `type` | `check_kicad_library --lookup`, `lookup_pinout`, or the datasheet | no — never invent |
| `side`, `index` | **your semantic grouping** | **yes** |

The engine cross-checks `set(symbol pin numbers) == set(netlist pins)` and errors on any
mismatch, so you cannot silently drop or invent a pin. Names/types are looked up, not guessed —
that's the anti-hallucination discipline. The only creative part is arranging the pins.

Passives (`Device:R/C/L/...`) and generic connectors (`Connector_Generic:Conn_01x0N`)
auto-register from their lib_id — **do not** give them a `symbols:` entry. Only true ICs and
named-pin parts need one.

---

## Pin-side grouping heuristics (and why)

Arrange pins by *function*, not by package pin order. A readable symbol also helps the analog
checks and the reader:

- **Left:** signal/analog inputs, sense inputs, primary power-in. Inputs flow in from the left.
- **Right:** outputs, digital I/O (I²C/SPI/UART), status/open-collector. Signals flow out right.
- **Top:** main supply rail(s) — `VCC`/`VDD`/`AVDD` enter from the top.
- **Bottom:** grounds (`GND`/`AVSS`/`DVSS`/exposed-pad) and references that return to ground.
- **Keep differential pairs adjacent** (e.g. `SRP`/`SRN`, `VIN1P`/`VIN1N`) and in polarity order —
  this makes the diff-pair intent obvious and pairs them visually for the analog reviewer.
- **Keep enable/config pins near what they gate** (an `EN` next to the rail it enables).

`index` is the 0-based position down a side (or left-to-right across top/bottom). Order pins on a
side so related ones sit together.

Why semantic and not the KiCad symbol's own geometry: the library symbol is often arranged by
package pin number, which scatters a differential pair or buries grounds among signals. A
function-grouped symbol is clearer and is why we author it rather than copy raw pin positions.

---

## Worked example A — a library IC (BQ24650)

`python check_kicad_library.py BQ24650 --lookup` returns authoritative numbers/names/types:

```
lib_id: Battery_Management:BQ24650
footprint: Package_DFN_QFN:Texas_RVA_VQFN-16-1EP_3.5x3.5mm_P0.5mm_EP2.14x2.14mm_ThermalVias
  1 VCC (power_in)   2 MPPSET (input)   3 STAT1 (open_collector)   4 TS (input)
  5 STAT2 (open_collector)  6 VREF (input)  7 TERM_EN (input)  8 VFB (input)
  9 SRN (input)  10 SRP (input)  11 GND (power_in)  12 REGN (output)
  13 LODRV (output)  14 PH (input)  15 HIDRV (output)  16 BTST (output)  17 GND (passive)
```

Lift the numbers/names/types verbatim; decide sides by function (setpoints/feedback/sense in on
the left, switching drives out on the right, supply top, grounds bottom, status on the right,
sense pair adjacent):

```yaml
symbols:
  "Battery_Management:BQ24650":
    ref_prefix: U
    width: 25.4
    pins:
      - ["1",  "VCC",     "power_in",       "top",    0]
      - ["2",  "MPPSET",  "input",          "left",   0]
      - ["8",  "VFB",     "input",          "left",   1]
      - ["4",  "TS",      "input",          "left",   2]
      - ["7",  "TERM_EN", "input",          "left",   3]
      - ["6",  "VREF",    "output",         "left",   4]   # reference out; kept with the analog setpoints
      - ["12", "REGN",    "output",         "left",   5]
      - ["16", "BTST",    "output",         "right",  0]
      - ["15", "HIDRV",   "output",         "right",  1]
      - ["14", "PH",      "input",          "right",  2]
      - ["13", "LODRV",   "output",         "right",  3]
      - ["10", "SRP",     "input",          "right",  4]   # sense pair, adjacent + in polarity order
      - ["9",  "SRN",     "input",          "right",  5]
      - ["3",  "STAT1",   "open_collector", "right",  6]
      - ["5",  "STAT2",   "open_collector", "right",  7]
      - ["11", "GND",     "power_in",       "bottom", 0]
      - ["17", "GND",     "passive",        "bottom", 1]   # exposed pad
```

The pin-number set `{1..17}` matches the netlist, so the engine's pin-set gate passes. (Tune a
type to your design's role for a pin where the library's generic type doesn't fit — e.g. `VREF`
used as an output — but keep `number`/`name` as looked up.)

---

## Worked example B — an IC not in KiCad's built-ins (NAU7802)

**First, look it up against every registered library** — built-ins *and* the user's own:

```bash
python check_kicad_library.py NAU7802 --lookup --project-dir {project_dir}
```

There are two cases. The split is **"does the symbol already exist in a library?"** — and if
it does, you use it *as-is* and author no pins at all.

### Case 1 — it resolves in a library (the common case): embed it verbatim

If the lookup returns `found: true` (e.g. `lib_id: Custom:NAU7802`, typically with
`from_user_library: true` for a part installed via `kicad-import-lib`), the symbol **is** the
authoritative source — its real drawing *and* its real pin geometry. **Do not re-author the
pins, and do not assign `side`/`index`.** Just mark the `symbols:` entry `from_library: true`
and the engine embeds the actual symbol, exactly like KiCad does when you drop the part on the
canvas:

```yaml
symbols:
  "Custom:NAU7802":
    from_library: true          # engine embeds the real symbol as-is
```

The engine resolves the symbol from the registered libraries at build time — pass
`--project-dir {project_dir}` (and any `--sym-lib`) to `generate_from_data.py` so it can find
it. The pin-set gate checks the netlist against the symbol's **real** pins. (For a fully
self-contained build you may instead inline the raw `(symbol …)` block under a `block:` key, but
`from_library: true` is the normal path.) There is **no `side`/`index` judgment here** — the
arrangement belongs to whoever drew the symbol.

### Case 2 — it resolves nowhere (`found: false`): author the symbol from the datasheet

Only when the part exists in *no* library do you build it by hand. The **package is still
standard** (SOIC-16 → `Package_SO:SOIC-16_3.9x9.9mm_P1.27mm`, or look it up in
`footprint_map.yaml`), so put the footprint in the BOM and author the pins with a `Custom:`
lib_id. **This is the only case where `side`/`index` is Claude's job** (and a good reason to
instead install a vendor symbol with `kicad-import-lib`, turning it into Case 1):

```yaml
symbols:
  "Custom:NAU7802":
    ref_prefix: U
    width: 17.78
    pins:
      - ["3",  "VIN1P", "input",         "left",   0]   # channel-1 diff pair, adjacent
      - ["2",  "VIN1N", "input",         "left",   1]
      - ["5",  "VIN2P", "input",         "left",   2]   # channel-2 diff pair, adjacent
      - ["4",  "VIN2N", "input",         "left",   3]
      - ["1",  "REFP",  "input",         "left",   4]
      - ["6",  "VBG",   "output",        "left",   5]
      - ["16", "AVDD",  "power_out",     "right",  0]
      - ["15", "DVDD",  "power_in",      "right",  1]
      - ["14", "SDIO",  "bidirectional", "right",  2]   # I2C grouped on the right
      - ["13", "SCLK",  "input",         "right",  3]
      - ["12", "DRDY",  "output",        "right",  4]
      - ["10", "XIN",   "input",         "right",  5]
      - ["11", "XOUT",  "output",        "right",  6]
      - ["7",  "REFN",  "power_in",      "bottom", 0]   # the three grounds on the bottom
      - ["8",  "AVSS",  "power_in",      "bottom", 1]
      - ["9",  "DVSS",  "power_in",      "bottom", 2]
```

Here the `number`/`name`/`type` come from the datasheet, and the `side`/`index` grouping is the
judgment a script can't make well — both differential input pairs grouped left and in polarity
order, I²C grouped right, all three grounds on the bottom. That arrangement is Claude-authored
*only because the symbol didn't already exist*; a real library symbol (Case 1) carries its own.

---

## Placement conventions

`placements` is a starter geometry you (or the user) refine in KiCad — aim for valid and
openable, not optimal routing.

- Band by functional block: ICs/transistors on a top band, their decoupling caps nearby, passive
  dividers grouped, connectors at the board edges.
- Spacing: ~20 mm between IC centres, ~7.6 mm between adjacent passives; everything snaps to the
  1.27 mm grid.
- **Rotate multi-pin connectors so wire stubs don't collide.** A vertical N-pin connector wired to
  GND on its *top* pin will drop the GND symbol straight down through the rows below it, colliding
  with a lower pin's stub (an unintended short — `missing_junction`). Rotating it so pins face
  **down** (`rotation: 270`) gives every pin its own drop column. This is the J3 balance-header
  lesson: `J3: { lib_id: "Connector_Generic:Conn_01x04", x: 555, y: 60, rotation: 270 }`.

---

## The author → run engine → fix loop

The engine is the verify step. Run it and map each error to a data fix:

```bash
python generate_from_data.py {project}_05b_netlist.yaml {project}_03_bom_flat.md \
    {project}_06_layout.yaml -o {project}.kicad_sch
```

| Gate error | Meaning | Fix (edit the data) |
|------------|---------|---------------------|
| `no placement` / `no BOM line` / `not declared in netlist` | the three docs disagree on the component set | reconcile netlist / BOM / layout |
| `symbol pin-set != netlist pin-set` | an IC's `symbols:` numbers don't match the netlist | fix the pin numbers (usually the symbol; sometimes the netlist) |
| `missing_junction` | two stubs collided — an unintended wire touch (short class) | rotate/move the offending part (often a connector → `rotation: 270`) |
| `floating_pin` / completeness | a pin isn't in any net or no-connect | fix the netlist (Stage 5b), not the layout |
| BOM cross-check error | value/footprint mismatch | fix the BOM (Stage 3) |

Iterate until the engine reports `PASSED` with no errors. Because the `.kicad_sch` is regenerated
from data, never hand-edit it — change the layout YAML (or the upstream netlist/BOM) and re-run.
