# Design Review Checklist — {PROJECT_NAME}

> Run after schematic generation. Both the automated validator and this manual
> checklist must pass before delivery.

## Automated Validation (validate_kicad_sch.py)

```
{Paste validator output here}
```

- [ ] Validator exit code: 0 (PASS)
- [ ] No ERRORs remaining
- [ ] All WARNINGs reviewed and either fixed or justified below

### Warning Justifications

| Warning | Justification |
|---------|---------------|
| {warning text} | {why it's OK to leave} |

---

## Power Integrity

- [ ] Every IC has decoupling cap(s) per datasheet recommendation
- [ ] Bulk capacitor present on input power rail
- [ ] LDO input cap meets minimum value from datasheet
- [ ] LDO output cap meets minimum value and ESR requirements
- [ ] Capacitor voltage ratings adequate (>= 2x operating voltage for MLCC)
- [ ] No power rail exceeds any component's absolute maximum rating
- [ ] Power indicator LED present (if design_rules.include_power_indicator)
- [ ] Enable pins properly handled (tied high, pulled up, or controlled)

## Signal Integrity

- [ ] I2C lines have pull-ups (one set per bus, correct value for speed)
- [ ] SPI chip-select lines have pull-ups (if active-low)
- [ ] Reset lines have proper pull-up/pull-down and decoupling
- [ ] No bus contention — no two outputs driving the same net
- [ ] UART TX→RX crossover correct (not TX→TX)

## Connectivity

- [ ] Every component pin is either wired, labeled, or marked no-connect
- [ ] Net names are consistent (no VCC vs Vcc vs vcc collisions)
- [ ] Power symbols used correctly (GND points down, VCC points up)
- [ ] All junctions present where wires cross/T-join
- [ ] Connector pinouts match user's cable/system expectations

## Component Correctness

- [ ] Every IC pinout verified against datasheet (not just assumed)
- [ ] Passive values match datasheet application circuit recommendations
- [ ] LED polarity correct (anode to resistor, cathode toward GND)
- [ ] Diode polarity correct for intended function
- [ ] Polarized cap polarity correct

## Footprint & Packaging

- [ ] All footprints assigned and match selected packages
- [ ] IC footprints match the specific package variant ordered
- [ ] Passive footprints match preferences.yaml default size
- [ ] Connector footprints match the selected (PCBway-sourceable) parts

## BOM Consistency

- [ ] Every component in schematic appears in BOM
- [ ] Every BOM entry has a valid footprint
- [ ] Reference designators are sequential (no gaps, no duplicates)
- [ ] Parts flagged as "needs ordering" are noted in delivery

## Schematic Readability

- [ ] Components are logically grouped (power, signals, connectors)
- [ ] No overlapping components or labels
- [ ] Wire routing is clean (minimal crossings, L-shaped routes)
- [ ] Power flows top-to-bottom (VCC top, GND bottom)
- [ ] Signal flow is left-to-right where possible

---

## Review Summary

| Category              | Status | Issues Found |
|-----------------------|--------|--------------|
| Automated validation  | {PASS/FAIL} | {count} |
| Power integrity       | {PASS/FAIL} | {notes} |
| Signal integrity      | {PASS/FAIL} | {notes} |
| Connectivity          | {PASS/FAIL} | {notes} |
| Component correctness | {PASS/FAIL} | {notes} |
| Footprint/packaging   | {PASS/FAIL} | {notes} |
| BOM consistency       | {PASS/FAIL} | {notes} |
| Schematic readability | {PASS/FAIL} | {notes} |

**Overall:** {PASS / FAIL — needs fixes}

**Issues to fix before delivery:**
1. {issue}

**Caveats for user:**
1. {anything they should double-check in KiCad}
