# Bringup Checklist — {project_name} rev {rev}

<!--
AUTHORING INSTRUCTIONS (Claude, Stage 10 — delete this comment in the output):
Author this from the verified artifacts, never from memory:
  * expected voltages  → the Stage-5 DC analysis ({project}_04c_analysis.md /
    design YAML) — cite the number the analyzer checked, not a re-derivation;
  * rails / nets       → the Stage-5b netlist YAML;
  * I2C addresses      → the {MPN}.facts.yaml cards / datasheets;
  * pin references     → board_pins.h from generate_pinmap.py (MCU boards);
  * idle-current bound → the current-budget block of the DC analysis.
Every line must be a MEASURABLE step with a concrete expected value and a
place to write the measured one. If a value can't be derived from the
artifacts, write [ASK] and resolve it with the user before bench day.
Order matters: nothing downstream is powered until its upstream rail passed.
-->

Board: {project_name} rev {rev} — schematic sha256 `{short}`, BOM `{bom file}`
Generated from: DC analysis `{04c file}`, netlist `{05b file}`

## 0 · Before power

| # | Check | Expected | Measured | ✓ |
|---|-------|----------|----------|---|
| 0.1 | Visual: orientation of {polarized parts: ICs, diodes, electrolytics} | matches layout | | |
| 0.2 | Ohmmeter: {each rail} to GND | no short (> {value} Ω) | | |
| 0.3 | Bench supply current limit set | {limit from budget, e.g. 100 mA} | | |

## 1 · First power (current-limited, nothing else connected)

| # | Check | Expected | Measured | ✓ |
|---|-------|----------|----------|---|
| 1.1 | Idle current at {Vin} | < {value from current budget} | | |
| 1.2 | {RAIL_1} at {test point / probe location} | {V ± tol, from analyze_dc} | | |
| 1.3 | {RAIL_2} … | | | |
| 1.4 | Thermal spot-check: {regulators, charger} | warm at most, not hot | | |

## 2 · Digital bringup

| # | Check | Expected | Measured | ✓ |
|---|-------|----------|----------|---|
| 2.1 | USB enumeration | {VID/PID or "serial port appears"} | | |
| 2.2 | Firmware flashes / bootloader entry | | | |
| 2.3 | I²C scan (bringup.ino) | exactly {addresses from fact cards, e.g. 0x2A NAU7802} | | |
| 2.4 | {status LED / heartbeat} | | | |

## 3 · Per-block functional checks

<!-- One subsection per functional block, cheapest-discriminating test first. -->

### {Block name, e.g. Load-cell front-end}
| # | Check | Expected | Measured | ✓ |
|---|-------|----------|----------|---|
| 3.1 | {e.g. bridge excitation voltage} | {V} | | |
| 3.2 | {e.g. ADC reads mV-level dead load, stable to ±N counts} | | | |

## 4 · Result

- Overall: PASS / PASS-with-anomalies / FAIL
- **Next step (non-optional): fill in `{project}_field_report.md`
  (templates/10_field_report.md) and update `validated_boards.yaml`** —
  see `references/promotion.md`. A bench session that doesn't end in a
  ledger update deposits nothing.
