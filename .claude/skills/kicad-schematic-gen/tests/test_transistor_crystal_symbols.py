#!/usr/bin/env python3
"""Tests for transistor (MOSFET, BJT) and crystal lib symbol methods."""

import sys
import os
import tempfile

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_scripts_dir = os.path.join(_tests_dir, "..", "scripts")
sys.path.insert(0, os.path.abspath(_scripts_dir))

from generate_kicad_sch import KicadSchematic
from validate_kicad_sch import validate
import pytest


# ─── Helpers ────────────────────────────────────────────────────────

def _snap(v):
    """Snap a value to KiCad 1.27mm grid."""
    return round(round(v / 1.27) * 1.27, 4)


def _expected_pin_pos(comp_x, comp_y, pin_sym_x, pin_sym_y, rotation=0):
    """Compute expected pin position after Y-inversion and rotation.

    In symbol space: pin is at (pin_sym_x, pin_sym_y).
    After Y-inversion: (pin_sym_x, -pin_sym_y).
    After rotation (degrees CCW): apply rotation matrix.
    Final: (comp_x + rotated_x, comp_y + rotated_y), snapped.
    """
    import math
    inv_x, inv_y = pin_sym_x, -pin_sym_y
    rad = math.radians(rotation)
    rot_x = inv_x * math.cos(rad) - inv_y * math.sin(rad)
    rot_y = inv_x * math.sin(rad) + inv_y * math.cos(rad)
    return (_snap(comp_x + rot_x), _snap(comp_y + rot_y))


# ─── Test: MOSFET N-channel symbol ─────────────────────────────────

class TestMosfetNSymbol:
    def test_registers_in_lib_symbols(self):
        sch = KicadSchematic("nmos_test")
        sch.add_lib_symbol_mosfet_n()
        assert "Device:Q_NMOS_GSD" in sch.lib_symbols
        sym = sch.lib_symbols["Device:Q_NMOS_GSD"]
        assert sym.properties["Reference"] == "Q"
        assert len(sym.pins) == 3

    def test_placement_and_pin_positions(self):
        sch = KicadSchematic("nmos_pins")
        sch.add_lib_symbol_mosfet_n()
        sch.place_component("Device:Q_NMOS_GSD", "Q1", "", x=100, y=80)

        g_pos = sch.get_pin_position("Q1", "1")  # Gate — left
        d_pos = sch.get_pin_position("Q1", "3")  # Drain — top
        s_pos = sch.get_pin_position("Q1", "2")  # Source — bottom

        # Gate is to the left of component center
        assert g_pos[0] < 100
        # Drain is above (lower Y in schematic coords) component center
        assert d_pos[1] < 80
        # Source is below (higher Y in schematic coords) component center
        assert s_pos[1] > 80

    def test_pin_coordinates_at_100_80(self):
        sch = KicadSchematic("nmos_coords")
        sch.add_lib_symbol_mosfet_n()
        sch.place_component("Device:Q_NMOS_GSD", "Q1", "", x=100, y=80)

        g = sch.get_pin_position("Q1", "1")
        s = sch.get_pin_position("Q1", "2")
        d = sch.get_pin_position("Q1", "3")

        # Pin1 (G) at sym (-2.54, 0) -> schematic (100-2.54, 80+0) = (97.46, 80)
        assert g == _expected_pin_pos(100, 80, -2.54, 0)
        # Pin2 (S) at sym (0, -2.54) -> schematic (100, 80+2.54) = (100, 82.54)
        assert s == _expected_pin_pos(100, 80, 0, -2.54)
        # Pin3 (D) at sym (0, 2.54) -> schematic (100, 80-2.54) = (100, 77.46)
        assert d == _expected_pin_pos(100, 80, 0, 2.54)


# ─── Test: MOSFET P-channel symbol ─────────────────────────────────

