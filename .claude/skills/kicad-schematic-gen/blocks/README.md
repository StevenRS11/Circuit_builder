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

**Using a block today (pre-W1b, by hand in KiCad):** Place a hierarchical
sheet on your board pointing at the block's `sheet.kicad_sch` (copy it into
your project), then "Import Sheet Pins" — every port appears as a sheet pin to
wire. Rails connect globally via the power symbols inside. One-time per
machine: register `CircuitBlocks` in the global fp-lib-table pointing at
`blocks/footprints/CircuitBlocks.pretty` (ideally via a `${CIRCUIT_BUILDER}`
path variable). W1b will teach `generate_from_data.py` to compose sheets
automatically.
