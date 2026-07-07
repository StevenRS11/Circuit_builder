# Context Pack — {board_name}

Manifest of everything derived from this KiCad project by the
`kicad-board-context` skill. The user's KiCad files are never modified;
this directory is the skill's entire footprint.

## Provenance

**Staleness rule:** before reasoning from this pack, re-hash the source files.
Any mismatch → re-run ingest for that artifact first.

| Source file | sha256 (at extraction) | Extracted | Tool |
|-------------|------------------------|-----------|------|
| {board}.kicad_sch | {sha256} | {utc timestamp} | extract_netlist.py, extract_bom.py |
| {board}.kicad_pcb | {sha256} | {utc timestamp} | summarize_pcb.py |
| {external bom, if any} | {sha256} | — | (user-supplied, not extracted) |

## Documents

| Document | Status | Notes |
|----------|--------|-------|
| netlist.yaml | extracted / enriched (class tags: {yes/no}) | {n} components, {n} nets, self-verify {pass/fail} |
| bom_flat.md | extracted | {n} lines; {n} missing MPN |
| pcb_summary.yaml | extracted / n/a (no board yet) | {n} footprints, {n} routed nets |
| reconstructed_intent.md | draft / user-confirmed | confirmed by user on {date} |
| datasheets/ | {n} parts cached, {n} fact cards ({n} pinout_verified) | |

## Ingest warnings (findings, not failures)

- {e.g. "3 floating pins: U2.7, J3.1, J3.2 — carried into findings/"}
- {e.g. "12 fitted lines have no MPN field"}

## Reconcile status

Last drift report: {date} — {PASS / n errors}. Accepted drift (user's call):
- {e.g. "R5 value drift sch=10k board=12k — user confirmed board is the bench-tuned truth, schematic to be updated"}

## Findings log

| Date | Doc | Topic | Outcome |
|------|-----|-------|---------|
| {YYYY-MM-DD} | findings/{file}.md | {topic} | {open / resolved / ruled out} |
