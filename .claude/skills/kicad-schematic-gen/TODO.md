# kicad-schematic-gen — TODO / backlog

Deferred features and improvements, newest context first. Each entry: what, why,
and any groundwork already in place.

## Wire the PCBWay plugin-BOM cross-check into Stage 9 (NOT yet wired)
`scripts/crosscheck_pcbway_plugin_bom.py` exists and is tested
(`tests/test_crosscheck_pcbway_plugin_bom.py`) but is **not referenced by SKILL.md
or any workflow step yet** — deliberately left standalone.

What it does: reproduces the **PCBWay KiCad plugin's** BOM from the schematic's
fields (mirrors the plugin's MPN field aliases, DNP handling, and
value+footprint+package+mpn grouping), and can `--against PCBWay_bom.csv` to diff
per-MPN quantities. It's an independent-source cross-check: the plugin reads
*board footprint* fields (post "Update PCB from Schematic"), this reads the
*schematic* — so a diff catches fields that never propagated to the board (the
failure mode that produced a wrong DualScale BOM). See [[pcbway-kicad-plugin-bom]]
in the user's memory for the plugin's field convention.

To wire it (when ready):
- Add a Stage-9 step: after the user runs the PCBWay plugin, run
  `crosscheck_pcbway_plugin_bom.py {project}.kicad_sch --against PCBWay_bom.csv`;
  a clean diff (no qty mismatch / no MPN-only-in-either) gates the upload.
- Decide how it relates to the existing `generate_pcbway_bom.py` (which sources
  from `03_bom.md`): for KiCad-plugin/brownfield boards this schematic-sourced
  path replaces the markdown BOM; reconcile the two so there's one Stage-9 story.
- Consider also flagging fragmented groups (same MPN, multiple Value strings) as a
  warning at the gate, and adding an optional Value-string normalization helper.

## Module fragment / hierarchical-sheet output (requested)
Emit a designed module as its **own `.kicad_sch` file** the user can place/paste,
rather than only generating a whole-board schematic. This is the unit a KiCad
**hierarchical sheet** uses, so it sets up multi-sheet designs later.
- Use case: add a module (e.g. a NAU7802 front-end) to an existing board.
- Composes with the embed-real-symbol work already done (`from_library: true`):
  a fragment that places `Custom:NAU7802` embeds the real symbol verbatim.
- Open questions: refdes allocation that won't collide with the parent board;
  tying new nets into existing rails by name; sheet pins vs flat global labels.

## Loader brownfield-fallback: resolve missing cached symbols from libraries
**[P1 — now the main trust gap for the `kicad-board-context` skill: its ingest
of hand-built/pasted schematics inherits this over-reporting. Recognition
guidance lives in that skill's `references/ingest.md` until this lands.]**
`load_kicad_sch` currently resolves placed symbols **only** from the file's
embedded `(lib_symbols)` cache, by exact `lib_id` match. If an instance's `lib_id`
isn't in that cache (stale cache from a cross-project paste — e.g. cached as
`Pololu_Breakout_DRV8825_1` while the instance is `Custom:Pololu_Breakout_DRV8825`),
the loader **drops the component**, losing its pins. Everything wired to those pins
then cascades to false `dangling_wire` / `disconnected_label` / `floating_pin`
errors (observed: 1 stale symbol → 14 false errors on pushbuttonDef). KiCad
tolerates this by falling back to the library, so our tool over-reports vs ERC.
- Fix: when an instance `lib_id` is absent from the cache, fall back to
  `check_kicad_library.load_symbol_block(lib_id, project_dir=…)` and place its
  pins anyway; emit a **warning** ("symbol resolved from library; file cache is
  stale"), not a hard error. The resolver + embed path needed for this already
  exist (`build_library_set`, `load_symbol_block`, `add_lib_symbol_from_block`).
- Makes the verify-existing-board path trustworthy (matches KiCad's behaviour).

## ~~Decompile an existing `.kicad_sch` → skill data model~~ — DONE (2026-07-06)
Implemented as the sibling **`kicad-board-context`** skill's ingest phase:
`extract_netlist.py` (schematic → 05b YAML, round-trip self-verified),
`extract_bom.py` (fields → flat BOM), `summarize_pcb.py`, `reconcile.py`
(cross-artifact drift, incl. the F8 field-propagation check). The judgment
layers (net class tags, intent doc) stay Claude-authored in that skill's
ENRICH phase, per the original scoping. Note: `parse_symbols` in
`crosscheck_pcbway_plugin_bom.py` was fixed along the way to identify placed
symbols structurally (`(lib_id ...)`) instead of by tab indentation — it
previously returned 0 rows on generator-emitted (space-indented) schematics.
