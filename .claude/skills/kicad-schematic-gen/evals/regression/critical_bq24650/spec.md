# Specification — 3S charger (negative regression: BQ24650 mis-selection)

This is a **frozen negative fixture**, not a real design. It reproduces the
selection-error class the answer-blind `[CRITICAL]` gate exists to catch: choosing a
charge controller with no internal system power path against a spec that demands one.

## Requirements Checklist

- R1 Accept 15 V from a USB-C PD source (single-resistor strap, no MCU).
- R2 [CRITICAL] The SYS rail must be live whenever VBUS is present, even with the
  battery absent or UVLO'd — a true system power path, not just a battery charger.
- R3 Charge a 3S Li-ion pack at ~1.5 A, resistor-set, MCU-free.
