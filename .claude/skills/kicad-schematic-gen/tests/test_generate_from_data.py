"""Unit tests for the data-driven schematic engine (generate_from_data.py).

Synthetic 2-4 component designs, fully offline. Covers the join (value/footprint
from BOM, geometry from layout), symbol dispatch, netlist-driven wiring, and each
pre-flight gate. The J3-class short regression guard lives in the integration test.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from generate_from_data import (
    EngineError, build_schematic, generate, self_verify,
    load_layout_from_string,
)
from verify_netlist import load_intended_netlist_from_string
from cross_check_bom import load_bom_from_markdown


# ─── Synthetic fixture: a tiny LDO-style board ───────────────────────
NETLIST = """
project: "unit"
components:
  U1: { part: "REG", pins: ["1", "2", "3"] }
  R1: { part: "10k", pins: ["1", "2"] }
  C1: { part: "100nF", pins: ["1", "2"] }
  J1: { part: "conn", pins: ["1", "2"] }
nets:
  "+3V3":
    type: power
    pins:
      - { ref: U1, pin: "3" }
      - { ref: C1, pin: "1" }
      - { ref: R1, pin: "1" }
    power_symbols: ["+3V3"]
  GND:
    type: power
    pins:
      - { ref: U1, pin: "1" }
      - { ref: C1, pin: "2" }
      - { ref: J1, pin: "2" }
    power_symbols: ["GND"]
  VIN_NET:
    type: signal
    pins:
      - { ref: U1, pin: "2" }
      - { ref: J1, pin: "1" }
no_connects:
  - { ref: R1, pin: "2", reason: "test NC" }
"""

BOM = """
| Ref | Value | Footprint |
|-----|-------|-----------|
| U1 | REG | Package_TO_SOT_SMD:SOT-23 |
| R1 | 10k | Resistor_SMD:R_0805_2012Metric |
| C1 | 100nF | Capacitor_SMD:C_0805_2012Metric |
| J1 | conn | Connector_Generic:dummy |
"""

LAYOUT = """
project: "unit"
title: "unit test"
power_nets: ["+3V3"]
placements:
  U1: { lib_id: "custom:REG", x: 100, y: 100 }
  R1: { lib_id: "Device:R", x: 120, y: 100 }
  C1: { lib_id: "Device:C", x: 140, y: 100 }
  J1: { lib_id: "Connector_Generic:Conn_01x02", x: 60, y: 100 }
symbols:
  "custom:REG":
    ref_prefix: U
    width: 10.16
    pins:
      - ["1", "GND", "power_in", "bottom", 0]
      - ["2", "VIN", "input", "left", 0]
      - ["3", "VOUT", "output", "right", 0]
