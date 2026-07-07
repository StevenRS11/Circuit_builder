#!/usr/bin/env python3
"""Tests for the W1a block pipeline: extract_block.py + check_block.py +
hierarchical-label support in the builder/loader.

The donor board is built with the real builder and saved to disk, so
extraction exercises the same file path a validated board would: a custom
sensor IC (in a personal footprint library, to exercise promotion), its
passives, and an external MCU + connector the block must NOT absorb —
their shared nets become the port contract.
"""

import os
import sys

_tests_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_tests_dir, "..", "scripts")))

import pytest

from generate_kicad_sch import KicadSchematic
from validate_kicad_sch import load_kicad_sch, validate
import extract_block as xb
import check_block as cb


SENSE8_MOD = '(footprint "SENSE-8" (version 20221018) (generator pcbnew)\n' \
             '  (layer "F.Cu") (attr smd)\n)\n'


def _build_donor(tmp_path):
    """Donor board: U1 sensor + R1 pull-up + C1 decouple (the block), plus
    external U2 MCU sharing I2C/DRDY nets and J-side sense nets."""
    fplib = tmp_path / "TestFp.pretty"
    fplib.mkdir()
    (fplib / "SENSE-8.kicad_mod").write_text(SENSE8_MOD, encoding="utf-8")

    sch = KicadSchematic("Donor Board")
    sch.add_lib_symbol_resistor()
    sch.add_lib_symbol_capacitor()
    sch.add_lib_symbol_ic("Custom:SENSE8", pins=[
        ("1", "VDD", "power_in", "top", 0),
        ("2", "GND", "power_in", "bottom", 0),
        ("3", "SDA", "bidirectional", "right", 0),
        ("4", "SCL", "input", "right", 1),
        ("5", "DRDY", "output", "right", 2),
        ("6", "INP", "input", "left", 0),
        ("7", "INN", "input", "left", 1),
        ("8", "TEST", "input", "left", 2),
    ])
    sch.add_lib_symbol_ic("Custom:MCUX", pins=[
        ("1", "IO1", "bidirectional", "left", 0),
        ("2", "IO2", "bidirectional", "left", 1),
        ("3", "IO3", "bidirectional", "left", 2),
        ("4", "GND", "power_in", "bottom", 0),
    ])
    sch.add_lib_symbol_power("+3V3")
    sch.add_lib_symbol_power("GND")

    sch.place_component("Custom:SENSE8", "U1", "SENSE8", 100, 100,
                        footprint="TestFp:SENSE-8",
                        MPN="SENSE8-MPN", Manufacturer="TestCo",
                        Package="SOP-8")
    sch.place_component("Device:R", "R1", "10k", 160, 60,
                        footprint="Resistor_SMD:R_0805_2012Metric",
                        MPN="RC0805FR-0710KL", Manufacturer="YAGEO")
    sch.place_component("Device:C", "C1", "100nF", 60, 60,
                        footprint="Capacitor_SMD:C_0805_2012Metric",
                        MPN="CL21B104KBCNNNC", Manufacturer="Samsung")
    sch.place_component("Custom:MCUX", "U2", "MCUX", 220, 100,
                        footprint="Package_SO:SOIC-8")

    # Block-internal power
    sch.power_at_pin("U1", "1", "+3V3")
    sch.gnd_at_pin("U1", "2")
    sch.power_at_pin("C1", "1", "+3V3")
    sch.gnd_at_pin("C1", "2")
    sch.power_at_pin("R1", "1", "+3V3")
    # Boundary nets (shared with U2)
    sch.label_at_pin("U1", "3", "I2C_SDA")
    sch.label_at_pin("R1", "2", "I2C_SDA")     # pull-up joins the boundary net
    sch.label_at_pin("U2", "1", "I2C_SDA")
    sch.label_at_pin("U1", "4", "I2C_SCL")
    sch.label_at_pin("U2", "2", "I2C_SCL")
    sch.label_at_pin("U1", "5", "NAU_DRDY")
    sch.label_at_pin("U2", "3", "NAU_DRDY")
    # Sense inputs stay internal to the donor's edge (external side absent):
    sch.label_at_pin("U1", "6", "SENSE_P")
    sch.label_at_pin("U1", "7", "SENSE_N")
    sch.nc_at_pin("U1", "8")
    sch.gnd_at_pin("U2", "4")

    path = str(tmp_path / "donor.kicad_sch")
    sch.save(path)
    return path, str(fplib)


