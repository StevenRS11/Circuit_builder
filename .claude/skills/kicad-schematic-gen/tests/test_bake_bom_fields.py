#!/usr/bin/env python3
"""Tests for bake_bom_fields.py — the after-the-fact PCBWay-field bake pathway.

Covers the robustness contract handed over from the battery_3s field work:
  - round-trip: bake → parse back with load_kicad_sch → every line with a real
    MPN carries the canonical field set;
  - idempotency: a second bake changes zero bytes;
  - reconcile-removal: stale managed fields (the R28 `LCSC Part #` class) and
    shadow MPN-family aliases (incl. forbidden `Mfg Part #`) are removed;
  - engine parity: a freshly engine-generated schematic (the battery_3s golden)
    is already correct — bake is a byte-level no-op on it;
  - lock guard: a KiCad ~*.lck beside the target refuses the write (--force
    overrides);
  - pcb kind: fields land on footprints, native Description is never touched.
"""

import os
import sys

_tests_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_tests_dir, "..", "scripts")))

import pytest

import bake_bom_fields as bake
from bake_bom_fields import bake_file, LockedFileError
from check_pcbway import load_bom_for_pcbway, CANONICAL_MPN_FIELD
from generate_from_data import _pcbway_symbol_props
from validate_kicad_sch import load_kicad_sch

FIXTURES = os.path.join(_tests_dir, "fixtures", "battery_3s")
GOLDEN_SCH = os.path.join(FIXTURES, "battery_3s.golden.kicad_sch")
FIXTURE_BOM = os.path.join(FIXTURES, "bom_flat.md")


MINI_BOM = """# Flat BOM
| Ref | Value | Part Number | Manufacturer | Package | Footprint |
|-----|-------|-------------|--------------|---------|-----------|
| R1 | 10k | RC0805FR-0710KL | YAGEO | 0805 | Resistor_SMD:R_0805_2012Metric |
| C1 | 100nF | CL21B104KBCNNNC | Samsung | 0805 | Capacitor_SMD:C_0805_2012Metric |
"""

# A hand-edited-style schematic: R1 carries a stale distributor field and both
# a forbidden and a shadow MPN alias; C1 carries an outdated canonical MPN.
MINI_SCH = """(kicad_sch
  (version 20230121)
  (generator eeschema)
  (uuid "00000000-0000-0000-0000-00000000root")
  (lib_symbols)
  (symbol
    (lib_id "Device:R")
    (at 50.8 50.8 0)
    (unit 1)
    (property "Reference" "R1"
      (at 52.07 49.53 0)
    )
    (property "Value" "10k"
      (at 52.07 52.07 0)
    )
    (property "Footprint" "Resistor_SMD:R_0805_2012Metric"
      (at 50.8 50.8 0)
    )
    (property "LCSC Part #" "C99999"
      (at 0 0 0)
    )
    (property "Mfg Part #" "STALE-TRAP"
      (at 0 0 0)
    )
    (property "Part Number" "C11111"
      (at 0 0 0)
    )
    (property "MyNote" "keep me"
      (at 0 0 0)
    )
    (pin "1"
      (uuid "00000000-0000-0000-0000-0000000000p1")
    )
    (instances
      (project "mini"
        (path "/00000000-0000-0000-0000-00000000root"
          (reference "R1")
          (unit 1)
        )
      )
    )
  )
  (symbol
    (lib_id "Device:C")
    (at 76.2 50.8 0)
    (unit 1)
    (property "Reference" "C1"
      (at 77.47 49.53 0)
    )
    (property "Value" "100nF"
      (at 77.47 52.07 0)
    )
    (property "Footprint" "Capacitor_SMD:C_0805_2012Metric"
      (at 76.2 50.8 0)
    )
    (property "MPN" "OLD-WRONG-MPN"
      (at 0 0 0)
    )
    (instances
      (project "mini"
        (path "/00000000-0000-0000-0000-00000000root"
          (reference "C1")
          (unit 1)
        )
      )
    )
  )
)
"""

MINI_PCB = """(kicad_pcb (version 20221018) (generator pcbnew)
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (footprint "Resistor_SMD:R_0805_2012Metric" (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1"
      (at 0 -2 0)
    )
    (property "Value" "10k"
      (at 0 2 0)
    )
    (property "Description" "Chip resistor, native library text"
      (at 0 0 0)
    )
    (property "Mfg Part" "SHADOW-ON-BOARD"
      (at 0 0 0)
    )
    (pad "1" smd rect (at -0.9 0) (size 1 1.2) (layers "F.Cu"))
  )
)
"""


def _fields_by_ref(bom_text):
    out = {}
    for r in load_bom_for_pcbway(bom_text):
        extra = _pcbway_symbol_props(r)
        if extra:
            out[r.reference] = extra
    return out


@pytest.fixture()
def mini_sch(tmp_path):
    p = tmp_path / "mini.kicad_sch"
    p.write_text(MINI_SCH, encoding="utf-8")
    return p


@pytest.fixture()
def fields():
    return _fields_by_ref(MINI_BOM)


