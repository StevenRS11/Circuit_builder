---
name: kicad-import-lib
description: Import a downloaded KiCad part bundle (zip) — symbol, footprint, and 3D model — into the local KiCad install. Use this whenever the user has a vendor zip from SnapEDA, Ultra Librarian, Component Search Engine/Samacsys, or a manufacturer, and wants the part available in KiCad. Triggers on "import this symbol/footprint/3D model into KiCad", "add this part to my KiCad libraries", "install this SnapEDA zip", or similar.
---

# KiCad Library Importer

Install a downloaded part bundle (a `.zip` containing some mix of `.kicad_sym`
symbol, `.kicad_mod` footprint, and `.step`/`.stp`/`.wrl` 3D model) into the
user's local KiCad install, and register it so it shows up in KiCad.

This is a single deterministic script — no design judgment. The work is file
shuffling plus idempotent edits to KiCad's global library tables.

## How it works

`scripts/import_lib.py` does all of it:

1. Extracts the zip and finds the symbol(s), footprint(s), and 3D model(s)
   anywhere in the tree (handles SnapEDA / Ultra Librarian `KiCAD` subfolders /
   Samacsys layouts — it globs by extension, so layout doesn't matter).
2. **Merges** symbols into one consolidated library (default: the user's
   existing `Custom` lib at `D:\Documents\KiCad\8.0\symbols\Library.kicad_sym`),
   skipping any symbol name already present.
3. Copies footprints into `Custom.pretty` and 3D models into `Custom.3dshapes`,
   then rewrites the copied footprints' `(model ...)` paths to point at the
   installed model files (matched by filename stem, preferring `.step`).
4. Registers the symbol/footprint libraries in the global `sym-lib-table` /
   `fp-lib-table` (idempotent — never duplicates a nickname; the `Custom` symbol
   lib is already registered, so usually only the footprint table changes).

It writes `.bak` copies before modifying any existing file (symbol lib + tables).

## Running

```bash
# Standard import (everything goes into the consolidated "Custom" libraries)
python .claude/skills/kicad-import-lib/scripts/import_lib.py <bundle.zip>

# Preview without writing anything
python .claude/skills/kicad-import-lib/scripts/import_lib.py <bundle.zip> --dry-run

# Machine-readable output
python .claude/skills/kicad-import-lib/scripts/import_lib.py <bundle.zip> --json

# Put this part in its own library instead of the shared "Custom" one
python .claude/skills/kicad-import-lib/scripts/import_lib.py <bundle.zip> --name INA219
```

Useful overrides (all optional — defaults match this install):
`--symbol-lib <file.kicad_sym>`, `--footprint-dir <dir.pretty>`,
`--model-dir <dir.3dshapes>`, `--config-dir <KiCad config dir>`.

## Workflow

1. Get the zip path from the user (or find the newest `*.zip` in their Downloads
   if they just say "the one I downloaded").
2. Run with `--dry-run` first and show the user what will be added/skipped.
3. Run for real. Report what was added.
4. Remind the user to **restart KiCad** (or re-scan libraries) so the new parts
   appear. The new parts live under the `Custom:` library nickname (or the
   `--name` nickname).

## Notes / limits

- **Legacy KiCad-5 `.lib` symbols** can't be merged directly. If a bundle ships
  only `.lib` (no `.kicad_sym`), the script warns and tells the user to convert
  via KiCad's Symbol Editor (Save As `.kicad_sym`) or `kicad-cli sym upgrade`,
  then re-run.
- Defaults (target library paths) live in the `DEFAULTS` dict at the top of
  `import_lib.py`. They match this machine's existing custom-library convention.
- The script is stdlib-only and needs no KiCad install to run.
