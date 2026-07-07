#!/usr/bin/env python3
"""extract_block.py — carve a proven subcircuit out of a validated board into
a self-contained, reusable block bundle (roadmap W1a).

A block is "active silicon with a defined, beyond-trivial function, validated
on a built board." This tool extracts one mechanically — the judgment calls
(which refs form the block, what its ports mean, its constraints) are inputs;
the carving, re-wiring, footprint promotion, and self-verification are code.

What it produces (blocks/{name}/):
  * ``sheet.kicad_sch``   — a KiCad hierarchical CHILD SHEET: the block's
    components at their as-validated positions, symbols embedded **verbatim**
    from the source file (real drawings + pin geometry), internal nets wired
    with local labels, power rails via power symbols (global by KiCad
    semantics), and every declared port as a **hierarchical label** — place
    the sheet on any parent schematic and KiCad turns the ports into sheet
    pins. Usable by hand in KiCad today; the W1b engine will compose it
    automatically.
  * ``netlist.yaml``      — the block's Stage-5b-format connectivity fragment
    (ports as nets named by port name) — the sheet self-verifies against it.
  * ``bom.md``            — flat BOM subset (identity fields carried verbatim).
  * ``block.yaml``        — the contract: ports, rails, provenance,
    dependencies. Judgment fields (per-port class/notes, constraints, rail
    budgets) are emitted as TODO for Claude/user to fill.
  * ``layout_intent.md``  — reserved stub (W3).

Dependency policy (ROADMAP W1a): symbols are embedded so the sheet has zero
symbol-library dependencies. Footprints may come from exactly two sources —
KiCad built-ins, or the repo's ``blocks/footprints/CircuitBlocks.pretty``;
any footprint living in a personal/fragmented library is **copied** into
CircuitBlocks at extraction time and the references rewritten, with
``copied_from`` provenance. Blocks never point into fragmented libraries.

Self-verify: after building, the sheet is validated (0 errors) and verified
against the emitted netlist fragment; on failure the bundle is REMOVED and
the tool exits 1 — a block that doesn't verify is not a block.

Net classification of the source board:
  * a net whose pins are all inside the ref set        → internal (local label)
  * a power net touching the block                     → rail (power symbols)
  * a net crossing the boundary                        → MUST be mapped to a
    port (--port NAME=NET[:shape]); unmapped boundary nets are an error that
    lists them — the port contract is explicit, never inferred.

CLI:
    python extract_block.py board.kicad_sch --name nau7802_frontend \
        --refs U3,C11,C12,R7 \
        --port SDA=I2C_SDA:bidirectional --port DRDY=NAU_DRDY:output \
        --validated-on "DualScale_Compact rev3" --bench-date 2026-06-15 \
        [--desc "..."] [--blocks-dir DIR] [--grid-layout]
        [--project-dir DIR] [--sym-lib SPEC] [--fp-lib SPEC] [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys

_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from generate_kicad_sch import KicadSchematic
from validate_kicad_sch import (
    load_kicad_sch, extract_netlist, validate,
    _extract_all_pin_positions, _coord_key,
)
from crosscheck_pcbway_plugin_bom import parse_symbols, _balanced_end, _refsort
from check_kicad_library import build_library_set, load_symbol_block, find_kicad_root
from bake_bom_fields import MANAGED_FIELDS_SCH

SKILL_DIR = os.path.normpath(os.path.join(_script_dir, ".."))
DEFAULT_BLOCKS_DIR = os.path.join(SKILL_DIR, "blocks")
CIRCUIT_BLOCKS_NICK = "CircuitBlocks"

PORT_SHAPES = {"input", "output", "bidirectional", "tri_state", "passive"}


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _q(s):
    out = str(s).replace("\\", "\\\\").replace('"', '\\"')
    out = "".join(f"\\x{ord(c):02x}" if ord(c) < 0x20 else c for c in out)
    return '"' + out + '"'


def _pinsort(pin):
    m = re.match(r"(\d+)", pin)
    return (int(m.group(1)), pin) if m else (10 ** 9, pin)


# ─── source-side helpers ─────────────────────────────────────────────

def _raw_lib_symbol(sch_text, lib_id):
    """The verbatim ``(symbol "{lib_id}" …)`` block from the source file's
    embedded lib_symbols cache, or None."""
    marker = f'(symbol "{lib_id}"'
    i = sch_text.find(marker)
    if i < 0:
        return None
    return sch_text[i:_balanced_end(sch_text, i)]


def _source_nc_pins(sch):
    """(ref, pin) pairs carrying an NC marker in the source schematic."""
    coord_to_pins = {}
    for ref, pnum, x, y, _t, _p in _extract_all_pin_positions(sch):
        coord_to_pins.setdefault(_coord_key(x, y), []).append((ref, pnum))
    nc = set()
    for marker in sch.no_connects:
        nc.update(coord_to_pins.get(_coord_key(marker.x, marker.y), []))
    return nc


def _power_symbol_refs(sch):
    out = set()
    for c in sch.components:
        lib = sch.lib_symbols.get(c.lib_id)
        if c.reference.startswith("#") or (lib and lib.is_power):
            out.add(c.reference)
    return out


# ─── net classification ──────────────────────────────────────────────

def classify_nets(sch, refs):
    """Split the source netlist relative to the block's ref set.

    Returns dict: net_name -> {kind: internal|boundary|rail,
                               block_pins: [(ref, pin)], is_power: bool}
    Only nets touching the block appear.
    """
    netlist = extract_netlist(sch)
    pwr_refs = _power_symbol_refs(sch)
    out = {}
    for name, entry in netlist.nets.items():
        block_pins = sorted([(r, p) for r, p in entry.pins if r in refs],
                            key=lambda rp: (_refsort(rp[0]), _pinsort(rp[1])))
        if not block_pins:
            continue
        external = [(r, p) for r, p in entry.pins
                    if r not in refs and r not in pwr_refs]
        if entry.is_power:
            kind = "rail"
        elif external:
            kind = "boundary"
        else:
            kind = "internal"
        out[name] = {"kind": kind, "block_pins": block_pins,
                     "is_power": entry.is_power}
    return out


def _is_floating_singleton(net_name, info):
    """An internal, single-pin, auto-named net = a pin floating in the source."""
    return (info["kind"] == "internal" and len(info["block_pins"]) == 1
            and net_name.startswith(("N$", "_NET_")))


# ─── footprint promotion ─────────────────────────────────────────────

def promote_footprints(fp_ids, blocks_dir, project_dir=None, extra_fp=None):
    """Resolve every footprint to builtin-or-CircuitBlocks; copy as needed.

    Returns (rewrite_map old_id -> new_id, dependencies list, errors list).
    """
    libraries = build_library_set(project_dir=project_dir, extra_fp=extra_fp)
    kicad_root = find_kicad_root()
    pretty = os.path.join(blocks_dir, "footprints", f"{CIRCUIT_BLOCKS_NICK}.pretty")

    rewrite, deps, errors = {}, [], []
    for fp_id in sorted(set(fp_ids)):
        if not fp_id or ":" not in fp_id:
            errors.append(f"footprint '{fp_id}' is empty/malformed")
            continue
        nick, name = fp_id.split(":", 1)
        if nick == CIRCUIT_BLOCKS_NICK:
            if not os.path.isfile(os.path.join(pretty, f"{name}.kicad_mod")):
                errors.append(f"'{fp_id}' claims CircuitBlocks but the .kicad_mod "
                              f"is not in the registry")
            else:
                deps.append({"ref": fp_id, "source": "blocks"})
            continue
        lib_dir = libraries.fp_libs.get(nick)
        if not lib_dir:
            errors.append(f"footprint library nickname '{nick}' (for {fp_id}) "
                          f"resolves nowhere — pass --project-dir / --fp-lib")
            continue
        if kicad_root and os.path.abspath(lib_dir).startswith(
                os.path.abspath(kicad_root)):
            deps.append({"ref": fp_id, "source": "builtin"})
            continue
        # Personal/fragmented library → promote the copy into CircuitBlocks.
        src = os.path.join(lib_dir, f"{name}.kicad_mod")
        if not os.path.isfile(src):
            errors.append(f"'{fp_id}': {src} does not exist")
            continue
        os.makedirs(pretty, exist_ok=True)
        shutil.copy2(src, os.path.join(pretty, f"{name}.kicad_mod"))
        new_id = f"{CIRCUIT_BLOCKS_NICK}:{name}"
        rewrite[fp_id] = new_id
        deps.append({"ref": new_id, "source": "blocks",
                     "copied_from": f"{nick} ({lib_dir})"})
    return rewrite, deps, errors


# ─── extraction ──────────────────────────────────────────────────────

def extract_block(sch_path, name, refs, ports, blocks_dir=DEFAULT_BLOCKS_DIR,
                  desc="", validated_on="", bench_date="", field_report="",
                  grid_layout=False, project_dir=None, extra_sym=None,
                  extra_fp=None, forced_rails=()):
    """Carve the block and write blocks/{name}/. Returns a summary dict.

    ports: {port_name: (source_net, shape)}
    forced_rails: net names to treat as power rails even when the donor board
    names them with plain labels instead of power symbols (common on
    hand-drawn boards — DualScale's 3V3 is a label net).
    Raises ValueError on any contract violation (bundle is not written).
    """
    with open(sch_path, "r", encoding="utf-8") as f:
        sch_text = f.read()
    sch = load_kicad_sch(sch_path, project_dir=project_dir, extra_sym=extra_sym)

    known = {c.reference for c in sch.components}
    missing = [r for r in refs if r not in known]
    if missing:
        raise ValueError(f"refs not in schematic: {', '.join(missing)}")
    for pname, (_net, shape) in ports.items():
        if shape not in PORT_SHAPES:
            raise ValueError(f"port {pname}: shape '{shape}' not in "
                             f"{sorted(PORT_SHAPES)}")

    nets = classify_nets(sch, set(refs))
    for rail in forced_rails:
        if rail not in nets:
            raise ValueError(f"--rail {rail}: net does not touch the block")
        nets[rail]["kind"] = "rail"

    # Internal nets that only exist as source-board auto-names (_NET_7, N$…)
    # get stable, semantic names anchored on the block's IC pin — auto-names
    # are position/order-derived and must not travel into a reusable block.
    renames = {}
    for net_name, info in nets.items():
        if info["kind"] == "internal" and \
                re.match(r"^(_NET_\d+|N\$)", net_name) and \
                not _is_floating_singleton(net_name, info):
            anchor = min(info["block_pins"],
                         key=lambda rp: (rp[0][0] not in "UQ", _refsort(rp[0]),
                                         _pinsort(rp[1])))
            renames[net_name] = f"N_{anchor[0]}_{anchor[1]}"
    for old, new in renames.items():
        nets[new] = nets.pop(old)

    net_to_port = {net: (pname, shape) for pname, (net, shape) in ports.items()}

    # Contract checks: every boundary net mapped, every mapped net real.
    unmapped = [n for n, info in nets.items()
                if info["kind"] == "boundary" and n not in net_to_port]
    if unmapped:
        raise ValueError(
            "boundary nets with no --port mapping (the port contract is "
            "explicit, never inferred): " + ", ".join(sorted(unmapped)))
    phantom = [f"{p}={n}" for p, (n, _s) in ports.items() if n not in nets]
    if phantom:
        raise ValueError("ports mapped to nets that don't touch the block: "
                         + ", ".join(sorted(phantom)))
    railed = [f"{p}={n}" for p, (n, _s) in ports.items()
              if nets[n]["kind"] == "rail"]
    if railed:
        raise ValueError("power nets cannot be ports (rails connect globally "
                         "via power symbols): " + ", ".join(sorted(railed)))

    rails = sorted(n for n, info in nets.items() if info["kind"] == "rail")

    # Footprint promotion (before building the sheet, so placements carry
    # the rewritten ids).
    comps_by_ref = {}
    for c in sch.components:
        if c.reference in refs and c.reference not in comps_by_ref:
            comps_by_ref[c.reference] = c
    rewrite, deps, fp_errors = promote_footprints(
        [c.footprint for c in comps_by_ref.values()], blocks_dir,
        project_dir=project_dir, extra_fp=extra_fp)
    if fp_errors:
        raise ValueError("footprint promotion failed:\n  " +
                         "\n  ".join(fp_errors))

    # ── Build the child sheet ──
    block = KicadSchematic(title=name, rev="1.0")
    for rail in rails:
        block.add_lib_symbol_power(rail)

    src_bom = {c["ref"]: c for c in parse_symbols(sch_text)}

    for j, ref in enumerate(sorted(refs, key=_refsort)):
        comp = comps_by_ref[ref]
        if comp.lib_id not in block.lib_symbols:
            raw = _raw_lib_symbol(sch_text, comp.lib_id)
            if raw is None:  # stale cache — the loader resolved it; fetch raw
                raw = load_symbol_block(comp.lib_id, project_dir=project_dir,
                                        extra_sym=extra_sym)
            if raw is None:
                raise ValueError(f"{ref}: cannot obtain symbol block for "
                                 f"'{comp.lib_id}'")
            block.add_lib_symbol_from_block(comp.lib_id, raw)
        extra = {k: v for k, v in comp.extra_properties.items()
                 if k in MANAGED_FIELDS_SCH and v}
        if grid_layout:
            is_ic = ref[0] in ("U", "Q")
            x = 50.8 + (j % 6) * 38.1
            y = 63.5 if is_ic else 127.0 + (j // 6) * 25.4
        else:
            x, y = comp.x, comp.y
        block.place_component(
            comp.lib_id, ref, comp.value, x, y,
            rotation=0 if grid_layout else comp.rotation,
            footprint=rewrite.get(comp.footprint, comp.footprint),
            **extra)

    # ── Wire it: rails → power symbols, ports → hierarchical labels,
    #             internal → local labels ──
    # A declared port wins over the boundary/internal classification — a
    # block's interface may include a net with no external connection in the
    # donor board (e.g. a sense input the donor never used).
    for net_name, info in sorted(nets.items()):
        for ref, pin in info["block_pins"]:
            if net_name in net_to_port:
                pname, shape = net_to_port[net_name]
                block.hlabel_at_pin(ref, pin, pname, shape=shape)
            elif info["kind"] == "rail":
                block.power_at_pin(ref, pin, net_name)
            elif _is_floating_singleton(net_name, info):
                # a single-pin auto-named "net" is a floating pin in the
                # source — carry it as an explicit NC, not a one-pin net
                block.nc_at_pin(ref, pin)
            else:
                block.label_at_pin(ref, pin, net_name)

    # NC markers from the source
    nc_pins = {(r, p) for r, p in _source_nc_pins(sch) if r in refs}
    netted = {(r, p) for info in nets.values() for r, p in info["block_pins"]}
    for ref, pin in sorted(nc_pins - netted,
                           key=lambda rp: (_refsort(rp[0]), _pinsort(rp[1]))):
        block.nc_at_pin(ref, pin)

    # ── Write the bundle ──
    out_dir = os.path.join(blocks_dir, name)
    os.makedirs(out_dir, exist_ok=True)
    sheet_path = os.path.join(out_dir, "sheet.kicad_sch")
    block.save(sheet_path)

    netlist_yaml = _emit_netlist(name, sch_path, nets, net_to_port, nc_pins - netted,
                                 comps_by_ref, block)
    with open(os.path.join(out_dir, "netlist.yaml"), "w", encoding="utf-8",
              newline="\n") as f:
        f.write(netlist_yaml)

    with open(os.path.join(out_dir, "bom.md"), "w", encoding="utf-8",
              newline="\n") as f:
        f.write(_emit_bom(name, refs, src_bom, comps_by_ref, rewrite))

    with open(os.path.join(out_dir, "block.yaml"), "w", encoding="utf-8",
              newline="\n") as f:
        f.write(_emit_contract(name, desc, sch_path, refs, ports, rails, deps,
                               validated_on, bench_date, field_report))

    intent = os.path.join(out_dir, "layout_intent.md")
    if not os.path.exists(intent):
        with open(intent, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"# Layout intent — {name}\n\n"
                    f"Reserved for roadmap W3 (placement/width/thermal intent "
                    f"carried by the block). TODO.\n")

    # ── Self-verify: a block that doesn't verify is not a block ──
    errors = _self_verify(sheet_path, os.path.join(out_dir, "netlist.yaml"))
    if errors:
        shutil.rmtree(out_dir)
        raise ValueError("self-verify FAILED (bundle removed):\n  " +
                         "\n  ".join(errors) +
                         "\n  (dense as-validated placement can collide label "
                         "stubs — try --grid-layout)")

    return {"block": name, "dir": out_dir, "components": len(refs),
            "ports": sorted(ports), "rails": rails,
            "internal_nets": sorted(n for n, i in nets.items()
                                    if i["kind"] == "internal"),
            "footprints_promoted": sorted(rewrite.values()),
            "dependencies": deps}


def _self_verify(sheet_path, netlist_path):
    from verify_netlist import verify, load_intended_netlist
    errors = []
    sheet = load_kicad_sch(sheet_path, resolve_from_libraries=False)
    result = validate(sheet)
    errors += [f"validate: {i.message}" for i in result.issues
               if i.severity == "error"]
    vres = verify(load_intended_netlist(netlist_path), sheet)
    errors += [f"verify_netlist: {i.message}" for i in vres.issues
               if i.severity == "error"]
    return errors


# ─── emitters ────────────────────────────────────────────────────────

def _emit_netlist(name, sch_path, nets, net_to_port, extra_nc, comps, block):
    lines = []
    w = lines.append
    w("# Block netlist fragment (Stage-5b format) — EXTRACTED, self-verified")
    w(f"# source board: {os.path.basename(sch_path)} "
      f"sha256 {_sha256(sch_path)[:16]}…")
    w(f"project: {_q(name)}")
    w(f"source: {_q(os.path.basename(sch_path))}")
    w("")
    w("# Ports — boundary nets renamed to their port names (the sheet's")
    w("# hierarchical labels). dir is the KiCad sheet-pin shape.")
    w("ports:")
    for net, (pname, shape) in sorted(net_to_port.items(), key=lambda kv: kv[1][0]):
        w(f"  - {{ name: {_q(pname)}, dir: {shape}, source_net: {_q(net)} }}")
    w("")
    w("components:")
    for ref in sorted(comps, key=_refsort):
        comp = comps[ref]
        pins = sorted({p.number for p in block.lib_symbols[comp.lib_id].pins},
                      key=_pinsort)
        w(f"  {ref}:")
        w(f"    part: {_q(comp.value)}")
        w(f"    pins: [{', '.join(_q(p) for p in pins)}]")
    w("")
    w("nets:")
    nc_like = []
    for net_name, info in sorted(nets.items()):
        is_port = net_name in net_to_port
        if not is_port and _is_floating_singleton(net_name, info):
            nc_like += info["block_pins"]
            continue
        out_name = net_to_port[net_name][0] if is_port else net_name
        w(f"  {_q(out_name)}:")
        w(f"    type: {'power' if info['kind'] == 'rail' else 'signal'}")
        w("    pins:")
        for ref, pin in info["block_pins"]:
            w(f"      - {{ ref: {ref}, pin: {_q(pin)} }}")
        if info["kind"] == "rail":
            w(f"    power_symbols: [{_q(net_name)}]")
        else:
            w(f"    labels: [{_q(out_name)}]")
    w("")
    all_nc = sorted(set(nc_like) | set(extra_nc),
                    key=lambda rp: (_refsort(rp[0]), _pinsort(rp[1])))
    if all_nc:
        w("no_connects:")
        for ref, pin in all_nc:
            w(f"  - {{ ref: {ref}, pin: {_q(pin)}, reason: \"NC in source board\" }}")
    else:
        w("no_connects: []")
    w("")
    return "\n".join(lines)


def _emit_bom(name, refs, src_bom, comps, rewrite):
    cols = ["Ref", "Value", "Part Number", "Manufacturer", "Package",
            "Footprint", "Type", "Notes"]
    lines = [f"# Flat BOM — block {name} (extracted subset)", "",
             "| " + " | ".join(cols) + " |",
             "|" + "|".join("-" * (len(c) + 2) for c in cols) + "|"]
    for ref in sorted(refs, key=_refsort):
        s = src_bom.get(ref, {})
        fp = comps[ref].footprint
        fp = rewrite.get(fp, fp)
        lines.append("| " + " | ".join([
            ref, s.get("value", comps[ref].value), s.get("mpn", ""),
            s.get("Manufacturer", ""), s.get("pack", ""), fp,
            "DNP" if s.get("dnp") else "",
            f"LCSC: {s['LCSC']}" if s.get("LCSC") else "",
        ]) + " |")
    lines.append("")
    return "\n".join(lines)


def _emit_contract(name, desc, sch_path, refs, ports, rails, deps,
                   validated_on, bench_date, field_report):
    lines = []
    w = lines.append
    w(f"# block.yaml — contract for block '{name}' (W1a)")
    w("# Mechanical fields are extracted; judgment fields are marked TODO —")
    w("# fill them (Claude + user) before first reuse. check_block.py gates.")
    w(f"name: {_q(name)}")
    desc_val = _q(desc) if desc else _q("TODO: one sentence - what this block does")
    w(f"description: {desc_val}")
    w("provenance:")
    w(f"  validated_on: {_q(validated_on) if validated_on else 'null  # TODO: board + rev'}")
    w(f"  bench_date: {_q(bench_date) if bench_date else 'null'}")
    w(f"  field_report: {_q(field_report) if field_report else 'null'}")
    w(f"  extracted_from: {_q(os.path.basename(sch_path))}")
    w(f"  source_sha256: {_q(_sha256(sch_path))}")
    w(f"refs: [{', '.join(sorted(refs, key=_refsort))}]")
    w("ports:")
    for pname, (net, shape) in sorted(ports.items()):
        w(f"  - {{ name: {_q(pname)}, dir: {shape}, class: \"\","
          f" note: \"TODO\" }}  # from net {net}")
    w("rails:")
    for rail in rails:
        w(f"  - {{ name: {_q(rail)}, budget_ma: null }}  # TODO: budget")
    w("constraints: []  # TODO: e.g. \"I2C addr 0x2A fixed — one per bus\"")
    w("dependencies:")
    w("  footprints:")
    for d in deps:
        extra = f", copied_from: {_q(d['copied_from'])}" if "copied_from" in d else ""
        w(f"    - {{ ref: {_q(d['ref'])}, source: {d['source']}{extra} }}")
    w("")
    return "\n".join(lines)


# ─── CLI ─────────────────────────────────────────────────────────────

def _parse_port(spec):
    # NAME=NET[:shape]
    if "=" not in spec:
        raise argparse.ArgumentTypeError(f"--port '{spec}': expected NAME=NET[:shape]")
    pname, rest = spec.split("=", 1)
    net, _, shape = rest.partition(":")
    return pname.strip(), (net.strip(), (shape.strip() or "bidirectional"))


def main():
    ap = argparse.ArgumentParser(
        description="Extract a validated subcircuit into a reusable block bundle")
    ap.add_argument("schematic", help="the validated board's .kicad_sch")
    ap.add_argument("--name", required=True, help="block name (dir under blocks/)")
    ap.add_argument("--refs", required=True,
                    help="comma-separated refs forming the block")
    ap.add_argument("--port", action="append", default=[], type=_parse_port,
                    metavar="NAME=NET[:shape]",
                    help="map a boundary net to a port (repeatable); shape: "
                         "input|output|bidirectional|tri_state|passive")
    ap.add_argument("--rail", action="append", default=[], metavar="NET",
                    help="treat this net as a power rail even though the donor "
                         "board names it with a label, not a power symbol "
                         "(repeatable)")
    ap.add_argument("--desc", default="", help="one-line block description")
    ap.add_argument("--validated-on", default="", help="board + rev (provenance)")
    ap.add_argument("--bench-date", default="")
    ap.add_argument("--field-report", default="")
    ap.add_argument("--blocks-dir", default=DEFAULT_BLOCKS_DIR)
    ap.add_argument("--grid-layout", action="store_true",
                    help="re-place components on a clean grid instead of the "
                         "as-validated positions (use if self-verify reports "
                         "stub collisions)")
    ap.add_argument("--project-dir", default=None)
    ap.add_argument("--sym-lib", action="append", default=None,
                    metavar="[NICK=]PATH")
    ap.add_argument("--fp-lib", action="append", default=None,
                    metavar="[NICK=]PATH")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    refs = [r.strip() for r in args.refs.split(",") if r.strip()]
    ports = dict(args.port)

    try:
        summary = extract_block(
            args.schematic, args.name, refs, ports,
            blocks_dir=args.blocks_dir, desc=args.desc,
            validated_on=args.validated_on, bench_date=args.bench_date,
            field_report=args.field_report, grid_layout=args.grid_layout,
            project_dir=args.project_dir, extra_sym=args.sym_lib,
            extra_fp=args.fp_lib, forced_rails=args.rail)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"block '{summary['block']}' extracted -> {summary['dir']}")
        print(f"  {summary['components']} components, ports: "
              f"{', '.join(summary['ports'])}, rails: {', '.join(summary['rails'])}")
        if summary["footprints_promoted"]:
            print(f"  footprints promoted into CircuitBlocks: "
                  f"{', '.join(summary['footprints_promoted'])}")
        print("  self-verified: sheet validates + matches netlist fragment")
        print("  NEXT: fill the TODO judgment fields in block.yaml, then run "
              "check_block.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
