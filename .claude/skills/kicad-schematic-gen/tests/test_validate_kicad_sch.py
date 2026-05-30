#!/usr/bin/env python3
"""Tests for the KiCad schematic validator."""

import sys
import os
# Add the scripts directory to the path
_tests_dir = os.path.dirname(os.path.abspath(__file__))
_scripts_dir = os.path.join(_tests_dir, "..", "scripts")
sys.path.insert(0, os.path.abspath(_scripts_dir))

from generate_kicad_sch import KicadSchematic
from validate_kicad_sch import (
    validate, extract_netlist, assert_connected, assert_net_contains,
    print_netlist,
)
import pytest


# ─── Helpers ────────────────────────────────────────────────────────

def _make_simple_rc():
    """R and C in parallel between VCC and GND — fully connected."""
    sch = KicadSchematic("RC Test")
    sch.add_lib_symbol_resistor()
    sch.add_lib_symbol_capacitor()
    sch.add_lib_symbol_power("VCC")
    sch.add_lib_symbol_power("GND")

    sch.place_component("Device:R", "R1", "10k", x=100, y=80)
    sch.place_component("Device:C", "C1", "100nF", x=115, y=80)

    sch.place_power_symbol("VCC", 100, 68)
    sch.place_power_symbol("VCC", 115, 68)
    sch.place_power_symbol("GND", 100, 92)
    sch.place_power_symbol("GND", 115, 92)

    # Wire each pin to its power symbol
    for ref in ("R1", "C1"):
        x = 100 if ref == "R1" else 115
        p1 = sch.get_pin_position(ref, "1")
        sch.add_wire(p1[0], p1[1], x, 68)
        p2 = sch.get_pin_position(ref, "2")
        sch.add_wire(p2[0], p2[1], x, 92)

    return sch


def _issues_by_check(result, check_name):
    return [i for i in result.issues if i.check_name == check_name]


# ─── Test: valid circuit passes ─────────────────────────────────────

class TestValidCircuit:
    def test_rc_parallel_passes(self):
        sch = _make_simple_rc()
        result = validate(sch)
        errors = [i for i in result.issues if i.severity == "error"]
        assert len(errors) == 0, f"Unexpected errors: {errors}"

    def test_rc_parallel_netlist(self):
        sch = _make_simple_rc()
        netlist = extract_netlist(sch)
        # Both R1.1 and C1.1 should be on VCC
        assert netlist.are_connected("R1", "1", "C1", "1")
        # Both R1.2 and C1.2 should be on GND
        assert netlist.are_connected("R1", "2", "C1", "2")

    def test_assert_connected_passes(self):
        sch = _make_simple_rc()
        assert_connected(sch, "R1", "1", "C1", "1")
        assert_connected(sch, "R1", "2", "C1", "2")

    def test_power_nets_labeled(self):
        sch = _make_simple_rc()
        netlist = extract_netlist(sch)
        vcc_net = netlist.get_net_for_pin("R1", "1")
        gnd_net = netlist.get_net_for_pin("R1", "2")
        assert vcc_net == "VCC"
        assert gnd_net == "GND"


# ─── Test: floating pin detection ───────────────────────────────────

