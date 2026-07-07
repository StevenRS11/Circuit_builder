#!/usr/bin/env python3
"""
Netlist Verification — checks a generated schematic against an intended netlist YAML.

Compares the pin-level connectivity declared in a netlist YAML document (design intent)
against the actual connectivity extracted from a .kicad_sch file (generated output).

Three checks:
    1. Completeness — every pin in the component manifest appears in exactly one net
       or in no_connects. Catches forgotten/unaccounted pins.
    2. Consistency — every (ref, pin) referenced in nets/no_connects exists as an actual
       component pin in the schematic. Catches typos and phantom references.
    3. Connectivity — for each declared net, the schematic's extracted netlist has those
       exact pins on the same net. Catches wiring errors in generation.

CLI Usage:
    python verify_netlist.py <netlist.yaml> <schematic.kicad_sch>
    python verify_netlist.py <netlist.yaml> <schematic.kicad_sch> --json

Python API:
    from verify_netlist import verify, load_intended_netlist
    from validate_kicad_sch import load_kicad_sch

    intended = load_intended_netlist("design_netlist.yaml")
    sch = load_kicad_sch("output.kicad_sch")
    result = verify(intended, sch)

    # Or from YAML string + in-memory schematic:
    from verify_netlist import load_intended_netlist_from_string, verify
    intended = load_intended_netlist_from_string(yaml_text)
    result = verify(intended, sch)
"""

import sys
import os
import json as json_module
from dataclasses import dataclass, field
from typing import Optional

# Ensure sibling imports work
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from validate_kicad_sch import (
    KicadSchematic, extract_netlist, load_kicad_sch, Netlist,
    iter_all_components,
)

try:
    import yaml
except ImportError:
    yaml = None


# ─── Data model ────────────────────────────────────────────────────

@dataclass
class IntendedPin:
    ref: str
    pin: str
    function: str = ""


@dataclass
class IntendedNet:
    name: str
    net_type: str = "signal"  # power | signal
    pins: list = field(default_factory=list)  # list of IntendedPin
    power_symbols: list = field(default_factory=list)  # e.g., ["+3V3"]
    labels: list = field(default_factory=list)  # e.g., ["SDA"]
    # Net classification (drives analog-noise checks — see references/analog_layout.md).
    net_class: str = ""        # analog | analog_differential | analog_supply |
                               # reference | high_impedance | rf | switching | digital | power
    pair: str = ""             # differential pair name (legs share it)
    polarity: str = ""         # "P" | "N" for a differential leg
    source_z: str = ""         # "low" | "high" — source impedance of the driving node
    ratiometric: bool = False  # reference/supply derived from bridge excitation


@dataclass
class IntendedNoConnect:
    ref: str
    pin: str
    reason: str = ""


@dataclass
class IntendedComponent:
    ref: str
    part: str
    pins: list = field(default_factory=list)  # list of pin number strings


@dataclass
class IntendedNetlist:
    project: str = ""
    source: str = ""
    components: dict = field(default_factory=dict)  # ref -> IntendedComponent
    nets: dict = field(default_factory=dict)  # net_name -> IntendedNet
    no_connects: list = field(default_factory=list)  # list of IntendedNoConnect


@dataclass
class VerificationIssue:
    severity: str  # "error", "warning"
    check_name: str  # "completeness", "consistency", "connectivity"
    message: str
    net_name: str = ""
    references: list = field(default_factory=list)


@dataclass
class VerificationResult:
    passed: bool
    issues: list = field(default_factory=list)

    @property
    def errors(self):
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self):
        return [i for i in self.issues if i.severity == "warning"]


# ─── YAML loading ─────────────────────────────────────────────────

