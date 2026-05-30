#!/usr/bin/env python3
"""Tests for the PCBway assembly checker (check_pcbway.py)."""

import sys
import os
import json

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_scripts_dir = os.path.join(_tests_dir, "..", "scripts")
sys.path.insert(0, os.path.abspath(_scripts_dir))

from check_pcbway import (
    classify_package, is_generic_passive, check_bom, check_bom_file,
    load_bom_for_pcbway, build_sourcing_sheet, _scan_notes,
    format_result_json, PcbwayPart,
)
import pytest


# ─── Helpers ────────────────────────────────────────────────────────

def _part(ref, value="", footprint="", part_number="", supplier="",
          supplier_pn="", notes="", package=""):
    return PcbwayPart(reference=ref, value=value, footprint=footprint,
                      part_number=part_number, supplier=supplier,
                      supplier_pn=supplier_pn, notes=notes, package=package)


def _ratings(result):
    return {p.reference: p.rating for p in result.parts}


def _checks(result):
    return {i.check_name for i in result.issues}


# ─── classify_package ───────────────────────────────────────────────

class TestClassifyPackage:
    def test_standard_smt_is_ok(self):
        assert classify_package("Package_TO_SOT_SMD:SOT-23-5")["rating"] == "ok"
        assert classify_package("Resistor_SMD:R_0805_2012Metric")["rating"] == "ok"
        assert classify_package("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")["rating"] == "ok"
        assert classify_package("Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm")["rating"] == "ok"

    def test_01005_is_blocked(self):
        assert classify_package("Capacitor_SMD:C_01005_0402Metric")["rating"] == "block"

    def test_0201_is_caution(self):
        assert classify_package("Resistor_SMD:R_0201_0603Metric")["rating"] == "caution"

    def test_bga_is_caution(self):
        assert classify_package("Package_BGA:BGA-256_17x17mm")["rating"] == "caution"

    def test_through_hole_is_caution(self):
        assert classify_package("Package_TO_SOT_THT:TO-220-3_Vertical")["rating"] == "caution"
        assert classify_package(
            "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical")["rating"] == "caution"

    def test_missing_footprint_is_blocked(self):
        assert classify_package("")["rating"] == "block"

    def test_unrecognized_is_unknown(self):
        r = classify_package("Some_Weird:MysteryPackage_99")
        assert r["rating"] == "unknown"


# ─── is_generic_passive ─────────────────────────────────────────────

class TestGenericPassive:
    def test_r_c_l_are_generic(self):
        assert is_generic_passive("R1", "Resistor_SMD:R_0805_2012Metric")
        assert is_generic_passive("C12", "Capacitor_SMD:C_0805_2012Metric")
        assert is_generic_passive("L3", "Inductor_SMD:L_0805_2012Metric")

    def test_ic_and_diode_are_not_generic(self):
        assert not is_generic_passive("U1", "Package_TO_SOT_SMD:SOT-23-5")
        assert not is_generic_passive("D1", "Diode_SMD:D_SOD-123")
        assert not is_generic_passive("J1", "Connector_JST:JST_PH_S2B-PH-K")


# ─── BOM parsing ────────────────────────────────────────────────────

_BOM_MD = """# Bill of Materials - Test Board

| Ref | Value | Part Number | Package | Footprint (KiCad) | Supplier | Supplier PN | Notes |
|-----|-------|-------------|---------|-------------------|----------|-------------|-------|
| U1  | AP2112K-3.3 | AP2112K-3.3TRG1 | SOT-23-5 | Package_TO_SOT_SMD:SOT-23-5 | LCSC | C51118 | |
| R1  | 10k |  | 0805 | Resistor_SMD:R_0805_2012Metric |  |  | |
| C1  | 100nF |  | 0805 | Capacitor_SMD:C_0805_2012Metric | LCSC | C49678 | |
"""


