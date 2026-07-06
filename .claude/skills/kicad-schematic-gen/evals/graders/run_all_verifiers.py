#!/usr/bin/env python3
"""
run_all_verifiers — the single grading entry point for an eval corpus case.

Given a case directory holding a frozen, normalized artifact set, this regenerates
the schematic deterministically (uuid_seed=0), checks it byte-for-byte against the
case's golden, and runs every applicable deterministic verifier (the same scripts
the skill uses as its gates) as graders. It aggregates each grader's
`passed / errors / warnings / infos` into one report.

It makes no judgments — it only runs the existing verifiers and tallies their typed
issues. A grader is "applicable" only if its input artifacts are present, so the same
runner works for partial cases.

Expected (normalized) filenames in a case dir:
    01_specification.md   03_bom.md          03_bom_flat.md     04b_design.yaml
    05b_netlist.yaml      06_layout.yaml     07_traceability.yaml
    datasheets/ (*.facts.yaml + index.md)    golden.kicad_sch

CLI:
    python run_all_verifiers.py <case_dir>
    python run_all_verifiers.py <case_dir> --json
    python run_all_verifiers.py <case_dir> --update-golden   # refresh golden.kicad_sch

Python API:
    from run_all_verifiers import grade_case
    report = grade_case("corpus/synthetic/battery_3s_full")
    # report["passed"], report["graders"]["check_cards"]["errors"], ...
"""

import os
import sys
import json as json_module
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from generate_from_data import generate, load_layout
from validate_kicad_sch import load_kicad_sch, validate
from verify_netlist import load_intended_netlist, verify
from cross_check_bom import load_bom_from_markdown, cross_check
from check_cards import load_cards_from_dir, check_cards
from check_requirements import load_spec_requirements, load_traceability, check_requirements
from analyze_dc import load_design, analyze as analyze_dc
from analyze_analog import analyze_netlist_file
from check_pcbway import load_bom_for_pcbway, check_bom, check_schematic_mpns


# ─── normalized case filenames ────────────────────────────────────
SPEC = "01_specification.md"
BOM = "03_bom.md"
BOM_FLAT = "03_bom_flat.md"
DESIGN = "04b_design.yaml"
NETLIST = "05b_netlist.yaml"
LAYOUT = "06_layout.yaml"
TRACE = "07_traceability.yaml"
DATASHEETS = "datasheets"
GOLDEN = "golden.kicad_sch"


def _tally(issues):
    """Count a result's typed issues by severity. Works for any *Issue with .severity."""
    out = {"errors": 0, "warnings": 0, "infos": 0}
    for i in issues:
        sev = getattr(i, "severity", "")
        if sev == "error":
            out["errors"] += 1
        elif sev == "warning":
            out["warnings"] += 1
        elif sev == "info":
            out["infos"] += 1
    return out


def _g(passed, issues=(), **extra):
    d = {"applicable": True, "passed": bool(passed)}
    d.update(_tally(issues))
    d.update(extra)
    return d


