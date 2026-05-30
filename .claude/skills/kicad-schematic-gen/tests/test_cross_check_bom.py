#!/usr/bin/env python3
"""Tests for BOM cross-check against schematic."""

import sys
import os
import json

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_scripts_dir = os.path.join(_tests_dir, "..", "scripts")
sys.path.insert(0, os.path.abspath(_scripts_dir))

from generate_kicad_sch import KicadSchematic
from cross_check_bom import (
    cross_check, load_bom_from_markdown, BomEntry, values_match,
    format_result_text, format_result_json,
)
import pytest


# ─── Helpers ────────────────────────────────────────────────────────

def _make_sch_with_parts():
    """Create a simple schematic with R1=10k, C1=100nF, U1=AP2112K-3.3."""
    sch = KicadSchematic("bom_test")
    sch.add_lib_symbol_resistor()
    sch.add_lib_symbol_capacitor()
    sch.place_component("Device:R", "R1", "10k", x=100, y=80,
                        footprint="Resistor_SMD:R_0805_2012Metric")
    sch.place_component("Device:C", "C1", "100nF", x=115, y=80,
                        footprint="Capacitor_SMD:C_0805_2012Metric")
    return sch


def _matching_bom():
    """BOM entries that match _make_sch_with_parts()."""
    return [
        BomEntry(reference="R1", value="10k",
                 footprint="Resistor_SMD:R_0805_2012Metric", quantity=1),
        BomEntry(reference="C1", value="100nF",
                 footprint="Capacitor_SMD:C_0805_2012Metric", quantity=1),
    ]


def _issues_by_check(result, check_name):
    return [i for i in result.issues if i.check_name == check_name]


# ─── Test: happy path ──────────────────────────────────────────────

class TestHappyPath:
    def test_matching_bom_passes(self):
        sch = _make_sch_with_parts()
        result = cross_check(_matching_bom(), sch)
        assert result.passed is True
        assert len(result.errors) == 0


# ─── Test: missing components ──────────────────────────────────────

class TestMissingComponents:
    def test_bom_entry_missing_from_schematic(self):
        """BOM has R2 but schematic does not."""
        sch = _make_sch_with_parts()
        bom = _matching_bom() + [
            BomEntry(reference="R2", value="4.7k",
                     footprint="Resistor_SMD:R_0805_2012Metric", quantity=1),
        ]
        result = cross_check(bom, sch)
        issues = _issues_by_check(result, "missing_from_schematic")
        assert len(issues) == 1
        assert issues[0].reference == "R2"
        assert issues[0].severity == "error"

    def test_schematic_component_missing_from_bom(self):
        """Schematic has C1 but BOM does not."""
        sch = _make_sch_with_parts()
        bom = [
            BomEntry(reference="R1", value="10k",
                     footprint="Resistor_SMD:R_0805_2012Metric", quantity=1),
            # C1 deliberately omitted
        ]
        result = cross_check(bom, sch)
        issues = _issues_by_check(result, "missing_from_bom")
        assert len(issues) == 1
        assert issues[0].reference == "C1"
        assert issues[0].severity == "error"


# ─── Test: value matching ──────────────────────────────────────────

class TestValueMatching:
    def test_value_mismatch(self):
        sch = _make_sch_with_parts()
        bom = [
            BomEntry(reference="R1", value="4.7k",
                     footprint="Resistor_SMD:R_0805_2012Metric", quantity=1),
            BomEntry(reference="C1", value="100nF",
                     footprint="Capacitor_SMD:C_0805_2012Metric", quantity=1),
        ]
        result = cross_check(bom, sch)
        issues = _issues_by_check(result, "value_mismatch")
        assert len(issues) == 1
        assert issues[0].reference == "R1"

    def test_exact_match(self):
        assert values_match("100nF", "100nF") is True

    def test_fuzzy_match_uF_nF(self):
        """0.1uF should equal 100nF."""
        assert values_match("0.1uF", "100nF") is True

    def test_fuzzy_match_k_ohm(self):
        """4.7k should equal 4700."""
        assert values_match("4.7k", "4700") is True

    def test_mismatch(self):
        assert values_match("10k", "4.7k") is False


# ─── Test: footprint matching ──────────────────────────────────────

class TestFootprintMatching:
    def test_footprint_mismatch(self):
        sch = _make_sch_with_parts()
        bom = [
            BomEntry(reference="R1", value="10k",
                     footprint="Resistor_SMD:R_0603_1608Metric", quantity=1),
            BomEntry(reference="C1", value="100nF",
                     footprint="Capacitor_SMD:C_0805_2012Metric", quantity=1),
        ]
        result = cross_check(bom, sch)
        issues = _issues_by_check(result, "footprint_mismatch")
        assert len(issues) == 1
        assert issues[0].reference == "R1"

    def test_empty_footprint_no_error(self):
        """If BOM footprint is empty, skip footprint check."""
        sch = _make_sch_with_parts()
        bom = [
            BomEntry(reference="R1", value="10k", footprint="", quantity=1),
            BomEntry(reference="C1", value="100nF",
                     footprint="Capacitor_SMD:C_0805_2012Metric", quantity=1),
        ]
        result = cross_check(bom, sch)
        fp_issues = _issues_by_check(result, "footprint_mismatch")
        assert len(fp_issues) == 0


