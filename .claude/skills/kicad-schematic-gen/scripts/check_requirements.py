"""Requirements traceability checker (Stage 7 backstop).

Deterministic structural check that the verified Stage-1 Requirements Checklist is
fully accounted for in the design. This is the *verify* half of the loop: Claude
authors the traceability matrix (which parts/topology satisfy each requirement, with
cited evidence — judgment); this script verifies that matrix is complete and
consistent against the spec and the BOM (deterministic).

It does NOT judge whether a part truly satisfies a requirement — that is the cited
evidence's job — but it catches the structural failure modes that let a spec drift
through unnoticed:

  * a dropped block — a requirement with no part mapped to it;
  * a hallucinated reference — a matrix entry citing a ref that isn't in the BOM;
  * a stale entry — a matrix requirement that no longer exists in the spec;
  * a [CRITICAL] requirement with no cited evidence;
  * an unaccounted IC/transistor/connector (cited by no requirement).

Inputs:
  spec.md            Stage 1 spec. Requirements are parsed from list lines of the
                     form '- R<n>. ...'; a requirement is [CRITICAL] if the line
                     contains a bracketed '[CRITICAL]' marker.
  traceability.yaml  Claude-authored. Maps each requirement id to the refs that
                     satisfy it and a one-line evidence string:
                        requirements:
                          R1: { satisfied_by: [U1, J1], evidence: "..." }
                          R2: { satisfied_by: [U2],     evidence: "..." }
  bom.md             Stage 3 BOM (flat or full table). Source of the component refs.

CLI:
  python check_requirements.py spec.md traceability.yaml bom.md [--json] [--strict]
Exit code 0 = pass, 1 = errors (or warnings under --strict).
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml

from cross_check_bom import load_bom_from_markdown

# Reference prefixes that implement a functional block — an unmapped one is worth a
# warning. Passives / indicators / mechanical (R, C, L, D, F, RT, Y, TP, MH, FB) are
# not expected to map one-to-one to a requirement, so they're exempt from the orphan check.
_BLOCK_PREFIXES = {"U", "Q", "J", "SW", "K", "M"}

# Reserved satisfied_by tokens for requirements met off-board or by deliberate omission
# (e.g. protection handled by the pack BMS, or "no on-board balancing per spec"). These
# are accepted without BOM validation but still require cited evidence.
_EXTERNAL_TOKENS = {"EXTERNAL", "OFF-BOARD", "N/A", "BY-DESIGN"}

# A spec requirement line: "- R12. text" / "* R3: text" / "- R7 text".
_REQ_RE = re.compile(r"^\s*[-*]\s*R(\d+)\b[.:]?\s*(.*)$")
_CRITICAL_RE = re.compile(r"\[\s*critical\s*\]", re.I)


@dataclass
class SpecRequirement:
    rid: str
    text: str
    critical: bool = False


@dataclass
class TraceEntry:
    rid: str
    satisfied_by: list = field(default_factory=list)
    evidence: str = ""


@dataclass
class ReqIssue:
    severity: str           # "error" | "warning"
    check_name: str
    message: str
    rid: str = ""


@dataclass
class ReqResult:
    passed: bool
    issues: list = field(default_factory=list)

    @property
    def errors(self):
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self):
        return [i for i in self.issues if i.severity == "warning"]


# ─── Parsing ─────────────────────────────────────────────────────────
def parse_spec_requirements(md_text):
    """Extract R# requirements from a spec markdown. Returns dict rid -> SpecRequirement."""
    reqs = {}
    for line in md_text.splitlines():
        m = _REQ_RE.match(line)
        if not m:
            continue
        rid = "R" + m.group(1)
        rest = m.group(2).strip()
        reqs[rid] = SpecRequirement(
            rid=rid, text=rest, critical=bool(_CRITICAL_RE.search(rest)))
    return reqs


def load_spec_requirements(path):
    with open(path, "r", encoding="utf-8") as f:
        return parse_spec_requirements(f.read())


def parse_traceability(raw):
    """Parse a traceability dict into rid -> TraceEntry."""
    out = {}
    for rid, data in (raw.get("requirements", {}) or {}).items():
        data = data or {}
        sb = data.get("satisfied_by", []) or []
        if isinstance(sb, str):
            sb = [sb]
        out[str(rid)] = TraceEntry(
            rid=str(rid),
            satisfied_by=[str(r).strip() for r in sb if str(r).strip()],
            evidence=str(data.get("evidence", "") or "").strip(),
        )
    return out


def load_traceability(path):
    with open(path, "r", encoding="utf-8") as f:
        return parse_traceability(yaml.safe_load(f) or {})


def _ref_prefix(ref):
    m = re.match(r"^([A-Za-z]+)", ref)
    return m.group(1).upper() if m else ""