class TestMosfetPSymbol:
    def test_registers_in_lib_symbols(self):
        sch = KicadSchematic("pmos_test")
        sch.add_lib_symbol_mosfet_p()
        assert "Device:Q_PMOS_GSD" in sch.lib_symbols
        sym = sch.lib_symbols["Device:Q_PMOS_GSD"]
        assert sym.properties["Reference"] == "Q"

    def test_pmos_pin_inversion_vs_nmos(self):
        """PMOS has S above (top) and D below (bottom) — swapped from NMOS."""
        sch = KicadSchematic("pmos_pins")
        sch.add_lib_symbol_mosfet_p()
        sch.place_component("Device:Q_PMOS_GSD", "Q1", "", x=100, y=80)

        g_pos = sch.get_pin_position("Q1", "1")  # Gate — left
        s_pos = sch.get_pin_position("Q1", "2")  # Source — top
        d_pos = sch.get_pin_position("Q1", "3")  # Drain — bottom

        assert g_pos[0] < 100           # Gate left
        assert s_pos[1] < 80            # Source above
        assert d_pos[1] > 80            # Drain below

    def test_pmos_coordinates(self):
        sch = KicadSchematic("pmos_coords")
        sch.add_lib_symbol_mosfet_p()
        sch.place_component("Device:Q_PMOS_GSD", "Q1", "", x=100, y=80)

        g = sch.get_pin_position("Q1", "1")
        s = sch.get_pin_position("Q1", "2")
        d = sch.get_pin_position("Q1", "3")

        # Pin1 (G) sym (-2.54, 0)
        assert g == _expected_pin_pos(100, 80, -2.54, 0)
        # Pin2 (S) sym (0, 2.54) -> source at top
        assert s == _expected_pin_pos(100, 80, 0, 2.54)
        # Pin3 (D) sym (0, -2.54) -> drain at bottom
        assert d == _expected_pin_pos(100, 80, 0, -2.54)


# ─── Test: BJT NPN symbol ──────────────────────────────────────────

class TestBjtNpnSymbol:
    def test_registers_in_lib_symbols(self):
        sch = KicadSchematic("npn_test")
        sch.add_lib_symbol_bjt_npn()
        assert "Device:Q_NPN_BCE" in sch.lib_symbols
        sym = sch.lib_symbols["Device:Q_NPN_BCE"]
        assert sym.properties["Reference"] == "Q"
        assert len(sym.pins) == 3

    def test_npn_pin_layout(self):
        sch = KicadSchematic("npn_pins")
        sch.add_lib_symbol_bjt_npn()
        sch.place_component("Device:Q_NPN_BCE", "Q1", "", x=100, y=80)

        b = sch.get_pin_position("Q1", "1")  # Base — left
        c = sch.get_pin_position("Q1", "2")  # Collector — top
        e = sch.get_pin_position("Q1", "3")  # Emitter — bottom

        assert b[0] < 100
        assert c[1] < 80
        assert e[1] > 80


# ─── Test: BJT PNP symbol ──────────────────────────────────────────

class TestBjtPnpSymbol:
    def test_registers_in_lib_symbols(self):
        sch = KicadSchematic("pnp_test")
        sch.add_lib_symbol_bjt_pnp()
        assert "Device:Q_PNP_BCE" in sch.lib_symbols
        sym = sch.lib_symbols["Device:Q_PNP_BCE"]
        assert sym.properties["Reference"] == "Q"

    def test_pnp_pin_layout(self):
        """PNP: B left, C below, E above — opposite of NPN for C/E."""
        sch = KicadSchematic("pnp_pins")
        sch.add_lib_symbol_bjt_pnp()
        sch.place_component("Device:Q_PNP_BCE", "Q1", "", x=100, y=80)

        b = sch.get_pin_position("Q1", "1")  # Base — left
        c = sch.get_pin_position("Q1", "2")  # Collector — bottom
        e = sch.get_pin_position("Q1", "3")  # Emitter — top

        assert b[0] < 100
        assert c[1] > 80
        assert e[1] < 80


# ─── Test: Crystal 2-pin symbol ────────────────────────────────────

