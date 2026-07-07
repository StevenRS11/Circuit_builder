#!/usr/bin/env python3
"""Tests for W1b: hierarchical sheets — builder emission, loader traversal,
netlist merge through sheet pins, sheet-integrity checks, and the engine's
`blocks:` composition (cloning the real nau7802_dual_loadcell registry block
with per-instance refdes ranges, then self-verifying across the hierarchy).
"""

import hashlib
import os
import sys

_tests_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_tests_dir, "..", "scripts")))

import pytest

from generate_kicad_sch import KicadSchematic
from validate_kicad_sch import load_kicad_sch, validate, extract_netlist
import generate_from_data as engine


# ─── helpers ─────────────────────────────────────────────────────────

def _build_child(path, port="P", extra_internal=False):
    """A child sheet: R1 with hierarchical label `port` on pin 1.
    With extra_internal, R1.2 joins R2.1 on local net MID (R2.2 to GND);
    otherwise R1.2 goes to GND."""
    ch = KicadSchematic("Child")
    ch.add_lib_symbol_resistor()
    ch.add_lib_symbol_power("GND")
    ch.place_component("Device:R", "R1", "10k", 100, 100,
                       footprint="Resistor_SMD:R_0805_2012Metric")
    ch.hlabel_at_pin("R1", "1", port, shape="input")
    if extra_internal:
        ch.place_component("Device:R", "R2", "1k", 130, 100,
                           footprint="Resistor_SMD:R_0805_2012Metric")
        ch.label_at_pin("R1", "2", "MID")
        ch.label_at_pin("R2", "1", "MID")
        ch.gnd_at_pin("R2", "2")
    else:
        ch.gnd_at_pin("R1", "2")
    ch.save(str(path))
    return ch


def _build_parent(path, child_filename, pin_name="P", wire_pin=True):
    """A parent: 2-pin connector, pin 1 on net SIG, sheet pin `pin_name`
    labeled SIG too (so the net crosses the hierarchy)."""
    par = KicadSchematic("Parent")
    par.add_lib_symbol_connector(2)
    par.add_lib_symbol_power("GND")
    par.place_component("Connector_Generic:Conn_01x02", "J1", "Conn_01x02",
                        100, 100, footprint="Connector_PinHeader_2.54mm:"
                        "PinHeader_1x02_P2.54mm_Vertical")
    par.label_at_pin("J1", "1", "SIG")
    par.gnd_at_pin("J1", "2")
    par.add_sheet("s1", child_filename, 160, 80,
                  ports=[(pin_name, "input")])
    if wire_pin:
        par.label_at_sheet_pin("s1", pin_name, "SIG")
    par.save(str(path))
    return par


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ─── builder: sheet emission + round trip ────────────────────────────

class TestBuilderSheets:
    def test_save_and_reload_sheet(self, tmp_path):
        _build_child(tmp_path / "child.kicad_sch")
        _build_parent(tmp_path / "parent.kicad_sch", "child.kicad_sch")

        text = (tmp_path / "parent.kicad_sch").read_text(encoding="utf-8")
        assert '(sheet' in text
        assert '(property "Sheetname" "s1"' in text
        assert '(property "Sheetfile" "child.kicad_sch"' in text
        assert '(pin "P" input' in text

        sch = load_kicad_sch(str(tmp_path / "parent.kicad_sch"),
                             resolve_from_libraries=False)
        assert len(sch.sheets) == 1
        sheet = sch.sheets[0]
        assert sheet.name == "s1"
        assert sheet.filename == "child.kicad_sch"
        assert [p.name for p in sheet.pins] == ["P"]
        assert sheet.child is not None
        assert {c.reference for c in sheet.child.components
                if not c.reference.startswith("#")} == {"R1"}

    def test_duplicate_sheet_name_raises(self):
        sch = KicadSchematic("X")
        sch.add_sheet("s1", "a.kicad_sch", 100, 100, ports=[])
        with pytest.raises(ValueError, match="already placed"):
            sch.add_sheet("s1", "b.kicad_sch", 150, 100, ports=[])

    def test_duplicate_port_raises(self):
        sch = KicadSchematic("X")
        with pytest.raises(ValueError, match="duplicate port"):
            sch.add_sheet("s1", "a.kicad_sch", 100, 100,
                          ports=[("P", "input"), ("P", "output")])