PORTS = {
    "SDA": ("I2C_SDA", "bidirectional"),
    "SCL": ("I2C_SCL", "input"),
    "DRDY": ("NAU_DRDY", "output"),
    "LC_P": ("SENSE_P", "input"),
    "LC_N": ("SENSE_N", "input"),
}


@pytest.fixture()
def extracted(tmp_path):
    donor, fplib = _build_donor(tmp_path)
    blocks_dir = str(tmp_path / "blocks")
    summary = xb.extract_block(
        donor, "sense8_frontend", ["U1", "R1", "C1"], dict(PORTS),
        blocks_dir=blocks_dir, desc="Test sensor front-end",
        validated_on="Donor Board rev1", bench_date="2026-07-01",
        extra_fp=[f"TestFp={fplib}"])
    return summary, blocks_dir, donor


class TestHierarchicalLabels:
    def test_builder_emits_and_loader_reads(self, tmp_path):
        sch = KicadSchematic("HL Test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", 100, 100,
                            footprint="Resistor_SMD:R_0805_2012Metric")
        sch.hlabel_at_pin("R1", "1", "PORT_A", shape="input")
        sch.label_at_pin("R1", "2", "INSIDE")
        path = str(tmp_path / "hl.kicad_sch")
        sch.save(path)

        loaded = load_kicad_sch(path, resolve_from_libraries=False)
        assert [(h.text, h.shape) for h in loaded.hierarchical_labels] == \
            [("PORT_A", "input")]
        # the hierarchical label names its net exactly like a label would
        from validate_kicad_sch import extract_netlist
        netlist = extract_netlist(loaded)
        assert netlist.get_net_for_pin("R1", "1") == "PORT_A"
        assert validate(loaded).passed


class TestExtraction:
    def test_summary_shape(self, extracted):
        summary, _bd, _donor = extracted
        assert summary["components"] == 3
        assert summary["rails"] == ["+3V3", "GND"]
        assert sorted(summary["ports"]) == sorted(PORTS)
        assert summary["footprints_promoted"] == ["CircuitBlocks:SENSE-8"]

    def test_bundle_files(self, extracted):
        _s, blocks_dir, _donor = extracted
        d = os.path.join(blocks_dir, "sense8_frontend")
        for f in ("block.yaml", "sheet.kicad_sch", "netlist.yaml", "bom.md",
                  "layout_intent.md"):
            assert os.path.isfile(os.path.join(d, f)), f
        assert os.path.isfile(os.path.join(
            blocks_dir, "footprints", "CircuitBlocks.pretty", "SENSE-8.kicad_mod"))

    def test_sheet_ports_and_promoted_footprint(self, extracted):
        _s, blocks_dir, _donor = extracted
        sheet = load_kicad_sch(
            os.path.join(blocks_dir, "sense8_frontend", "sheet.kicad_sch"),
            resolve_from_libraries=False)
        assert {h.text for h in sheet.hierarchical_labels} == set(PORTS)
        u1 = next(c for c in sheet.components if c.reference == "U1")
        assert u1.footprint == "CircuitBlocks:SENSE-8"
        # identity fields survived extraction
        assert u1.extra_properties["MPN"] == "SENSE8-MPN"
        # the external MCU was not absorbed
        assert not any(c.reference == "U2" for c in sheet.components)

    def test_nc_carried(self, extracted):
        _s, blocks_dir, _donor = extracted
        with open(os.path.join(blocks_dir, "sense8_frontend", "netlist.yaml"),
                  encoding="utf-8") as f:
            text = f.read()
        assert "no_connects:" in text and 'pin: "8"' in text

    def test_unmapped_boundary_net_is_error(self, tmp_path):
        donor, fplib = _build_donor(tmp_path)
        ports = {k: v for k, v in PORTS.items() if k != "SCL"}
        with pytest.raises(ValueError, match="I2C_SCL"):
            xb.extract_block(donor, "b", ["U1", "R1", "C1"], ports,
                             blocks_dir=str(tmp_path / "blocks"),
                             extra_fp=[f"TestFp={fplib}"])

    def test_phantom_port_net_is_error(self, tmp_path):
        donor, fplib = _build_donor(tmp_path)
        ports = dict(PORTS)
        ports["GHOST"] = ("NO_SUCH_NET", "input")
        with pytest.raises(ValueError, match="GHOST"):
            xb.extract_block(donor, "b", ["U1", "R1", "C1"], ports,
                             blocks_dir=str(tmp_path / "blocks"),
                             extra_fp=[f"TestFp={fplib}"])

    def test_bad_shape_is_error(self, tmp_path):
        donor, fplib = _build_donor(tmp_path)
        ports = dict(PORTS)
        ports["SDA"] = ("I2C_SDA", "sideways")
        with pytest.raises(ValueError, match="sideways"):
            xb.extract_block(donor, "b", ["U1", "R1", "C1"], ports,
                             blocks_dir=str(tmp_path / "blocks"),
                             extra_fp=[f"TestFp={fplib}"])


