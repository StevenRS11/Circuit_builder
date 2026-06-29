# kicad-schematic-gen — TODO / backlog

Deferred features and improvements, newest context first. Each entry: what, why,
and any groundwork already in place.

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

## Decompile an existing `.kicad_sch` → skill data model (thin, reusable atom)
The reusable core of "brownfield" mode: turn an existing schematic into the
skill's `05b_netlist.yaml` using the existing `extract_netlist()`. This is mostly
mechanical (geometry → nets) and is the prerequisite for both the fragment feature
and any review of an existing board. The *judgment* layers (spec, BOM intent, net
class tags, current budgets) are NOT worth reverse-generating as tooling — most
builds go through the full greenfield pipeline; do those by hand for one-offs.
