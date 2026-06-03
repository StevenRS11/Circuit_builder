#!/usr/bin/env python3
"""
BOM line verifier (Stage 9) — the bookkeeping half of the answer-blind, per-line
live BOM check. The *judgment* half (does this part actually exist / match) is a
web lookup performed by an answer-blind subagent per line — see SKILL.md and
references/subagents.md. This script never touches the network and makes no
correctness call; it only:

  --worklist : parse the BOM, run the structural gate, and emit the list of lines
               that need a live check (non-passives + structurally-flagged, or all
               with --all), each reduced to the bare CLAIM an agent must verify
               BLIND: {manufacturer, mpn, value, package, distributor code}.

  --report   : take the agents' verdicts (JSON) back, join them to the BOM, and
               write a verification report. Exits 1 if any line's verdict is
               'mismatch' (a confirmed wrong part — the C914291 class) so it can
               gate the upload.

Why answer-blind: the verifier is handed only the claim, not our reasoning or the
datasheet we used, so its lookup is an independent second opinion. A well-formed
LCSC code can still point at the wrong part (that's how C914291=Zener slipped
past every offline check); only an independent live lookup catches it.

CLI:
    python bom_verify.py --worklist <bom.md> [--all] [-o worklist.json]
    python bom_verify.py --report   <bom.md> <verdicts.json> [-o verification_report.md]

Verdict JSON (one object per worklist entry, produced by the agents):
    {"ref": "RS1", "verdict": "mismatch"|"confirmed"|"uncertain",
     "mpn_exists": true, "manufacturer_ok": true, "value_match": true,
     "package_match": false, "distributor_resolves": false,
     "lifecycle": "active"|"NRND"|"EOL"|"unknown",
     "evidence": "LCSC C914291 resolves to an MCC BZT52B5V1BS-TP Zener diode, not a 10mΩ resistor"}
"""

import sys
import os
import re
import json as json_module

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_pcbway import (load_bom_for_pcbway, check_bom,  # noqa: E402
                          verification_worklist, is_generic_passive)


def _distributor_code(part):
    """The distributor catalog code for the line (supplier_pn, else scanned from notes)."""
    pn = (part.supplier_pn or "").strip()
    if pn and pn not in ("-", "—", "–"):
        sup = (part.supplier or "").strip()
        return f"{sup} {pn}".strip()
    m = re.search(r"\b(C\d{3,}|LCSC\s*C\d+)\b", part.notes or "")
    return m.group(0) if m else ""


def build_worklist(parts, include_passives=False):
    """Group worklist lines by part identity and reduce each to the verifiable claim."""
    # populate .flags via the structural gate first
    check_bom(parts)
    work = verification_worklist(parts, include_passives=include_passives)

    groups, order = {}, []
    for p in work:
        key = (p.manufacturer, p.part_number, p.value, p.package)
        if key not in groups:
            groups[key] = {"refs": [], "part": p}
            order.append(key)
        groups[key]["refs"].append(p.reference)

    items = []
    for key in order:
        g = groups[key]
        p = g["part"]
        items.append({
            "ref": p.reference,                       # representative designator
            "designators": ",".join(g["refs"]),
            "claim": {
                "manufacturer": p.manufacturer,
                "mpn": p.part_number,
                "value": p.value,
                "package": p.package,
                "footprint": p.footprint,
                "distributor_code": _distributor_code(p),
            },
            "structural_flags": p.flags,
            "is_passive": is_generic_passive(p.reference, p.footprint),
        })
    return items


# ─── report ──────────────────────────────────────────────────────────

def build_report(parts, verdicts, project=""):
    """Join verdicts to the BOM and render a markdown verification report.

    Returns (markdown, n_mismatch). n_mismatch > 0 should gate the upload.
    """
    by_ref = {v.get("ref"): v for v in verdicts}
    title = f"BOM Verification Report — {project}" if project else "BOM Verification Report"
    lines = [f"# {title}", "",
             "> Answer-blind, per-line live verification (Stage 9). Each line's "
             "{manufacturer, MPN, value, package, distributor code} claim was checked "
             "by an independent web lookup. `mismatch` = confirmed wrong/inconsistent part.",
             ""]

    icon = {"confirmed": "OK", "mismatch": "**MISMATCH**", "uncertain": "uncertain"}
    lines.append("| Ref | MPN | Verdict | Pkg | Dist | Lifecycle | Evidence |")
    lines.append("|-----|-----|---------|-----|------|-----------|----------|")

    n_mismatch = n_uncertain = n_ok = n_unchecked = 0
    # Build per-line rows in BOM order, one row per unique part that was on the worklist.
    seen = set()
    for p in parts:
        v = by_ref.get(p.reference)
        if v is None:
            continue
        key = (p.manufacturer, p.part_number, p.value, p.package)
        if key in seen:
            continue
        seen.add(key)
        verdict = v.get("verdict", "uncertain")
        if verdict == "mismatch":
            n_mismatch += 1
        elif verdict == "confirmed":
            n_ok += 1
        else:
            n_uncertain += 1

        def mark(b):
            return {True: "y", False: "**N**", None: "?"}.get(b, "?")

        lines.append(
            f"| {p.reference} | {p.part_number} | {icon.get(verdict, verdict)} | "
            f"{mark(v.get('package_match'))} | {mark(v.get('distributor_resolves'))} | "
            f"{v.get('lifecycle', 'unknown')} | {v.get('evidence', '')[:120]} |")

    lines.append("")
    lines.append(f"**Verified:** {n_ok} OK · {n_uncertain} uncertain · "
                 f"**{n_mismatch} mismatch**")
    if n_mismatch:
        lines.append("")
        lines.append("⛔ **Mismatches must be resolved before generating the PCBway upload.**")
    return "\n".join(lines), n_mismatch


def _project(bom_path, md):
    m = re.search(r"#\s*Bill of Materials\s*[—-]\s*(.+)", md)
    return m.group(1).strip() if m else os.path.splitext(os.path.basename(bom_path))[0]


# ─── CLI ──────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Emit the BOM live-verification worklist, "
                                             "or aggregate agent verdicts into a report.")
    ap.add_argument("bom", help="Stage-3 BOM markdown")
    ap.add_argument("verdicts", nargs="?", help="Agent verdicts JSON (for --report)")
    ap.add_argument("--worklist", action="store_true", help="Emit the verification worklist (JSON)")
    ap.add_argument("--report", action="store_true", help="Aggregate verdicts into a report")
    ap.add_argument("--all", action="store_true", help="Worklist: include passives too")
    ap.add_argument("-o", "--output", help="Write to file instead of stdout")
    args = ap.parse_args()

    with open(args.bom, "r", encoding="utf-8") as f:
        md = f.read()
    parts = load_bom_for_pcbway(md)

    if args.worklist:
        out = json_module.dumps(build_worklist(parts, include_passives=args.all), indent=2)
        exit_code = 0
    elif args.report:
        if not args.verdicts:
            ap.error("--report needs a verdicts JSON file")
        with open(args.verdicts, "r", encoding="utf-8") as f:
            verdicts = json_module.load(f)
        md_report, n_mismatch = build_report(parts, verdicts, _project(args.bom, md))
        out = md_report
        exit_code = 1 if n_mismatch else 0
    else:
        ap.error("choose --worklist or --report")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out + "\n")
        print(f"Wrote {args.output}")
    else:
        print(out)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
