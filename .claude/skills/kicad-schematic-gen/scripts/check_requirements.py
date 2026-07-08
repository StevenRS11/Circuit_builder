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

Proven-block evidence (ROADMAP W1c): a requirement satisfied by a composed registry
block cites the token `block:{name}` in satisfied_by (e.g. `block:nau7802_dual_loadcell`).
The checker verifies the block exists in the blocks/ registry (error `unknown_block`
otherwise), and treats the block's components — re-annotated into per-instance ranges
by the Stage 6 engine (U2 -> U102/U202, +k*100) — as cited, so they don't trip the
orphan check. Bench provenance ("validated on DualScale rev3") goes in `evidence`; a
[CRITICAL] requirement still requires it.

CLI:
  python check_requirements.py spec.md traceability.yaml bom.md [--json] [--strict]
      [--blocks-dir DIR]   # registry override (default: sibling blocks/)
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

# Proven-block evidence token: `block:{registry_name}` (W1c). Verified against the
# blocks/ registry instead of the BOM.
_BLOCK_TOKEN_RE = re.compile(r"^block:(.+)$", re.I)

# Default registry location — sibling of scripts/ (same convention as generate_from_data).
DEFAULT_BLOCKS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "blocks")

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


_REF_SPLIT_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def load_block_registry(blocks_dir=None):
    """Load the proven-block registry: {block_name: set(component refs)}.

    Reads each blocks/{name}/block.yaml `refs:` list. A missing/empty registry
    returns {} — block:{name} citations then fail as unknown_block.
    """
    blocks_dir = blocks_dir or DEFAULT_BLOCKS_DIR
    registry = {}
    if not os.path.isdir(blocks_dir):
        return registry
    for entry in sorted(os.listdir(blocks_dir)):
        contract = os.path.join(blocks_dir, entry, "block.yaml")
        if not os.path.isfile(contract):
            continue
        with open(contract, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        name = str(data.get("name", entry))
        registry[name] = {str(r) for r in (data.get("refs") or [])}
    return registry


def _block_instance_refs(block_refs, bom_refs):
    """BOM refs that belong to a composed instance of a block.

    The Stage 6 engine re-annotates block refs into per-instance ranges
    (U2 -> U102/U202: +k*100, k >= 1 — see generate_from_data._instance_ref_maps).
    A BOM ref is covered if stripping some positive multiple of 100 lands on a
    block ref. Manually placed sheets whose refs KiCad re-annotated arbitrarily
    are NOT covered — cite those refs directly.
    """
    parsed_block = []
    for ref in block_refs:
        m = _REF_SPLIT_RE.match(ref)
        if m:
            parsed_block.append((m.group(1).upper(), int(m.group(2))))
    covered = set()
    for ref in bom_refs:
        m = _REF_SPLIT_RE.match(ref)
        if not m:
            continue
        prefix, num = m.group(1).upper(), int(m.group(2))
        for bp, bn in parsed_block:
            if prefix == bp and num > bn and (num - bn) % 100 == 0:
                covered.add(ref)
                break
    return covered


# ─── Check ───────────────────────────────────────────────────────────
def check_requirements(spec_reqs, trace, bom_entries, registry_blocks=None):
    """Verify the traceability matrix is complete and consistent.

    Args:
        spec_reqs: dict rid -> SpecRequirement (from the spec).
        trace:     dict rid -> TraceEntry (Claude-authored matrix).
        bom_entries: list of BomEntry (from the BOM).
        registry_blocks: {block_name: set(refs)} from load_block_registry();
            None means no registry is available, so any block:{name} citation
            fails as unknown_block.
    """
    issues = []
    bom_refs = {e.reference for e in bom_entries}
    cited_refs = set()
    registry_blocks = registry_blocks or {}

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
                          if r.upper() not in _EXTERNAL_TOKENS
                          and not _BLOCK_TOKEN_RE.match(r))
        if req.critical and not entry.evidence:
            issues.append(ReqIssue(
                "error", "critical_no_evidence",
                f"{rid} [CRITICAL] is mapped to {entry.satisfied_by} but has no cited "
                f"evidence — a critical requirement must be justified, not asserted.",
                rid))

    # 2. Matrix references must exist in the BOM (no phantom/hallucinated refs);
    #    block:{name} citations must exist in the registry, and their composed
    #    instances' re-annotated refs count as cited.
    for rid, entry in sorted(trace.items()):
        for ref in entry.satisfied_by:
            if ref.upper() in _EXTERNAL_TOKENS:
                continue
            bm = _BLOCK_TOKEN_RE.match(ref)
            if bm:
                block_name = bm.group(1).strip()
                if block_name not in registry_blocks:
                    issues.append(ReqIssue(
                        "error", "unknown_block",
                        f"{rid} cites '{ref}', but no block named '{block_name}' "
                        f"exists in the blocks/ registry.", rid))
                else:
                    cited_refs.update(_block_instance_refs(
                        registry_blocks[block_name], bom_refs))
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
    ap.add_argument("--blocks-dir", default=None,
                    help="proven-block registry for block:{name} citations "
                         "(default: the skill's blocks/ directory)")
    args = ap.parse_args(argv)

    spec_reqs = load_spec_requirements(args.spec)
    trace = load_traceability(args.traceability)
    with open(args.bom, "r", encoding="utf-8") as f:
        bom = load_bom_from_markdown(f.read())

    result = check_requirements(spec_reqs, trace, bom,
                                registry_blocks=load_block_registry(args.blocks_dir))

    if args.json:
        print(json.dumps(result_to_dict(result), indent=2))
    else:
        print(format_result_text(result, spec_reqs))

    failed = (not result.passed) or (args.strict and result.warnings)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