# ─── loader + netlist merge through sheet pins ───────────────────────

class TestHierarchyNetlist:
    def test_port_net_merges_into_parent(self, tmp_path):
        _build_child(tmp_path / "child.kicad_sch")
        _build_parent(tmp_path / "parent.kicad_sch", "child.kicad_sch")
        sch = load_kicad_sch(str(tmp_path / "parent.kicad_sch"),
                             resolve_from_libraries=False)
        netlist = extract_netlist(sch)

        sig = netlist.nets["SIG"]
        assert ("J1", "1") in sig.pins
        assert ("R1", "1") in sig.pins  # through the sheet pin

    def test_child_power_merges_globally(self, tmp_path):
        _build_child(tmp_path / "child.kicad_sch")
        _build_parent(tmp_path / "parent.kicad_sch", "child.kicad_sch")
        sch = load_kicad_sch(str(tmp_path / "parent.kicad_sch"),
                             resolve_from_libraries=False)
        netlist = extract_netlist(sch)
        gnd = netlist.nets["GND"]
        assert ("J1", "2") in gnd.pins
        assert ("R1", "2") in gnd.pins
        assert gnd.is_power

    def test_internal_net_gets_instance_prefix(self, tmp_path):
        _build_child(tmp_path / "child.kicad_sch", extra_internal=True)
        _build_parent(tmp_path / "parent.kicad_sch", "child.kicad_sch")
        sch = load_kicad_sch(str(tmp_path / "parent.kicad_sch"),
                             resolve_from_libraries=False)
        netlist = extract_netlist(sch)
        assert "s1/MID" in netlist.nets
        assert ("R1", "2") in netlist.nets["s1/MID"].pins
        assert ("R2", "1") in netlist.nets["s1/MID"].pins
        assert netlist.nets["s1/MID"].from_sheet

    def test_hierarchical_board_validates_clean(self, tmp_path):
        _build_child(tmp_path / "child.kicad_sch", extra_internal=True)
        _build_parent(tmp_path / "parent.kicad_sch", "child.kicad_sch")
        sch = load_kicad_sch(str(tmp_path / "parent.kicad_sch"),
                             resolve_from_libraries=False)
        result = validate(sch)
        assert result.passed, [i.message for i in result.issues]


# ─── sheet_integrity + hierarchy-wide checks ─────────────────────────