def _parse_intended(raw: dict) -> IntendedNetlist:
    """Parse a raw YAML dict into an IntendedNetlist."""
    result = IntendedNetlist(
        project=raw.get("project", ""),
        source=raw.get("source", ""),
    )

    # Components
    for ref, comp_data in raw.get("components", {}).items():
        result.components[ref] = IntendedComponent(
            ref=ref,
            part=comp_data.get("part", ""),
            pins=[str(p) for p in comp_data.get("pins", [])],
        )

    # Nets
    for net_name, net_data in raw.get("nets", {}).items():
        pins = []
        for p in net_data.get("pins", []):
            pins.append(IntendedPin(
                ref=p["ref"],
                pin=str(p["pin"]),
                function=p.get("function", ""),
            ))
        result.nets[net_name] = IntendedNet(
            name=net_name,
            net_type=net_data.get("type", "signal"),
            pins=pins,
            power_symbols=net_data.get("power_symbols", []),
            labels=net_data.get("labels", []),
            net_class=net_data.get("class", ""),
            pair=net_data.get("pair", ""),
            polarity=net_data.get("polarity", ""),
            source_z=net_data.get("source_z", ""),
            ratiometric=bool(net_data.get("ratiometric", False)),
        )

    # No-connects
    for nc in raw.get("no_connects", []):
        result.no_connects.append(IntendedNoConnect(
            ref=nc["ref"],
            pin=str(nc["pin"]),
            reason=nc.get("reason", ""),
        ))

    return result


def load_intended_netlist(filepath: str) -> IntendedNetlist:
    """Load an intended netlist from a YAML file."""
    if yaml is None:
        raise ImportError("PyYAML is required: pip install pyyaml")
    with open(filepath, "r") as f:
        raw = yaml.safe_load(f)
    return _parse_intended(raw)


def load_intended_netlist_from_string(text: str) -> IntendedNetlist:
    """Load an intended netlist from a YAML string."""
    if yaml is None:
        raise ImportError("PyYAML is required: pip install pyyaml")
    raw = yaml.safe_load(text)
    return _parse_intended(raw)


# ─── Verification engine ──────────────────────────────────────────