class TestCrystal2Pin:
    def test_registers_in_lib_symbols(self):
        sch = KicadSchematic("crystal_test")
        sch.add_lib_symbol_crystal()
        assert "Device:Crystal" in sch.lib_symbols
        sym = sch.lib_symbols["Device:Crystal"]
        assert sym.properties["Reference"] == "Y"
        assert len(sym.pins) == 2

    def test_crystal_horizontal_pins(self):
        sch = KicadSchematic("crystal_pins")
        sch.add_lib_symbol_crystal()
        sch.place_component("Device:Crystal", "Y1", "12MHz", x=100, y=80)

        p1 = sch.get_pin_position("Y1", "1")  # left
        p2 = sch.get_pin_position("Y1", "2")  # right

        # Pin 1 to the left, Pin 2 to the right
        assert p1[0] < 100
        assert p2[0] > 100
        # Both at same vertical height
        assert p1[1] == p2[1]

    def test_crystal_coordinates(self):
        sch = KicadSchematic("crystal_coords")
        sch.add_lib_symbol_crystal()
        sch.place_component("Device:Crystal", "Y1", "12MHz", x=100, y=80)

        p1 = sch.get_pin_position("Y1", "1")
        p2 = sch.get_pin_position("Y1", "2")

        assert p1 == _expected_pin_pos(100, 80, -2.54, 0)
        assert p2 == _expected_pin_pos(100, 80, 2.54, 0)


# ─── Test: Crystal 4-pin symbol ────────────────────────────────────

class TestCrystal4Pin:
    def test_registers_in_lib_symbols(self):
        sch = KicadSchematic("crystal4_test")
        sch.add_lib_symbol_crystal_4pin()
        assert "Device:Crystal_GND24" in sch.lib_symbols
        sym = sch.lib_symbols["Device:Crystal_GND24"]
        assert sym.properties["Reference"] == "Y"
        assert len(sym.pins) == 4

    def test_crystal_4pin_layout(self):
        sch = KicadSchematic("crystal4_pins")
        sch.add_lib_symbol_crystal_4pin()
        sch.place_component("Device:Crystal_GND24", "Y1", "12MHz", x=100, y=80)

        p1 = sch.get_pin_position("Y1", "1")  # IN — left
        p3 = sch.get_pin_position("Y1", "3")  # OUT — right
        p2 = sch.get_pin_position("Y1", "2")  # GND — bottom
        p4 = sch.get_pin_position("Y1", "4")  # GND — bottom

        # IN left of center, OUT right of center
        assert p1[0] < 100
        assert p3[0] > 100
        # Both GND pins below center
        assert p2[1] > 80
        assert p4[1] > 80

    def test_crystal_4pin_coordinates(self):
        sch = KicadSchematic("crystal4_coords")
        sch.add_lib_symbol_crystal_4pin()
        sch.place_component("Device:Crystal_GND24", "Y1", "12MHz", x=100, y=80)

        p1 = sch.get_pin_position("Y1", "1")
        p2 = sch.get_pin_position("Y1", "2")
        p3 = sch.get_pin_position("Y1", "3")
        p4 = sch.get_pin_position("Y1", "4")

        assert p1 == _expected_pin_pos(100, 80, -3.81, 0)
        assert p3 == _expected_pin_pos(100, 80, 3.81, 0)
        assert p2 == _expected_pin_pos(100, 80, -1.27, -2.54)
        assert p4 == _expected_pin_pos(100, 80, 1.27, -2.54)


# ─── Test: rotation ────────────────────────────────────────────────

class TestRotation:
    def test_mosfet_90_degree_rotation(self):
        """Place NMOS at 90 degrees; pins should rotate accordingly."""
        sch = KicadSchematic("nmos_rot90")
        sch.add_lib_symbol_mosfet_n()
        sch.place_component("Device:Q_NMOS_GSD", "Q1", "", x=100, y=80,
                            rotation=90)

        g = sch.get_pin_position("Q1", "1")
        s = sch.get_pin_position("Q1", "2")
        d = sch.get_pin_position("Q1", "3")

        # At 90 degrees: Gate sym(-2.54, 0) -> inv(-2.54, 0) -> rot(0, -2.54)
        # Gate should be above (lower Y) the center after 90 CCW rotation
        assert g == _expected_pin_pos(100, 80, -2.54, 0, rotation=90)
        assert s == _expected_pin_pos(100, 80, 0, -2.54, rotation=90)
        assert d == _expected_pin_pos(100, 80, 0, 2.54, rotation=90)