class TestCheckBlock:
    def test_extracted_block_passes(self, extracted):
        _s, blocks_dir, _donor = extracted
        passed, issues = cb.check_block(
            os.path.join(blocks_dir, "sense8_frontend"))
        assert passed, [i.message for i in issues if i.severity == "error"]
        # judgment TODOs are flagged as warnings (strict would fail)
        assert any(i.check == "todo" for i in issues)

    def test_contract_port_drift_fails(self, extracted):
        _s, blocks_dir, _donor = extracted
        block_yaml = os.path.join(blocks_dir, "sense8_frontend", "block.yaml")
        with open(block_yaml, encoding="utf-8") as f:
            text = f.read()
        with open(block_yaml, "w", encoding="utf-8") as f:
            f.write(text + '\n')
        # add a phantom port the sheet doesn't have
        with open(block_yaml, "a", encoding="utf-8") as f:
            f.write('')
        text2 = text.replace(
            "ports:",
            'ports:\n  - { name: "PHANTOM", dir: input, class: "", note: "x" }',
            1)
        with open(block_yaml, "w", encoding="utf-8") as f:
            f.write(text2)
        passed, issues = cb.check_block(
            os.path.join(blocks_dir, "sense8_frontend"))
        assert not passed
        assert any(i.check == "port_parity" for i in issues)

    def test_foreign_footprint_source_fails(self, extracted):
        _s, blocks_dir, _donor = extracted
        block_yaml = os.path.join(blocks_dir, "sense8_frontend", "block.yaml")
        with open(block_yaml, encoding="utf-8") as f:
            text = f.read()
        with open(block_yaml, "w", encoding="utf-8") as f:
            f.write(text.replace("source: builtin", "source: my_random_lib"))
        passed, issues = cb.check_block(
            os.path.join(blocks_dir, "sense8_frontend"))
        assert not passed
        assert any("two-source policy" in i.message for i in issues)


class TestAutoNameCollision:
    def test_label_named_like_auto_net_is_not_clobbered(self, tmp_path):
        """Regression (found extracting the NAU7802 block from DualScale): a
        label literally named '_NET_1' plus an unlabeled floating group used
        to collide — the auto-namer overwrote the labeled net in the dict."""
        from validate_kicad_sch import extract_netlist
        sch = KicadSchematic("Collide")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", 100, 100,
                            footprint="Resistor_SMD:R_0805_2012Metric")
        sch.place_component("Device:R", "R2", "10k", 160, 100,
                            footprint="Resistor_SMD:R_0805_2012Metric")
        sch.label_at_pin("R1", "1", "_NET_1")
        sch.label_at_pin("R1", "2", "_NET_2")
        # R2 left fully unconnected -> two auto-named single-pin groups
        path = str(tmp_path / "collide.kicad_sch")
        sch.save(path)
        netlist = extract_netlist(load_kicad_sch(path, resolve_from_libraries=False))
        assert netlist.get_net_for_pin("R1", "1") == "_NET_1"
        assert netlist.get_net_for_pin("R1", "2") == "_NET_2"
        # the floating pins got fresh names, not the labeled ones
        assert netlist.get_net_for_pin("R2", "1") not in ("_NET_1", "_NET_2")