class TestSheetIntegrity:
    def test_missing_child_file_errors(self, tmp_path):
        _build_parent(tmp_path / "parent.kicad_sch", "nonexistent.kicad_sch")
        sch = load_kicad_sch(str(tmp_path / "parent.kicad_sch"),
                             resolve_from_libraries=False)
        result = validate(sch)
        msgs = [i.message for i in result.issues
                if i.check_name == "sheet_integrity"]
        assert any("not found" in m for m in msgs)
        assert not result.passed

    def test_pin_hlabel_parity_both_directions(self, tmp_path):
        # Child declares port B; parent sheet pin is named A.
        _build_child(tmp_path / "child.kicad_sch", port="B")
        _build_parent(tmp_path / "parent.kicad_sch", "child.kicad_sch",
                      pin_name="A")
        sch = load_kicad_sch(str(tmp_path / "parent.kicad_sch"),
                             resolve_from_libraries=False)
        result = validate(sch)
        msgs = [i.message for i in result.issues
                if i.check_name == "sheet_integrity"]
        assert any("pin 'A' has no matching hierarchical label" in m
                   for m in msgs)
        assert any("port 'B' but the sheet symbol has no such pin" in m
                   for m in msgs)

    def test_unwired_sheet_pin_warns(self, tmp_path):
        _build_child(tmp_path / "child.kicad_sch")
        _build_parent(tmp_path / "parent.kicad_sch", "child.kicad_sch",
                      wire_pin=False)
        sch = load_kicad_sch(str(tmp_path / "parent.kicad_sch"),
                             resolve_from_libraries=False)
        result = validate(sch)
        warns = [i for i in result.issues
                 if i.check_name == "sheet_integrity"
                 and i.severity == "warning"]
        assert any("not wired" in i.message for i in warns)

    def test_duplicate_ref_across_hierarchy_errors(self, tmp_path):
        # Parent also places an R1 — collides with the child's R1.
        _build_child(tmp_path / "child.kicad_sch")
        par = KicadSchematic("Parent")
        par.add_lib_symbol_resistor()
        par.add_lib_symbol_power("GND")
        par.place_component("Device:R", "R1", "1k", 100, 100,
                            footprint="Resistor_SMD:R_0805_2012Metric")
        par.label_at_pin("R1", "1", "SIG")
        par.gnd_at_pin("R1", "2")
        par.add_sheet("s1", "child.kicad_sch", 160, 80,
                      ports=[("P", "input")])
        par.label_at_sheet_pin("s1", "P", "SIG")
        par.save(str(tmp_path / "parent.kicad_sch"))

        sch = load_kicad_sch(str(tmp_path / "parent.kicad_sch"),
                             resolve_from_libraries=False)
        result = validate(sch)
        dups = [i for i in result.issues
                if i.check_name == "duplicate_reference"]
        assert dups and "R1" in dups[0].message

    def test_sheet_cycle_raises(self, tmp_path):
        a = KicadSchematic("A")
        a.add_sheet("to_b", "b.kicad_sch", 100, 100, ports=[])
        a.save(str(tmp_path / "a.kicad_sch"))
        b = KicadSchematic("B")
        b.add_sheet("to_a", "a.kicad_sch", 100, 100, ports=[])
        b.save(str(tmp_path / "b.kicad_sch"))
        with pytest.raises(ValueError, match="cycle"):
            load_kicad_sch(str(tmp_path / "a.kicad_sch"),
                           resolve_from_libraries=False)

    def test_nc_pin_not_flagged_single_pin_net(self):
        # Regression: an explicitly NC'd pin must not warn single_pin_net
        # (pre-W1b false positive, surfaced by child-sheet recursion).
        sch = KicadSchematic("NC")
        sch.add_lib_symbol_ic("Custom:TINY", pins=[
            ("1", "IN", "input", "left", 0),
            ("2", "NCP", "no_connect", "right", 0),
        ])
        sch.add_lib_symbol_power("GND")
        sch.place_component("Custom:TINY", "U1", "TINY", 100, 100,
                            footprint="Package_SO:SOIC-8")
        sch.gnd_at_pin("U1", "1")
        sch.nc_at_pin("U1", "2")
        result = validate(sch)
        assert not any(i.check_name == "single_pin_net"
                       for i in result.issues), \
            [i.message for i in result.issues]


# ─── engine: blocks composition (real registry block) ────────────────

NETLIST_ONE = """\
project: "compose_one"
components:
  J1:
    part: "Conn_01x05"
    pins: ["1", "2", "3", "4", "5"]
nets:
  "3V3":
    type: power
    pins: [{ ref: J1, pin: "1" }]
    power_symbols: ["3V3"]
  "I2C_SDA":
    type: signal
    pins: [{ ref: J1, pin: "2" }]
    labels: ["I2C_SDA"]
  "I2C_SCL":
    type: signal
    pins: [{ ref: J1, pin: "3" }]
    labels: ["I2C_SCL"]
  "SCALE1_DRDY":
    type: signal
    pins: [{ ref: J1, pin: "4" }]
    labels: ["SCALE1_DRDY"]
  "GND":
    type: power
    pins: [{ ref: J1, pin: "5" }]
    power_symbols: ["GND"]
no_connects: []
"""