# ─── Test: save to file ────────────────────────────────────────────

class TestSaveWithTransistorsAndCrystals:
    def test_save_no_crash(self):
        """Create a schematic with transistor + crystal, save, verify no error."""
        sch = KicadSchematic("save_test")
        sch.add_lib_symbol_mosfet_n()
        sch.add_lib_symbol_crystal()
        sch.add_lib_symbol_resistor()

        sch.place_component("Device:Q_NMOS_GSD", "Q1", "", x=100, y=80)
        sch.place_component("Device:Crystal", "Y1", "12MHz", x=130, y=80)
        sch.place_component("Device:R", "R1", "10k", x=80, y=80)

        outpath = os.path.join(tempfile.gettempdir(), "test_transistor_save.kicad_sch")
        try:
            sch.save(outpath)
            assert os.path.exists(outpath)
            assert os.path.getsize(outpath) > 0
        finally:
            if os.path.exists(outpath):
                os.remove(outpath)


# ─── Test: validation of wired MOSFET circuit ──────────────────────

class TestValidationWithMosfet:
    def test_wired_mosfet_circuit_passes(self):
        """Wire a basic MOSFET switching circuit and validate."""
        sch = KicadSchematic("mosfet_circuit")
        sch.add_lib_symbol_mosfet_n()
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_power("VCC")
        sch.add_lib_symbol_power("GND")

        sch.place_component("Device:Q_NMOS_GSD", "Q1", "", x=100, y=80)
        sch.place_component("Device:R", "R1", "10k", x=100, y=60)

        sch.place_power_symbol("VCC", 100, 48)
        sch.place_power_symbol("GND", 100, 92)

        # Wire drain to resistor pin 2
        d_pos = sch.get_pin_position("Q1", "3")
        r2_pos = sch.get_pin_position("R1", "2")
        sch.wire_between("R1", "2", "Q1", "3")

        # Wire R1 pin 1 to VCC
        r1_pos = sch.get_pin_position("R1", "1")
        sch.add_wire(r1_pos[0], r1_pos[1], 100, 48)

        # Wire source to GND
        s_pos = sch.get_pin_position("Q1", "2")
        sch.add_wire(s_pos[0], s_pos[1], 100, 92)

        # Gate gets a label so it is not floating
        g_pos = sch.get_pin_position("Q1", "1")
        sch.add_label("GATE_IN", g_pos[0], g_pos[1])

        result = validate(sch)
        errors = [i for i in result.issues if i.severity == "error"]
        assert len(errors) == 0, f"Unexpected errors: {errors}"


# ─── Test: auto-reference assignment ───────────────────────────────

class TestAutoReference:
    def test_q_prefix_increments(self):
        sch = KicadSchematic("autoref_q")
        sch.add_lib_symbol_mosfet_n()
        sch.add_lib_symbol_bjt_npn()

        sch.place_component("Device:Q_NMOS_GSD", "Q1", "", x=100, y=80)
        sch.place_component("Device:Q_NPN_BCE", "Q2", "", x=130, y=80)

        refs = [c.reference for c in sch.components]
        assert "Q1" in refs
        assert "Q2" in refs

    def test_y_prefix_increments(self):
        sch = KicadSchematic("autoref_y")
        sch.add_lib_symbol_crystal()
        sch.add_lib_symbol_crystal_4pin()

        sch.place_component("Device:Crystal", "Y1", "12MHz", x=100, y=80)
        sch.place_component("Device:Crystal_GND24", "Y2", "8MHz", x=130, y=80)

        refs = [c.reference for c in sch.components]
        assert "Y1" in refs
        assert "Y2" in refs
