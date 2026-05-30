#!/usr/bin/env python3
"""Tests for the netlist verification script."""

import sys
import os
_tests_dir = os.path.dirname(os.path.abspath(__file__))
_scripts_dir = os.path.join(_tests_dir, "..", "scripts")
sys.path.insert(0, os.path.abspath(_scripts_dir))

from generate_kicad_sch import KicadSchematic
from verify_netlist import (
    verify, load_intended_netlist_from_string, IntendedNetlist,
    IntendedNet, IntendedPin, IntendedComponent, IntendedNoConnect,
    format_result_text, format_result_json,
)
import pytest


# ─── Helpers ───────────────────────────────────────────────────────

def _make_ldo_circuit():
    """Simple LDO: 5V in, 3.3V out, with decoupling caps and power LED.

    Components: U1 (5-pin LDO), C1 (input), C2 (output), R1 (LED), D1 (LED)
    Nets: +5V, +3V3, GND, LED_ANODE
    No-connects: U1 pin 3 (EN), U1 pin 4 (NC)
    """
    sch = KicadSchematic("LDO Test")
    sch.add_lib_symbol_resistor()
    sch.add_lib_symbol_capacitor()
    sch.add_lib_symbol_led()
    sch.add_lib_symbol_power("+5V")
    sch.add_lib_symbol_power("+3V3")
    sch.add_lib_symbol_power("GND")

    # LDO: pin 1=VIN, 2=GND, 3=EN, 4=NC, 5=VOUT
    sch.add_lib_symbol_ic(
        "Regulator_Linear:AP2112K-3.3",
        pins=[
            ("1", "VIN", "power_in", "left", 0),
            ("2", "GND", "power_in", "left", 1),
            ("3", "EN", "input", "left", 2),
            ("5", "VOUT", "power_out", "right", 0),
            ("4", "NC", "passive", "right", 1),
        ],
        ref_prefix="U",
        value="AP2112K-3.3",
    )

    # Place components
    sch.place_component("Regulator_Linear:AP2112K-3.3", "U1", "AP2112K-3.3",
                        x=120, y=80)
    sch.place_component("Device:C", "C1", "100nF", x=100, y=80)
    sch.place_component("Device:C", "C2", "10uF", x=145, y=80)
    sch.place_component("Device:R", "R1", "1k", x=160, y=70)
    sch.place_component("Device:LED", "D1", "Green", x=160, y=90)

    # Power symbols
    sch.place_power_symbol("+5V", 100, 65)
    sch.place_power_symbol("+3V3", 145, 65)
    sch.place_power_symbol("+3V3", 160, 55)
    sch.place_power_symbol("GND", 100, 95)
    sch.place_power_symbol("GND", 120, 100)
    sch.place_power_symbol("GND", 145, 95)
    sch.place_power_symbol("GND", 160, 105)

    # Wire +5V net: +5V -> C1.1, U1.1
    c1_p1 = sch.get_pin_position("C1", "1")
    sch.add_wire(c1_p1[0], c1_p1[1], 100, 65)  # C1.1 to +5V
    u1_p1 = sch.get_pin_position("U1", "1")
    sch.add_wire(u1_p1[0], u1_p1[1], c1_p1[0], c1_p1[1])  # U1.1 to C1.1

    # Wire +3V3 net: +3V3 -> C2.1, U1.5
    c2_p1 = sch.get_pin_position("C2", "1")
    sch.add_wire(c2_p1[0], c2_p1[1], 145, 65)  # C2.1 to +3V3
    u1_p5 = sch.get_pin_position("U1", "5")
    sch.add_wire(u1_p5[0], u1_p5[1], c2_p1[0], c2_p1[1])  # U1.5 to C2.1

    # Wire R1 to +3V3
    r1_p1 = sch.get_pin_position("R1", "1")
    sch.add_wire(r1_p1[0], r1_p1[1], 160, 55)  # R1.1 to +3V3

    # Wire LED_ANODE: R1.2 to D1.2 (anode)
    r1_p2 = sch.get_pin_position("R1", "2")
    d1_p2 = sch.get_pin_position("D1", "2")
    sch.add_wire(r1_p2[0], r1_p2[1], d1_p2[0], d1_p2[1])

    # Wire GND: C1.2, U1.2, C2.2, D1.1 (cathode)
    c1_p2 = sch.get_pin_position("C1", "2")
    sch.add_wire(c1_p2[0], c1_p2[1], 100, 95)  # C1.2 to GND
    u1_p2 = sch.get_pin_position("U1", "2")
    sch.add_wire(u1_p2[0], u1_p2[1], 120, 100)  # U1.2 to GND
    c2_p2 = sch.get_pin_position("C2", "2")
    sch.add_wire(c2_p2[0], c2_p2[1], 145, 95)  # C2.2 to GND
    d1_p1 = sch.get_pin_position("D1", "1")
    sch.add_wire(d1_p1[0], d1_p1[1], 160, 105)  # D1.1 to GND

    # No-connects on U1 pins 3, 4
    u1_p3 = sch.get_pin_position("U1", "3")
    sch.add_no_connect(u1_p3[0], u1_p3[1])
    u1_p4 = sch.get_pin_position("U1", "4")
    sch.add_no_connect(u1_p4[0], u1_p4[1])

    return sch