def verify(intended: IntendedNetlist, sch: KicadSchematic) -> VerificationResult:
    """Verify a schematic's connectivity matches the intended netlist.

    Args:
        intended: The declared netlist (design intent).
        sch: The generated KicadSchematic to check.

    Returns:
        VerificationResult with pass/fail and detailed issues.
    """
    issues = []

    # Extract actual netlist from schematic
    actual = extract_netlist(sch)

    # Build lookup: (ref, pin) -> set of actual schematic component refs.
    # Hierarchy-aware: components inside child sheets count (ROADMAP W1b).
    sch_component_pins = set()
    for comp, lib_sym, _prefix in iter_all_components(sch):
        if lib_sym and lib_sym.is_power:
            continue  # skip power symbols
        if lib_sym:
            for pin in lib_sym.pins:
                sch_component_pins.add((comp.reference, pin.number))

    # ── Check 1: Completeness ──────────────────────────────────────
    # Every pin declared in components{} must appear in exactly one net or no_connects.

    assigned_pins = set()  # (ref, pin) tuples that appear in nets or no_connects

    for net_name, net in intended.nets.items():
        for p in net.pins:
            key = (p.ref, p.pin)
            if key in assigned_pins:
                issues.append(VerificationIssue(
                    severity="error",
                    check_name="completeness",
                    message=f"{p.ref} pin {p.pin} appears in multiple nets "
                            f"(duplicate found in '{net_name}')",
                    net_name=net_name,
                    references=[p.ref],
                ))
            assigned_pins.add(key)

    for nc in intended.no_connects:
        key = (nc.ref, nc.pin)
        if key in assigned_pins:
            issues.append(VerificationIssue(
                severity="error",
                check_name="completeness",
                message=f"{nc.ref} pin {nc.pin} is in both a net and no_connects",
                references=[nc.ref],
            ))
        assigned_pins.add(key)

    # Check every declared component pin is accounted for
    for ref, comp in intended.components.items():
        for pin in comp.pins:
            if (ref, pin) not in assigned_pins:
                issues.append(VerificationIssue(
                    severity="error",
                    check_name="completeness",
                    message=f"{ref} pin {pin} is not assigned to any net or no_connect",
                    references=[ref],
                ))

    # ── Check 2: Consistency ───────────────────────────────────────
    # Every (ref, pin) in nets/no_connects should exist in the schematic.

    for net_name, net in intended.nets.items():
        for p in net.pins:
            if (p.ref, p.pin) not in sch_component_pins:
                issues.append(VerificationIssue(
                    severity="error",
                    check_name="consistency",
                    message=f"Net '{net_name}': {p.ref} pin {p.pin} does not exist "
                            f"in the schematic",
                    net_name=net_name,
                    references=[p.ref],
                ))

    for nc in intended.no_connects:
        if (nc.ref, nc.pin) not in sch_component_pins:
            issues.append(VerificationIssue(
                severity="warning",
                check_name="consistency",
                message=f"No-connect {nc.ref} pin {nc.pin} does not exist "
                        f"in the schematic",
                references=[nc.ref],
            ))

    # Check declared components exist in schematic (across the hierarchy)
    sch_refs = set()
    for comp, lib_sym, _prefix in iter_all_components(sch):
        if lib_sym and lib_sym.is_power:
            continue
        sch_refs.add(comp.reference)
    for ref in intended.components:
        if ref not in sch_refs:
            issues.append(VerificationIssue(
                severity="error",
                check_name="consistency",
                message=f"Component {ref} declared in netlist but not found "
                        f"in schematic",
                references=[ref],
            ))

    # Reverse check: schematic components not declared in netlist
    for ref in sch_refs:
        if ref not in intended.components:
            issues.append(VerificationIssue(
                severity="warning",
                check_name="consistency",
                message=f"Component {ref} exists in schematic but not declared "
                        f"in netlist components",
                references=[ref],
            ))

    # ── Check 3: Connectivity ──────────────────────────────────────
    # For each intended net, verify the schematic has those pins connected.

    # Build reverse map: (ref, pin) -> actual net name
    actual_pin_to_net = {}
    for net_name, entry in actual.nets.items():
        for ref, pin in entry.pins:
            actual_pin_to_net[(ref, pin)] = net_name

    # Build reverse map: (ref, pin) -> intended net name. Used to tell a benign
    # undeclared extra pin apart from a pin the netlist assigns to a DIFFERENT
    # net (a real netlist/schematic parity violation — e.g. a short).
    intended_pin_to_net = {}
    for net_name, net in intended.nets.items():
        for p in net.pins:
            intended_pin_to_net[(p.ref, p.pin)] = net_name

    for intended_name, intended_net in intended.nets.items():
        if len(intended_net.pins) < 2:
            # Single-pin nets: just check the pin exists on some net
            continue

        # Get the actual net for the first pin
        first_pin = intended_net.pins[0]
        first_key = (first_pin.ref, first_pin.pin)
        first_actual_net = actual_pin_to_net.get(first_key)

        if first_actual_net is None:
            issues.append(VerificationIssue(
                severity="error",
                check_name="connectivity",
                message=f"Net '{intended_name}': {first_pin.ref} pin {first_pin.pin} "
                        f"is not on any net in the schematic",
                net_name=intended_name,
                references=[first_pin.ref],
            ))
            continue

        # All other pins should be on the same actual net
        for p in intended_net.pins[1:]:
            p_key = (p.ref, p.pin)
            p_actual_net = actual_pin_to_net.get(p_key)

            if p_actual_net is None:
                issues.append(VerificationIssue(
                    severity="error",
                    check_name="connectivity",
                    message=f"Net '{intended_name}': {p.ref} pin {p.pin} "
                            f"is not on any net in the schematic",
                    net_name=intended_name,
                    references=[p.ref],
                ))
            elif p_actual_net != first_actual_net:
                issues.append(VerificationIssue(
                    severity="error",
                    check_name="connectivity",
                    message=f"Net '{intended_name}': {p.ref} pin {p.pin} is on "
                            f"net '{p_actual_net}' but {first_pin.ref} pin "
                            f"{first_pin.pin} is on net '{first_actual_net}' — "
                            f"they should be connected",
                    net_name=intended_name,
                    references=[p.ref, first_pin.ref],
                ))

        # Check for unexpected extra pins on this actual net
        if first_actual_net:
            actual_entry = actual.nets.get(first_actual_net)
            if actual_entry:
                intended_pin_set = {(p.ref, p.pin) for p in intended_net.pins}
                # Filter out power symbol refs (#PWRnnn)
                actual_non_power = {
                    (r, p) for r, p in actual_entry.pins
                    if not r.startswith("#PWR")
                }
                extra = actual_non_power - intended_pin_set
                if extra:
                    # Split extras: a pin the netlist assigns to a different net
                    # is a parity violation (error); a pin not declared on any
                    # net is a benign-ish extra connection (warning).
                    misassigned = []
                    undeclared = []
                    for r, p in sorted(extra):
                        other = intended_pin_to_net.get((r, p))
                        if other is not None and other != intended_name:
                            misassigned.append((r, p, other))
                        else:
                            undeclared.append((r, p))
                    for r, p, other in misassigned:
                        issues.append(VerificationIssue(
                            severity="error",
                            check_name="connectivity",
                            message=f"Net '{intended_name}': {r} pin {p} is on this "
                                    f"net in the schematic, but the netlist assigns "
                                    f"it to net '{other}' — netlist/schematic "
                                    f"mismatch (possible short)",
                            net_name=intended_name,
                            references=[r],
                        ))
                    if undeclared:
                        extra_str = ", ".join(f"{r}.{p}" for r, p in undeclared)
                        issues.append(VerificationIssue(
                            severity="warning",
                            check_name="connectivity",
                            message=f"Net '{intended_name}': schematic has extra pins "
                                    f"not in netlist: {extra_str}",
                            net_name=intended_name,
                        ))

    has_errors = any(i.severity == "error" for i in issues)
    return VerificationResult(passed=not has_errors, issues=issues)


