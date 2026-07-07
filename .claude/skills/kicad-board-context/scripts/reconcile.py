#!/usr/bin/env python3
"""reconcile.py — cross-artifact drift report for an existing KiCad project
(the RECONCILE phase of the board context pack).

A brownfield board has up to three descriptions of itself — schematic, board,
BOM — and a large share of real-world defects are *disagreements between
them*, not errors within any one (a symbol field that never reached the board
because F8 wasn't run, a BOM edited by hand after the schematic changed, a
footprint swapped on the board only). This script makes those disagreements
first-class findings before any reasoning starts.

Checks (each pair only when both artifacts are given):

  SCH ↔ BOM   — reuses cross_check_bom verbatim: every BOM line exists in the
                schematic and vice versa; values and footprints match.
  SCH ↔ PCB   — reference sets match (board-only mechanical refs are info);
                Value / footprint-lib_id per ref match; and every MPN field on
                a schematic symbol exists with the same value on the board
                footprint (the "plugin reads the BOARD, F8 was never run"
                failure that produced a wrong uploaded BOM).

Deterministic, verify-only, offline. It never decides which side is right —
that is a judgment call made with the user (usually: the schematic is the
source of truth, the board is stale).

CLI:
    python reconcile.py board.kicad_sch --bom bom.md --pcb board.kicad_pcb
    python reconcile.py board.kicad_sch --bom bom.md --json
Exit 0 = no drift errors, 1 = drift found (warnings don't fail).
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import _paths  # noqa: F401
from crosscheck_pcbway_plugin_bom import parse_symbols, MPN_KEYS
from check_pcbway import _is_mechanical_ref


# ─── data model ──────────────────────────────────────────────────────

class DriftIssue:
    def __init__(self, severity, pair, check, message, reference=""):
        self.severity = severity      # "error" | "warning" | "info"
        self.pair = pair              # "sch-bom" | "sch-pcb"
        self.check = check
        self.message = message
        self.reference = reference

    def as_dict(self):
        return {"severity": self.severity, "pair": self.pair,
                "check": self.check, "message": self.message,
                "reference": self.reference}


# ─── SCH ↔ BOM (delegates to the shared cross-checker) ──────────────

def reconcile_sch_bom(sch_path: str, bom_md_text: str):
    from cross_check_bom import cross_check, load_bom_from_markdown
    from validate_kicad_sch import load_kicad_sch
    result = cross_check(load_bom_from_markdown(bom_md_text),
                         load_kicad_sch(sch_path))
    return [DriftIssue(i.severity, "sch-bom", i.check_name, i.message,
                       i.reference) for i in result.issues]


# ─── SCH ↔ PCB ───────────────────────────────────────────────────────

def _parse_pcb_footprints(pcb_text: str):
    """ref -> {lib_id, value, props{}} for every footprint on the board."""
    fps = {}
    for b in re.split(r"\n\s*\(footprint ", pcb_text)[1:]:
        lid = re.match(r'"([^"]+)"', b)
        ref = re.search(r'\(property "Reference" "([^"]+)"', b)
        if not ref:
            continue
        props = dict(re.findall(r'\(property "([^"]+)" "([^"]*)"', b))
        fps[ref.group(1)] = {
            "lib_id": lid.group(1) if lid else "",
            "value": props.get("Value", ""),
            "props": props,
        }
    return fps


def _sch_symbols_by_ref(sch_text: str):
    """Dedup parse_symbols output by reference (multi-unit symbols repeat)."""
    by_ref = {}
    for c in parse_symbols(sch_text):
        by_ref.setdefault(c["ref"], c)
    return by_ref


def reconcile_sch_pcb(sch_text: str, pcb_text: str):
    issues = []
    sch_by_ref = _sch_symbols_by_ref(sch_text)
    pcb_by_ref = _parse_pcb_footprints(pcb_text)

    # Reference-set drift.
    for ref in sorted(set(sch_by_ref) - set(pcb_by_ref)):
        issues.append(DriftIssue(
            "error", "sch-pcb", "missing_on_board",
            f"{ref} is in the schematic but has no footprint on the board "
            f"(schematic changed after last 'Update PCB from Schematic'?)", ref))
    for ref in sorted(set(pcb_by_ref) - set(sch_by_ref)):
        sev = "info" if _is_mechanical_ref(ref) else "error"
        issues.append(DriftIssue(
            sev, "sch-pcb", "board_only_component",
            f"{ref} is on the board but not in the schematic"
            + (" (mechanical — usually fine)" if sev == "info" else
               " (deleted from schematic but not the board?)"), ref))

    # Per-ref field drift.
    for ref in sorted(set(sch_by_ref) & set(pcb_by_ref)):
        s, p = sch_by_ref[ref], pcb_by_ref[ref]

        if s["value"] != p["value"]:
            issues.append(DriftIssue(
                "error", "sch-pcb", "value_drift",
                f"{ref} value differs: schematic '{s['value']}' vs "
                f"board '{p['value']}'", ref))

        if s["footprint"] and p["lib_id"] and s["footprint"] != p["lib_id"]:
            issues.append(DriftIssue(
                "error", "sch-pcb", "footprint_drift",
                f"{ref} footprint differs: schematic '{s['footprint']}' vs "
                f"board '{p['lib_id']}' (swapped on the board only?)", ref))

        # MPN propagation — the PCBWay plugin reads BOARD fields; a schematic
        # MPN that never reached the footprint uploads a blank/wrong BOM line.
        if s["mpn"]:
            board_mpn = next((p["props"][k] for k in MPN_KEYS
                              if p["props"].get(k)), "")
            if not board_mpn:
                issues.append(DriftIssue(
                    "error", "sch-pcb", "field_not_propagated",
                    f"{ref} has MPN '{s['mpn']}' in the schematic but no MPN "
                    f"field on its board footprint — run F8 'Update PCB from "
                    f"Schematic' and re-check", ref))
            elif board_mpn != s["mpn"]:
                issues.append(DriftIssue(
                    "error", "sch-pcb", "field_drift",
                    f"{ref} MPN differs: schematic '{s['mpn']}' vs board "
                    f"'{board_mpn}' (stale board fields — run F8)", ref))
    return issues


# ─── report ──────────────────────────────────────────────────────────

def format_text(issues, sch_path, bom_path=None, pcb_path=None):
    lines = [f"Drift report — {sch_path}"]
    if bom_path:
        lines.append(f"  vs BOM: {bom_path}")
    if pcb_path:
        lines.append(f"  vs PCB: {pcb_path}")
    lines.append("")
    if not issues:
        lines.append("PASS — no drift between artifacts.")
        return "\n".join(lines)
    for pair in ("sch-bom", "sch-pcb"):
        chunk = [i for i in issues if i.pair == pair]
        if not chunk:
            continue
        lines.append(f"[{pair.upper()}]")
        for i in chunk:
            lines.append(f"  {i.severity.upper():7s} {i.check}: {i.message}")
        lines.append("")
    n_err = sum(1 for i in issues if i.severity == "error")
    n_warn = sum(1 for i in issues if i.severity == "warning")
    lines.append(f"{'FAIL' if n_err else 'PASS'} — {n_err} error(s), "
                 f"{n_warn} warning(s), "
                 f"{len(issues) - n_err - n_warn} info")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description="Cross-artifact drift report: schematic vs BOM vs board")
    ap.add_argument("schematic", help="path to .kicad_sch")
    ap.add_argument("--bom", help="path to flat BOM markdown (bom_flat.md)")
    ap.add_argument("--pcb", help="path to .kicad_pcb")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not args.bom and not args.pcb:
        ap.error("nothing to reconcile against — pass --bom and/or --pcb")

    with open(args.schematic, "r", encoding="utf-8") as f:
        sch_text = f.read()

    issues = []
    if args.bom:
        with open(args.bom, "r", encoding="utf-8") as f:
            issues += reconcile_sch_bom(args.schematic, f.read())
    if args.pcb:
        with open(args.pcb, "r", encoding="utf-8") as f:
            issues += reconcile_sch_pcb(sch_text, f.read())

    has_errors = any(i.severity == "error" for i in issues)
    if args.json:
        print(json.dumps({
            "passed": not has_errors,
            "issues": [i.as_dict() for i in issues],
        }, indent=2))
    else:
        print(format_text(issues, args.schematic, args.bom, args.pcb))
    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