# ─── Check ───────────────────────────────────────────────────────────
def check_requirements(spec_reqs, trace, bom_entries):
    """Verify the traceability matrix is complete and consistent.

    Args:
        spec_reqs: dict rid -> SpecRequirement (from the spec).
        trace:     dict rid -> TraceEntry (Claude-authored matrix).
        bom_entries: list of BomEntry (from the BOM).
    """
    issues = []
    bom_refs = {e.reference for e in bom_entries}
    cited_refs = set()

    # 1. Every spec requirement must be traced; [CRITICAL] must carry evidence.
    for rid, req in sorted(spec_reqs.items(), key=lambda kv: int(kv[0][1:])):
        entry = trace.get(rid)
        crit = " [CRITICAL]" if req.critical else ""
        if entry is None or not entry.satisfied_by:
            issues.append(ReqIssue(
                "error", "untraced_requirement",
                f"{rid}{crit} has no part mapped to it in the traceability matrix "
                f"— dropped block or forgotten requirement? ({req.text[:60]})",
                rid))
            continue
        cited_refs.update(r for r in entry.satisfied_by
                          if r.upper() not in _EXTERNAL_TOKENS)
        if req.critical and not entry.evidence:
            issues.append(ReqIssue(
                "error", "critical_no_evidence",
                f"{rid} [CRITICAL] is mapped to {entry.satisfied_by} but has no cited "
                f"evidence — a critical requirement must be justified, not asserted.",
                rid))

    # 2. Matrix references must exist in the BOM (no phantom/hallucinated refs).
    for rid, entry in trace.items():
        for ref in entry.satisfied_by:
            if ref.upper() in _EXTERNAL_TOKENS:
                continue
            if ref not in bom_refs:
                issues.append(ReqIssue(
                    "error", "phantom_ref",
                    f"{rid} cites '{ref}', which is not in the BOM.", rid))

    # 3. Matrix entries with no matching spec requirement (stale).
    for rid in trace:
        if rid not in spec_reqs:
            issues.append(ReqIssue(
                "warning", "stale_requirement",
                f"{rid} is in the traceability matrix but not in the spec "
                f"(stale — was a requirement removed/renumbered?).", rid))

    # 4. Functional parts (IC/transistor/connector/switch) cited by no requirement.
    for ref in sorted(bom_refs):
        if _ref_prefix(ref) in _BLOCK_PREFIXES and ref not in cited_refs:
            issues.append(ReqIssue(
                "warning", "orphan_part",
                f"{ref} is in the BOM but maps to no requirement "
                f"(why is it on the board?).", ref))

    has_err = any(i.severity == "error" for i in issues)
    return ReqResult(passed=not has_err, issues=issues)


# ─── Reporting ───────────────────────────────────────────────────────
def format_result_text(result, spec_reqs=None):
    lines = ["=" * 60, "REQUIREMENTS TRACEABILITY CHECK", "=" * 60]
    if spec_reqs is not None:
        n_crit = sum(1 for r in spec_reqs.values() if r.critical)
        lines.append(f"Requirements: {len(spec_reqs)} ({n_crit} critical)")
    errs, warns = result.errors, result.warnings
    lines.append("")
    lines.append(f"RESULT: {'PASSED' if result.passed else 'FAILED'} "
                 f"({len(errs)} errors, {len(warns)} warnings)")
    if errs:
        lines.append("\nERRORS:")
        for i in errs:
            lines.append(f"  [{i.check_name}] {i.message}")
    if warns:
        lines.append("\nWARNINGS:")
        for i in warns:
            lines.append(f"  [{i.check_name}] {i.message}")
    lines.append("=" * 60)
    return "\n".join(lines)


def result_to_dict(result):
    return {
        "passed": result.passed,
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "issues": [
            {"severity": i.severity, "check": i.check_name,
             "message": i.message, "requirement": i.rid}
            for i in result.issues
        ],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Requirements traceability checker.")
    ap.add_argument("spec", help="Stage 1 spec markdown")
    ap.add_argument("traceability", help="Claude-authored traceability YAML")
    ap.add_argument("bom", help="Stage 3 BOM markdown")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="warnings also fail")
    args = ap.parse_args(argv)

    spec_reqs = load_spec_requirements(args.spec)
    trace = load_traceability(args.traceability)
    with open(args.bom, "r", encoding="utf-8") as f:
        bom = load_bom_from_markdown(f.read())

    result = check_requirements(spec_reqs, trace, bom)

    if args.json:
        print(json.dumps(result_to_dict(result), indent=2))
    else:
        print(format_result_text(result, spec_reqs))

    failed = (not result.passed) or (args.strict and result.warnings)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