class TestForcedRail:
    def test_label_powered_board_needs_rail_flag(self, tmp_path):
        """DualScale's 3V3 is a plain label net (no power symbols); --rail
        forces rail treatment so the block gets power symbols + a rails entry."""
        donor, fplib = _build_donor(tmp_path)
        # rewire: donor uses power symbols; simulate a label-only rail by
        # forcing a signal net that goes nowhere external? Use the real donor
        # net I2C_SDA as a negative control instead: forcing a rail on it
        # must work mechanically, and a port on a rail must be refused.
        ports = {k: v for k, v in PORTS.items() if k != "SDA"}
        summary = xb.extract_block(
            donor, "railtest", ["U1", "R1", "C1"], ports,
            blocks_dir=str(tmp_path / "blocks"),
            extra_fp=[f"TestFp={fplib}"], forced_rails=["I2C_SDA"])
        assert "I2C_SDA" in summary["rails"]
        passed, issues = cb.check_block(
            os.path.join(str(tmp_path / "blocks"), "railtest"))
        assert passed, [i.message for i in issues if i.severity == "error"]

    def test_port_on_forced_rail_is_refused(self, tmp_path):
        donor, fplib = _build_donor(tmp_path)
        with pytest.raises(ValueError, match="power nets cannot be ports"):
            xb.extract_block(donor, "railport", ["U1", "R1", "C1"], dict(PORTS),
                             blocks_dir=str(tmp_path / "blocks"),
                             extra_fp=[f"TestFp={fplib}"],
                             forced_rails=["I2C_SDA"])

    def test_rail_must_touch_block(self, tmp_path):
        donor, fplib = _build_donor(tmp_path)
        with pytest.raises(ValueError, match="does not touch"):
            xb.extract_block(donor, "railmiss", ["U1", "R1", "C1"], dict(PORTS),
                             blocks_dir=str(tmp_path / "blocks"),
                             extra_fp=[f"TestFp={fplib}"],
                             forced_rails=["NO_SUCH_NET"])


class TestInternalAutoNetRenaming:
    def test_source_auto_names_do_not_travel(self, tmp_path):
        """Internal nets known only by source auto-names (_NET_7) are renamed
        to IC-anchored names (N_U1_6) — auto-names are position-derived and
        must not become a reusable block's net names."""
        fplib = tmp_path / "TestFp.pretty"
        fplib.mkdir()
        (fplib / "SENSE-8.kicad_mod").write_text(SENSE8_MOD, encoding="utf-8")
        sch = KicadSchematic("Wired Donor")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_ic("Custom:SENSE8", pins=[
            ("1", "VDD", "power_in", "top", 0),
            ("2", "GND", "power_in", "bottom", 0),
            ("6", "INP", "input", "left", 0),
        ])
        sch.add_lib_symbol_power("+3V3")
        sch.add_lib_symbol_power("GND")
        sch.place_component("Custom:SENSE8", "U1", "SENSE8", 100, 100,
                            footprint="TestFp:SENSE-8")
        sch.place_component("Device:R", "R1", "1k", 140, 100,
                            footprint="Resistor_SMD:R_0805_2012Metric")
        sch.power_at_pin("U1", "1", "+3V3")
        sch.gnd_at_pin("U1", "2")
        sch.power_at_pin("R1", "2", "+3V3")
        # direct WIRE between U1.6 and R1.1 -> unlabeled internal net
        x1, y1 = sch.get_pin_position("U1", "6")
        x2, y2 = sch.get_pin_position("R1", "1")
        sch.add_wire(x1, y1, x1 - 5.08, y1)
        sch.add_wire(x1 - 5.08, y1, x1 - 5.08, y2)
        sch.add_wire(x1 - 5.08, y2, x2, y2)
        path = str(tmp_path / "wired.kicad_sch")
        sch.save(path)

        summary = xb.extract_block(path, "renametest", ["U1", "R1"], {},
                                   blocks_dir=str(tmp_path / "blocks"),
                                   extra_fp=[f"TestFp={fplib}"])
        assert summary["internal_nets"] == ["N_U1_6"]
        passed, issues = cb.check_block(
            os.path.join(str(tmp_path / "blocks"), "renametest"))
        assert passed, [i.message for i in issues if i.severity == "error"]