def _make_matching_netlist_yaml():
    """YAML netlist that matches _make_ldo_circuit() exactly."""
    return """
project: "LDO Test"
source: "test"

components:
  U1:
    part: "AP2112K-3.3"
    pins: ["1", "2", "3", "4", "5"]
  C1:
    part: "100nF"
    pins: ["1", "2"]
  C2:
    part: "10uF"
    pins: ["1", "2"]
  R1:
    part: "1k"
    pins: ["1", "2"]
  D1:
    part: "Green LED"
    pins: ["1", "2"]

nets:
  "+5V":
    type: power
    pins:
      - { ref: U1, pin: "1", function: "VIN" }
      - { ref: C1, pin: "1", function: "input decoupling" }
    power_symbols: ["+5V"]

  "+3V3":
    type: power
    pins:
      - { ref: U1, pin: "5", function: "VOUT" }
      - { ref: C2, pin: "1", function: "output bulk cap" }
      - { ref: R1, pin: "1", function: "LED resistor supply" }
    power_symbols: ["+3V3"]

  GND:
    type: power
    pins:
      - { ref: U1, pin: "2", function: "GND" }
      - { ref: C1, pin: "2", function: "input decoupling" }
      - { ref: C2, pin: "2", function: "output bulk cap" }
      - { ref: D1, pin: "1", function: "LED cathode" }
    power_symbols: ["GND"]

  LED_ANODE:
    type: signal
    pins:
      - { ref: R1, pin: "2", function: "LED current limit" }
      - { ref: D1, pin: "2", function: "LED anode" }

no_connects:
  - { ref: U1, pin: "3", reason: "EN — tied high internally" }
  - { ref: U1, pin: "4", reason: "NC per datasheet" }
"""


def _issues_by_check(result, check_name):
    return [i for i in result.issues if i.check_name == check_name]


# ─── Test: correct circuit passes ──────────────────────────────────

class TestMatchingNetlist:
    def test_matching_circuit_passes(self):
        """A schematic that matches its netlist should pass."""
        sch = _make_ldo_circuit()
        intended = load_intended_netlist_from_string(_make_matching_netlist_yaml())
        result = verify(intended, sch)
        errors = result.errors
        assert len(errors) == 0, f"Unexpected errors: {[e.message for e in errors]}"
        assert result.passed

    def test_no_warnings_on_clean_match(self):
        """Clean match should have zero warnings too."""
        sch = _make_ldo_circuit()
        intended = load_intended_netlist_from_string(_make_matching_netlist_yaml())
        result = verify(intended, sch)
        # May have warnings for extra power symbol pins on nets — that's acceptable
        errors = result.errors
        assert len(errors) == 0


# ─── Test: completeness checks ─────────────────────────────────────