BOM_ONE = """\
# Flat BOM — compose_one

| Ref | Value | Part Number | Manufacturer | Package | Footprint | Type | Notes |
|-----|-------|-------------|--------------|---------|-----------|------|-------|
| J1 | Conn_01x05 | S5B-PH-K-S | JST | PH-5 | Connector_JST:JST_PH_S5B-PH-K-S_1x05_P2.00mm_Horizontal | | |
"""

LAYOUT_ONE = """\
project: "compose_one"
title: "Compose One"
rev: "0.1"
power_nets: ["3V3"]
placements:
  J1: { lib_id: "Connector_Generic:Conn_01x05", x: 100, y: 100 }
blocks:
  scale1:
    block: nau7802_dual_loadcell
    x: 180
    y: 80
    port_map:
      SDA: I2C_SDA
      SCL: I2C_SCL
      DRDY: SCALE1_DRDY
"""


def _write_inputs(tmp_path, netlist=NETLIST_ONE, bom=BOM_ONE,
                  layout=LAYOUT_ONE):
    (tmp_path / "netlist.yaml").write_text(netlist, encoding="utf-8")
    (tmp_path / "bom.md").write_text(bom, encoding="utf-8")
    (tmp_path / "layout.yaml").write_text(layout, encoding="utf-8")
    return (str(tmp_path / "netlist.yaml"), str(tmp_path / "bom.md"),
            str(tmp_path / "layout.yaml"))


