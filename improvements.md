# Validator Improvements — Known Gaps

Issues identified during electrical analysis that don't have clean deterministic
solutions yet. Each needs either design decisions, new data model fields, or
component-specific knowledge before it can be implemented.

---

## 1. Multi-unit symbol awareness

**Problem**: Quad op-amps (LM324), hex inverters (74HC04), dual comparators — when
only some units are placed, the unused units' inputs float. Electrically, floating
CMOS inputs can oscillate, draw excess current, and couple noise. The validator
has no concept of "this symbol has 4 units but only unit 1 was placed."

**Why it's hard**: `LibSymbol` doesn't track total unit count. The generator would
need a `total_units` field, and the validator would need to check that either all
units are placed or unused units have their inputs tied and outputs NC'd.

**What it would take**: Add `total_units: int = 1` to `LibSymbol`. In
`add_lib_symbol_ic`, accept a `units` parameter. Validator check: for each
lib_id with `total_units > 1`, verify that all units 1..N have a placed component,
or emit a warning listing unplaced units.

---

## 2. Open-drain / open-collector outputs missing pull-ups

**Problem**: I2C (SDA/SCL), interrupt lines, comparator outputs, NMOS drain
pins — these are open-drain and physically cannot drive a logic high without an
external pull-up resistor. The validator currently only checks connectivity, not
whether the net has a path to a supply through a resistor.

**Why it's hard**: Requires pin-level metadata that doesn't exist today. KiCad's
pin types (`passive`, `output`, `open_collector`, `open_emitter`) could carry this
info, but the generator's `Pin.pin_type` doesn't distinguish push-pull from
open-drain. Even if it did, the validator would need to trace the net and confirm
a resistor connects it to a power rail — not just any connection, but specifically
a resistive pull-up.

**What it would take**: Extend `Pin.pin_type` to include `open_collector` /
`open_drain`. Add a check that walks the net and verifies at least one passive
component (resistor) connects the net to a power net.

---

## 3. Wire X-crossings (midpoint-to-midpoint)

**Problem**: Two wires cross at a point that's interior to both segments. Neither
wire has an endpoint at the crossing. KiCad does NOT connect them (correct), but
if the designer intended a connection, the validator can't flag the missing
junction because `_check_missing_junctions` only looks for endpoints landing on
segment interiors.

**Why it's hard**: Detecting the geometric intersection of two axis-aligned
segments is straightforward, but knowing whether the crossing is intentional
(parallel bus lines crossing a perpendicular bus) or accidental (forgot a junction)
is not. Flagging every crossing would produce false positives on any schematic
with bus-style routing.

**What it would take**: Compute all segment intersections. Emit an "info"-level
notice (not warning) for each crossing without a junction and without an existing
wire endpoint. Optionally suppress for known bus patterns.

---

## 4. Power-through-resistor false positives

**Problem**: The `missing_power_source` check looks for a `power_out` pin or power
symbol on each net that has a `power_in` pin. If an IC's VIN pin is fed through a
ferrite bead, current-sense resistor, or reverse-polarity protection FET, the net
between that component and the IC has only `passive` pins — no power source. The
check fires a false warning.

**Why it's hard**: The validator would need to trace transitively through passive
components to find a power source. A ferrite bead between +3V3 and IC_VIN means
the IC_VIN net is powered, just indirectly. But how many hops of passive components
should it traverse? And should it distinguish resistors (current-limiting, maybe
intentional voltage drop) from ferrite beads (basically zero DC resistance)?

**What it would take**: Transitive power-source search: from a `power_in` pin's
net, follow nets through components that have all-passive pins (both pins on
different nets), and check if any reached net has a power source. Limit depth
to 2-3 hops to avoid false connections.

---

## 5. Missing decoupling capacitors

**Problem**: Every IC's VCC/VDD pin should have a decoupling capacitor (typically
100nF) placed close to it on the same net. The preferences file even specifies
`include_decoupling: true` and `decoupling_cap: "100nF"`. But the validator
doesn't check for this. A schematic with an IC's VCC wired to a power rail but
no capacitor on that net passes validation.

**Why it's hard**: "Close to" is a layout concept, not a schematic one. On the
schematic, the cap just needs to be on the same net. But checking "does this net
have a capacitor" requires knowing which components are capacitors (by lib_id
prefix "Device:C") and which nets are power nets. Also, shared decoupling
(one cap serving multiple ICs) is sometimes acceptable.

**What it would take**: For each non-power component with at least one `power_in`
pin, check that every `power_in` pin's net also has at least one capacitor
(lib_id starts with `Device:C`). Emit a warning if not. Could be made
preference-aware by reading `preferences.yaml`.

---

## 6. Exposed / thermal pad connections

**Problem**: QFN, DFN, and similar packages have exposed pads that must be
soldered to GND (or a specific net) for thermal dissipation and sometimes
electrical function. If the KiCad symbol doesn't include this pad as a pin,
the validator can't check it.

**Why it's hard**: This is a symbol definition completeness issue. The validator
operates on what's in the schematic. If the exposed pad is defined as a pin
(common practice — it's usually the highest-numbered pin), it gets checked like
any other pin. If it's omitted from the symbol, no tool can catch it.

**What it would take**: A library-level audit rather than a schematic-level check.
Could add an optional `has_exposed_pad: bool` flag to `LibSymbol` and check that
a matching pin exists. Or maintain a list of known packages that require exposed
pad connections.

---

## 7. Non-90-degree rotation grid misalignment

**Problem**: The rotation math in `get_pin_position` uses float sin/cos, then
`snap_to_grid` rounds the result to 1.27mm grid. For 0/90/180/270 rotations this
is exact. For arbitrary angles (e.g., 45 degrees), the snapped pin position may
not match where a human would place a wire, causing phantom disconnections.

**Why it's hard**: The generator doesn't restrict rotation angles. In practice,
KiCad schematics almost never use non-orthogonal rotations, but nothing prevents it.

**What it would take**: Add a `check_rotation_angles` validation that warns if
any component has a rotation that isn't a multiple of 90 degrees.