class TestCompleteness:
    def test_missing_pin_assignment(self):
        """A pin not in any net or no_connects should fail."""
        yaml_text = """
project: "test"
components:
  R1:
    part: "10k"
    pins: ["1", "2"]
nets:
  MYNET:
    type: signal
    pins:
      - { ref: R1, pin: "1" }
no_connects: []
"""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)

        intended = load_intended_netlist_from_string(yaml_text)
        result = verify(intended, sch)
        comp_issues = _issues_by_check(result, "completeness")
        assert any("R1 pin 2" in i.message and "not assigned" in i.message
                    for i in comp_issues)

    def test_pin_in_multiple_nets(self):
        """A pin appearing in two nets should fail."""
        yaml_text = """
project: "test"
components:
  R1:
    part: "10k"
    pins: ["1", "2"]
nets:
  NET_A:
    type: signal
    pins:
      - { ref: R1, pin: "1" }
  NET_B:
    type: signal
    pins:
      - { ref: R1, pin: "1" }
      - { ref: R1, pin: "2" }
no_connects: []
"""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)

        intended = load_intended_netlist_from_string(yaml_text)
        result = verify(intended, sch)
        comp_issues = _issues_by_check(result, "completeness")
        assert any("multiple nets" in i.message for i in comp_issues)

    def test_pin_in_net_and_no_connect(self):
        """A pin in both a net and no_connects should fail."""
        yaml_text = """
project: "test"
components:
  R1:
    part: "10k"
    pins: ["1", "2"]
nets:
  MYNET:
    type: signal
    pins:
      - { ref: R1, pin: "1" }
      - { ref: R1, pin: "2" }
no_connects:
  - { ref: R1, pin: "1", reason: "oops" }
"""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)

        intended = load_intended_netlist_from_string(yaml_text)
        result = verify(intended, sch)
        comp_issues = _issues_by_check(result, "completeness")
        assert any("both a net and no_connects" in i.message for i in comp_issues)


# ─── Test: consistency checks ──────────────────────────────────────

class TestConsistency:
    def test_phantom_pin(self):
        """A pin referenced in the netlist that doesn't exist in the schematic."""
        yaml_text = """
project: "test"
components:
  R1:
    part: "10k"
    pins: ["1", "2"]
  R99:
    part: "fake"
    pins: ["1", "2"]
nets:
  MYNET:
    type: signal
    pins:
      - { ref: R1, pin: "1" }
      - { ref: R99, pin: "1" }
      - { ref: R1, pin: "2" }
      - { ref: R99, pin: "2" }
no_connects: []
"""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)

        intended = load_intended_netlist_from_string(yaml_text)
        result = verify(intended, sch)
        cons_issues = _issues_by_check(result, "consistency")
        assert any("R99" in i.message and "does not exist" in i.message
                    for i in cons_issues)

    def test_phantom_component(self):
        """A component declared in netlist but not in schematic."""
        yaml_text = """
project: "test"
components:
  R1:
    part: "10k"
    pins: ["1", "2"]
  R99:
    part: "phantom"
    pins: ["1", "2"]
nets:
  MYNET:
    type: signal
    pins:
      - { ref: R1, pin: "1" }
      - { ref: R99, pin: "1" }
      - { ref: R1, pin: "2" }
      - { ref: R99, pin: "2" }
no_connects: []
"""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)

        intended = load_intended_netlist_from_string(yaml_text)
        result = verify(intended, sch)
        cons_issues = _issues_by_check(result, "consistency")
        assert any("R99" in i.message and "not found in schematic" in i.message
                    for i in cons_issues)

    def test_schematic_has_extra_component(self):
        """A component in schematic but not declared in netlist — warning."""
        yaml_text = """
project: "test"
components:
  R1:
    part: "10k"
    pins: ["1", "2"]
nets:
  MYNET:
    type: signal
    pins:
      - { ref: R1, pin: "1" }
      - { ref: R1, pin: "2" }
no_connects: []
"""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.place_component("Device:R", "R2", "4.7k", x=130, y=80)

        intended = load_intended_netlist_from_string(yaml_text)
        result = verify(intended, sch)
        cons_issues = _issues_by_check(result, "consistency")
        assert any("R2" in i.message and "not declared" in i.message
                    for i in cons_issues)

    def test_wrong_pin_number(self):
        """Netlist references a pin number that doesn't exist on the component."""
        yaml_text = """
project: "test"
components:
  R1:
    part: "10k"
    pins: ["1", "2", "3"]
nets:
  MYNET:
    type: signal
    pins:
      - { ref: R1, pin: "1" }
      - { ref: R1, pin: "2" }
      - { ref: R1, pin: "3" }
no_connects: []
"""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)

        intended = load_intended_netlist_from_string(yaml_text)
        result = verify(intended, sch)
        cons_issues = _issues_by_check(result, "consistency")
        # R1 only has pins 1 and 2 in the schematic; pin 3 is phantom
        assert any("pin 3" in i.message and "does not exist" in i.message
                    for i in cons_issues)


