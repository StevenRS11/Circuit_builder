"""Regression: symbol rotation / mirror must follow KiCad's convention.

Found 2026-10-08 reviewing pushbuttonDef + DualScale_Compact: the shared
transform rotated symbols CLOCKWISE on screen, KiCad rotates them
COUNTER-clockwise. Every part placed at 90°/270° came out with pins mirrored
(pin 1 <-> pin 2 on passives) — in the loader, validator, extractor AND the
generator. Generator and validator shared the bug, so their tests agreed with
each other; only KiCad's own netlist / the board's pad nets exposed it.

These tests pin the convention to KiCad ground truth, not to our own math.
"""
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "kicad-board-context", "scripts"))

from generate_kicad_sch import KicadSchematic, sym_to_sch  # noqa: E402
from validate_kicad_sch import load_kicad_sch  # noqa: E402

BLOCK_SHEET = os.path.normpath(os.path.join(
    HERE, "..", "blocks", "nau7802_dual_loadcell", "sheet.kicad_sch"))


def _r(p):
    return tuple(round(v, 6) + 0.0 for v in p)


# ─── the transform itself ───────────────────────────────────────────

class TestSymToSch:
    # symbol-space "north" pin (Y up) — where Device:C / Device:R keep pin 1
    NORTH = (0, 3.81)

    @pytest.mark.parametrize("rot,expected", [
        (0, (0, -3.81)),     # north stays up (smaller Y on screen)
        (90, (-3.81, 0)),    # CCW: north -> west   (KiCad ground truth)
        (180, (0, 3.81)),    # north -> south
        (270, (3.81, 0)),    # north -> east
    ])
    def test_rotation_is_ccw_on_screen(self, rot, expected):
        assert _r(sym_to_sch(*self.NORTH, rot)) == _r(expected)

    def test_west_pin_goes_south_at_90(self):
        assert _r(sym_to_sch(-2.54, 0, 90)) == _r((0, 2.54))

    def test_mirror_applies_after_rotation_in_schematic_space(self):
        # mirror x flips vertically, mirror y flips horizontally
        assert _r(sym_to_sch(*self.NORTH, 0, "x")) == _r((0, 3.81))
        assert _r(sym_to_sch(2.54, 0, 0, "y")) == _r((-2.54, 0))
        # DualScale R5: Device:R @270 + (mirror x) — pins lie on the X axis
        # after rotation, so the vertical flip must NOT swap them (verified
        # against the board's pad nets).
        assert _r(sym_to_sch(*self.NORTH, 270, "x")) == _r((3.81, 0))


# ─── builder + loader round trip ────────────────────────────────────

class TestBuilderAndLoader:
    def test_rotated_resistor_pin1_on_left(self):
        sch = KicadSchematic("rot")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", 100.33, 100.33,
                            rotation=90)
        p1 = sch.get_pin_position("R1", "1")
        p2 = sch.get_pin_position("R1", "2")
        assert p1[0] < 100.33 < p2[0] and p1[1] == p2[1] == 100.33

    def test_mirror_round_trips_through_save_and_load(self, tmp_path):
        sch = KicadSchematic("mir")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", 100.33, 100.33,
                            rotation=90)
        sch.components[-1].mirror = "y"
        out = tmp_path / "mir.kicad_sch"
        sch.save(str(out))
        assert "(mirror y)" in out.read_text(encoding="utf-8")
        loaded = load_kicad_sch(str(out), resolve_from_libraries=False)
        (r1,) = [c for c in loaded.components if c.reference == "R1"]
        assert r1.mirror == "y" and r1.rotation == 90
        # rotated 90 puts pin 1 west; mirror y moves it back east
        assert loaded.get_pin_position("R1", "1")[0] > 100.33


# ─── KiCad-authored ground truth ────────────────────────────────────

def _nets_by_pin():
    from extract_netlist import extract
    doc, _ = extract(BLOCK_SHEET)
    return {(p["ref"], str(p["pin"])): name
            for name, n in doc["nets"].items() for p in n["pins"]}


class TestKiCadGroundTruth:
    def test_block_sheet_rotated_caps_match_board_pad_nets(self):
        """The block sheet was cut from DualScale_Compact. On that board
        (KiCad's own F8 output) C13 pad 1 is on VIN1N (U2.2) and pad 2 on
        VIN1P (U2.3); C13 sits at 90° in the schematic."""
        of = _nets_by_pin()
        assert of[("C13", "1")] == of[("U2", "2")]
        assert of[("C13", "2")] == of[("U2", "3")]

    @pytest.mark.skipif(
        not (shutil.which("kicad-cli") or os.path.exists(
            r"C:\Program Files\KiCad\8.0\bin\kicad-cli.exe")),
        reason="kicad-cli not installed")
    def test_extraction_matches_kicad_cli_netlist(self, tmp_path):
        cli = shutil.which("kicad-cli") or \
            r"C:\Program Files\KiCad\8.0\bin\kicad-cli.exe"
        xml_path = tmp_path / "n.xml"
        subprocess.run([cli, "sch", "export", "netlist", "--format",
                        "kicadxml", "-o", str(xml_path), BLOCK_SHEET],
                       check=True, capture_output=True)
        kic = {}
        for net in ET.parse(xml_path).getroot().iter("net"):
            for node in net.iter("node"):
                kic[(node.get("ref"), node.get("pin"))] = net.get("name")
        ours = _nets_by_pin()
        s2k, k2s = {}, {}
        for k, n in ours.items():
            if k in kic:
                s2k.setdefault(n, set()).add(kic[k])
                k2s.setdefault(kic[k], set()).add(n)
        assert {n: v for n, v in s2k.items() if len(v) > 1} == {}
        assert {n: v for n, v in k2s.items() if len(v) > 1} == {}
