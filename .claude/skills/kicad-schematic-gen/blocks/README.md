# blocks/ — the proven-block registry (roadmap W1)

A block = **active silicon with a defined, beyond-trivial function, validated
on a built board.** Blocks are extracted from as-validated boards by
`scripts/extract_block.py` (never hand-invented), gated by
`scripts/check_block.py`, and recorded under `blocks_extracted` in
`validated_boards.yaml`.

```
blocks/
├── footprints/CircuitBlocks.pretty/   # THE one non-builtin footprint library
│                                      # blocks may reference (populated by
│                                      # extraction-time promotion, git-versioned)
├── 3dmodels/                          # best-effort model copies (never load-bearing)
└── {block_name}/
    ├── block.yaml          # contract: ports, rails, constraints, provenance, deps
    ├── sheet.kicad_sch     # hierarchical child sheet — symbols EMBEDDED verbatim,
    │                       # ports as hierarchical labels (KiCad sheet pins)
    ├── netlist.yaml        # Stage-5b fragment the sheet self-verifies against
    ├── bom.md              # flat BOM subset (identity fields verbatim)
    └── layout_intent.md    # W3 slot — placement/width/thermal intent
```

**Dependency policy:** symbols embedded (zero library deps); footprints from
KiCad built-ins or `CircuitBlocks` ONLY — never a personal/fragmented library.
`check_block.py` enforces this.

**Using a block from the engine (W1b — the primary path):** add a `blocks:`
section to the Stage 6 layout YAML and `generate_from_data.py` does the rest —
clones the block's `sheet.kicad_sch` next to your output with refs
re-annotated into a per-instance range (U2 → U102, second instance U202…),
places the `(sheet)` symbol with one pin per contract port, wires each pin to
the net you map it to, and self-verifies the flattened hierarchy (netlist
through the ports + merged BOM). It also writes `{out}_bom_flat.md` — the
whole-board flat BOM (board + blocks) for Stage 9.

```yaml
blocks:
  scale1:
    block: nau7802_dual_loadcell   # directory name under blocks/
    x: 180
    y: 80
    port_map:            # EVERY contract port, mapped to a board net
      SDA: I2C_SDA
      SCL: I2C_SCL
      DRDY: SCALE1_DRDY
    # refdes_base: 300   # optional; default (sorted-instance-index+1)*100
```

Gates: port_map must cover the contract ports exactly; every mapped net must
be declared in the netlist YAML (or be a power net); every rail in
`block.yaml` must be in the layout's `power_nets` (GND implicit); re-annotated
refs must not collide. **Constraints in `block.yaml` are judgment, not
scripted** — read them before mapping (e.g. NAU7802's fixed I2C address means
two instances need separate buses).

**Using a block by hand in KiCad:** Place a hierarchical sheet pointing at the
block's `sheet.kicad_sch` (copy it into your project), then "Import Sheet
Pins" — every port appears as a sheet pin to wire. Rails connect globally via
the power symbols inside. One-time per machine: register `CircuitBlocks` in
the global fp-lib-table pointing at `blocks/footprints/CircuitBlocks.pretty`
(ideally via a `${CIRCUIT_BUILDER}` path variable).
