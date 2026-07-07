#!/usr/bin/env python3
"""extract_bom.py — harvest a flat BOM (bom_flat.md) from an existing
.kicad_sch's symbol fields.

The BOM half of the ingest phase: reads the same per-symbol fields the PCBWay
KiCad plugin reads (via crosscheck_pcbway_plugin_bom's field readers, so the
two can never drift on field-name aliases) and emits the house flat-BOM
markdown — one row per reference, the exact format `cross_check_bom.py`,
`check_pcbway.py`, and the Stage-9 tooling already consume.

What lands where:
  * Part Number  ← first present of the plugin MPN aliases (MPN, mpn, ...)
  * Manufacturer ← Manufacturer / MF / MANUFACTURER field
  * Package      ← pack/Package/case aliases
  * Type         ← "DNP" for (dnp yes) or Value=="DNP" parts (PCBway convention)
  * Notes        ← LCSC code if a field carries one
Blank cells mean the schematic simply doesn't carry that field — a finding
(the Stage-9 MPN gate will block on it), not something this script invents.

Extraction-only: no lookup, no normalization beyond whitespace. Fix data in
KiCad (or during a re-entry into the generator pipeline), never here.

CLI:
    python extract_bom.py board.kicad_sch -o claude_context/bom_flat.md
    python extract_bom.py board.kicad_sch --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

import _paths  # noqa: F401
from crosscheck_pcbway_plugin_bom import parse_symbols, _refsort


COLUMNS = ["Ref", "Value", "Part Number", "Manufacturer",
           "Package", "Footprint", "Type", "Notes"]


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def extract(sch_path: str):
    """Return (rows, summary). One row dict per in-BOM symbol instance."""
    with open(sch_path, "r", encoding="utf-8") as f:
        text = f.read()

    comps = sorted(parse_symbols(text), key=lambda c: _refsort(c["ref"]))

    rows = []
    for c in comps:
        notes = f"LCSC: {c['LCSC']}" if c["LCSC"] else ""
        rows.append({
            "Ref": c["ref"],
            "Value": c["value"],
            "Part Number": c["mpn"],
            "Manufacturer": c["Manufacturer"],
            "Package": c["pack"],
            "Footprint": c["footprint"],
            "Type": "DNP" if c["dnp"] else "",
            "Notes": notes,
        })

    fitted = [r for r in rows if r["Type"] != "DNP"]
    summary = {
        "lines": len(rows),
        "dnp_lines": len(rows) - len(fitted),
        "missing_mpn": [r["Ref"] for r in fitted if not r["Part Number"]],
        "missing_manufacturer": [r["Ref"] for r in fitted if not r["Manufacturer"]],
        "missing_footprint": [r["Ref"] for r in fitted if not r["Footprint"]],
        "distributor_code_as_mpn": [
            r["Ref"] for r in fitted
            if re.fullmatch(r"C\d{3,}", r["Part Number"] or "")],
    }
    return rows, summary


def emit_markdown(rows, sch_path: str) -> str:
    lines = [
        "# Flat BOM (extracted from schematic)",
        "",
        f"Source: `{os.path.basename(sch_path)}`  ",
        f"source_sha256: `{_sha256(sch_path)}`  ",
        f"extracted_utc: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
        f"by kicad-board-context/scripts/extract_bom.py",
        "",
        "Field values are copied verbatim from the schematic's symbol fields —",
        "blank cells are findings about the schematic, not omissions of this tool.",
        "",
        "| " + " | ".join(COLUMNS) + " |",
        "|" + "|".join("-" * (len(c) + 2) for c in COLUMNS) + "|",
    ]
    for r in rows:
        lines.append("| " + " | ".join(r[c] for c in COLUMNS) + " |")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description="Harvest a flat BOM markdown from a .kicad_sch's fields")
    ap.add_argument("schematic", help="path to .kicad_sch")
    ap.add_argument("-o", "--output", help="output markdown path (default stdout)")
    ap.add_argument("--json", action="store_true",
                    help="print extraction summary as JSON")
    args = ap.parse_args()

    rows, summary = extract(args.schematic)
    md = emit_markdown(rows, args.schematic)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(md)
    else:
        print(md)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"extracted: {summary['lines']} lines "
              f"({summary['dnp_lines']} DNP)", file=sys.stderr)
        for key, label in [
                ("missing_mpn", "fitted line(s) with NO Part Number/MPN field"),
                ("missing_manufacturer", "fitted line(s) with no Manufacturer"),
                ("missing_footprint", "fitted line(s) with no Footprint"),
                ("distributor_code_as_mpn",
                 "line(s) with an LCSC distributor code where the MPN belongs")]:
            if summary[key]:
                print(f"WARNING: {len(summary[key])} {label}: "
                      f"{', '.join(summary[key])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