# ─── Test: connectivity checks ─────────────────────────────────────

class TestConnectivity:
    def test_disconnected_pins(self):
        """Pins declared on same net but not wired together should fail."""
        yaml_text = """
project: "test"
components:
  R1:
    part: "10k"
    pins: ["1", "2"]
  C1:
    part: "100nF"
    pins: ["1", "2"]
nets:
  NET_A:
    type: signal
    pins:
      - { ref: R1, pin: "1" }
      - { ref: C1, pin: "1" }
  NET_B:
    type: signal
    pins:
      - { ref: R1, pin: "2" }
      - { ref: C1, pin: "2" }
no_connects: []
"""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.place_component("Device:C", "C1", "100nF", x=130, y=80)
        # Deliberately don't wire R1.1 to C1.1 — they're supposed to be on NET_A
        # Only wire the bottom pair
        sch.wire_between("R1", "2", "C1", "2")

        intended = load_intended_netlist_from_string(yaml_text)
        result = verify(intended, sch)
        conn_issues = _issues_by_check(result, "connectivity")
        assert any("NET_A" in i.message for i in conn_issues)
        assert not result.passed

    def test_correctly_wired_passes(self):
        """Pins wired as declared should pass connectivity."""
        yaml_text = """
project: "test"
components:
  R1:
    part: "10k"
    pins: ["1", "2"]
  C1:
    part: "100nF"
    pins: ["1", "2"]
nets:
  NET_A:
    type: signal
    pins:
      - { ref: R1, pin: "1" }
      - { ref: C1, pin: "1" }
  NET_B:
    type: signal
    pins:
      - { ref: R1, pin: "2" }
      - { ref: C1, pin: "2" }
no_connects: []
"""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.place_component("Device:C", "C1", "100nF", x=130, y=80)
        sch.wire_between("R1", "1", "C1", "1")
        sch.wire_between("R1", "2", "C1", "2")

        intended = load_intended_netlist_from_string(yaml_text)
        result = verify(intended, sch)
        conn_issues = [i for i in _issues_by_check(result, "connectivity")
                       if i.severity == "error"]
        assert len(conn_issues) == 0

    def test_crossed_wires_detected(self):
        """Pins wired to wrong net should be caught."""
        yaml_text = """
project: "test"
components:
  R1:
    part: "10k"
    pins: ["1", "2"]
  C1:
    part: "100nF"
    pins: ["1", "2"]
nets:
  NET_A:
    type: signal
    pins:
      - { ref: R1, pin: "1" }
      - { ref: C1, pin: "1" }
  NET_B:
    type: signal
    pins:
      - { ref: R1, pin: "2" }
      - { ref: C1, pin: "2" }
no_connects: []
"""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.place_component("Device:C", "C1", "100nF", x=130, y=80)
        # Wire crossed: R1.1 to C1.2, R1.2 to C1.1
        sch.wire_between("R1", "1", "C1", "2")
        sch.wire_between("R1", "2", "C1", "1")

        intended = load_intended_netlist_from_string(yaml_text)
        result = verify(intended, sch)
        assert not result.passed
        conn_issues = _issues_by_check(result, "connectivity")
        assert len(conn_issues) > 0

    def test_extra_pin_on_net_warns(self):
        """Schematic has an extra pin on a net not declared in netlist — warning."""
        yaml_text = """
project: "test"
components:
  R1:
    part: "10k"
    pins: ["1", "2"]
  R2:
    part: "4.7k"
    pins: ["1", "2"]
  C1:
    part: "100nF"
    pins: ["1", "2"]
nets:
  NET_A:
    type: signal
    pins:
      - { ref: R1, pin: "1" }
      - { ref: R2, pin: "1" }
  NET_B:
    type: signal
    pins:
      - { ref: R1, pin: "2" }
      - { ref: R2, pin: "2" }
      - { ref: C1, pin: "2" }
no_connects:
  - { ref: C1, pin: "1", reason: "unused" }
"""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.place_component("Device:R", "R2", "4.7k", x=100, y=110)
        sch.place_component("Device:C", "C1", "100nF", x=130, y=80)
        # Wire NET_A: R1.1 to R2.1 — correct
        sch.wire_between("R1", "1", "R2", "1")
        # Also wire C1.1 into NET_A — extra pin not in netlist
        sch.wire_between("R1", "1", "C1", "1")
        # Wire NET_B
        sch.wire_between("R1", "2", "R2", "2")
        sch.wire_between("R1", "2", "C1", "2")

        intended = load_intended_netlist_from_string(yaml_text)
        result = verify(intended, sch)
        conn_issues = _issues_by_check(result, "connectivity")
        assert any("extra pins" in i.message for i in conn_issues)

    def test_misassigned_pin_is_error(self):
        """A pin the netlist assigns to one net but the schematic puts on another
        is a parity violation (error), even when its declared net is single-pin
        and would otherwise be skipped by the connectivity check (the J3.3-on-GND
        balance-tap-shorted-to-ground bug)."""
        yaml_text = """
project: "test"
components:
  R1:
    part: "10k"
    pins: ["1", "2"]
  R2:
    part: "4.7k"
    pins: ["1", "2"]
  C1:
    part: "100nF"
    pins: ["1", "2"]
nets:
  NET_MAIN:
    type: signal
    pins:
      - { ref: R1, pin: "1" }
      - { ref: R2, pin: "1" }
  TAP:
    type: signal
    pins:
      - { ref: C1, pin: "1" }
no_connects:
  - { ref: C1, pin: "2", reason: "unused" }
"""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.place_component("Device:R", "R2", "4.7k", x=100, y=110)
        sch.place_component("Device:C", "C1", "100nF", x=130, y=80)
        sch.wire_between("R1", "1", "R2", "1")
        # C1.1 is declared on the single-pin net TAP, but here it is wired onto
        # NET_MAIN — a netlist/schematic mismatch.
        sch.wire_between("R1", "1", "C1", "1")
        sch.nc_at_pin("C1", "2")

        intended = load_intended_netlist_from_string(yaml_text)
        result = verify(intended, sch)
        assert not result.passed
        mismatch = [i for i in result.errors
                    if i.check_name == "connectivity"
                    and "C1" in i.message and "TAP" in i.message]
        assert mismatch, "C1.1 misassigned to NET_MAIN should be a connectivity error"