class TestBomParsing:
    def test_parses_all_rows(self):
        parts = load_bom_for_pcbway(_BOM_MD)
        assert [p.reference for p in parts] == ["U1", "R1", "C1"]

    def test_interior_empty_cells_keep_alignment(self):
        # R1 has empty Part Number / Supplier / Supplier PN columns; the
        # footprint column must still land in the footprint field.
        parts = load_bom_for_pcbway(_BOM_MD)
        r1 = next(p for p in parts if p.reference == "R1")
        assert r1.footprint == "Resistor_SMD:R_0805_2012Metric"
        assert r1.supplier_pn == ""
        assert r1.value == "10k"

    def test_captures_supplier_fields(self):
        parts = load_bom_for_pcbway(_BOM_MD)
        u1 = next(p for p in parts if p.reference == "U1")
        assert u1.supplier == "LCSC"
        assert u1.supplier_pn == "C51118"
        assert u1.part_number == "AP2112K-3.3TRG1"

    def test_skips_placeholder_rows(self):
        md = _BOM_MD + "| {ref} | {value} | {part} | {pkg} | {fp} | {s} | {pn} | {n} |\n"
        parts = load_bom_for_pcbway(md)
        assert all(not p.reference.startswith("{") for p in parts)
        assert len(parts) == 3

    def test_skips_template_stub_with_real_designator(self):
        # The 03_bom.md template has real refs (U1) but {value} placeholder fields.
        md = _BOM_MD + "| U9 | {value} | {part} | {pkg} | {fp} | {s} | {pn} | {n} |\n"
        parts = load_bom_for_pcbway(md)
        assert "U9" not in [p.reference for p in parts]
        assert len(parts) == 3


# ─── Notes flags ────────────────────────────────────────────────────

class TestNotesFlags:
    def test_obsolete_flagged(self):
        assert any("obsolete" in m for _, m in _scan_notes("This part is obsolete"))

    def test_nrnd_flagged(self):
        assert _scan_notes("NRND")

    def test_clean_notes_no_flags(self):
        assert _scan_notes("standard decoupling cap") == []


# ─── check_bom rubric ───────────────────────────────────────────────

class TestCheckBom:
    def test_clean_bom_passes(self):
        parts = [
            _part("U1", "AP2112K-3.3", "Package_TO_SOT_SMD:SOT-23-5",
                  part_number="AP2112K-3.3TRG1", supplier="LCSC", supplier_pn="C51118"),
            _part("C1", "100nF", "Capacitor_SMD:C_0805_2012Metric",
                  supplier="LCSC", supplier_pn="C49678"),
        ]
        result = check_bom(parts)
        assert result.passed is True
        assert result.errors == []
        assert _ratings(result) == {"U1": "ok", "C1": "ok"}

    def test_ic_without_pn_blocks(self):
        result = check_bom([_part("U1", "BigChip", "Package_TO_SOT_SMD:SOT-23-5")])
        assert result.passed is False
        assert "sourcing" in _checks(result)
        assert _ratings(result)["U1"] == "block"

    def test_passive_without_pn_is_only_caution(self):
        result = check_bom([_part("R1", "10k", "Resistor_SMD:R_0805_2012Metric")])
        assert result.passed is True            # caution, not block
        assert result.warnings
        assert _ratings(result)["R1"] == "caution"

    def test_01005_blocks(self):
        result = check_bom([
            _part("C1", "100nF", "Capacitor_SMD:C_01005_0402Metric",
                  supplier_pn="C123")
        ])
        assert result.passed is False
        assert _ratings(result)["C1"] == "block"

    def test_missing_footprint_blocks(self):
        result = check_bom([_part("U1", "Chip", footprint="", supplier_pn="C1")])
        assert result.passed is False
        assert _ratings(result)["U1"] == "block"

    def test_eol_note_cautions(self):
        result = check_bom([
            _part("U1", "Old", "Package_TO_SOT_SMD:SOT-23-5",
                  supplier_pn="C1", notes="EOL")
        ])
        assert _ratings(result)["U1"] == "caution"
        assert "notes_flag" in _checks(result)

    def test_worst_rating_wins(self):
        # BGA (caution) + no PN (block) -> block
        result = check_bom([_part("U1", "FPGA", "Package_BGA:BGA-256_17x17mm")])
        assert _ratings(result)["U1"] == "block"


# ─── Sourcing sheet ─────────────────────────────────────────────────

class TestSourcingSheet:
    def test_sheet_lists_every_part(self):
        parts = load_bom_for_pcbway(_BOM_MD)
        result = check_bom(parts)
        sheet = build_sourcing_sheet(result, "Test Board")
        for ref in ("U1", "R1", "C1"):
            assert ref in sheet
        assert "Distributor PN" in sheet
        assert "PCBway Sourcing Sheet" in sheet

    def test_sheet_marks_blocks(self):
        result = check_bom([_part("U1", "Chip", "Package_TO_SOT_SMD:SOT-23-5")])
        sheet = build_sourcing_sheet(result)
        assert "BLOCK" in sheet


# ─── JSON output ────────────────────────────────────────────────────

class TestJsonOutput:
    def test_json_is_valid_and_complete(self):
        parts = load_bom_for_pcbway(_BOM_MD)
        result = check_bom(parts)
        data = json.loads(format_result_json(result, "test.md"))
        assert data["bom_file"] == "test.md"
        assert "passed" in data
        assert len(data["parts"]) == 3
        assert all("rating" in p for p in data["parts"])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
