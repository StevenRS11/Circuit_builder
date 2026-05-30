# Low-level `KicadSchematic` builder API (Stage 6 fallback path)

The **primary** Stage 6 path is the data-driven engine (`generate_from_data.py`) —
see the "Stage 6: Schematic Generation" section in `SKILL.md`. The engine is built
on the `KicadSchematic` builder library documented here; use it **directly only for
custom symbol shapes or one-off edge cases the layout schema can't express.**

The same source-of-truth rules apply: connectivity comes from the Stage 5b netlist
YAML, footprints/values from the Stage 3 BOM, IC pin definitions from the Stage 4
implementation reference.

**The netlist YAML is the single source of truth for connectivity. Do not add
connections not in the netlist. Do not omit connections that are in the netlist.**

## What Claude does

Import the generation script, then follow Steps 1–7 below mechanically:
```python
import sys
sys.path.insert(0, "/path/to/skill/scripts")
from generate_kicad_sch import KicadSchematic
```

### Pre-flight Checks

Before writing any builder code:
1. Parse the netlist YAML. Count: total components, total nets, total pin assignments, total no-connects.
2. Verify every component in the netlist has a footprint in the BOM.
3. Verify every component in the BOM appears in the netlist.
4. Verify every pin listed in each component's `pins` array appears in exactly one net OR in `no_connects`.
5. List the power symbols needed (from net `power_symbols` fields).
6. **If any check fails, stop and reconcile the documents before generating.**

### Step 1: Library Symbols

Register all required symbol definitions:

- **Standard passives:** `add_lib_symbol_resistor()`, `_capacitor()`, `_inductor()`, `_led()`, `_diode()` — one call each for types used
- **Power symbols:** `add_lib_symbol_power("GND")` plus any power symbols from the netlist (e.g., `"+5V"`, `"VCC"`)
- **Connectors:** `add_lib_symbol_connector(N)` for generic connectors, or `add_lib_symbol_ic(...)` for connectors with named pins
- **ICs:** `add_lib_symbol_ic(lib_id, pins=[...])` for each unique IC

**Critical:** The IC symbol's pin number set must **exactly match** the component's `pins` array in the netlist YAML. Cross-check before proceeding.

```python
sch.add_lib_symbol_resistor()
sch.add_lib_symbol_capacitor()
sch.add_lib_symbol_power("GND")
sch.add_lib_symbol_ic("custom:TP4056", ref_prefix="U", value="TP4056", width=12.7, pins=[
    ("4", "VCC",   "power_in",      "left",  0),
    # ... all pins from Stage 4 pinout table
])
```

### Step 2: Component Placement

Place components grouped by functional block from Stage 4.

- Start at approximately (50, 50), flow right and down
- Each functional block gets a horizontal band ~50mm tall
- ICs: 40mm horizontal spacing between IC centers
- Passives associated with an IC: within 15-25mm
- Connectors: input left edge, output right edge
- Use `suggest_placement()` where available
- All components at rotation=0 unless a specific layout reason exists
- **Every `place_component()` call MUST include `footprint=` from the BOM**

```python
sch.place_component("custom:TP4056", "U1", "TP4056", 100, 65,
    footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")
sch.place_component("Device:C", "C1", "10uF", 75, 55,
    footprint="Capacitor_SMD:C_0805_2012Metric")
```

### Step 3: Net Wiring — Label-Based (Critical Step)

Process each net from the netlist YAML mechanically. **Use labels for ALL connectivity. No point-to-point wires between distant components.**

**For the GND net:**
```python
# For EVERY pin in the GND net's pins list:
sch.gnd_at_pin("U1", "3")    # auto-routes stub + GND symbol
sch.gnd_at_pin("R3", "2")
# ... every pin listed under the GND net
```

**For nets with `power_symbols` entries (e.g., VBUS with ["+5V"]):**
```python
# First pin: place the power symbol (satisfies KiCad ERC power source rule)
sch.power_at_pin("J1", "A9", "+5V")
# All remaining pins: use labels
sch.label_at_pin("U1", "4", "VBUS")   # auto-detects stub direction
sch.label_at_pin("U1", "8", "VBUS")
# ... every other pin in the net
```

**For all other nets (signal nets, power nets without power_symbols):**
```python
# For EVERY pin in the net's pins list:
sch.label_at_pin("U3", "3", "FB_NET")
sch.label_at_pin("R6", "2", "FB_NET")
sch.label_at_pin("R7", "1", "FB_NET")
```

**Rules:**
- Process **every pin in every net**. No exceptions, no shortcuts.
- Use the **exact net name** from the YAML as the label text.
- `label_at_pin` auto-computes the correct stub direction (via `get_pin_stub_direction`). Do not specify direction manually unless overriding for aesthetics.
- Do NOT use `wire_between()` or manual `add_wire()` for inter-component connections.
- Junctions are not needed (label-based connectivity has no T-joints).

### Step 4: No-Connects

