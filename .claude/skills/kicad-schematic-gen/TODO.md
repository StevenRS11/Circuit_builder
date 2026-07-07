# kicad-schematic-gen — TODO / backlog

Deferred features and improvements, newest context first. Each entry: what, why,
and any groundwork already in place.

> **See `ROADMAP.md` for the agreed strategic plan (2026-07-06):** block library
> via hierarchical sheets (W1), validation loop / promotion ritual (W2), layout
> verify (W3, design later), plus the loader-fallback (A1) and pinmap-handoff
> (A2) adjacencies. Items below that overlap with it (loader fallback, module
> fragments, verify_layout) are sequenced there.

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

## ~~Loader brownfield-fallback~~ — DONE (2026-07-06, roadmap item A1)
`load_kicad_sch` now resolves cache-missing lib_ids from installed/registered
libraries (`resolve_from_libraries=True` default; `--project-dir` / `--sym-lib`
/ `--no-lib-fallback` CLI flags), emits a `stale_lib_cache` *warning* per
resolved symbol (new 12th validator check) and tracks `unresolved_lib_ids`
for symbols that resolve nowhere (still a `missing_lib_symbol` error).
`extract_netlist.py` (board-context) surfaces both in its summary. Fixed along
the way: `check_kicad_library._split_symbols` only recognized tab-indented
top-level symbols — space-indented `.kicad_sym` files (vendor exports,
hand-written libs) returned zero blocks; now depth-based. Tests:
`tests/test_loader_fallback.py` incl. the 1-stale-symbol→false-cascade
regression.

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
