#!/usr/bin/env python3
"""extract_netlist.py — decompile an existing .kicad_sch into the Stage-5b
netlist YAML format (the connectivity document of a board's context pack).

This is the ingest half of "brownfield" mode: it turns geometry (wires, labels,
junctions, power symbols) back into the same pin-level netlist YAML the
greenfield pipeline authors at Stage 5b — so every downstream consumer
(verify_netlist, analyze_analog, the design-review subagent, re-entry into the
generator pipeline) works on an existing board exactly as it would on a
generated one.

Deterministic, extraction-only — it reads what the schematic says, it never
guesses design intent. Judgment layers (net `class:` tags, current budgets,
functional grouping) are added afterwards by Claude during the ENRICH phase,
by editing the emitted YAML.

Self-check: after emitting, the YAML is loaded back with verify_netlist and
verified against the source schematic. On a healthy file this passes by
construction; a failure means an extractor bug or a schematic the loader can't
fully resolve (see references/ingest.md — stale lib_symbols cache).

Extraction policy for imperfect boards (they're the normal case here):
  * A pin on no wire and with no NC marker is *floating* — a real finding.
    It is recorded in `no_connects` with reason "EXTRACTED-FLOATING: ..." so
    the document stays complete/loadable, and counted in the summary so the
    finding isn't silently absorbed.
  * Nets with no label are auto-named "N$<ref>_<pin>" from their first pin
    (deterministic across runs; stable under unrelated edits).
  * Power-symbol instances (#PWR...) are not components; they appear as
    `power_symbols:` on their nets, matching the 05b schema.

CLI:
    python extract_netlist.py board.kicad_sch -o claude_context/netlist.yaml
    python extract_netlist.py board.kicad_sch -o netlist.yaml --json
    python extract_netlist.py board.kicad_sch --strict   # exit 1 on self-verify failure
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
from validate_kicad_sch import (
    load_kicad_sch,
    extract_netlist,
    _extract_all_pin_positions,
    _coord_key,
)


# ─── helpers ─────────────────────────────────────────────────────────

def _refsort(ref: str):
    m = re.match(r"([A-Za-z_#]+)(\d+)", ref)
    return (m.group(1), int(m.group(2))) if m else (ref, 0)


def _pinsort(pin: str):
    m = re.match(r"(\d+)", pin)
    return (int(m.group(1)), pin) if m else (10 ** 9, pin)


def _q(s) -> str:
    """Always-double-quoted YAML scalar. Control characters are escaped as
    \\xNN — real boards contain them (e.g. a stray keychord embedded a 0x1F
    inside a component Value on DualScale), and the emitted YAML must both
    load and preserve the oddity so it can be reported as a finding."""
    out = str(s).replace("\\", "\\\\").replace('"', '\\"')
    out = "".join(f"\\x{ord(c):02x}" if ord(c) < 0x20 else c for c in out)
    return '"' + out + '"'


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_power_component(sch, comp) -> bool:
    if comp.reference.startswith("#"):
        return True
    lib_sym = sch.lib_symbols.get(comp.lib_id)
    return bool(lib_sym and lib_sym.is_power)


# ─── extraction ──────────────────────────────────────────────────────

def extract(sch_path: str, project_dir=None, extra_sym=None):
    """Extract the intended-netlist data model from a .kicad_sch.

    Returns (doc, summary) where doc is a plain dict in 05b shape and summary
    holds extraction statistics/warnings. Symbols missing from the file's
    embedded cache are resolved from installed/project libraries (the loader
    fallback) and reported in the summary — see references/ingest.md.
    """
    sch = load_kicad_sch(sch_path, project_dir=project_dir, extra_sym=extra_sym)
    netlist = extract_netlist(sch)

    power_refs = {c.reference for c in sch.components if _is_power_component(sch, c)}

    # Component manifest (dedup multi-unit instances by reference).
    components = {}
    pin_names = {}  # ref -> {pin_number: pin_name}
    for comp in sch.components:
        if comp.reference in power_refs or comp.reference in components:
            continue
        lib_sym = sch.lib_symbols.get(comp.lib_id)
        pins = []
        names = {}
        if lib_sym:
            seen = set()
            for p in lib_sym.pins:
                if p.number in seen:
                    continue
                seen.add(p.number)
                pins.append(p.number)
                if p.name and p.name != "~":
                    names[p.number] = p.name
        components[comp.reference] = {
            "part": comp.value,
            "lib_id": comp.lib_id,
            "footprint": comp.footprint,
            "pins": sorted(pins, key=_pinsort),
        }
        pin_names[comp.reference] = names

    # Nets — drop power-symbol pseudo-pins, rename unlabeled nets.
    nets = {}
    auto_named = 0
    for name, entry in netlist.nets.items():
        real_pins = sorted(
            [(r, p) for (r, p) in entry.pins if r not in power_refs],
            key=lambda rp: (_refsort(rp[0]), _pinsort(rp[1])),
        )
        if not real_pins:
            continue
        net_name = name
        if not entry.has_label:
            ref0, pin0 = real_pins[0]
            net_name = f"N${ref0}_{pin0}"
            auto_named += 1
        nets[net_name] = {
            "type": "power" if entry.is_power else "signal",
            "pins": [{"ref": r, "pin": p,
                      "function": pin_names.get(r, {}).get(p, "")}
                     for r, p in real_pins],
            "power_symbols": [name] if entry.is_power else [],
            "labels": [name] if (entry.has_label and not entry.is_power) else [],
        }

    # No-connects — map NC markers back to (ref, pin) by position.
    pin_positions = _extract_all_pin_positions(sch)
    coord_to_pins = {}
    for ref, pnum, x, y, _ptype, _pwr in pin_positions:
        if ref in power_refs:
            continue
        coord_to_pins.setdefault(_coord_key(x, y), []).append((ref, pnum))

    nc_pins = set()
    unmatched_nc = 0
    for nc in sch.no_connects:
        hits = coord_to_pins.get(_coord_key(nc.x, nc.y), [])
        if hits:
            nc_pins.update(hits)
        else:
            unmatched_nc += 1

    connected = set()
    for net in nets.values():
        for p in net["pins"]:
            connected.add((p["ref"], p["pin"]))

    no_connects = []
    for ref, pin in sorted(nc_pins - connected,
                           key=lambda rp: (_refsort(rp[0]), _pinsort(rp[1]))):
        no_connects.append({"ref": ref, "pin": pin,
                            "reason": "NC marker in source schematic"})

    # Floating pins: neither on a net nor NC-marked. Keep the document
    # complete (loadable/verifiable) but tag them loudly — they are findings.
    floating = []
    for ref, comp in components.items():
        for pin in comp["pins"]:
            key = (ref, pin)
            if key not in connected and key not in nc_pins:
                floating.append(key)
                no_connects.append({
                    "ref": ref, "pin": pin,
                    "reason": "EXTRACTED-FLOATING: pin unconnected in source, "
                              "no NC marker — review",
                })

    doc = {
        "project": sch.title if sch.title != "Untitled" else
                   os.path.splitext(os.path.basename(sch_path))[0],
        "source": os.path.basename(sch_path),
        "components": components,
        "nets": nets,
        "no_connects": no_connects,
    }
    summary = {
        "components": len(components),
        "nets": len(nets),
        "power_nets": sum(1 for n in nets.values() if n["type"] == "power"),
        "auto_named_nets": auto_named,
        "no_connect_markers": len(nc_pins),
        "unmatched_nc_markers": unmatched_nc,
        "floating_pins": [f"{r}.{p}" for r, p in floating],
        "stale_lib_cache": list(getattr(sch, "stale_lib_cache", [])),
        "unresolved_lib_ids": list(getattr(sch, "unresolved_lib_ids", [])),
    }
    return doc, summary


# ─── YAML emission (hand-rolled for the house 05b style) ────────────

def emit_yaml(doc: dict, sch_path: str) -> str:
    lines = []
    w = lines.append
    w("# " + "─" * 61)
    w(f"# Netlist — {doc['project']} (EXTRACTED from existing schematic)")
    w("# " + "─" * 61)
    w("# Decompiled by kicad-board-context/scripts/extract_netlist.py.")
    w("# Connectivity is what the schematic geometry actually says — not a")
    w("# statement of design intent. `class:` tags / current budgets are")
    w("# added by hand (ENRICH phase) after extraction.")
    w(f"# source_sha256: {_sha256(sch_path)}")
    w(f"# extracted_utc: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    w("# " + "─" * 61)
    w("")
    w(f"project: {_q(doc['project'])}")
    w(f"source: {_q(doc['source'])}")
    w("")
    w("components:")
    for ref in sorted(doc["components"], key=_refsort):
        comp = doc["components"][ref]
        w(f"  {ref}:")
        w(f"    part: {_q(comp['part'])}")
        pins = ", ".join(_q(p) for p in comp["pins"])
        w(f"    pins: [{pins}]")
    w("")
    w("nets:")

    def _netsort(item):
        name, net = item
        return (net["type"] != "power", name)

    for name, net in sorted(doc["nets"].items(), key=_netsort):
        w(f"  {_q(name)}:")
        w(f"    type: {net['type']}")
        w("    pins:")
        for p in net["pins"]:
            func = f", function: {_q(p['function'])}" if p["function"] else ""
            w(f"      - {{ ref: {p['ref']}, pin: {_q(p['pin'])}{func} }}")
        if net["power_symbols"]:
            w(f"    power_symbols: [{', '.join(_q(s) for s in net['power_symbols'])}]")
        if net["labels"]:
            w(f"    labels: [{', '.join(_q(s) for s in net['labels'])}]")
    w("")
    if doc["no_connects"]:
        w("no_connects:")
        for nc in doc["no_connects"]:
            w(f"  - {{ ref: {nc['ref']}, pin: {_q(nc['pin'])}, reason: {_q(nc['reason'])} }}")
    else:
        w("no_connects: []")
    w("")
    return "\n".join(lines)


# ─── self-verification ───────────────────────────────────────────────

def self_verify(yaml_text: str, sch_path: str, project_dir=None, extra_sym=None):
    """Round-trip: load the emitted YAML and verify it against the source."""
    from verify_netlist import verify, load_intended_netlist_from_string
    intended = load_intended_netlist_from_string(yaml_text)
    sch = load_kicad_sch(sch_path, project_dir=project_dir, extra_sym=extra_sym)
    return verify(intended, sch)


# ─── CLI ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Decompile a .kicad_sch into Stage-5b netlist YAML")
    ap.add_argument("schematic", help="path to .kicad_sch")
    ap.add_argument("-o", "--output", help="output YAML path (default stdout)")
    ap.add_argument("--json", action="store_true",
                    help="print extraction summary as JSON")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if the self-verification round-trip fails")
    ap.add_argument("--project-dir", default=None,
                    help="KiCad project dir — searched when resolving symbols "
                         "missing from the file's embedded cache")
    ap.add_argument("--sym-lib", action="append", default=None,
                    metavar="[NICK=]PATH",
                    help="extra symbol library for stale-cache resolution "
                         "(repeatable)")
    args = ap.parse_args()

    doc, summary = extract(args.schematic, project_dir=args.project_dir,
                           extra_sym=args.sym_lib)
    yaml_text = emit_yaml(doc, args.schematic)

    result = self_verify(yaml_text, args.schematic,
                         project_dir=args.project_dir, extra_sym=args.sym_lib)
    summary["self_verify_passed"] = result.passed
    summary["self_verify_errors"] = [i.message for i in result.errors]

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(yaml_text)
    else:
        print(yaml_text)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"extracted: {summary['components']} components, "
              f"{summary['nets']} nets ({summary['power_nets']} power, "
              f"{summary['auto_named_nets']} auto-named), "
              f"{summary['no_connect_markers']} NC markers", file=sys.stderr)
        if summary["floating_pins"]:
            print(f"WARNING: {len(summary['floating_pins'])} floating pin(s) "
                  f"(no net, no NC marker): {', '.join(summary['floating_pins'])}",
                  file=sys.stderr)
        if summary["stale_lib_cache"]:
            print(f"WARNING: stale lib_symbols cache — resolved from installed "
                  f"libraries: {', '.join(summary['stale_lib_cache'])} "
                  f"(re-save in KiCad to refresh)", file=sys.stderr)
        if summary["unresolved_lib_ids"]:
            print(f"WARNING: unresolvable lib_id(s) — components dropped, "
                  f"connectivity around them is UNRELIABLE: "
                  f"{', '.join(summary['unresolved_lib_ids'])} "
                  f"(pass --project-dir / --sym-lib)", file=sys.stderr)
        if not result.passed:
            print("WARNING: self-verification failed — the extracted netlist "
                  "does not round-trip against the source schematic. Suspect a "
                  "stale lib_symbols cache (see references/ingest.md):",
                  file=sys.stderr)
            for issue in result.errors:
                print(f"  - {issue.message}", file=sys.stderr)

    if args.strict and not result.passed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
