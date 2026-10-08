"""reconcile.reconcile_connectivity — schematic vs board pad-net partition.

Added 2026-10-08: reconcile passed both pushbuttonDef and DualScale_Compact
while the extracted netlist disagreed with the board on 25 pins (an extractor
rotation bug). Field-level checks can't see that; a pin->pad net partition
check can.
"""
import os

import reconcile
from extract_netlist import extract

HERE = os.path.dirname(os.path.abspath(__file__))
BLOCK_SHEET = os.path.normpath(os.path.join(
    HERE, "..", "..", "kicad-schematic-gen", "blocks",
    "nau7802_dual_loadcell", "sheet.kicad_sch"))


def _board_from(pin_nets, swap=()):
    """Minimal .kicad_pcb text whose pads carry the given nets.
    Board net names are deliberately different from schematic names
    (KiCad's Net-(...) style) — the check must compare partitions."""
    by_ref = {}
    for (ref, pin), net in pin_nets.items():
        by_ref.setdefault(ref, {})[pin] = net
    for ref in swap:
        by_ref[ref]["1"], by_ref[ref]["2"] = by_ref[ref]["2"], by_ref[ref]["1"]
    out = ["(kicad_pcb (version 20221018) (generator pcbnew)"]
    for ref, pads in sorted(by_ref.items()):
        out.append('  (footprint "X:Y" (layer "F.Cu")')
        out.append(f'    (property "Reference" "{ref}")')
        for pin, net in sorted(pads.items()):
            out.append(f'    (pad "{pin}" smd rect (at 0 0) (size 1 1) '
                       f'(layers "F.Cu") (net 1 "Net-({net})"))')
        out.append("  )")
    out.append(")")
    return "\n".join(out)


def _sch_pin_nets():
    doc, _ = extract(BLOCK_SHEET)
    return {(p["ref"], str(p["pin"])): name
            for name, n in doc["nets"].items() if len(n["pins"]) > 1
            for p in n["pins"]}


def test_consistent_board_passes():
    pcb = _board_from(_sch_pin_nets())
    assert reconcile.reconcile_connectivity(BLOCK_SHEET, pcb) == []


def test_swapped_pads_on_rotated_part_are_reported():
    # R4 sits at 90° on the sheet — exactly the class the bug mis-read.
    pcb = _board_from(_sch_pin_nets(), swap=("R4",))
    issues = reconcile.reconcile_connectivity(BLOCK_SHEET, pcb)
    checks = {i.check for i in issues}
    assert checks == {"net_split", "net_merge"}
    assert all(i.severity == "error" for i in issues)
    assert any("R4.1" in i.message for i in issues if i.check == "net_split")
