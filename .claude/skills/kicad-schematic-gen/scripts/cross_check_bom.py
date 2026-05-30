#!/usr/bin/env python3
"""
BOM Cross-Check — verifies a schematic matches its Bill of Materials.

Compares a BOM (from Stage 3 markdown) against a generated .kicad_sch file,
checking that every BOM line item appears in the schematic with matching
reference designator, value, and footprint.

CLI Usage:
    python cross_check_bom.py <bom.md> <schematic.kicad_sch>
    python cross_check_bom.py <bom.md> <schematic.kicad_sch> --json

Python API:
    from cross_check_bom import cross_check, load_bom_from_markdown
    from validate_kicad_sch import load_kicad_sch

    bom = load_bom_from_markdown(md_text)
    sch = load_kicad_sch("output.kicad_sch")
    result = cross_check(bom, sch)
"""

import sys
import os
import re
import json as json_module
from dataclasses import dataclass, field

_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from validate_kicad_sch import KicadSchematic, load_kicad_sch


# ─── Value normalization ──────────────────────────────────────────

# Reuse the same suffix table as analyze_dc._parse_value
_SUFFIXES = [
    ("mF", 1e-3),   ("uF", 1e-6),   ("µF", 1e-6),  ("nF", 1e-9),
    ("pF", 1e-12),  ("mH", 1e-3),   ("uH", 1e-6),  ("µH", 1e-6),
    ("nH", 1e-9),   ("mA", 1e-3),   ("uA", 1e-6),  ("µA", 1e-6),
    ("mV", 1e-3),   ("mW", 1e-3),
    ("meg", 1e6),   ("Meg", 1e6),   ("MEG", 1e6),
    ("M", 1e6),
    ("k", 1e3),     ("K", 1e3),
    ("m", 1e-3),
    ("u", 1e-6),    ("µ", 1e-6),
    ("n", 1e-9),    ("p", 1e-12),
    ("F", 1.0),     ("H", 1.0),     ("V", 1.0),
    ("A", 1.0),     ("W", 1.0),     ("R", 1.0),     ("ohm", 1.0),
    ("Ohm", 1.0),   ("OHM", 1.0),   ("Ω", 1.0),
]


def _parse_value_numeric(val):
    """Try to parse a component value to a float for comparison. Returns None on failure."""
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    for suffix, mult in _SUFFIXES:
        if s.endswith(suffix):
            num_part = s[:-len(suffix)].strip()
            if not num_part:
                continue
            try:
                return float(num_part) * mult
            except ValueError:
                continue
    try:
        return float(s)
    except ValueError:
        return None


def values_match(bom_val, sch_val):
    """Check if two component values are equivalent.

    Tries numeric comparison first (handles "100nF" == "0.1uF"),
    falls back to case-insensitive string comparison.
    """
    if bom_val == sch_val:
        return True
    # Try numeric comparison
    bom_num = _parse_value_numeric(bom_val)
    sch_num = _parse_value_numeric(sch_val)
    if bom_num is not None and sch_num is not None:
        if bom_num == 0 and sch_num == 0:
            return True
        if bom_num != 0:
            return abs(bom_num - sch_num) / abs(bom_num) < 0.001
    # String fallback
    return str(bom_val).strip().lower() == str(sch_val).strip().lower()


# ─── Data model ───────────────────────────────────────────────────

@dataclass
class BomEntry:
    reference: str
    value: str
    footprint: str = ""
    quantity: int = 1


@dataclass
class CrossCheckIssue:
    severity: str  # "error", "warning"
    check_name: str  # "missing_from_schematic", "missing_from_bom", "value_mismatch", "footprint_mismatch"
    message: str
    reference: str = ""


@dataclass
class CrossCheckResult:
    passed: bool
    issues: list = field(default_factory=list)

    @property
    def errors(self):
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self):
        return [i for i in self.issues if i.severity == "warning"]


# ─── BOM markdown parser ─────────────────────────────────────────

def load_bom_from_markdown(md_text):
    """Parse BOM entries from a Stage 3 markdown table.

    Expects a markdown table with at minimum: Ref, Value columns.
    Footprint column is optional but used if present.

    Returns list of BomEntry.
    """
    entries = []
    lines = md_text.strip().split('\n')

    # Find the BOM table header
    header_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('|') and 'Ref' in stripped and 'Value' in stripped:
            header_idx = i
            break

    if header_idx is None:
        return entries

    # Parse header to find column indices
    header = lines[header_idx]
    cols = [c.strip() for c in header.split('|')]
    # Remove empty strings from leading/trailing pipes
    cols = [c for c in cols if c]

    col_map = {}
    for idx, col in enumerate(cols):
        col_lower = col.lower().strip()
        if col_lower in ('ref', 'reference'):
            col_map['ref'] = idx
        elif col_lower == 'value':
            col_map['value'] = idx
        elif 'footprint' in col_lower:
            col_map['footprint'] = idx

    if 'ref' not in col_map or 'value' not in col_map:
        return entries

    # Skip the separator line (---|---|---)
    data_start = header_idx + 2

    for i in range(data_start, len(lines)):
        line = lines[i].strip()
        if not line.startswith('|'):
            break
        cells = [c.strip() for c in line.split('|')]
        cells = [c for c in cells if c != '']

        if len(cells) <= max(col_map.values()):
            continue

        ref = cells[col_map['ref']].strip()
        value = cells[col_map['value']].strip()

        # Skip template/placeholder rows
        if ref.startswith('{') or value.startswith('{'):
            continue

        footprint = ""
        if 'footprint' in col_map and col_map['footprint'] < len(cells):
            fp = cells[col_map['footprint']].strip()
            if not fp.startswith('{'):
                footprint = fp

        entries.append(BomEntry(reference=ref, value=value, footprint=footprint))

    return entries