class TestBakeSchematic:
    def test_roundtrip_fields_land_and_parse_back(self, mini_sch, fields):
        res = bake_file(str(mini_sch), fields, "sch")
        assert res["refs_touched"] == 2
        sch = load_kicad_sch(str(mini_sch))
        by_ref = {c.reference: c for c in sch.components}
        assert by_ref["R1"].extra_properties[CANONICAL_MPN_FIELD] == "RC0805FR-0710KL"
        assert by_ref["R1"].extra_properties["Manufacturer"] == "YAGEO"
        assert by_ref["R1"].extra_properties["Package"] == "0805"
        # stale canonical MPN is corrected in place
        assert by_ref["C1"].extra_properties[CANONICAL_MPN_FIELD] == "CL21B104KBCNNNC"

    def test_stale_and_shadow_fields_removed_user_fields_kept(self, mini_sch, fields):
        res = bake_file(str(mini_sch), fields, "sch")
        text = mini_sch.read_text(encoding="utf-8")
        # stale managed field (BOM has no LCSC supplier column) — removed
        assert "LCSC Part #" not in text
        # forbidden + shadow MPN aliases — removed
        assert "Mfg Part #" not in text
        assert '"Part Number"' not in text
        # unmanaged user field survives
        assert '"MyNote" "keep me"' in text
        assert set(res["field_removals"]["R1"]) == \
            {"LCSC Part #", "Mfg Part #", "Part Number"}
        # exactly one MPN-family key remains on each part
        assert text.count(f'(property "{CANONICAL_MPN_FIELD}"') == 2

    def test_idempotent(self, mini_sch, fields):
        bake_file(str(mini_sch), fields, "sch")
        first = mini_sch.read_text(encoding="utf-8")
        res2 = bake_file(str(mini_sch), fields, "sch")
        assert mini_sch.read_text(encoding="utf-8") == first
        assert res2["field_writes"] == 0 and res2["field_removals"] == {}

    def test_ref_not_in_bom_is_untouched(self, mini_sch):
        only_r1 = _fields_by_ref(
            "| Ref | Value | Part Number | Manufacturer | Package | Footprint |\n"
            "|--|--|--|--|--|--|\n"
            "| R1 | 10k | RC0805FR-0710KL | YAGEO | 0805 | Resistor_SMD:R_0805_2012Metric |\n")
        bake_file(str(mini_sch), only_r1, "sch")
        text = mini_sch.read_text(encoding="utf-8")
        # C1 (absent from BOM) keeps its old field verbatim — reconcile.py's
        # finding, not bake's to delete
        assert '"MPN" "OLD-WRONG-MPN"' in text


class TestEngineParity:
    def test_bake_is_noop_on_engine_generated_golden(self, tmp_path):
        target = tmp_path / "golden.kicad_sch"
        original = open(GOLDEN_SCH, encoding="utf-8").read()
        target.write_text(original, encoding="utf-8")
        with open(FIXTURE_BOM, encoding="utf-8") as f:
            fields = _fields_by_ref(f.read())
        res = bake_file(str(target), fields, "sch")
        assert res["field_writes"] == 0 and res["field_removals"] == {}
        assert target.read_text(encoding="utf-8") == original


class TestLockGuard:
    def test_refuses_locked_file(self, mini_sch, fields):
        lock = mini_sch.parent / f"~{mini_sch.name}.lck"
        lock.write_text("{}", encoding="utf-8")
        before = mini_sch.read_text(encoding="utf-8")
        with pytest.raises(LockedFileError):
            bake_file(str(mini_sch), fields, "sch")
        assert mini_sch.read_text(encoding="utf-8") == before

    def test_force_overrides_lock(self, mini_sch, fields):
        (mini_sch.parent / f"~{mini_sch.name}.lck").write_text("{}", encoding="utf-8")
        res = bake_file(str(mini_sch), fields, "sch", force=True)
        assert res["refs_touched"] == 2


class TestBakePcb:
    def test_fields_land_on_footprint_native_description_kept(self, tmp_path, fields):
        p = tmp_path / "mini.kicad_pcb"
        p.write_text(MINI_PCB, encoding="utf-8")
        res = bake_file(str(p), fields, "pcb")
        text = p.read_text(encoding="utf-8")
        assert f'(property "{CANONICAL_MPN_FIELD}" "RC0805FR-0710KL"' in text
        assert '(property "Manufacturer" "YAGEO"' in text
        # native footprint Description is NOT managed on the board side
        assert '"Description" "Chip resistor, native library text"' in text
        # board-side shadow alias removed
        assert '"Mfg Part"' not in text
        assert res["field_removals"]["R1"] == ["Mfg Part"]

    def test_pcb_bake_idempotent(self, tmp_path, fields):
        p = tmp_path / "mini.kicad_pcb"
        p.write_text(MINI_PCB, encoding="utf-8")
        bake_file(str(p), fields, "pcb")
        first = p.read_text(encoding="utf-8")
        res2 = bake_file(str(p), fields, "pcb")
        assert p.read_text(encoding="utf-8") == first
        assert res2["field_writes"] == 0
