# Validated corpus — built + bench-verified boards (EMPTY today)

A case belongs here **only** if the board was physically fabbed and bench-verified. These are
the *correctness ground truth* — the anchors a fresh design is graded against in Tier 3, and
the realistic targets for Tier 2 gate-adherence runs. A `synthetic/` case (however clean it
passes every verifier) is **not** correctness ground truth: passing the deterministic checks
proves self-consistency, not that the design actually works.

## The bar
- Board fabricated and **bench-verified** — state exactly what was confirmed (rails come up,
  charge current correct, sensor reads, etc.).
- Full current-skill artifact set frozen here (selective freeze, same as
  `../synthetic/battery_3s_full`: text artifacts + `*.facts.yaml` cards + `index.md` +
  `uuid_seed=0` golden; no PDFs/backups/PNGs).
- `case.yaml` with `provenance: validated` and a `verified:` block (fab date + what was tested).

## First planned member
`nau7802_loadcell/` — the DualScale NAU7802 dual-load-cell ADC board. Reserved; promote it via
the checklist in `../../README.md` once it is built and verified.