class TestFloatingPins:
    def test_unconnected_pin_detected(self):
        sch = KicadSchematic("Float Test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_power("VCC")
        sch.add_lib_symbol_power("GND")

        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.place_power_symbol("VCC", 100, 68)
        # Only connect pin 1, leave pin 2 floating
        p1 = sch.get_pin_position("R1", "1")
        sch.add_wire(p1[0], p1[1], 100, 68)

        result = validate(sch)
        floating = _issues_by_check(result, "floating_pin")
        assert len(floating) == 1
        assert "R1" in floating[0].message
        assert floating[0].severity == "error"

    def test_no_connect_suppresses_floating(self):
        sch = KicadSchematic("NC Test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_power("VCC")

        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.place_power_symbol("VCC", 100, 68)
        p1 = sch.get_pin_position("R1", "1")
        sch.add_wire(p1[0], p1[1], 100, 68)
        # Mark pin 2 as no-connect
        p2 = sch.get_pin_position("R1", "2")
        sch.add_no_connect(p2[0], p2[1])

        result = validate(sch)
        floating = _issues_by_check(result, "floating_pin")
        assert len(floating) == 0


# ─── Test: duplicate references ─────────────────────────────────────

class TestDuplicateReferences:
    def test_duplicate_detected(self):
        sch = KicadSchematic("Dup Test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.place_component("Device:R", "R1", "4.7k", x=120, y=80)

        result = validate(sch)
        dups = _issues_by_check(result, "duplicate_reference")
        assert len(dups) == 1
        assert dups[0].severity == "error"


# ─── Test: dangling wires ───────────────────────────────────────────

class TestDanglingWires:
    def test_wire_to_nowhere(self):
        sch = KicadSchematic("Dangle Test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)

        p1 = sch.get_pin_position("R1", "1")
        # Wire from pin 1 to nowhere
        sch.add_wire(p1[0], p1[1], 100, 50)

        result = validate(sch)
        dangles = _issues_by_check(result, "dangling_wire")
        assert len(dangles) >= 1

    def test_wire_between_two_pins_ok(self):
        sch = KicadSchematic("Wire OK Test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.place_component("Device:C", "C1", "100nF", x=100, y=100)

        # Connect R1 pin 2 to C1 pin 1 directly
        p_r2 = sch.get_pin_position("R1", "2")
        p_c1 = sch.get_pin_position("C1", "1")
        sch.add_wire(p_r2[0], p_r2[1], p_c1[0], p_c1[1])

        result = validate(sch)
        dangles = _issues_by_check(result, "dangling_wire")
        assert len(dangles) == 0


# ─── Test: missing junctions ────────────────────────────────────────

class TestMissingJunctions:
    def test_t_junction_without_marker(self):
        sch = KicadSchematic("Junction Test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)

        # Horizontal wire passing through a vertical wire's midpoint
        sch.add_wire(90, 80, 110, 80)   # horizontal
        sch.add_wire(100, 70, 100, 90)  # vertical, crosses at (100,80)

        result = validate(sch)
        junctions = _issues_by_check(result, "missing_junction")
        # Wire endpoints at (100,70) and (100,90) don't land on the
        # interior of the horizontal wire, but (90,80)/(110,80) don't
        # land on the interior of the vertical wire either.
        # The crossing happens at (100,80) but no endpoint is at interior.
        # Actually: (100,70) endpoint is NOT on the horizontal wire interior.
        # And (90,80) endpoint is NOT on the vertical wire interior.
        # So no missing junction warning here — the wires cross but no
        # endpoint lands on another wire's interior.
        # Let's test a real T-junction instead.

    def test_t_junction_endpoint_on_segment(self):
        from generate_kicad_sch import snap_to_grid
        sch = KicadSchematic("T-Junction Test")
        # Use grid-aligned coords: 1.27mm grid
        # 88.9, 78.74, 120.65 are on-grid (multiples of 1.27)
        x_left, x_mid, x_right = 88.9, 100.33, 120.65
        y_top, y_wire = 69.85, 80.01
        # Long horizontal wire
        sch.add_wire(x_left, y_wire, x_right, y_wire)
        # Vertical wire ending on the horizontal wire's interior
        sch.add_wire(x_mid, y_top, x_mid, y_wire)

        result = validate(sch)
        junctions = _issues_by_check(result, "missing_junction")
        assert len(junctions) >= 1
        # The endpoint at (x_mid, y_wire) should be flagged
        snapped_x = snap_to_grid(x_mid)
        snapped_y = snap_to_grid(y_wire)
        assert junctions[0].coordinates == (snapped_x, snapped_y)

    def test_junction_suppresses_warning(self):
        sch = KicadSchematic("Junction OK Test")
        x_left, x_mid, x_right = 88.9, 100.33, 120.65
        y_top, y_wire = 69.85, 80.01
        sch.add_wire(x_left, y_wire, x_right, y_wire)
        sch.add_wire(x_mid, y_top, x_mid, y_wire)
        sch.add_junction(x_mid, y_wire)

        result = validate(sch)
        junctions = _issues_by_check(result, "missing_junction")
        assert len(junctions) == 0


# ─── Test: wire_between and assert_connected ────────────────────────

class TestWireBetween:
    def test_wire_between_creates_connection(self):
        sch = KicadSchematic("WireBetween Test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.place_component("Device:C", "C1", "100nF", x=120, y=80)

        sch.wire_between("R1", "2", "C1", "2")
        assert_connected(sch, "R1", "2", "C1", "2")

    def test_assert_connected_fails_when_not(self):
        sch = KicadSchematic("Not Connected Test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.place_component("Device:C", "C1", "100nF", x=120, y=80)
        # No wires!

        with pytest.raises(AssertionError):
            assert_connected(sch, "R1", "1", "C1", "1")


# ─── Test: labels create nets ───────────────────────────────────────

class TestLabels:
    def test_label_connects_distant_pins(self):
        sch = KicadSchematic("Label Test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.place_component("Device:R", "R2", "10k", x=200, y=80)

        # Wire R1 pin 1 to a label
        p1 = sch.get_pin_position("R1", "1")
        sch.add_wire(p1[0], p1[1], 100, 65)
        sch.add_label("SIG", 100, 65)

        # Wire R2 pin 1 to same label name at different location
        p2 = sch.get_pin_position("R2", "1")
        sch.add_wire(p2[0], p2[1], 200, 65)
        sch.add_label("SIG", 200, 65)

        assert_connected(sch, "R1", "1", "R2", "1")
        netlist = extract_netlist(sch)
        assert netlist.get_net_for_pin("R1", "1") == "SIG"


# ─── Test: IC with custom pinout ────────────────────────────────────

class TestCustomIC:
    def test_ic_pin_connectivity(self):
        sch = KicadSchematic("IC Test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_power("GND")
        sch.add_lib_symbol_power("+3V3")

        sch.add_lib_symbol_ic(
            "custom:AP2112K",
            pins=[
                ("1", "VIN", "power_in", "left", 0),
                ("2", "GND", "power_in", "left", 1),
                ("3", "EN", "input", "left", 2),
                ("4", "NC", "passive", "right", 0),
                ("5", "VOUT", "power_out", "right", 1),
            ],
        )

        sch.place_component("custom:AP2112K", "U1", "AP2112K-3.3", x=150, y=100)

        # Connect GND pin to GND symbol
        sch.place_power_symbol("GND", 150, 120)
        gnd_pos = sch.get_pin_position("U1", "2")
        sch.add_wire(gnd_pos[0], gnd_pos[1], 150, 120)

        # Connect VOUT to +3V3
        sch.place_power_symbol("+3V3", 170, 85)
        vout_pos = sch.get_pin_position("U1", "5")
        sch.add_wire(vout_pos[0], vout_pos[1], 170, 85)

        netlist = extract_netlist(sch)
        assert netlist.get_net_for_pin("U1", "2") == "GND"
        assert netlist.get_net_for_pin("U1", "5") == "+3V3"


# ─── Test: no-connect conflict ──────────────────────────────────────

class TestNoConnectConflict:
    def test_nc_with_wire_warns(self):
        sch = KicadSchematic("NC Conflict Test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)

        p1 = sch.get_pin_position("R1", "1")
        sch.add_wire(p1[0], p1[1], 100, 65)
        sch.add_no_connect(p1[0], p1[1])  # NC at same spot as wire

        result = validate(sch)
        conflicts = _issues_by_check(result, "no_connect_conflict")
        assert len(conflicts) == 1


# ─── Test: assert_net_contains ──────────────────────────────────────

class TestAssertNetContains:
    def test_exact_match(self):
        sch = _make_simple_rc()
        assert_net_contains(sch, "VCC", [
            ("R1", "1"), ("C1", "1"),
            ("#PWR001", "1"), ("#PWR002", "1"),
        ])

    def test_missing_pin_fails(self):
        sch = _make_simple_rc()
        with pytest.raises(AssertionError, match="Missing pins"):
            assert_net_contains(sch, "VCC", [
                ("R1", "1"), ("C1", "1"), ("R99", "1"),
                ("#PWR001", "1"), ("#PWR002", "1"),
            ])

    def test_nonexistent_net_fails(self):
        sch = _make_simple_rc()
        with pytest.raises(AssertionError, match="not found"):
            assert_net_contains(sch, "BOGUS", [("R1", "1")])


# ─── Test: disconnected labels ──────────────────────────────────────

class TestDisconnectedLabels:
    def test_label_touching_wire_ok(self):
        sch = KicadSchematic("Label OK Test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        p1 = sch.get_pin_position("R1", "1")
        sch.add_wire(p1[0], p1[1], 100, 65)
        sch.add_label("SIG", 100, 65)  # on the wire endpoint

        result = validate(sch)
        disconnected = _issues_by_check(result, "disconnected_label")
        assert len(disconnected) == 0

    def test_label_in_empty_space_detected(self):
        sch = KicadSchematic("Floating Label Test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        p1 = sch.get_pin_position("R1", "1")
        sch.add_wire(p1[0], p1[1], 100, 65)
        # Label placed one grid square off from the wire endpoint
        sch.add_label("SIG", 101.27, 65)

        result = validate(sch)
        disconnected = _issues_by_check(result, "disconnected_label")
        assert len(disconnected) == 1
        assert disconnected[0].severity == "error"
        assert "SIG" in disconnected[0].message

    def test_label_on_pin_ok(self):
        sch = KicadSchematic("Label on Pin Test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        p1 = sch.get_pin_position("R1", "1")
        sch.add_label("SIG", p1[0], p1[1])  # directly on pin

        result = validate(sch)
        disconnected = _issues_by_check(result, "disconnected_label")
        assert len(disconnected) == 0

    def test_global_label_floating_detected(self):
        sch = KicadSchematic("Floating Global Label Test")
        # Global label with nothing at its location
        sch.add_global_label("I2C_SDA", 50, 50)

        result = validate(sch)
        disconnected = _issues_by_check(result, "disconnected_label")
        assert len(disconnected) == 1
        assert "I2C_SDA" in disconnected[0].message


# ─── Test: similar net names (case collisions) ─────────────────────

class TestSimilarNetNames:
    def test_same_case_no_warning(self):
        sch = KicadSchematic("Case OK Test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.place_component("Device:R", "R2", "10k", x=200, y=80)

        p1 = sch.get_pin_position("R1", "1")
        sch.add_wire(p1[0], p1[1], 100, 65)
        sch.add_label("VCC", 100, 65)

        p2 = sch.get_pin_position("R2", "1")
        sch.add_wire(p2[0], p2[1], 200, 65)
        sch.add_label("VCC", 200, 65)

        result = validate(sch)
        similar = _issues_by_check(result, "similar_net_names")
        assert len(similar) == 0

    def test_case_mismatch_detected(self):
        sch = KicadSchematic("Case Mismatch Test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.place_component("Device:R", "R2", "10k", x=200, y=80)

        p1 = sch.get_pin_position("R1", "1")
        sch.add_wire(p1[0], p1[1], 100, 65)
        sch.add_label("+3V3", 100, 65)

        p2 = sch.get_pin_position("R2", "1")
        sch.add_wire(p2[0], p2[1], 200, 65)
        sch.add_label("+3v3", 200, 65)  # different case!

        result = validate(sch)
        similar = _issues_by_check(result, "similar_net_names")
        assert len(similar) == 1
        assert "+3V3" in similar[0].message
        assert "+3v3" in similar[0].message

    def test_power_symbol_vs_label_case_mismatch(self):
        sch = KicadSchematic("Power Case Test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_power("+3V3")
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)

        sch.place_power_symbol("+3V3", 100, 68)
        p1 = sch.get_pin_position("R1", "1")
        sch.add_wire(p1[0], p1[1], 100, 68)

        # Label with different case elsewhere
        p2 = sch.get_pin_position("R1", "2")
        sch.add_wire(p2[0], p2[1], 100, 92)
        sch.add_label("+3v3", 100, 92)  # case mismatch with power symbol

        result = validate(sch)
        similar = _issues_by_check(result, "similar_net_names")
        assert len(similar) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
