#!/usr/bin/env python3
"""check_block.py — deterministic checker for a block bundle (roadmap W1a).

A block earns its "proven, self-contained, reusable" claim only if the bundle
holds together mechanically. This verifies:

  * ``block.yaml`` schema: name, ports (shape enum), dependencies manifest,
    provenance filled (TODO leftovers are warnings; ``--strict`` makes them
    errors — a block with TODO judgment fields isn't ready for reuse);
  * ``sheet.kicad_sch`` validates with 0 errors, **standalone** (no library
    fallback — a block must not depend on any symbol library);
  * the sheet's connectivity verifies against ``netlist.yaml``;
  * contract ↔ artifact parity: block.yaml ports == the sheet's hierarchical
    labels == the netlist's ports/nets; rails == the netlist's power nets;
  * ``bom.md`` cross-checks against the sheet (refs, values, footprints);
  * the dependency policy holds: every footprint on the sheet appears in the
    manifest with source builtin|blocks; every ``blocks`` footprint's
    .kicad_mod actually exists in CircuitBlocks.pretty; **no footprint may
    reference any other library**.

Verify-only; makes no judgment about whether the block is *good* — that
evidence lives in the ledger (validated_boards.yaml) it was promoted from.

CLI:
    python check_block.py blocks/nau7802_frontend [--json] [--strict]
Exit 0 = pass, 1 = errors (or warnings with --strict).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

import yaml

from validate_kicad_sch import load_kicad_sch, validate
from verify_netlist import verify, load_intended_netlist
from cross_check_bom import cross_check, load_bom_from_markdown

PORT_SHAPES = {"input", "output", "bidirectional", "tri_state", "passive"}
CIRCUIT_BLOCKS_NICK = "CircuitBlocks"


class BlockIssue:
    def __init__(self, severity, check, message):
        self.severity = severity
        self.check = check
        self.message = message

    def as_dict(self):
        return {"severity": self.severity, "check": self.check,
                "message": self.message}


def check_block(block_dir, blocks_root=None):
    """Check one block bundle. Returns (passed, issues)."""
    issues = []
    err = lambda c, m: issues.append(BlockIssue("error", c, m))
    warn = lambda c, m: issues.append(BlockIssue("warning", c, m))

    block_dir = os.path.abspath(block_dir)
    if blocks_root is None:
        blocks_root = os.path.dirname(block_dir)
    pretty = os.path.join(blocks_root, "footprints",
                          f"{CIRCUIT_BLOCKS_NICK}.pretty")

    # ── bundle files present ──
    paths = {n: os.path.join(block_dir, n) for n in
             ("block.yaml", "sheet.kicad_sch", "netlist.yaml", "bom.md")}
    missing = [n for n, p in paths.items() if not os.path.isfile(p)]
    if missing:
        err("bundle", f"missing bundle file(s): {', '.join(missing)}")
        return False, issues

    with open(paths["block.yaml"], encoding="utf-8") as f:
        contract = yaml.safe_load(f) or {}

    # ── contract schema ──
    name = str(contract.get("name", "") or "").strip()
    if not name:
        err("schema", "block.yaml: missing 'name'")
    ports = contract.get("ports") or []
    port_names = set()
    for p in ports:
        pname = str(p.get("name", "") or "").strip()
        if not pname:
            err("schema", "block.yaml: port with no name")
            continue
        port_names.add(pname)
        if p.get("dir") not in PORT_SHAPES:
            err("schema", f"port {pname}: dir '{p.get('dir')}' not in "
                          f"{sorted(PORT_SHAPES)}")
    rails = {str(r.get("name", "")).strip()
             for r in (contract.get("rails") or []) if r.get("name")}

    # TODO leftovers = judgment fields never filled
    todo_hits = []
    for field, value in (("description", contract.get("description")),
                         ("provenance.validated_on",
                          (contract.get("provenance") or {}).get("validated_on"))):
        if value is None or "TODO" in str(value):
            todo_hits.append(field)
    for p in ports:
        if "TODO" in str(p.get("note", "")):
            todo_hits.append(f"ports[{p.get('name')}].note")
    for r in (contract.get("rails") or []):
        if r.get("budget_ma") is None:
            todo_hits.append(f"rails[{r.get('name')}].budget_ma")
    if todo_hits:
        warn("todo", "judgment fields still TODO/empty: " + ", ".join(todo_hits))

    # ── sheet: standalone validation (no library fallback by design) ──
    sheet = load_kicad_sch(paths["sheet.kicad_sch"], resolve_from_libraries=False)
    result = validate(sheet)
    for i in result.issues:
        if i.severity == "error":
            err("sheet", f"validate: {i.message}")
    if getattr(sheet, "unresolved_lib_ids", []):
        err("self_contained",
            f"sheet depends on external symbol libraries: "
            f"{sheet.unresolved_lib_ids} — blocks must embed symbols")

    # ── netlist round-trip ──
    intended = load_intended_netlist(paths["netlist.yaml"])
    vres = verify(intended, sheet)
    for i in vres.issues:
        if i.severity == "error":
            err("netlist", f"verify: {i.message}")

    # ── port parity: contract == sheet hierarchical labels == netlist ──
    sheet_ports = {hl.text for hl in getattr(sheet, "hierarchical_labels", [])}
    if port_names != sheet_ports:
        err("port_parity",
            f"contract ports {sorted(port_names)} != sheet hierarchical "
            f"labels {sorted(sheet_ports)}")
    net_names = set(intended.nets)
    for pname in sorted(port_names):
        if pname not in net_names:
            err("port_parity", f"port '{pname}' has no net in netlist.yaml")
    power_nets = {n for n, net in intended.nets.items()
                  if net.net_type == "power" or net.power_symbols}
    for rail in sorted(rails):
        if rail not in power_nets:
            err("rail_parity", f"rail '{rail}' is not a power net in netlist.yaml")
    for pnet in sorted(power_nets - rails):
        warn("rail_parity", f"power net '{pnet}' in netlist but not listed "
                            f"under rails: in block.yaml")

    # ── BOM parity ──
    with open(paths["bom.md"], encoding="utf-8") as f:
        bres = cross_check(load_bom_from_markdown(f.read()), sheet)
    for i in bres.issues:
        if i.severity == "error":
            err("bom", i.message)

    # ── dependency policy ──
    manifest = {}
    for d in ((contract.get("dependencies") or {}).get("footprints") or []):
        manifest[str(d.get("ref", ""))] = str(d.get("source", ""))
    used = {c.footprint for c in sheet.components
            if c.footprint and not c.reference.startswith("#")}
    for fp in sorted(used):
        src = manifest.get(fp)
        if src is None:
            err("dependencies", f"footprint '{fp}' used on sheet but absent "
                                f"from the dependencies manifest")
        elif src not in ("builtin", "blocks"):
            err("dependencies", f"footprint '{fp}': source '{src}' violates the "
                                f"two-source policy (builtin | blocks)")
        elif src == "blocks":
            nick, _, fname = fp.partition(":")
            mod = os.path.join(pretty, f"{fname}.kicad_mod")
            if nick != CIRCUIT_BLOCKS_NICK:
                err("dependencies", f"'{fp}' claims source blocks but nickname "
                                    f"is not {CIRCUIT_BLOCKS_NICK}")
            elif not os.path.isfile(mod):
                err("dependencies", f"'{fp}': {mod} missing from the registry")
    for fp in sorted(set(manifest) - used):
        warn("dependencies", f"manifest lists '{fp}' but the sheet doesn't use it")

    passed = not any(i.severity == "error" for i in issues)
    return passed, issues


def main():
    ap = argparse.ArgumentParser(description="Check a block bundle")
    ap.add_argument("block_dir", help="blocks/{name} directory")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="warnings (incl. TODO judgment fields) also fail")
    args = ap.parse_args()

    passed, issues = check_block(args.block_dir)
    if args.json:
        print(json.dumps({"passed": passed,
                          "issues": [i.as_dict() for i in issues]}, indent=2))
    else:
        for i in issues:
            print(f"{i.severity.upper():7s} {i.check}: {i.message}")
        n_err = sum(1 for i in issues if i.severity == "error")
        print(f"{'PASS' if passed else 'FAIL'} — {n_err} error(s), "
              f"{len(issues) - n_err} warning(s)")
    if args.strict and issues:
        return 1
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