# ─── Output formatters ────────────────────────────────────────────

def format_result_text(result: VerificationResult,
                       netlist_path: str = None,
                       schematic_path: str = None) -> str:
    """Format verification result as human-readable text."""
    lines = []
    lines.append("=" * 60)
    lines.append("NETLIST VERIFICATION REPORT")
    lines.append("=" * 60)
    if netlist_path:
        lines.append(f"Netlist:   {netlist_path}")
    if schematic_path:
        lines.append(f"Schematic: {schematic_path}")
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
            net_str = f" [{i.net_name}]" if i.net_name else ""
            lines.append(f"  [{i.check_name}]{net_str} {i.message}")
        lines.append("")

    if warnings:
        lines.append("WARNINGS:")
        for i in warnings:
            net_str = f" [{i.net_name}]" if i.net_name else ""
            lines.append(f"  [{i.check_name}]{net_str} {i.message}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def format_result_json(result: VerificationResult,
                       netlist_path: str = None,
                       schematic_path: str = None) -> str:
    """Format verification result as JSON."""
    output = {
        "netlist_file": netlist_path,
        "schematic_file": schematic_path,
        "passed": result.passed,
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "issues": [
            {
                "severity": i.severity,
                "check": i.check_name,
                "net": i.net_name or None,
                "message": i.message,
                "references": i.references,
            }
            for i in result.issues
        ],
    }
    return json_module.dumps(output, indent=2)


# ─── CLI entry point ──────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Verify a KiCad schematic against an intended netlist YAML.",
        epilog="""
Examples:
  python verify_netlist.py design_netlist.yaml board.kicad_sch
  python verify_netlist.py design_netlist.yaml board.kicad_sch --json
        """,
    )
    parser.add_argument("netlist", help="Intended netlist YAML file")
    parser.add_argument("schematic", help="KiCad .kicad_sch file to verify")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    intended = load_intended_netlist(args.netlist)
    sch = load_kicad_sch(args.schematic)
    result = verify(intended, sch)

    if args.json:
        print(format_result_json(result, args.netlist, args.schematic))
    else:
        print(format_result_text(result, args.netlist, args.schematic))

    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
