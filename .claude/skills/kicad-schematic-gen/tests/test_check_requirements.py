"""Unit tests for the requirements traceability checker (check_requirements.py)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from check_requirements import (
    parse_spec_requirements, parse_traceability, check_requirements,
)
from cross_check_bom import load_bom_from_markdown
import yaml as _yaml


SPEC = """
## 8. Requirements Checklist

- R1. Accept USB-C PD input at 15V.
- R2. **[CRITICAL]** SYS rail live whenever VBUS is present, even with battery absent/UVLO.
- R3. Provide a regulated 5V logic rail at 1.5A.
"""

BOM = """
| Ref | Value | Footprint |
|-----|-------|-----------|
| U1 | CH224K | F:F |
| U2 | BQ25703A | F:F |
| U3 | TPS54202 | F:F |
| J1 | USB-C | F:F |
| C1 | 100nF | F:F |
"""


def _trace(d):
    return parse_traceability(_yaml.safe_load(d))


def _bom():
    return load_bom_from_markdown(BOM)


# ─── Parsing ─────────────────────────────────────────────────────────
def test_parses_requirements_and_critical_flag():
    reqs = parse_spec_requirements(SPEC)
    assert set(reqs) == {"R1", "R2", "R3"}
    assert reqs["R2"].critical is True
    assert reqs["R1"].critical is False


# ─── Happy path ──────────────────────────────────────────────────────
GOOD_TRACE = """
requirements:
  R1: { satisfied_by: [U1, J1], evidence: "CH224K negotiates 15V; J1 USB-C input" }
  R2: { satisfied_by: [U2], evidence: "BQ25703A SYS regulator independent of BAT (datasheet 8.3)" }
  R3: { satisfied_by: [U3], evidence: "TPS54202 buck, 5V/2A" }
"""


def test_complete_consistent_matrix_passes():
    res = check_requirements(parse_spec_requirements(SPEC), _trace(GOOD_TRACE), _bom())
    assert res.passed, [i.message for i in res.errors]
    assert res.errors == []


# ─── Each failure mode ───────────────────────────────────────────────
def test_untraced_requirement_is_error():
    # Drop R2 from the matrix entirely (the "dropped block" case).
    trace = _trace("""
requirements:
  R1: { satisfied_by: [U1, J1], evidence: "x" }
  R3: { satisfied_by: [U3], evidence: "x" }
""")
    res = check_requirements(parse_spec_requirements(SPEC), trace, _bom())
    assert not res.passed
    assert any(i.check_name == "untraced_requirement" and i.rid == "R2"
               for i in res.errors)


def test_critical_without_evidence_is_error():
    trace = _trace("""
requirements:
  R1: { satisfied_by: [U1, J1], evidence: "x" }
  R2: { satisfied_by: [U2], evidence: "" }
  R3: { satisfied_by: [U3], evidence: "x" }
""")
    res = check_requirements(parse_spec_requirements(SPEC), trace, _bom())
    assert not res.passed
    assert any(i.check_name == "critical_no_evidence" and i.rid == "R2"
               for i in res.errors)


def test_phantom_ref_is_error():
    trace = _trace("""
requirements:
  R1: { satisfied_by: [U1, J1], evidence: "x" }
  R2: { satisfied_by: [U9], evidence: "x" }
  R3: { satisfied_by: [U3], evidence: "x" }
""")
    res = check_requirements(parse_spec_requirements(SPEC), trace, _bom())
    assert not res.passed
    assert any(i.check_name == "phantom_ref" and "U9" in i.message
               for i in res.errors)


def test_stale_requirement_is_warning():
    trace = _trace("""
requirements:
  R1: { satisfied_by: [U1, J1], evidence: "x" }
  R2: { satisfied_by: [U2], evidence: "x" }
  R3: { satisfied_by: [U3], evidence: "x" }
  R9: { satisfied_by: [U1], evidence: "x" }
""")
    res = check_requirements(parse_spec_requirements(SPEC), trace, _bom())
    assert res.passed  # warnings don't fail
    assert any(i.check_name == "stale_requirement" and i.rid == "R9"
               for i in res.warnings)


def test_orphan_ic_is_warning():
    # Add an uncited IC to the BOM.
    bom = load_bom_from_markdown(BOM + "| U7 | EXTRA | F:F |\n")
    res = check_requirements(parse_spec_requirements(SPEC), _trace(GOOD_TRACE), bom)
    assert res.passed
    assert any(i.check_name == "orphan_part" and i.rid == "U7"
               for i in res.warnings)


def test_passive_orphan_not_flagged():
    # A stray decoupling cap should NOT be flagged (passives are exempt).
    bom = load_bom_from_markdown(BOM + "| C9 | 100nF | F:F |\n")
    res = check_requirements(parse_spec_requirements(SPEC), _trace(GOOD_TRACE), bom)
    assert not any(i.rid == "C9" for i in res.warnings)


def test_external_token_accepted_with_evidence():
    # A requirement satisfied off-board (e.g. by the pack BMS) uses a reserved token,
    # not a BOM ref — accepted, not a phantom_ref, as long as evidence is present.
    spec = parse_spec_requirements(
        "- R1. x\n- R2. **[CRITICAL]** Cell protection.\n- R3. x\n")
    trace = _trace("""
requirements:
  R1: { satisfied_by: [U1], evidence: "x" }
  R2: { satisfied_by: [EXTERNAL], evidence: "handled by the pack BMS (OVP/UVP/OCP)" }
  R3: { satisfied_by: [U3], evidence: "x" }
""")
    res = check_requirements(spec, trace, _bom())
    assert res.passed, [i.message for i in res.errors]
    assert not any(i.check_name == "phantom_ref" for i in res.issues)