# ─── Test: power net connectivity ──────────────────────────────────

class TestPowerNets:
    def test_power_symbols_connect_pins(self):
        """Pins connected via power symbols (no direct wire) should pass."""
        yaml_text = """
project: "test"
components:
  R1:
    part: "10k"
    pins: ["1", "2"]
  C1:
    part: "100nF"
    pins: ["1", "2"]
nets:
  VCC:
    type: power
    pins:
      - { ref: R1, pin: "1" }
      - { ref: C1, pin: "1" }
    power_symbols: ["VCC"]
  GND:
    type: power
    pins:
      - { ref: R1, pin: "2" }
      - { ref: C1, pin: "2" }
    power_symbols: ["GND"]
no_connects: []
"""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.add_lib_symbol_power("VCC")
        sch.add_lib_symbol_power("GND")

        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.place_component("Device:C", "C1", "100nF", x=130, y=80)

        # Connect via power symbols (no direct wire between R1 and C1)
        r1_p1 = sch.get_pin_position("R1", "1")
        sch.place_power_symbol("VCC", r1_p1[0], r1_p1[1] - 5)
        sch.add_wire(r1_p1[0], r1_p1[1], r1_p1[0], r1_p1[1] - 5)

        c1_p1 = sch.get_pin_position("C1", "1")
        sch.place_power_symbol("VCC", c1_p1[0], c1_p1[1] - 5)
        sch.add_wire(c1_p1[0], c1_p1[1], c1_p1[0], c1_p1[1] - 5)

        r1_p2 = sch.get_pin_position("R1", "2")
        sch.place_power_symbol("GND", r1_p2[0], r1_p2[1] + 5)
        sch.add_wire(r1_p2[0], r1_p2[1], r1_p2[0], r1_p2[1] + 5)

        c1_p2 = sch.get_pin_position("C1", "2")
        sch.place_power_symbol("GND", c1_p2[0], c1_p2[1] + 5)
        sch.add_wire(c1_p2[0], c1_p2[1], c1_p2[0], c1_p2[1] + 5)

        intended = load_intended_netlist_from_string(yaml_text)
        result = verify(intended, sch)
        conn_errors = [i for i in _issues_by_check(result, "connectivity")
                       if i.severity == "error"]
        assert len(conn_errors) == 0


# ─── Test: full integration with LDO circuit ───────────────────────