# ─── Test: exclusions ──────────────────────────────────────────────

class TestExclusions:
    def test_power_symbols_excluded(self):
        """Power symbols (VCC, GND) should not appear in cross-check."""
        sch = _make_sch_with_parts()
        sch.add_lib_symbol_power("VCC")
        sch.add_lib_symbol_power("GND")
        sch.place_power_symbol("VCC", 100, 68)
        sch.place_power_symbol("GND", 100, 92)

        result = cross_check(_matching_bom(), sch)
        # Power symbols not in BOM, but no "missing_from_bom" for them
        missing = _issues_by_check(result, "missing_from_bom")
        power_missing = [i for i in missing
                         if i.reference and i.reference.startswith("#")]
        assert len(power_missing) == 0

    def test_in_bom_false_excluded(self):
        """Components with in_bom=False should be skipped."""
        sch = KicadSchematic("bom_exclude_test")
        sch.add_lib_symbol_resistor()
        comp = sch.place_component("Device:R", "R1", "10k", x=100, y=80,
                                   footprint="Resistor_SMD:R_0805_2012Metric")
        comp.in_bom = False

        # Empty BOM — should not report R1 as missing
        result = cross_check([], sch)
        missing = _issues_by_check(result, "missing_from_bom")
        assert len(missing) == 0


# ─── Test: markdown parsing ────────────────────────────────────────

class TestMarkdownParsing:
    def test_valid_table(self):
        md = """| Reference | Value | Footprint | Qty |
| --- | --- | --- | --- |
| R1 | 10k | Resistor_SMD:R_0805_2012Metric | 1 |
| C1 | 100nF | Capacitor_SMD:C_0805_2012Metric | 1 |
"""
        entries = load_bom_from_markdown(md)
        assert len(entries) == 2
        assert entries[0].reference == "R1"
        assert entries[0].value == "10k"
        assert entries[1].reference == "C1"

    def test_template_rows_skipped(self):
        md = """| Reference | Value | Footprint | Qty |
| --- | --- | --- | --- |
| R1 | 10k | Resistor_SMD:R_0805_2012Metric | 1 |
| {ref} | {value} | {footprint} | {qty} |
"""
        entries = load_bom_from_markdown(md)
        assert len(entries) == 1
        assert entries[0].reference == "R1"

    def test_extra_columns(self):
        md = """| Reference | Value | Footprint | Qty | MPN | Notes |
| --- | --- | --- | --- | --- | --- |
| R1 | 10k | Resistor_SMD:R_0805_2012Metric | 1 | RC0805 | pull-up |
"""
        entries = load_bom_from_markdown(md)
        assert len(entries) == 1
        assert entries[0].reference == "R1"

    def test_empty_table(self):
        entries = load_bom_from_markdown("")
        assert entries == []

    def test_no_table(self):
        md = "# BOM\n\nNo table here.\n"
        entries = load_bom_from_markdown(md)
        assert entries == []


# ─── Test: output formatting ───────────────────────────────────────

class TestFormatting:
    def test_text_output_passed(self):
        sch = _make_sch_with_parts()
        result = cross_check(_matching_bom(), sch)
        text = format_result_text(result)
        assert "PASSED" in text.upper()

    def test_text_output_failed(self):
        sch = _make_sch_with_parts()
        bom = [
            BomEntry(reference="R1", value="4.7k",
                     footprint="Resistor_SMD:R_0805_2012Metric", quantity=1),
        ]
        result = cross_check(bom, sch)
        text = format_result_text(result)
        assert "FAILED" in text.upper() or "ERROR" in text.upper()

    def test_json_output(self):
        sch = _make_sch_with_parts()
        result = cross_check(_matching_bom(), sch)
        raw = format_result_json(result)
        data = json.loads(raw)
        assert "passed" in data
        assert "issues" in data


# ─── Test: multiple issues at once ─────────────────────────────────

class TestMultipleIssues:
    def test_several_errors_reported(self):
        """Value mismatch + missing from BOM + missing from schematic."""
        sch = _make_sch_with_parts()
        bom = [
            BomEntry(reference="R1", value="4.7k",  # value mismatch
                     footprint="Resistor_SMD:R_0805_2012Metric", quantity=1),
            # C1 missing from BOM
            BomEntry(reference="R99", value="1k",    # missing from schematic
                     footprint="Resistor_SMD:R_0805_2012Metric", quantity=1),
        ]
        result = cross_check(bom, sch)
        assert result.passed is False
        assert len(result.errors) >= 3

        check_names = {i.check_name for i in result.errors}
        assert "value_mismatch" in check_names
        assert "missing_from_bom" in check_names
        assert "missing_from_schematic" in check_names
