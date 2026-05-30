# Stage 7 structural design-review checklist

This is the structural/semantic review walked in **Stage 7, Part B** of `SKILL.md`
(after the requirements-traceability pass and alongside
`templates/05_design_review_checklist.md`).

It catches *structural* and *semantic* errors the Stage 5 DC analyzer can't — wrong
pin assignment, missing connection, polarity flip — whereas the DC analyzer catches
*value* errors quantitatively (wrong resistor → wrong voltage). Together they cover
both classes. Work through every item:

## Power Integrity
- Every IC has decoupling caps per its datasheet
- Bulk cap on input rail
- LDO caps meet minimum values and ESR requirements
- Cap voltage ratings adequate (2x operating for MLCC derating)
- No component exceeds absolute maximum ratings
- Enable pins properly handled
- Power LED present if `design_rules.include_power_indicator`

## Signal Integrity
- I2C buses have exactly one set of pull-ups at correct value
- SPI CS lines have pull-ups if active-low
- Reset lines properly terminated
- No bus contention (two outputs on same net)
- UART TX/RX crossover correct

## Connectivity
- Every pin wired, labeled, or NC-marked
- Net names consistent (no case collisions)
- Power symbol orientation correct
- All junctions present at T-joins
- Connector pinouts match system expectations

## Component Correctness
- IC pinouts verified against datasheet (cross-reference Stage 4)
- LED polarity (anode→resistor→VCC, cathode→GND)
- Diode polarity correct for function
- Polarized cap polarity correct
- Passive values match datasheet recommendations

## Footprint & BOM
- All footprints assigned and match packages
- Reference designators sequential
- Schematic matches BOM exactly