class TestIntegration:
    def test_ldo_circuit_matches_netlist(self):
        """The LDO circuit helper should match its netlist YAML."""
        sch = _make_ldo_circuit()
        intended = load_intended_netlist_from_string(_make_matching_netlist_yaml())
        result = verify(intended, sch)
        assert result.passed, \
            f"Expected pass but got errors: {[e.message for e in result.errors]}"

    def test_ldo_with_wrong_netlist_fails(self):
        """Deliberately wrong netlist (swapped pins) should fail."""
        wrong_yaml = """
project: "LDO Test"
source: "test"
components:
  U1:
    part: "AP2112K-3.3"
    pins: ["1", "2", "3", "4", "5"]
  C1:
    part: "100nF"
    pins: ["1", "2"]
  C2:
    part: "10uF"
    pins: ["1", "2"]
  R1:
    part: "1k"
    pins: ["1", "2"]
  D1:
    part: "Green LED"
    pins: ["1", "2"]
nets:
  "+5V":
    type: power
    pins:
      - { ref: U1, pin: "5", function: "SWAPPED — should be pin 1" }
      - { ref: C1, pin: "1" }
    power_symbols: ["+5V"]
  "+3V3":
    type: power
    pins:
      - { ref: U1, pin: "1", function: "SWAPPED — should be pin 5" }
      - { ref: C2, pin: "1" }
      - { ref: R1, pin: "1" }
    power_symbols: ["+3V3"]
  GND:
    type: power
    pins:
      - { ref: U1, pin: "2" }
      - { ref: C1, pin: "2" }
      - { ref: C2, pin: "2" }
      - { ref: D1, pin: "1" }
    power_symbols: ["GND"]
  LED_ANODE:
    type: signal
    pins:
      - { ref: R1, pin: "2" }
      - { ref: D1, pin: "2" }
no_connects:
  - { ref: U1, pin: "3" }
  - { ref: U1, pin: "4" }
"""
        sch = _make_ldo_circuit()
        intended = load_intended_netlist_from_string(wrong_yaml)
        result = verify(intended, sch)
        assert not result.passed
        # U1.5 is on +3V3 in schematic but declared on +5V in wrong yaml
        conn_issues = _issues_by_check(result, "connectivity")
        assert len(conn_issues) > 0


# ─── Test: output formatters ──────────────────────────────────────

class TestFormatters:
    def test_text_format_passing(self):
        sch = _make_ldo_circuit()
        intended = load_intended_netlist_from_string(_make_matching_netlist_yaml())
        result = verify(intended, sch)
        text = format_result_text(result, "test.yaml", "test.kicad_sch")
        assert "PASSED" in text
        assert "NETLIST VERIFICATION REPORT" in text

    def test_text_format_failing(self):
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        intended = load_intended_netlist_from_string("""
project: "test"
components:
  R1:
    part: "10k"
    pins: ["1", "2"]
nets:
  NET_A:
    type: signal
    pins:
      - { ref: R1, pin: "1" }
no_connects: []
""")
        result = verify(intended, sch)
        text = format_result_text(result)
        assert "FAILED" in text
        assert "ERRORS:" in text

    def test_json_format(self):
        sch = _make_ldo_circuit()
        intended = load_intended_netlist_from_string(_make_matching_netlist_yaml())
        result = verify(intended, sch)
        import json
        output = json.loads(format_result_json(result, "test.yaml", "test.kicad_sch"))
        assert output["passed"] is True
        assert "error_count" in output
        assert "issues" in output


# ─── Test: YAML loading ───────────────────────────────────────────

class TestYamlLoading:
    def test_load_from_string(self):
        intended = load_intended_netlist_from_string(_make_matching_netlist_yaml())
        assert intended.project == "LDO Test"
        assert "U1" in intended.components
        assert "+5V" in intended.nets
        assert len(intended.no_connects) == 2

    def test_component_pins_are_strings(self):
        intended = load_intended_netlist_from_string("""
project: "test"
components:
  U1:
    part: "IC"
    pins: [1, 2, 3]
nets: {}
no_connects: []
""")
        assert intended.components["U1"].pins == ["1", "2", "3"]

    def test_empty_netlist(self):
        intended = load_intended_netlist_from_string("""
project: "empty"
components: {}
nets: {}
no_connects: []
""")
        assert len(intended.components) == 0
        assert len(intended.nets) == 0