def grade_case(case_dir):
    """Run every applicable grader over a case directory.

    Returns a report dict: {passed, case, graders: {name: {...}}, missing_for: {...}}.
    `passed` is True iff every applicable grader passed and (if a golden exists) the
    regenerated schematic byte-matches it.
    """
    case_dir = os.path.abspath(case_dir)
    def p(name):
        return os.path.join(case_dir, name)
    def has(name):
        return os.path.exists(p(name))

    graders = {}
    skipped = {}

    # ── 1. generate (uuid_seed=0) + golden byte-match ────────────────
    sch = None
    if has(NETLIST) and has(BOM_FLAT) and has(LAYOUT):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "gen.kicad_sch")
            res = generate(p(NETLIST), p(BOM_FLAT), p(LAYOUT), out_path=out, uuid_seed=0)
            entry = {
                "applicable": True,
                "passed": bool(res.passed),
                "errors": len(res.errors),
                "warnings": len(res.warnings),
                "infos": 0,
            }
            if res.passed and os.path.exists(out):
                produced = open(out, encoding="utf-8").read()
                if has(GOLDEN):
                    golden = open(p(GOLDEN), encoding="utf-8").read()
                    entry["golden_match"] = (produced == golden)
                    if not entry["golden_match"]:
                        entry["passed"] = False
                        entry["golden_bytes"] = len(golden)
                        entry["produced_bytes"] = len(produced)
                else:
                    entry["golden_match"] = None  # no golden to compare
                # keep the produced sch in-memory for the sch-based graders
                sch = load_kicad_sch(out)
            graders["generate"] = entry
    else:
        skipped["generate"] = "needs 05b_netlist + 03_bom_flat + 06_layout"

    # ── 2. validator on the generated sch ───────────────────────────
    if sch is not None:
        r = validate(sch)
        graders["validate"] = _g(r.passed, r.issues)
    else:
        skipped["validate"] = "no generated schematic"

    # ── 3. netlist verification ─────────────────────────────────────
    if sch is not None and has(NETLIST):
        r = verify(load_intended_netlist(p(NETLIST)), sch)
        graders["verify_netlist"] = _g(r.passed, r.issues)
    else:
        skipped["verify_netlist"] = "needs generated sch + 05b_netlist"

    # ── 4. BOM cross-check ──────────────────────────────────────────
    if sch is not None and has(BOM):
        bom = load_bom_from_markdown(open(p(BOM), encoding="utf-8").read())
        r = cross_check(bom, sch)
        graders["cross_check_bom"] = _g(r.passed, r.issues)
    else:
        skipped["cross_check_bom"] = "needs generated sch + 03_bom.md"

    # ── 5. fact-card cross-check ────────────────────────────────────
    if has(LAYOUT) and has(BOM_FLAT) and os.path.isdir(p(DATASHEETS)):
        cards = load_cards_from_dir(p(DATASHEETS))
        layout = load_layout(p(LAYOUT))
        bom_flat = load_bom_from_markdown(open(p(BOM_FLAT), encoding="utf-8").read())
        r = check_cards(cards, layout, bom_flat)
        graders["check_cards"] = _g(r.passed, r.issues)
    else:
        skipped["check_cards"] = "needs 06_layout + 03_bom_flat + datasheets/"

    # ── 6. requirements traceability ────────────────────────────────
    if has(SPEC) and has(TRACE) and has(BOM_FLAT):
        spec_reqs = load_spec_requirements(p(SPEC))
        trace = load_traceability(p(TRACE))
        bom_flat = load_bom_from_markdown(open(p(BOM_FLAT), encoding="utf-8").read())
        r = check_requirements(spec_reqs, trace, bom_flat)
        graders["check_requirements"] = _g(r.passed, r.issues)
    else:
        skipped["check_requirements"] = "needs 01_specification + 07_traceability + 03_bom_flat"

    # ── 7. DC analysis ──────────────────────────────────────────────
    if has(DESIGN):
        r = analyze_dc(load_design(p(DESIGN)))
        graders["analyze_dc"] = _g(r.passed, r.issues)
    else:
        skipped["analyze_dc"] = "no 04b_design.yaml"

    # ── 8. analog front-end completeness ────────────────────────────
    if has(NETLIST):
        r = analyze_netlist_file(p(NETLIST))
        graders["analyze_analog"] = _g(r.passed, r.issues)
    else:
        skipped["analyze_analog"] = "no 05b_netlist.yaml"

    # ── 9. PCBway assembly readiness ────────────────────────────────
    if has(BOM):
        r = check_bom(load_bom_for_pcbway(open(p(BOM), encoding="utf-8").read()))
        graders["check_pcbway"] = _g(r.passed, r.issues)
    else:
        skipped["check_pcbway"] = "no 03_bom.md"

    # ── 10. [CRITICAL] schematic-MPN gate on the baked symbol fields ──
    if sch is not None:
        r = check_schematic_mpns(sch)
        graders["check_schematic_mpn"] = _g(r.passed, r.issues)
    else:
        skipped["check_schematic_mpn"] = "no generated schematic"

    overall = all(g["passed"] for g in graders.values()) and len(graders) > 0
    return {
        "case": case_dir,
        "passed": overall,
        "graders": graders,
        "skipped": skipped,
    }


def update_golden(case_dir):
    """Regenerate the case's golden.kicad_sch from its frozen inputs at uuid_seed=0."""
    case_dir = os.path.abspath(case_dir)
    netlist = os.path.join(case_dir, NETLIST)
    bom_flat = os.path.join(case_dir, BOM_FLAT)
    layout = os.path.join(case_dir, LAYOUT)
    golden = os.path.join(case_dir, GOLDEN)
    res = generate(netlist, bom_flat, layout, out_path=golden, uuid_seed=0)
    if not res.passed:
        raise SystemExit("refused to write golden — generation failed:\n  " +
                         "\n  ".join(res.errors))
    return golden


# ─── output ───────────────────────────────────────────────────────

def format_report_text(report):
    lines = []
    lines.append("=" * 64)
    lines.append("EVAL CASE GRADE REPORT")
    lines.append("=" * 64)
    lines.append(f"Case:   {report['case']}")
    lines.append(f"RESULT: {'PASSED' if report['passed'] else 'FAILED'}")
    lines.append("")
    name_w = max((len(n) for n in report["graders"]), default=0)
    for name, g in report["graders"].items():
        flag = "ok " if g["passed"] else "XX "
        extra = ""
        if "golden_match" in g and g["golden_match"] is not None:
            extra = f"  golden={'match' if g['golden_match'] else 'MISMATCH'}"
        lines.append(f"  [{flag}] {name:<{name_w}}  "
                     f"err={g['errors']} warn={g['warnings']} info={g['infos']}{extra}")
    if report["skipped"]:
        lines.append("")
        lines.append("  skipped:")
        for name, why in report["skipped"].items():
            lines.append(f"    - {name}: {why}")
    lines.append("=" * 64)
    return "\n".join(lines)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Grade an eval corpus case with every applicable verifier.")
    ap.add_argument("case_dir", help="path to a case directory")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--update-golden", action="store_true",
                    help="regenerate golden.kicad_sch from the case inputs (uuid_seed=0) and exit")
    args = ap.parse_args()

    if args.update_golden:
        g = update_golden(args.case_dir)
        print(f"golden updated: {g}")
        return

    report = grade_case(args.case_dir)
    if args.json:
        print(json_module.dumps(report, indent=2))
    else:
        print(format_report_text(report))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