class TestEngineBlocks:
    def test_compose_single_instance(self, tmp_path):
        n, b, l = _write_inputs(tmp_path)
        out = str(tmp_path / "board.kicad_sch")
        res = engine.generate(n, b, l, out_path=out, uuid_seed=0)
        assert res.passed, res.errors
        assert os.path.isfile(out)
        clone = tmp_path / "board_scale1.kicad_sch"
        assert clone.is_file()

        sch = load_kicad_sch(out, resolve_from_libraries=False)
        netlist = extract_netlist(sch)
        # Board pin ↔ block pin connected through the port (U2→U102 range)
        sda = netlist.nets["I2C_SDA"]
        assert ("J1", "2") in sda.pins
        assert ("U102", "14") in sda.pins   # NAU7802 SDIO, re-annotated
        # Rails merged globally: block DVDD decoupling on the board 3V3
        assert ("U102", "15") in netlist.nets["3V3"].pins
        # Flat BOM artifact carries the block parts
        flat = (tmp_path / "board_bom_flat.md").read_text(encoding="utf-8")
        assert "U102" in flat and "J104" in flat

    def test_two_instances_get_distinct_ranges(self, tmp_path):
        netlist2 = NETLIST_ONE.replace('pins: [{ ref: J1, pin: "5" }]\n    power_symbols: ["GND"]',
                                       'pins: [{ ref: J1, pin: "5" }]\n    power_symbols: ["GND"]')
        # extend the board with a second I2C bus + DRDY on a bigger connector
        netlist2 = """\
project: "compose_two"
components:
  J1:
    part: "Conn_01x08"
    pins: ["1", "2", "3", "4", "5", "6", "7", "8"]
nets:
  "3V3":
    type: power
    pins: [{ ref: J1, pin: "1" }]
    power_symbols: ["3V3"]
  "I2C1_SDA":
    type: signal
    pins: [{ ref: J1, pin: "2" }]
    labels: ["I2C1_SDA"]
  "I2C1_SCL":
    type: signal
    pins: [{ ref: J1, pin: "3" }]
    labels: ["I2C1_SCL"]
  "SCALE1_DRDY":
    type: signal
    pins: [{ ref: J1, pin: "4" }]
    labels: ["SCALE1_DRDY"]
  "I2C2_SDA":
    type: signal
    pins: [{ ref: J1, pin: "5" }]
    labels: ["I2C2_SDA"]
  "I2C2_SCL":
    type: signal
    pins: [{ ref: J1, pin: "6" }]
    labels: ["I2C2_SCL"]
  "SCALE2_DRDY":
    type: signal
    pins: [{ ref: J1, pin: "7" }]
    labels: ["SCALE2_DRDY"]
  "GND":
    type: power
    pins: [{ ref: J1, pin: "8" }]
    power_symbols: ["GND"]
no_connects: []
"""
        bom2 = BOM_ONE.replace("Conn_01x05", "Conn_01x08").replace(
            "S5B-PH-K-S", "S8B-PH-K-S").replace("PH-5", "PH-8").replace(
            "S5B-PH-K-S_1x05", "S8B-PH-K-S_1x08")
        layout2 = """\
project: "compose_two"
title: "Compose Two"
rev: "0.1"
power_nets: ["3V3"]
placements:
  J1: { lib_id: "Connector_Generic:Conn_01x08", x: 100, y: 100 }
blocks:
  scale1:
    block: nau7802_dual_loadcell
    x: 180
    y: 60
    port_map: { SDA: I2C1_SDA, SCL: I2C1_SCL, DRDY: SCALE1_DRDY }
  scale2:
    block: nau7802_dual_loadcell
    x: 180
    y: 120
    port_map: { SDA: I2C2_SDA, SCL: I2C2_SCL, DRDY: SCALE2_DRDY }
"""
        n, b, l = _write_inputs(tmp_path, netlist2, bom2, layout2)
        out = str(tmp_path / "board.kicad_sch")
        res = engine.generate(n, b, l, out_path=out, uuid_seed=0)
        assert res.passed, res.errors

        sch = load_kicad_sch(out, resolve_from_libraries=False)
        netlist = extract_netlist(sch)
        assert ("U102", "14") in netlist.nets["I2C1_SDA"].pins
        assert ("U202", "14") in netlist.nets["I2C2_SDA"].pins
        # Internal nets are scoped per instance
        assert "scale1/AVDD" in netlist.nets
        assert "scale2/AVDD" in netlist.nets
        result = validate(sch)
        assert result.passed, [i.message for i in result.issues]

    def test_byte_deterministic_with_seed(self, tmp_path):
        n, b, l = _write_inputs(tmp_path)
        out1 = tmp_path / "one" / "board.kicad_sch"
        out2 = tmp_path / "two" / "board.kicad_sch"
        for out in (out1, out2):
            out.parent.mkdir()
            res = engine.generate(n, b, l, out_path=str(out), uuid_seed=0)
            assert res.passed, res.errors
        assert _sha(out1) == _sha(out2)
        assert _sha(out1.parent / "board_scale1.kicad_sch") == \
            _sha(out2.parent / "board_scale1.kicad_sch")

    def test_gate_unmapped_port(self, tmp_path):
        layout = LAYOUT_ONE.replace("      DRDY: SCALE1_DRDY\n", "")
        n, b, l = _write_inputs(tmp_path, layout=layout)
        res = engine.generate(n, b, l, out_path=str(tmp_path / "o.kicad_sch"),
                              uuid_seed=0)
        assert not res.passed
        assert any("port 'DRDY' is not mapped" in e for e in res.errors)

    def test_gate_unknown_port(self, tmp_path):
        layout = LAYOUT_ONE + "      BOGUS: I2C_SDA\n"
        n, b, l = _write_inputs(tmp_path, layout=layout)
        res = engine.generate(n, b, l, out_path=str(tmp_path / "o.kicad_sch"),
                              uuid_seed=0)
        assert not res.passed
        assert any("unknown port 'BOGUS'" in e for e in res.errors)

    def test_gate_port_maps_to_undeclared_net(self, tmp_path):
        layout = LAYOUT_ONE.replace("DRDY: SCALE1_DRDY", "DRDY: PHANTOM_NET")
        n, b, l = _write_inputs(tmp_path, layout=layout)
        res = engine.generate(n, b, l, out_path=str(tmp_path / "o.kicad_sch"),
                              uuid_seed=0)
        assert not res.passed
        assert any("PHANTOM_NET" in e and "neither declared" in e
                   for e in res.errors)

    def test_gate_missing_rail(self, tmp_path):
        # Block needs 3V3; strip it from power_nets (and give the netlist a
        # different rail so the layout stays otherwise valid).
        netlist = NETLIST_ONE.replace('"3V3"', '"5V0"')
        layout = LAYOUT_ONE.replace('power_nets: ["3V3"]',
                                    'power_nets: ["5V0"]')
        n, b, l = _write_inputs(tmp_path, netlist=netlist, layout=layout)
        res = engine.generate(n, b, l, out_path=str(tmp_path / "o.kicad_sch"),
                              uuid_seed=0)
        assert not res.passed
        assert any("requires rail '3V3'" in e for e in res.errors)

    def test_gate_refdes_collision(self, tmp_path):
        # Board already owns U102 — the instance range must refuse to collide.
        netlist = NETLIST_ONE.replace(
            'components:\n  J1:',
            'components:\n  U102:\n    part: "Decoy"\n    pins: ["1"]\n  J1:')
        netlist = netlist.replace(
            '  "GND":\n    type: power\n    pins: [{ ref: J1, pin: "5" }]',
            '  "GND":\n    type: power\n    pins: [{ ref: J1, pin: "5" }, '
            '{ ref: U102, pin: "1" }]')
        bom = BOM_ONE + "| U102 | Decoy | X1 | X | X | Resistor_SMD:R_0805_2012Metric | | |\n"
        layout = LAYOUT_ONE.replace(
            "placements:",
            'placements:\n  U102: { lib_id: "Device:R", x: 60, y: 100 }')
        # Device:R has pins 1,2 but the netlist only declares pin 1 — dodge
        # the pin-set gate by using a 1-pin connector instead.
        layout = layout.replace('U102: { lib_id: "Device:R"',
                                'U102: { lib_id: "Connector_Generic:Conn_01x01"')
        bom = bom.replace("| U102 | Decoy | X1 | X | X | Resistor_SMD:R_0805_2012Metric | | |",
                          "| U102 | Decoy | X1 | X | X | Connector_PinHeader_2.54mm:PinHeader_1x01_P2.54mm_Vertical | | |")
        n, b, l = _write_inputs(tmp_path, netlist=netlist, bom=bom,
                                layout=layout)
        res = engine.generate(n, b, l, out_path=str(tmp_path / "o.kicad_sch"),
                              uuid_seed=0)
        assert not res.passed
        assert any("collides" in e and "U102" in e for e in res.errors)

    def test_failed_build_removes_clones(self, tmp_path):
        # GND on pin 2 sends its power drop across the label stubs of the
        # pins below — the missing_junction gate must block AND the engine
        # must not leave orphan clone files behind.
        netlist = NETLIST_ONE.replace(
            '"I2C_SDA":\n    type: signal\n    pins: [{ ref: J1, pin: "2" }]',
            '"I2C_SDA":\n    type: signal\n    pins: [{ ref: J1, pin: "5" }]')
        netlist = netlist.replace(
            '"GND":\n    type: power\n    pins: [{ ref: J1, pin: "5" }]',
            '"GND":\n    type: power\n    pins: [{ ref: J1, pin: "2" }]')
        n, b, l = _write_inputs(tmp_path, netlist=netlist)
        out = str(tmp_path / "board.kicad_sch")
        res = engine.generate(n, b, l, out_path=out, uuid_seed=0)
        assert not res.passed
        assert any("missing_junction" in e for e in res.errors)
        assert not os.path.isfile(out)
        assert not (tmp_path / "board_scale1.kicad_sch").is_file()

    def test_missing_block_bundle(self, tmp_path):
        layout = LAYOUT_ONE.replace("nau7802_dual_loadcell", "no_such_block")
        n, b, l = _write_inputs(tmp_path, layout=layout)
        res = engine.generate(n, b, l, out_path=str(tmp_path / "o.kicad_sch"),
                              uuid_seed=0)
        assert not res.passed
        assert any("no_such_block" in e and "bundle incomplete" in e
                   for e in res.errors)
