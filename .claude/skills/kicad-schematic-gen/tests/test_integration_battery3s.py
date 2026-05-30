"""End-to-end integration test: regenerate battery_3s from frozen data fixtures.

This is the strongest guarantee — a real 55-component board through the whole
engine. It asserts the three validators pass, that the J3 balance-tap short stays
fixed (verify_netlist 0/0), and that output is byte-stable against a golden file.

To intentionally refresh the golden after a deliberate change:
    UPDATE_GOLDEN=1 python -m pytest tests/test_integration_battery3s.py -k golden
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from generate_from_data import generate, build_schematic, self_verify, load_layout
from verify_netlist import load_intended_netlist, verify
from cross_check_bom import load_bom_from_markdown, cross_check
from validate_kicad_sch import validate

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "battery_3s")
NETLIST = os.path.join(FIX, "netlist.yaml")
BOM = os.path.join(FIX, "bom_flat.md")
LAYOUT = os.path.join(FIX, "layout.yaml")
GOLDEN = os.path.join(FIX, "battery_3s.golden.kicad_sch")

EXPECTED_COMPONENT_COUNT = 58  # non-power components in the BOM


def _load_objs():
    netlist = load_intended_netlist(NETLIST)
    with open(BOM, encoding="utf-8") as f:
        bom = load_bom_from_markdown(f.read())
    layout = load_layout(LAYOUT)
    return netlist, bom, layout


class TestIntegration:
    def test_generates_and_passes_gate(self, tmp_path):
        out = tmp_path / "battery_3s.kicad_sch"
        res = generate(NETLIST, BOM, LAYOUT, out_path=str(out), uuid_seed=0)
        assert res.passed, res.errors
        assert out.exists()

    def test_all_three_validators(self):
        netlist, bom, layout = _load_objs()
        sch = build_schematic(netlist, bom, layout, uuid_seed=0)

        vres = validate(sch)
        assert [i for i in vres.issues if i.severity == "error"] == []

        nres = verify(netlist, sch)
        # J3 short fixed + full parity: zero errors AND zero warnings.
        assert nres.errors == []
        assert nres.warnings == []

        cres = cross_check(bom, sch)
        assert cres.errors == []

    def test_component_count(self):
        netlist, bom, layout = _load_objs()
        sch = build_schematic(netlist, bom, layout, uuid_seed=0)
        non_power = [c for c in sch.components
                     if not c.reference.startswith("#PWR")]
        assert len(non_power) == EXPECTED_COMPONENT_COUNT

    def test_golden_snapshot(self, tmp_path):
        out = tmp_path / "battery_3s.kicad_sch"
        res = generate(NETLIST, BOM, LAYOUT, out_path=str(out), uuid_seed=0)
        assert res.passed, res.errors
        produced = out.read_text(encoding="utf-8")

        if os.environ.get("UPDATE_GOLDEN") == "1":
            with open(GOLDEN, "w", encoding="utf-8") as f:
                f.write(produced)
            pytest.skip("golden updated")

        with open(GOLDEN, encoding="utf-8") as f:
            assert produced == f.read(), \
                "output drifted from golden; rerun with UPDATE_GOLDEN=1 if intended"


class TestShippedLayoutClean:
    def test_fixed_layout_has_no_missing_junction(self):
        # The shipped battery_3s layout must be free of wire-collision shorts.
        netlist, bom, layout = _load_objs()
        sch = build_schematic(netlist, bom, layout, uuid_seed=0)
        mj = [i for i in validate(sch).issues
              if i.check_name == "missing_junction"]
        assert mj == []
