#!/usr/bin/env python3
"""crosscheck_pcbway_plugin_bom.py — reproduce the PCBWay KiCad plugin's BOM
from a schematic's fields, as an independent cross-check of what the plugin
will actually upload.

WHY THIS EXISTS
---------------
The "PCBWay Plug-in for KiCad" builds its BOM by reading **footprint fields on
the board**, and only after "Update PCB from Schematic (F8)" pushes the symbol
fields onto the footprints. If that step is skipped (or a footprint field drifts
out of sync) the uploaded BOM comes out blank/wrong with no obvious error — this
actually happened on a prior board and had to be fixed with an emailed follow-up
BOM.

This tool reads the **schematic** (the authored source of truth) and reproduces
the plugin's exact grouping/selection rules. Because it reads an *independent*
source from the plugin, diffing the two catches propagation failures:

  * generate here from the .kicad_sch, then run the plugin (which writes
    ``PCBWay_bom.csv`` from the board), then ``--against PCBWay_bom.csv``;
  * if they match, the plugin saw every field — safe to upload;
  * if they differ, a field never reached the board (usually: F8 not run).

Plugin rules mirrored (from the plugin's utils.py / process.py):
  * MPN is the first present of the plugin's field-name aliases (see MPN_KEYS);
  * Package from pack/Package/case aliases (blank -> the Footprint column still
    identifies the part);
  * DNP from ``(dnp yes)`` or Value == "DNP";
  * components grouped by value + footprint + package + mpn (DNP kept separate);
  * power symbols and ``(in_bom no)`` parts excluded.

CLI:
    python crosscheck_pcbway_plugin_bom.py board.kicad_sch
    python crosscheck_pcbway_plugin_bom.py board.kicad_sch -o bom_check.csv
    python crosscheck_pcbway_plugin_bom.py board.kicad_sch --against PCBWay_bom.csv
    python crosscheck_pcbway_plugin_bom.py board.kicad_sch --json

NOTE: NOT yet wired into the Stage-9 workflow. See TODO.md ("Wire the PCBWay
plugin-BOM cross-check into Stage 9").
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys

# The plugin's MPN field-name aliases (utils.py get_mpn_keys), first match wins.
MPN_KEYS = ["mpn", "MPN", "Mpn", "PCBWay_MPN", "part number", "Part Number",
            "Part No.", "Mfr. Part No.", "Mfg Part", "Manufacturer_Part_Number"]
PACK_KEYS = ["pack", "PACK", "Pack", "package", "PACKAGE", "Package",
             "case", "CASE", "Case"]
# Extra fields the KiCad-8 plugin appends as columns if present.
EXTRA_FIELDS = ["Manufacturer", "LCSC"]

COLUMNS = ["Designator", "Quantity", "Value", "Footprint", "Package", "MPN",
           "Manufacturer", "LCSC", "DNP"]


def _balanced_end(s: str, i: int) -> int:
    """Index just past the balanced paren group starting at s[i] == '('."""
    depth = 0
    in_str = esc = False
    while i < len(s):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return len(s)


def _field(block: str, name: str):
    m = re.search(r'\(property\s+"' + re.escape(name) + r'"\s+"([^"]*)"', block)
    return m.group(1) if m else None


def _first_of(block: str, keys) -> str:
    for k in keys:
        v = _field(block, k)
        if v is not None:
            return v
    return ""


def parse_symbols(sch_text: str) -> list:
    """Return one dict per placed, in-BOM symbol instance in the schematic.

    Placed symbols are identified structurally — a placed instance carries a
    ``(lib_id ...)`` while ``lib_symbols`` definitions never do — rather than
    by indentation, so this works on both KiCad-saved files (tab-indented)
    and generator-emitted files (space-indented).
    """
    comps = []
    for m in re.finditer(r"(?m)^[ \t]*(\(symbol)\b", sch_text):
        s = m.start(1)
        block = sch_text[s:_balanced_end(sch_text, s)]
        if "(lib_id " not in block:
            continue  # a lib_symbols definition, not a placed instance
        ref = _field(block, "Reference")
        if not ref or ref.startswith("#"):
            continue
        if re.search(r"\(in_bom no\)", block):
            continue
        value = _field(block, "Value") or ""
        dnp_m = re.search(r"\(dnp (\w+)\)", block)
        dnp = bool(dnp_m and dnp_m.group(1) == "yes") or value.upper() == "DNP"
        comps.append({
            "ref": ref,
            "value": value,
            "footprint": _field(block, "Footprint") or "",
            "pack": _first_of(block, PACK_KEYS),
            "mpn": _first_of(block, MPN_KEYS),
            "Manufacturer": _field(block, "Manufacturer")
            or _field(block, "MF") or _field(block, "MANUFACTURER") or "",
            "LCSC": _field(block, "LCSC") or "",
            "dnp": dnp,
        })
    return comps


def _refsort(ref: str):
    m = re.match(r"([A-Za-z_]+)(\d+)", ref)
    return (m.group(1), int(m.group(2))) if m else (ref, 0)


def build_bom(comps: list) -> list:
    """Group components the way the plugin does; return BOM rows."""
    groups = {}
    for c in comps:
        key = f"{c['value']}_{c['footprint']}_{c['pack']}_{c['mpn']}"
        if c["dnp"]:
            key = c["ref"] + "_" + key  # plugin keeps DNP parts unmerged
        groups.setdefault(key, []).append(c)

    rows = []
    for items in groups.values():
        refs = sorted((i["ref"] for i in items), key=_refsort)
        f = items[0]
        rows.append({
            "Designator": ", ".join(refs),
            "Quantity": len(refs),
            "Value": f["value"],
            "Footprint": f["footprint"],
            "Package": f["pack"],
            "MPN": f["mpn"],
            "Manufacturer": f["Manufacturer"],
            "LCSC": f["LCSC"],
            "DNP": "Yes" if f["dnp"] else "",
        })
    rows.sort(key=lambda r: (r["DNP"] != "",
                             _refsort(r["Designator"].split(",")[0])))
    return rows


def anomalies(rows: list) -> dict:
    """Non-fatal issues worth surfacing before upload."""
    assembled = [r for r in rows if not r["DNP"]]
    blank_mpn = [r["Designator"] for r in assembled if not r["MPN"]]
    # same MPN split across >1 value string -> redundant rows (cosmetic)
    by_mpn = {}
    for r in assembled:
        by_mpn.setdefault(r["MPN"], set()).add(r["Value"])
    fragmented = {mpn: sorted(vs) for mpn, vs in by_mpn.items()
                  if mpn and len(vs) > 1}
    return {"blank_mpn": blank_mpn, "fragmented_groups": fragmented}


def to_csv(rows: list) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def diff_against(rows: list, plugin_csv_text: str) -> dict:
    """Compare (MPN -> total qty) between this BOM and a plugin PCBWay_bom.csv.

    Quantity-by-MPN is compared (row fragmentation differs harmlessly between
    the two, but per-MPN totals must agree). Returns lists of discrepancies.
    """
    def qty_by_mpn(rws, mpn_key, qty_key):
        out = {}
        for r in rws:
            mpn = (r.get(mpn_key) or "").strip()
            try:
                q = int(str(r.get(qty_key, "0")).strip() or 0)
            except ValueError:
                q = 0
            if mpn:
                out[mpn] = out.get(mpn, 0) + q
        return out

    ours = qty_by_mpn(rows, "MPN", "Quantity")
    plugin_rows = list(csv.DictReader(io.StringIO(plugin_csv_text)))
    # tolerate case/spacing variants of the plugin's column headers
    def find_col(fieldnames, *cands):
        low = {f.lower().strip(): f for f in fieldnames}
        for c in cands:
            if c in low:
                return low[c]
        return None
    fns = plugin_rows[0].keys() if plugin_rows else []
    mcol = find_col(fns, "mpn", "manufacturer part number", "part number", "mfg part #")
    qcol = find_col(fns, "quantity", "qty", "*qty")
    theirs = qty_by_mpn(plugin_rows, mcol, qcol) if (mcol and qcol) else {}

    only_ours = {m: q for m, q in ours.items() if m not in theirs}
    only_theirs = {m: q for m, q in theirs.items() if m not in ours}
    qty_mismatch = {m: (ours[m], theirs[m]) for m in ours
                    if m in theirs and ours[m] != theirs[m]}
    return {"only_in_schematic": only_ours, "only_in_plugin": only_theirs,
            "qty_mismatch": qty_mismatch,
            "plugin_columns_understood": bool(mcol and qcol)}


def format_text(rows: list, anom: dict) -> str:
    lines = []
    assembled = [r for r in rows if not r["DNP"]]
    total = sum(r["Quantity"] for r in assembled)
    lines.append(f"{len(rows)} BOM lines ({len(assembled)} assembled, "
                 f"{len(rows) - len(assembled)} DNP); {total} pieces to place")
    lines.append("")
    for r in rows:
        tag = "DNP" if r["DNP"] else "   "
        lines.append(f"  {r['Quantity']:>2}x {r['Value']:<18} "
                     f"{r['MPN']:<24} {r['LCSC']:<9} {tag}  [{r['Designator']}]")
    lines.append("")
    lines.append("--- CHECKS ---")
    lines.append("assembled lines with blank MPN: "
                 + (", ".join(anom["blank_mpn"]) or "none"))
    if anom["fragmented_groups"]:
        lines.append("same MPN split into multiple lines by Value string (cosmetic):")
        for mpn, vs in anom["fragmented_groups"].items():
            lines.append(f"    {mpn}: {vs}")
    else:
        lines.append("no fragmented groups")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Reproduce the PCBWay KiCad plugin's BOM from a schematic, "
                    "as a cross-check of what the plugin uploads.")
    ap.add_argument("schematic", help="path to the .kicad_sch")
    ap.add_argument("-o", "--output", help="write the BOM CSV to this path")
    ap.add_argument("--against", metavar="PLUGIN_CSV",
                    help="diff per-MPN quantities against the plugin's PCBWay_bom.csv")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    with open(args.schematic, encoding="utf-8", errors="replace") as f:
        rows = build_bom(parse_symbols(f.read()))
    anom = anomalies(rows)

    diff = None
    if args.against:
        with open(args.against, encoding="utf-8-sig", errors="replace") as f:
            diff = diff_against(rows, f.read())

    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
            f.write(to_csv(rows))

    if args.json:
        print(json.dumps({"rows": rows, "anomalies": anom, "diff": diff}, indent=2))
    else:
        print(format_text(rows, anom))
        if diff is not None:
            print("\n--- DIFF vs plugin BOM ---")
            if not diff["plugin_columns_understood"]:
                print("  (could not find MPN/Quantity columns in the plugin CSV)")
            print("  MPNs only in schematic:", diff["only_in_schematic"] or "none")
            print("  MPNs only in plugin:   ", diff["only_in_plugin"] or "none")
            print("  qty mismatches (sch, plugin):", diff["qty_mismatch"] or "none")

    # exit non-zero if something needs attention
    bad = anom["blank_mpn"] or (diff and (diff["only_in_schematic"]
                                          or diff["only_in_plugin"]
                                          or diff["qty_mismatch"]))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
