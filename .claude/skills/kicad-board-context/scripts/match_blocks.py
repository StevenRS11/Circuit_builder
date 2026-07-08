"""match_blocks.py — reviewer block recognition (ROADMAP W1c).

Deterministic, read-only: matches fragments of an ingested board netlist
(the Stage-5b YAML written by extract_netlist.py) against the proven-block
registry (kicad-schematic-gen/blocks/), and reports every recognized instance
with its component/net correspondence and every deviation from the validated
block. The highest-value sentence brownfield review can produce is exactly
this report's job: "U3/C11-C14/R7 is your NAU7802 block as validated on
DualScale, except C12 is 10nF where the block says 100nF."

Doctrine: this script does the READING (graph correspondence over two netlist
documents); Claude does the INTERPRETING (is a deviation a bug, an improvement,
or an accepted change — and what the block's constraints imply for the board).
It makes no design decisions and never modifies anything.

How matching works (all deterministic):
  1. Anchor on active silicon: each block's IC/transistor refs (U/Q), seeded by
     part-name match against board components (fuzzy: "NAU7802" ~ "NAU7802SGI").
  2. Grow the correspondence from the anchor: matched pins bind block nets to
     board nets; bound nets identify further components (same ref prefix, same
     pin set, consistent connectivity — 2-pin passives may be flipped).
  3. Rail pass: components attached only to power rails (decoupling) match by
     value across the mapped rail pair — identity is inherently ambiguous
     there, and the report says so.
  4. Everything the block has that the board doesn't (and vice versa on the
     block's internal nets) becomes a deviation, as do value mismatches,
     merged/split nets, and wired pins the block declares no-connect.

CLI:
  python match_blocks.py claude_context/netlist.yaml [--blocks-dir DIR]
      [--min-match 0.5] [--json]
Exit code 0 (this is a report, not a gate); 1 on unreadable inputs.
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field

import _paths  # noqa: F401  (puts kicad-schematic-gen/scripts on sys.path)

import yaml

from cross_check_bom import values_match
from verify_netlist import load_intended_netlist

DEFAULT_BLOCKS_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "kicad-schematic-gen", "blocks"))

_REF_PREFIX_RE = re.compile(r"^([A-Za-z]+)")

# Ref prefixes that anchor a match (active silicon — a block is defined by it).
_ANCHOR_PREFIXES = ("U", "Q")

# Deviation kinds that are informational, not disagreements with the block.
_INFO_KINDS = {"ambiguous_identity"}


# ─── Data model ──────────────────────────────────────────────────────
@dataclass
class BlockDef:
    name: str
    dir: str
    contract: dict                 # parsed block.yaml
    netlist: object                # IntendedNetlist (Stage-5b fragment)
    ports: list                    # port names (fragment nets carry these names)
    power_nets: set                # fragment nets with type: power
    nc_pins: set                   # {(ref, pin)} declared no-connect


@dataclass
class Deviation:
    kind: str        # value_mismatch | missing_component | connectivity_mismatch |
                     # nets_merged_on_board | unwired_pin | nc_violated |
                     # extra_attachment | ambiguous_identity
    message: str
    block_ref: str = ""
    board_ref: str = ""
    net: str = ""


@dataclass
class BlockMatch:
    block: str
    anchor_block_ref: str
    anchor_board_ref: str
    comp_map: dict = field(default_factory=dict)   # block ref -> board ref
    port_map: dict = field(default_factory=dict)   # port -> board net (or None)
    rail_map: dict = field(default_factory=dict)   # block rail -> board net (or None)
    deviations: list = field(default_factory=list)
    matched: int = 0
    total: int = 0

    @property
    def fraction(self):
        return self.matched / self.total if self.total else 0.0

    @property
    def quality(self):
        real = [d for d in self.deviations if d.kind not in _INFO_KINDS]
        if self.matched == self.total and not real:
            return "exact"
        if self.matched == self.total:
            return "match_with_deviations"
        return "partial"


# ─── Registry loading ────────────────────────────────────────────────
def load_registry(blocks_dir=None):
    """Load every block bundle's contract + netlist fragment. Sorted by name."""
    blocks_dir = blocks_dir or DEFAULT_BLOCKS_DIR
    blocks = []
    if not os.path.isdir(blocks_dir):
        return blocks
    for entry in sorted(os.listdir(blocks_dir)):
        bdir = os.path.join(blocks_dir, entry)
        contract_path = os.path.join(bdir, "block.yaml")
        netlist_path = os.path.join(bdir, "netlist.yaml")
        if not (os.path.isfile(contract_path) and os.path.isfile(netlist_path)):
            continue
        with open(contract_path, "r", encoding="utf-8") as f:
            contract = yaml.safe_load(f) or {}
        netlist = load_intended_netlist(netlist_path)
        with open(netlist_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        ports = [str(p.get("name", "")) for p in (raw.get("ports") or [])]
        blocks.append(BlockDef(
            name=str(contract.get("name", entry)),
            dir=bdir,
            contract=contract,
            netlist=netlist,
            ports=[p for p in ports if p],
            power_nets={n for n, net in netlist.nets.items()
                        if net.net_type == "power"},
            nc_pins={(nc.ref, nc.pin) for nc in netlist.no_connects},
        ))
    return blocks


# ─── Helpers ─────────────────────────────────────────────────────────
def _pin2net(netlist):
    """{(ref, pin): net_name} for an IntendedNetlist."""
    out = {}
    for name, net in netlist.nets.items():
        for p in net.pins:
            out[(p.ref, p.pin)] = name
    return out


def _prefix(ref):
    m = _REF_PREFIX_RE.match(ref)
    return m.group(1).upper() if m else ""


def _norm_part(part):
    return re.sub(r"[^A-Z0-9]", "", str(part).upper())


def parts_equal(a, b):
    """Fuzzy part-name equality: 'NAU7802' matches 'NAU7802SGI-REEL'."""
    na, nb = _norm_part(a), _norm_part(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return min(len(na), len(nb)) >= 4 and (na in nb or nb in na)


# ─── Core matcher ────────────────────────────────────────────────────
class _Grower:
    """Grows one block-instance correspondence from a seeded anchor pair."""

    def __init__(self, block, board, consumed):
        self.block = block
        self.board = board
        self.bp2n = _pin2net(block.netlist)
        self.dp2n = _pin2net(board)
        self.comp_map = {}
        self.used = set(consumed)
        self.net_map = {}          # block net -> board net
        self.deviations = []

    # -- binding -------------------------------------------------------
    def absorb(self, bref, dref, pin_map):
        self.comp_map[bref] = dref
        self.used.add(dref)
        for bpin, dpin in sorted(pin_map.items()):
            bn = self.bp2n.get((bref, bpin))
            dn = self.dp2n.get((dref, dpin))
            if bn is None:
                if (bref, bpin) in self.block.nc_pins and dn is not None:
                    self.deviations.append(Deviation(
                        "nc_violated",
                        f"block declares {bref} pin {bpin} no-connect, but the "
                        f"board wires {dref} pin {dpin} to net '{dn}' — check "
                        f"the block's constraints before assuming this is OK",
                        block_ref=bref, board_ref=dref, net=dn))
                continue
            if dn is None:
                self.deviations.append(Deviation(
                    "unwired_pin",
                    f"board leaves {dref} pin {dpin} unwired where the block "
                    f"connects {bref} pin {bpin} to net '{bn}'",
                    block_ref=bref, board_ref=dref, net=bn))
                continue
            prev = self.net_map.get(bn)
            if prev is None:
                if dn in self.net_map.values():
                    other = sorted(k for k, v in self.net_map.items() if v == dn)
                    self.deviations.append(Deviation(
                        "nets_merged_on_board",
                        f"board net '{dn}' carries both block net '{bn}' and "
                        f"block net '{other[0]}' — nets the block keeps "
                        f"separate are joined on this board",
                        block_ref=bref, board_ref=dref, net=dn))
                self.net_map[bn] = dn
            elif prev != dn:
                self.deviations.append(Deviation(
                    "connectivity_mismatch",
                    f"{dref} pin {dpin} lands on board net '{dn}' but block "
                    f"net '{bn}' was already bound to '{prev}'",
                    block_ref=bref, board_ref=dref, net=bn))

    # -- candidate discovery --------------------------------------------
    def _orientations(self, bref, dref):
        """Valid pin_maps (block pin -> board pin) for pairing bref with dref."""
        bpins = self.block.netlist.components[bref].pins
        dpins = self.board.components[dref].pins
        if set(bpins) != set(dpins):
            return []
        orders = [{p: p for p in bpins}]
        if len(bpins) == 2:
            orders.append({bpins[0]: bpins[1], bpins[1]: bpins[0]})
        valid = []
        for pin_map in orders:
            tentative = {}
            ok = True
            for bpin, dpin in pin_map.items():
                bn = self.bp2n.get((bref, bpin))
                dn = self.dp2n.get((dref, dpin))
                if bn is None:
                    continue           # NC/unnetted in block — judged at absorb
                bound = self.net_map.get(bn, tentative.get(bn))
                if bound is not None:
                    if dn != bound:
                        ok = False
                        break
                else:
                    if dn is None:
                        ok = False     # block nets this pin; board floats it
                        break
                    tentative[bn] = dn
            if ok:
                valid.append(pin_map)
        return valid

    def _candidates(self, bref):
        """Board components that could be bref, found via a bound non-power net."""
        bcomp = self.block.netlist.components[bref]
        for bpin in bcomp.pins:
            bn = self.bp2n.get((bref, bpin))
            if bn is None or bn in self.block.power_nets:
                continue
            dn = self.net_map.get(bn)
            if dn is None or dn not in self.board.nets:
                continue
            pool = sorted({p.ref for p in self.board.nets[dn].pins}
                          - self.used - set(self.comp_map.values()))
            out = []
            for dref in pool:
                if _prefix(dref) != _prefix(bref):
                    continue
                if dref not in self.board.components:
                    continue
                orients = self._orientations(bref, dref)
                if orients:
                    out.append((dref, orients[0]))
            if out:
                return out
        return []

    # -- passes ----------------------------------------------------------
    def run_signal_pass(self):
        """Fixpoint over signal-net-identified components."""
        while True:
            remaining = [r for r in sorted(self.block.netlist.components)
                         if r not in self.comp_map]
            accepted = False
            deferred = []
            for bref in remaining:
                cands = self._candidates(bref)
                if len(cands) == 1:
                    self.absorb(bref, cands[0][0], cands[0][1])
                    accepted = True
                elif len(cands) > 1:
                    deferred.append((bref, cands))
            if accepted:
                continue
            if deferred:
                # No unambiguous progress: take the deterministically-first
                # ambiguous pairing (prefer a value match) and say so.
                bref, cands = deferred[0]
                bpart = self.block.netlist.components[bref].part
                pref = [c for c in cands
                        if values_match(bpart, self.board.components[c[0]].part)]
                dref, pin_map = (pref or cands)[0]
                others = ", ".join(c[0] for c in cands if c[0] != dref)
                self.deviations.append(Deviation(
                    "ambiguous_identity",
                    f"{bref} matched to {dref}, but {others} sits on the same "
                    f"nets and would also fit — identity chosen by value/order",
                    block_ref=bref, board_ref=dref))
                self.absorb(bref, dref, pin_map)
                continue
            break

    def run_rail_pass(self):
        """Match rail-only components (decoupling) by value across mapped rails."""
        for bref in sorted(r for r in self.block.netlist.components
                           if r not in self.comp_map):
            bcomp = self.block.netlist.components[bref]
            target = set()
            rail_only = bool(bcomp.pins)
            for bpin in bcomp.pins:
                bn = self.bp2n.get((bref, bpin))
                if bn is None or bn not in self.block.power_nets \
                        or bn not in self.net_map:
                    rail_only = False
                    break
                target.add(self.net_map[bn])
            if not rail_only:
                continue
            pool = []
            for dref in sorted(self.board.components):
                if dref in self.used or dref in self.comp_map.values():
                    continue
                if _prefix(dref) != _prefix(bref):
                    continue
                dcomp = self.board.components[dref]
                if len(dcomp.pins) != len(bcomp.pins):
                    continue
                dnets = {self.dp2n.get((dref, p)) for p in dcomp.pins}
                if dnets != target:
                    continue
                if values_match(bcomp.part, dcomp.part):
                    pool.append(dref)
            if pool:
                dref = pool[0]
                self.deviations.append(Deviation(
                    "ambiguous_identity",
                    f"{bref} ({bcomp.part} across {'/'.join(sorted(target))}) "
                    f"matched to {dref} by value+rails only — rail-attached "
                    f"identity is inherently ambiguous",
                    block_ref=bref, board_ref=dref))
                self.absorb(bref, dref, {p: p for p in bcomp.pins})
            # else: falls through to missing_component

    def finish(self, anchor_bref, anchor_dref):
        """Missing components, value mismatches, extra attachments → BlockMatch."""
        block = self.block
        for bref in sorted(r for r in block.netlist.components
                           if r not in self.comp_map):
            part = block.netlist.components[bref].part
            self.deviations.append(Deviation(
                "missing_component",
                f"block component {bref} ({part}) has no counterpart on the "
                f"board", block_ref=bref))

        for bref, dref in sorted(self.comp_map.items()):
            bpart = block.netlist.components[bref].part
            dpart = self.board.components[dref].part
            if not values_match(bpart, dpart) and not parts_equal(bpart, dpart):
                self.deviations.append(Deviation(
                    "value_mismatch",
                    f"{dref} is '{dpart}' where the block says {bref} = "
                    f"'{bpart}'", block_ref=bref, board_ref=dref))

        matched_board = set(self.comp_map.values())
        for bn in sorted(block.netlist.nets):
            if bn in block.power_nets or bn in block.ports:
                continue
            dn = self.net_map.get(bn)
            if dn is None or dn not in self.board.nets:
                continue
            extras = sorted({p.ref for p in self.board.nets[dn].pins}
                            - matched_board)
            if extras:
                self.deviations.append(Deviation(
                    "extra_attachment",
                    f"board net '{dn}' (block-internal net '{bn}') also "
                    f"connects {', '.join(extras)} — foreign parts on a net "
                    f"the block treats as internal", net=dn))

        m = BlockMatch(
            block=block.name,
            anchor_block_ref=anchor_bref, anchor_board_ref=anchor_dref,
            comp_map=dict(sorted(self.comp_map.items())),
            port_map={p: self.net_map.get(p) for p in block.ports},
            rail_map={r: self.net_map.get(r)
                      for r in sorted(block.power_nets)},
            deviations=self.deviations,
            matched=len(self.comp_map),
            total=len(block.netlist.components),
        )
        return m


def match_block(block, board, consumed=None, min_fraction=0.5):
    """Find every instance of one block in the board netlist.

    Returns (matches, anchor_only): matches meet min_fraction; anchor_only
    lists board parts whose silicon matches but whose circuit doesn't grow.
    """
    consumed = set() if consumed is None else consumed
    anchors = sorted(
        (r for r in block.netlist.components if _prefix(r) in _ANCHOR_PREFIXES),
        key=lambda r: (-len(block.netlist.components[r].pins), r))
    if not anchors:
        return [], []
    seed = anchors[0]
    seed_part = block.netlist.components[seed].part
    matches, anchor_only = [], []
    for dref in sorted(board.components):
        if dref in consumed:
            continue
        if not parts_equal(seed_part, board.components[dref].part):
            continue
        g = _Grower(block, board, consumed)
        pin_map = {p: p for p in block.netlist.components[seed].pins
                   if p in board.components[dref].pins}
        if len(pin_map) < len(block.netlist.components[seed].pins):
            anchor_only.append({
                "block": block.name, "board_ref": dref,
                "note": f"{dref} looks like {seed_part} but its pin set "
                        f"differs from the block's {seed} — different package?"})
            continue
        g.absorb(seed, dref, pin_map)
        g.run_signal_pass()
        g.run_rail_pass()
        m = g.finish(seed, dref)
        if m.fraction >= min_fraction:
            matches.append(m)
            consumed.update(m.comp_map.values())
        else:
            anchor_only.append({
                "block": block.name, "board_ref": dref,
                "note": f"{dref} is a {seed_part} but only "
                        f"{m.matched}/{m.total} block components matched "
                        f"around it — same silicon, different circuit"})
    return matches, anchor_only


def match_board(board, blocks, min_fraction=0.5):
    """Match every registry block against the board. Board components are
    consumed by earlier matches (registry order is name-sorted, deterministic)."""
    consumed = set()
    all_matches, all_anchor_only = [], []
    for block in blocks:
        matches, anchor_only = match_block(block, board, consumed, min_fraction)
        all_matches.extend(matches)
        all_anchor_only.extend(anchor_only)
    return all_matches, all_anchor_only


# ─── Reporting ───────────────────────────────────────────────────────
def _provenance_line(block_defs, name):
    for b in block_defs:
        if b.name == name:
            prov = b.contract.get("provenance") or {}
            v = prov.get("validated_on") or "?"
            d = prov.get("bench_date") or "?"
            return f"validated on {v} (bench {d})"
    return ""


def _constraints(block_defs, name):
    for b in block_defs:
        if b.name == name:
            return [str(c) for c in (b.contract.get("constraints") or [])]
    return []


def format_report(matches, anchor_only, block_defs, source):
    lines = ["=" * 60, "PROVEN-BLOCK RECOGNITION", "=" * 60,
             f"Board netlist: {source}",
             f"Registry blocks checked: {len(block_defs)}", ""]
    if not matches and not anchor_only:
        lines.append("No registry block recognized on this board.")
    for m in matches:
        lines.append(f"MATCH [{m.quality}] block '{m.block}' — anchor "
                     f"{m.anchor_board_ref} (= block {m.anchor_block_ref}), "
                     f"{m.matched}/{m.total} components")
        prov = _provenance_line(block_defs, m.block)
        if prov:
            lines.append(f"  provenance: {prov}")
        lines.append("  ports: " + ", ".join(
            f"{p} -> {n or 'UNRESOLVED'}" for p, n in sorted(m.port_map.items())))
        lines.append("  rails: " + ", ".join(
            f"{r} -> {n or 'UNRESOLVED'}" for r, n in sorted(m.rail_map.items())))
        real = [d for d in m.deviations if d.kind not in _INFO_KINDS]
        notes = [d for d in m.deviations if d.kind in _INFO_KINDS]
        if real:
            lines.append("  DEVIATIONS from the validated block:")
            for d in real:
                lines.append(f"    [{d.kind}] {d.message}")
        if notes:
            lines.append("  notes:")
            for d in notes:
                lines.append(f"    [{d.kind}] {d.message}")
        cons = _constraints(block_defs, m.block)
        if cons:
            lines.append("  block constraints (judgment — check each against "
                         "this board):")
            for c in cons:
                lines.append(f"    - {c}")
        lines.append("")
    for a in anchor_only:
        lines.append(f"ANCHOR-ONLY: {a['note']}")
    lines.append("=" * 60)
    return "\n".join(lines)


def result_to_dict(matches, anchor_only, block_defs, source):
    return {
        "source": source,
        "blocks_checked": [b.name for b in block_defs],
        "matches": [{
            "block": m.block,
            "quality": m.quality,
            "anchor": {"block_ref": m.anchor_block_ref,
                       "board_ref": m.anchor_board_ref},
            "matched": m.matched, "total": m.total,
            "fraction": round(m.fraction, 3),
            "component_map": m.comp_map,
            "port_map": m.port_map,
            "rail_map": m.rail_map,
            "deviations": [{"kind": d.kind, "message": d.message,
                            "block_ref": d.block_ref, "board_ref": d.board_ref,
                            "net": d.net} for d in m.deviations],
            "constraints": _constraints(block_defs, m.block),
            "provenance": _provenance_line(block_defs, m.block),
        } for m in matches],
        "anchor_only": anchor_only,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Recognize proven registry blocks in an ingested board netlist.")
    ap.add_argument("netlist", help="extracted Stage-5b netlist YAML "
                                    "(claude_context/netlist.yaml)")
    ap.add_argument("--blocks-dir", default=None,
                    help="proven-block registry (default: the generator "
                         "skill's blocks/)")
    ap.add_argument("--min-match", type=float, default=0.5,
                    help="minimum matched-component fraction to report as a "
                         "match (default 0.5; below reports anchor-only)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    board = load_intended_netlist(args.netlist)
    block_defs = load_registry(args.blocks_dir)
    matches, anchor_only = match_board(board, block_defs, args.min_match)

    if args.json:
        print(json.dumps(result_to_dict(matches, anchor_only, block_defs,
                                        args.netlist), indent=2))
    else:
        print(format_report(matches, anchor_only, block_defs, args.netlist))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