```python
# For EVERY entry in the netlist's no_connects list:
sch.nc_at_pin("U3", "5")
sch.nc_at_pin("U4", "4")
```

### Step 5: Pre-Save Audit

```python
# Check 1: every pin has a label, power symbol, or NC
unassigned = sch.ensure_all_pins_assigned()
if unassigned:
    print(f"UNASSIGNED PINS: {unassigned}")
    # Fix: add missing label_at_pin / gnd_at_pin / nc_at_pin calls

# Check 2: every component has a footprint
missing_fp = sch.ensure_footprints()
if missing_fp:
    print(f"MISSING FOOTPRINTS: {missing_fp}")
    # Fix: add footprint= to place_component calls
```

### Step 6: Save and Validate

```python
sch.save(output_path)
```

Then run all three validators **in sequence**:

**Check 1 — Structural validation:**
```bash
python validate_kicad_sch.py output.kicad_sch --json
```
- **0 errors required.** Warnings acceptable if explained (e.g., `single_pin_net` for NC pins, `missing_power_source` for battery-referenced nets like B_NEG).

**Check 2 — Netlist verification (the critical check):**
```bash
python verify_netlist.py project_05b_netlist.yaml output.kicad_sch --json
```
- **0 errors required. 0 warnings required.** If this passes, the schematic matches the design intent exactly.

**Check 3 — BOM cross-check:**
```bash
python cross_check_bom.py project_03_bom.md output.kicad_sch --json
```
- Verify all components match in reference, value, and footprint.

### Step 7: Fix Loop

If any validation fails:
1. Read each error message carefully.
2. Map the error to a specific missing/wrong `label_at_pin`, `gnd_at_pin`, or `nc_at_pin` call.
3. Add/fix the call, re-save, re-validate from Check 1.
4. Maximum **3 fix iterations**. If still failing, present errors to user with analysis.

### API Quick Reference

```python
sch = KicadSchematic("Board Title", rev="1.0")

# Library symbols
sch.add_lib_symbol_resistor()           # Device:R, pins 1(top) 2(bottom)
sch.add_lib_symbol_capacitor()          # Device:C, pins 1(top) 2(bottom)
sch.add_lib_symbol_capacitor_polarized() # Device:C_Polarized, 1(+) 2(-)
sch.add_lib_symbol_led()                # Device:LED, 1(K/left) 2(A/right)
sch.add_lib_symbol_inductor()           # Device:L, pins 1(top) 2(bottom)
sch.add_lib_symbol_diode()              # Device:D, 1(K/left) 2(A/right)
sch.add_lib_symbol_mosfet_n()           # Q_NMOS_GSD: G=1, S=2, D=3
sch.add_lib_symbol_mosfet_p()           # Q_PMOS_GSD: G=1, S=2, D=3
sch.add_lib_symbol_bjt_npn()            # Q_NPN_BCE: B=1, C=2, E=3
sch.add_lib_symbol_bjt_pnp()            # Q_PNP_BCE: B=1, C=2, E=3
sch.add_lib_symbol_crystal()            # Crystal: 2-pin, prefix Y
sch.add_lib_symbol_crystal_4pin()       # Crystal_GND24: 4-pin with case GND
sch.add_lib_symbol_power("GND")         # one call per power symbol
sch.add_lib_symbol_connector(num_pins, lib_id)
sch.add_lib_symbol_ic(lib_id, pins=[...], ref_prefix="U", value="...", width=10)

# Place components (footprint REQUIRED)
sch.place_component(lib_id, ref, value, x, y, rotation=0, footprint="...")
sch.place_power_symbol(name, x, y, rotation=0)

# Netlist-driven wiring (primary methods for Stage 6)
sch.label_at_pin(ref, pin, label_name)        # auto-directed stub + label
sch.gnd_at_pin(ref, pin)                       # auto-directed stub + GND symbol
sch.power_at_pin(ref, pin, power_name)         # auto-directed stub + power symbol
sch.nc_at_pin(ref, pin)                        # no-connect at pin
sch.get_pin_stub_direction(ref, pin)           # returns (dx, dy) outward direction

# Pre-save audits
unassigned = sch.ensure_all_pins_assigned()    # list of (ref, pin) with no connection
missing_fp = sch.ensure_footprints()           # list of refs with no footprint

# Layout helpers
x, y = sch.suggest_placement(lib_id, relative_to={"ref": "U1", "pin": "5"},
                              relationship="decoupling")
sch.register_group(group_id, reference)

# Manual wiring (rarely needed in label-based approach)
sch.add_wire(x1, y1, x2, y2)
sch.wire_between(ref1, pin1, ref2, pin2)
sch.add_label(text, x, y, rotation=0)
sch.add_global_label(text, x, y, shape="input", rotation=0)
sch.add_junction(x, y)
sch.add_no_connect(x, y)
x, y = sch.get_pin_position(ref, pin)

# Save
sch.save("output.kicad_sch")
```
