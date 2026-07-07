#!/usr/bin/env python3
"""bake_bom_fields.py — inject PCBWay identity fields onto an EXISTING board.

The Stage-6 engine (`generate_from_data`) bakes each part's PCBWay identity fields
(`MPN`, `Manufacturer`, `Package`, `Description`, and the optional `LCSC Part #`) onto
its symbol at generation time, so the PCBWay KiCad plugin auto-populates the upload BOM
after an F8 sync. Boards generated *before* that feature existed — or hand-edited after
generation (and therefore un-regenerable without losing manual edits / PCB routing) —
carry no such fields, and the plugin then emits a blank/wrong BOM.

This tool bakes the same field set onto an already-authored `.kicad_sch` (symbols) and/or
`.kicad_pcb` (footprints), joined on reference designator. It is:

  * **consistent with the engine** — the field set comes from the very same
    ``generate_from_data._pcbway_symbol_props`` + ``check_pcbway.load_bom_for_pcbway``
    (which also means the MPN guards apply: a distributor code or a description in the
    Part Number column is never baked as an MPN — see ``_real_mpn``);
  * **reconciling, not just additive** — a *managed* field already present is updated in
    place; a missing one inserted; and a managed field that is **no longer applicable is
    removed** (the stale-`LCSC Part #` class). Non-canonical MPN-family alias fields
    (``Mfg Part``, ``Part Number``, ...) and the forbidden ``Mfg Part #`` are always
    removed, so the baked part carries **exactly one** plugin-readable MPN key;
  * **surgical** — it touches only property blocks, never geometry/UUIDs of pads/pins,
    so it is safe on a routed board;
  * **lock-aware** — it refuses to write a file with a KiCad ``~<name>.lck`` lock beside
    it (the file is open in KiCad and would be clobbered on save); ``--force`` overrides.

Managed field sets (only these are ever inserted/updated/removed):
  * schematic symbols: MPN, Manufacturer, Package, Description, LCSC Part #
  * board footprints:  MPN, Manufacturer, Package, LCSC Part # — **never Description**,
    because KiCad footprints natively carry their own ``Description`` property (the
    library description); managing it here would clobber that.

**Preferred propagation path: bake the schematic, then run "Update PCB from Schematic
(F8)" in KiCad** so the fields reach the board the plugin reads. Baking the .kicad_pcb
directly works (proven on a copy) and is belt-and-suspenders for the same result — but
F8 remains the safe default. Verify propagation with ``crosscheck_pcbway_plugin_bom.py``
(or the kicad-board-context skill's ``reconcile.py``).

Note the split of responsibility: this tool guarantees the fields are *well-formed and
consistent with the BOM*. Whether the MPN resolves to the *right physical part* (the
C190158=varistor / C2827654=clone / C17513=wrong-mfr class) is the Stage-9 answer-blind
live verification's job (``bom_verify.py``) — no offline tool can catch that.

CLI:
    python bake_bom_fields.py <bom.md> <file.kicad_sch> [<file.kicad_pcb> ...]
    python bake_bom_fields.py <bom.md> <file.kicad_sch> --json
    python bake_bom_fields.py <bom.md> <file.kicad_sch> --force   # ignore ~*.lck locks
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_pcbway import (
    load_bom_for_pcbway,
    PLUGIN_MPN_KEYS,
    CANONICAL_MPN_FIELD,
    CANONICAL_PACKAGE_FIELD,
    FORBIDDEN_MPN_FIELD,
)
from generate_from_data import _pcbway_symbol_props

# Every field this tool may insert/update/REMOVE. Nothing outside these sets is
# ever touched (Reference/Value/Footprint/Datasheet and user fields are safe).
MANAGED_FIELDS_SCH = [CANONICAL_MPN_FIELD, "Manufacturer", CANONICAL_PACKAGE_FIELD,
                      "Description", "LCSC Part #"]
MANAGED_FIELDS_PCB = [CANONICAL_MPN_FIELD, "Manufacturer", CANONICAL_PACKAGE_FIELD,
                      "LCSC Part #"]  # native footprint Description is not ours

# MPN-family aliases that must NOT coexist with the canonical key (the plugin
# takes the first alias hit, so a stale shadow can override or blank the MPN).
_SHADOW_MPN_KEYS_LOWER = ({k.strip().lower() for k in PLUGIN_MPN_KEYS}
                          | {FORBIDDEN_MPN_FIELD.lower()}) - {CANONICAL_MPN_FIELD.lower()}


class LockedFileError(RuntimeError):
    """Target file has a KiCad lock (~<name>.lck) beside it — open in KiCad."""


def _lock_path(path):
    p = Path(path)
    return p.parent / f"~{p.name}.lck"


def _esc(value):
    """Escape a value for a KiCad quoted string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _uuid_for(ref, name):
    """Deterministic UUID (so re-baking is stable) from ref + field name."""
    h = hashlib.md5(f"battery-bake|{ref}|{name}".encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _match_block_end(text, start):
    """Return index just past the paren-balanced block opening at `start`."""
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _iter_blocks(text, head):
    """Yield (start, end) for every paren-balanced block beginning with `head`."""
    i = 0
    while True:
        j = text.find(head, i)
        if j < 0:
            return
        end = _match_block_end(text, j)
        yield (j, end)
        i = end


def _ref_of(block, root_uuid=""):
    """Active reference for a block — instances-resolved for schematic symbols
    (the property cache goes stale after cross-project pastes; baking by the
    cached ref would write fields onto the WRONG component)."""
    if root_uuid:
        from crosscheck_pcbway_plugin_bom import resolve_active_reference
        ref = resolve_active_reference(block, root_uuid)
        if ref:
            return ref
    m = re.search(r'\(property "Reference" "([^"]+)"', block)
    return m.group(1) if m else None


def _sch_prop(name, value, px, py):
    e = _esc(value)
    return (
        f'\t\t(property "{name}" "{e}"\n'
        f"\t\t\t(at {px} {py} 0)\n"
        f"\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n"
        f"\t\t\t\t(hide yes)\n\t\t\t)\n"
        f"\t\t)\n"
    )


def _fp_prop(name, value, ref):
    e = _esc(value)
    return (
        f'\t\t(property "{name}" "{e}"\n'
        f"\t\t\t(at 0 0 0)\n"
        f'\t\t\t(layer "F.Fab")\n'
        f"\t\t\t(hide yes)\n"
        f'\t\t\t(uuid "{_uuid_for(ref, name)}")\n'
        f"\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t\t(thickness 0.15)\n\t\t\t\t)\n\t\t\t)\n"
        f"\t\t)\n"
    )


def _field_names(block):
    """All property names present in a symbol/footprint block."""
    return [m.group(1) for m in re.finditer(r'\(property "((?:[^"\\]|\\.)+)" ', block)]


def _remove_field(block, name):
    """Return (new_block, removed). Deletes the whole property block for `name`,
    including its leading indentation and trailing newline."""
    m = re.search(r'\(property "' + re.escape(name) + r'" ', block)
    if not m:
        return block, False
    end = _match_block_end(block, m.start())
    # widen to the start of the line…
    start = block.rfind("\n", 0, m.start())
    start = start + 1 if start >= 0 else 0
    # …and past the trailing newline
    nl = block.find("\n", end)
    end = nl + 1 if nl >= 0 else end
    return block[:start] + block[end:], True


def _reconcile_removals(block, desired, managed):
    """Remove managed fields no longer applicable + every shadow MPN-family key.

    Returns (new_block, removed_names). Only names in `managed` or the shadow/
    forbidden MPN alias set are candidates — user fields are never touched.
    """
    removed = []
    for name in _field_names(block):
        stale_managed = (name in managed and name not in desired)
        shadow_mpn = (name.strip().lower() in _SHADOW_MPN_KEYS_LOWER)
        if not (stale_managed or shadow_mpn):
            continue
        block, ch = _remove_field(block, name)
        if ch:
            removed.append(name)
    return block, removed


def _ensure_field(block, name, value, make_prop, anchor_name):
    """Return (new_block, changed). Update the field's value if present, else insert
    a fresh property block right after the `anchor_name` property."""
    e = _esc(value)
    pat = re.compile(r'(\(property "' + re.escape(name) + r'" ")(?:[^"\\]|\\.)*(")')
    if pat.search(block):
        new = pat.sub(lambda m: m.group(1) + e + m.group(2), block, count=1)
        return new, (new != block)
    # insert after the anchor property block
    am = re.search(r'\(property "' + re.escape(anchor_name) + r'" ', block)
    if not am:
        return block, False
    ins = _match_block_end(block, am.start())
    # advance past the trailing newline of the anchor block
    nl = block.find("\n", ins)
    ins = nl + 1 if nl >= 0 else ins
    return block[:ins] + make_prop() + block[ins:], True


def bake_file(path, fields_by_ref, kind, force=False):
    """Bake fields into a .kicad_sch (kind='sch') or .kicad_pcb (kind='pcb').

    Reconciles each known-ref block against its desired managed-field set:
    update in place / insert missing / REMOVE stale managed fields and shadow
    MPN aliases. Refs absent from the BOM are left untouched (that gap is
    reconcile.py's finding, not this tool's to guess at).

    Raises LockedFileError if a KiCad ``~<name>.lck`` exists (unless force).
    """
    if not force and _lock_path(path).exists():
        raise LockedFileError(
            f"{path} is locked ({_lock_path(path).name}) — close it in KiCad "
            f"first (KiCad would clobber this bake on save), or pass --force")

    text = Path(path).read_text(encoding="utf-8")
    head = "(symbol" if kind == "sch" else "(footprint"
    managed = MANAGED_FIELDS_SCH if kind == "sch" else MANAGED_FIELDS_PCB
    root_uuid = ""
    if kind == "sch":
        rm = re.search(r'\(uuid "([^"]+)"\)', text)
        root_uuid = rm.group(1) if rm else ""
    # Collect target blocks (instance symbols / footprints with a Reference we know).
    targets = []
    for start, end in _iter_blocks(text, head):
        block = text[start:end]
        if kind == "sch" and "(instances" not in block:
            continue  # skip lib_symbols definitions
        ref = _ref_of(block, root_uuid if kind == "sch" else "")
        if ref and ref in fields_by_ref:
            targets.append((start, end, ref))
    changed_refs, removed_fields = {}, {}
    # Process last→first so earlier indices stay valid.
    for start, end, ref in sorted(targets, reverse=True):
        block = text[start:end]
        desired = {k: v for k, v in fields_by_ref[ref].items() if k in managed}
        # anchor / position from the Footprint property
        fpm = re.search(r'\(property "Footprint" "[^"]*"\s*\(at ([\-0-9.]+) ([\-0-9.]+)', block)
        px, py = (fpm.group(1), fpm.group(2)) if fpm else ("0", "0")
        newblock, removed = _reconcile_removals(block, desired, managed)
        if removed:
            removed_fields[ref] = removed
        for name, value in desired.items():
            if kind == "sch":
                mk = lambda n=name, v=value: _sch_prop(n, v, px, py)
                anchor = "Footprint"
            else:
                mk = lambda n=name, v=value: _fp_prop(n, v, ref)
                anchor = "Reference"
            newblock, ch = _ensure_field(newblock, name, value, mk, anchor)
            if ch:
                changed_refs.setdefault(ref, 0)
                changed_refs[ref] += 1
        if newblock != block:
            text = text[:start] + newblock + text[end:]
    Path(path).write_text(text, encoding="utf-8", newline="\n")
    return {"file": str(path),
            "refs_touched": len(set(changed_refs) | set(removed_fields)),
            "field_writes": sum(changed_refs.values()),
            "field_removals": {r: names for r, names in sorted(removed_fields.items())}}


def main():
    ap = argparse.ArgumentParser(description="Bake PCBWay identity fields onto an existing board")
    ap.add_argument("bom", help="Stage-3 BOM markdown (full column set)")
    ap.add_argument("targets", nargs="+", help=".kicad_sch and/or .kicad_pcb files")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="write even if a KiCad ~*.lck lock file is present")
    args = ap.parse_args()

    recs = load_bom_for_pcbway(Path(args.bom).read_text(encoding="utf-8"))
    fields_by_ref = {}
    for r in recs:
        extra = _pcbway_symbol_props(r)
        if extra:
            fields_by_ref[r.reference] = extra

    results, missing_mpn, locked = [], [], []
    for r in recs:
        if CANONICAL_MPN_FIELD not in fields_by_ref.get(r.reference, {}):
            missing_mpn.append(r.reference)
    for t in args.targets:
        kind = "sch" if str(t).endswith(".kicad_sch") else "pcb"
        try:
            results.append(bake_file(t, fields_by_ref, kind, force=args.force))
        except LockedFileError as e:
            locked.append({"file": str(t), "error": str(e)})

    out = {"parts": len(recs), "results": results,
           "missing_mpn": missing_mpn, "locked": locked}
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"Baked {len(recs)} BOM lines")
        for res in results:
            print(f"  {res['file']}: {res['refs_touched']} refs, "
                  f"{res['field_writes']} field writes")
            for ref, names in res["field_removals"].items():
                print(f"    removed stale/shadow field(s) on {ref}: {', '.join(names)}")
        for lk in locked:
            print(f"  LOCKED (not written): {lk['error']}")
        if missing_mpn:
            print(f"  WARNING: {len(missing_mpn)} lines have no real MPN (not baked): {missing_mpn}")
    if locked:
        return 2
    return 1 if missing_mpn else 0


if __name__ == "__main__":
    sys.exit(main())