# ─── Cross-check engine ──────────────────────────────────────────

def cross_check(bom_entries, sch):
    """Compare BOM entries against a KicadSchematic.

    Args:
        bom_entries: List of BomEntry from the BOM document.
        sch: KicadSchematic object (in-memory or loaded from file).

    Returns:
        CrossCheckResult with pass/fail and detailed issues.
    """
    issues = []

    # Build schematic component lookup (excluding power symbols)
    sch_components = {}
    for comp in sch.components:
        lib_sym = sch.lib_symbols.get(comp.lib_id)
        if lib_sym and lib_sym.is_power:
            continue
        if not comp.in_bom:
            continue
        sch_components[comp.reference] = comp

    bom_refs = {e.reference for e in bom_entries}
    sch_refs = set(sch_components.keys())

    # Check 1: BOM entries missing from schematic
    for entry in bom_entries:
        if entry.reference not in sch_refs:
            issues.append(CrossCheckIssue(
                severity="error",
                check_name="missing_from_schematic",
                message=f"{entry.reference} ({entry.value}) is in BOM but not in schematic",
                reference=entry.reference,
            ))

    # Check 2: Schematic components missing from BOM
    for ref in sorted(sch_refs):
        if ref not in bom_refs:
            comp = sch_components[ref]
            issues.append(CrossCheckIssue(
                severity="error",
                check_name="missing_from_bom",
                message=f"{ref} ({comp.value}) is in schematic but not in BOM",
                reference=ref,
            ))

    # Check 3: Value mismatches
    for entry in bom_entries:
        if entry.reference in sch_components:
            comp = sch_components[entry.reference]
            if not values_match(entry.value, comp.value):
                issues.append(CrossCheckIssue(
                    severity="error",
                    check_name="value_mismatch",
                    message=f"{entry.reference}: BOM value '{entry.value}' != "
                            f"schematic value '{comp.value}'",
                    reference=entry.reference,
                ))

    # Check 4: Footprint mismatches
    for entry in bom_entries:
        if entry.footprint and entry.reference in sch_components:
            comp = sch_components[entry.reference]
            if comp.footprint and entry.footprint != comp.footprint:
                issues.append(CrossCheckIssue(
                    severity="error",
                    check_name="footprint_mismatch",
                    message=f"{entry.reference}: BOM footprint '{entry.footprint}' != "
                            f"schematic footprint '{comp.footprint}'",
                    reference=entry.reference,
                ))

    has_errors = any(i.severity == "error" for i in issues)
    return CrossCheckResult(passed=not has_errors, issues=issues)


def cross_check_file(bom_entries, sch_path):
    """Compare BOM entries against a .kicad_sch file."""
    sch = load_kicad_sch(sch_path)
    return cross_check(bom_entries, sch)


# ─── Output formatters ───────────────────────────────────────────

def format_result_text(result, bom_path=None, sch_path=None):
    lines = []
    lines.append("=" * 60)
    lines.append("BOM CROSS-CHECK REPORT")
    lines.append("=" * 60)
    if bom_path:
        lines.append(f"BOM:       {bom_path}")
    if sch_path:
        lines.append(f"Schematic: {sch_path}")
    lines.append("")

    errors = result.errors
    warnings = result.warnings
    if result.passed:
        lines.append(f"RESULT: PASSED ({len(warnings)} warnings)")
    else:
        lines.append(f"RESULT: FAILED ({len(errors)} errors, {len(warnings)} warnings)")
    lines.append("")

    if errors:
        lines.append("ERRORS:")
        for i in errors:
            lines.append(f"  [{i.check_name}] {i.message}")
        lines.append("")
    if warnings:
        lines.append("WARNINGS:")
        for i in warnings:
            lines.append(f"  [{i.check_name}] {i.message}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def format_result_json(result, bom_path=None, sch_path=None):
    output = {
        "bom_file": bom_path,
        "schematic_file": sch_path,
        "passed": result.passed,
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "issues": [
            {
                "severity": i.severity,
                "check": i.check_name,
                "reference": i.reference,
                "message": i.message,
            }
            for i in result.issues
        ],
    }
    return json_module.dumps(output, indent=2)


# ─── CLI ──────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Cross-check a BOM markdown against a KiCad schematic.",
    )
    parser.add_argument("bom", help="BOM markdown file (Stage 3)")
    parser.add_argument("schematic", help="KiCad .kicad_sch file")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    with open(args.bom, "r") as f:
        bom_text = f.read()
    bom_entries = load_bom_from_markdown(bom_text)
    sch = load_kicad_sch(args.schematic)
    result = cross_check(bom_entries, sch)

    if args.json:
        print(format_result_json(result, args.bom, args.schematic))
    else:
        print(format_result_text(result, args.bom, args.schematic))

    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