"""


def _load(netlist=NETLIST, bom=BOM, layout=LAYOUT):
    return (load_intended_netlist_from_string(netlist),
            load_bom_from_markdown(bom),
            load_layout_from_string(layout))


def _comp(sch, ref):
    for c in sch.components:
        if c.reference == ref:
            return c
    return None


# ─── Happy path: join + placement + wiring ───────────────────────────
class TestBuild:
    def test_builds_all_components(self):
        sch = build_schematic(*_load())
        for ref in ("U1", "R1", "C1", "J1"):
            assert _comp(sch, ref) is not None, f"{ref} not placed"

    def test_value_and_footprint_come_from_bom(self):
        # Layout carries no value/footprint — they must be pulled from the BOM.
        sch = build_schematic(*_load())
        u1 = _comp(sch, "U1")
        assert u1.value == "REG"
        assert u1.footprint == "Package_TO_SOT_SMD:SOT-23"
        c1 = _comp(sch, "C1")
        assert c1.value == "100nF"
        assert c1.footprint == "Capacitor_SMD:C_0805_2012Metric"

    def test_placement_geometry_from_layout(self):
        # Geometry comes from the layout (snapped + possibly nudged), not the BOM.
        # Robust to small auto-nudges: R1 lands near its own layout x (120), far
        # from C1's (140).
        sch = build_schematic(*_load())
        r1 = _comp(sch, "R1")
        assert abs(r1.x - 120) < abs(r1.x - 140)
        assert abs(r1.y - 100) <= 2.54

    def test_passive_and_connector_symbols_registered(self):
        sch = build_schematic(*_load())
        assert "Device:R" in sch.lib_symbols
        assert "Device:C" in sch.lib_symbols
        assert "Connector_Generic:Conn_01x02" in sch.lib_symbols
        assert "custom:REG" in sch.lib_symbols

    def test_power_gnd_and_label_wiring(self):
        sch = build_schematic(*_load())
        values = {c.value for c in sch.components}
        assert "GND" in values            # GND power symbol placed
        assert "+3V3" in values           # declared power net -> power symbol
        assert any(l.text == "VIN_NET" for l in sch.labels)  # signal -> label

    def test_no_connect_applied(self):
        sch = build_schematic(*_load())
        assert len(sch.no_connects) >= 1  # R1.2

    def test_self_verify_clean(self):
        netlist, bom, layout = _load()
        sch = build_schematic(netlist, bom, layout)
        errors, _ = self_verify(netlist, bom, sch)
        assert errors == [], errors

    def test_generate_saves_when_clean(self, tmp_path):
        out = tmp_path / "unit.kicad_sch"
        # Write the fixture strings to files for the file-based entry point.
        nf, bf, lf = tmp_path / "n.yaml", tmp_path / "b.md", tmp_path / "l.yaml"
        nf.write_text(NETLIST); bf.write_text(BOM); lf.write_text(LAYOUT)
        res = generate(str(nf), str(bf), str(lf), out_path=str(out))
        assert res.passed, res.errors
        assert out.exists()

    def test_deterministic_with_seed(self, tmp_path):
        pa, pb = tmp_path / "a.kicad_sch", tmp_path / "b.kicad_sch"
        build_schematic(*_load(), uuid_seed=7).save(str(pa))
        build_schematic(*_load(), uuid_seed=7).save(str(pb))
        assert pa.read_text() == pb.read_text()


# ─── Pre-flight gates (each must raise EngineError) ──────────────────
class TestGates:
    def test_missing_placement(self):
        layout = LAYOUT.replace(
            '  U1: { lib_id: "custom:REG", x: 100, y: 100 }\n', "")
        with pytest.raises(EngineError, match="no placement"):
            build_schematic(load_intended_netlist_from_string(NETLIST),
                            load_bom_from_markdown(BOM),
                            load_layout_from_string(layout))

    def test_missing_bom_line(self):
        bom = BOM.replace(
            "| R1 | 10k | Resistor_SMD:R_0805_2012Metric |\n", "")
        with pytest.raises(EngineError, match="no BOM line"):
            build_schematic(load_intended_netlist_from_string(NETLIST),
                            load_bom_from_markdown(bom),
                            load_layout_from_string(LAYOUT))

    def test_placement_not_in_netlist(self):
        layout = LAYOUT.replace(
            "placements:",
            'placements:\n  X9: { lib_id: "Device:R", x: 10, y: 10 }')
        with pytest.raises(EngineError, match="not declared in netlist"):
            build_schematic(load_intended_netlist_from_string(NETLIST),
                            load_bom_from_markdown(BOM),
                            load_layout_from_string(layout))

    def test_symbol_pinset_mismatch(self):
        # Drop pin "3" from the symbol — must not match netlist pin-set.
        layout = LAYOUT.replace(
            '      - ["3", "VOUT", "output", "right", 0]\n', "")
        with pytest.raises(EngineError, match="pin-set"):
            build_schematic(load_intended_netlist_from_string(NETLIST),
                            load_bom_from_markdown(BOM),
                            load_layout_from_string(layout))

    def test_pin_in_no_net(self):
        # Remove VIN_NET entirely so U1.2 and J1.1 are unassigned.
        netlist = NETLIST.split("  VIN_NET:")[0] + \
            "no_connects:\n  - { ref: R1, pin: \"2\", reason: \"test NC\" }\n"
        with pytest.raises(EngineError, match="not assigned to any net"):
            build_schematic(load_intended_netlist_from_string(netlist),
                            load_bom_from_markdown(BOM),
                            load_layout_from_string(LAYOUT))

    def test_unknown_lib_id_without_symbol(self):
        # Point U1's placement at a lib_id with no symbols: entry (the symbols
        # key stays "custom:REG", so "Mystery:PART" is genuinely undefined).
        layout = LAYOUT.replace(
            '  U1: { lib_id: "custom:REG", x: 100, y: 100 }',
            '  U1: { lib_id: "Mystery:PART", x: 100, y: 100 }')
        with pytest.raises(EngineError, match="no entry in the layout"):
            build_schematic(load_intended_netlist_from_string(NETLIST),
                            load_bom_from_markdown(BOM),
                            load_layout_from_string(layout))


# ─── Regression: missing_junction is a blocking error ────────────────
class TestMissingJunctionBlocking:
    """A label-based schematic has no intentional T-joints, so a missing junction
    is an unintended wire collision (the balance-tap-to-GND short class). The engine
    must promote it to a blocking error. Synthetic repro: a 4-pin connector with GND
    on pin 1 and a label on pin 3 collides when unrotated; rotating it so the pins
    face down gives each its own drop column and clears it."""

    _NET = """
project: t
components:
  J1: { part: c4, pins: ["1", "2", "3", "4"] }
  R1: { part: 10k, pins: ["1", "2"] }
nets:
  GND:
    type: power
    pins:
      - { ref: J1, pin: "1" }
      - { ref: R1, pin: "2" }
    power_symbols: ["GND"]
  NETB:
    type: signal
    pins:
      - { ref: J1, pin: "3" }
      - { ref: R1, pin: "1" }
no_connects:
  - { ref: J1, pin: "2", reason: nc }
  - { ref: J1, pin: "4", reason: nc }
"""
    _BOM = "| Ref | Value | Footprint |\n|--|--|--|\n| J1 | c4 | F:F |\n| R1 | 10k | F:F |\n"

    def _verify(self, rotation):
        rot = f", rotation: {rotation}" if rotation else ""
        layout = f"""
project: t
power_nets: []
placements:
  J1: {{ lib_id: "Connector_Generic:Conn_01x04", x: 100, y: 100{rot} }}
  R1: {{ lib_id: "Device:R", x: 130, y: 100 }}
"""
        netlist = load_intended_netlist_from_string(self._NET)
        bom = load_bom_from_markdown(self._BOM)
        sch = build_schematic(netlist, bom, load_layout_from_string(layout), uuid_seed=0)
        return self_verify(netlist, bom, sch)

    def test_collision_is_blocking_error(self):
        errors, _ = self._verify(rotation=None)
        assert any("missing_junction" in e for e in errors), errors

    def test_rotation_clears_it(self):
        errors, _ = self._verify(rotation=270)
        assert not any("missing_junction" in e for e in errors), errors

    def test_policy_lists_missing_junction(self):
        from generate_from_data import BLOCKING_VALIDATE_CHECKS
        assert "missing_junction" in BLOCKING_VALIDATE_CHECKS
