#!/usr/bin/env python3
"""Tests for the pinout database lookup utility."""

import sys
import os

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_scripts_dir = os.path.join(_tests_dir, "..", "scripts")
sys.path.insert(0, os.path.abspath(_scripts_dir))

from lookup_pinout import load_pinout_db, lookup_pinout, get_ic_pins_for_generator
import pytest


# Path to the pinout database
_DB_PATH = os.path.join(
    _tests_dir, "..", "pinouts", "pinout_db.json"
)


# ─── Test: database loading ────────────────────────────────────────

class TestLoadDatabase:
    def test_loads_successfully(self):
        db = load_pinout_db(_DB_PATH)
        assert db is not None
        assert len(db) > 0

    def test_has_expected_parts(self):
        db = load_pinout_db(_DB_PATH)
        # Check a few known parts exist
        part_names = {entry.get("part_number", entry.get("name", "")).upper()
                      for entry in (db if isinstance(db, list)
                                    else db.values())}
        # Fallback: try key-based lookup
        if not part_names or all(p == "" for p in part_names):
            part_names = {k.upper() for k in db.keys()}
        for pn in ["AP2112K-3.3", "CH340G", "RP2040"]:
            assert lookup_pinout(pn, db) is not None, f"{pn} not found in DB"


# ─── Test: exact match ─────────────────────────────────────────────

class TestExactMatch:
    def test_ap2112k_33(self):
        db = load_pinout_db(_DB_PATH)
        result = lookup_pinout("AP2112K-3.3", db)
        assert result is not None
        # Should have pin info
        pins = result.get("pins", [])
        assert len(pins) == 5, f"AP2112K-3.3 should have 5 pins, got {len(pins)}"

    def test_unknown_part_returns_none(self):
        db = load_pinout_db(_DB_PATH)
        result = lookup_pinout("NONEXISTENT_PART_XYZ", db)
        assert result is None


# ─── Test: case-insensitive match ──────────────────────────────────

class TestCaseInsensitive:
    def test_lowercase_matches(self):
        db = load_pinout_db(_DB_PATH)
        result = lookup_pinout("ap2112k-3.3", db)
        assert result is not None

    def test_mixed_case_matches(self):
        db = load_pinout_db(_DB_PATH)
        result = lookup_pinout("Ap2112K-3.3", db)
        assert result is not None


# ─── Test: prefix match ────────────────────────────────────────────

class TestPrefixMatch:
    def test_prefix_returns_variant(self):
        """AP2112K (without voltage suffix) should match one of the variants."""
        db = load_pinout_db(_DB_PATH)
        result = lookup_pinout("AP2112K", db)
        assert result is not None


# ─── Test: get_ic_pins_for_generator ───────────────────────────────

class TestGetIcPins:
    def test_ap2112k_returns_tuples(self):
        db = load_pinout_db(_DB_PATH)
        pins = get_ic_pins_for_generator("AP2112K-3.3", db)
        assert len(pins) == 5
        # Each tuple: (pin_num, pin_name, pin_type, side, position_index)
        for pin in pins:
            assert len(pin) == 5
            pin_num, pin_name, pin_type, side, pos_idx = pin
            assert isinstance(pin_num, (int, str))
            assert isinstance(pin_name, str)
            assert isinstance(pin_type, str)
            assert side in ("left", "right", "top", "bottom")
            assert isinstance(pos_idx, int)

    def test_power_pins_on_expected_sides(self):
        """Power input pins should be on the left, power output on the right."""
        db = load_pinout_db(_DB_PATH)
        pins = get_ic_pins_for_generator("AP2112K-3.3", db)
        pin_map = {str(p[0]): p for p in pins}  # by pin_num

        for pin in pins:
            pin_num, pin_name, pin_type, side, pos_idx = pin
            if pin_type == "power_in":
                assert side == "left", (
                    f"power_in pin {pin_name} should be on left, got {side}")
            elif pin_type == "power_out":
                assert side == "right", (
                    f"power_out pin {pin_name} should be on right, got {side}")

    def test_unknown_part_returns_empty(self):
        db = load_pinout_db(_DB_PATH)
        pins = get_ic_pins_for_generator("NONEXISTENT_PART_XYZ", db)
        assert pins == []


# ─── Test: database integrity ──────────────────────────────────────

VALID_PIN_TYPES = {
    "input", "output", "bidirectional", "passive",
    "power_in", "power_out", "tri_state", "open_collector",
    "open_drain", "open_emitter", "unconnected", "free",
    "no_connect",
}


class TestDatabaseIntegrity:
    def test_all_entries_have_required_fields(self):
        db = load_pinout_db(_DB_PATH)
        entries = db if isinstance(db, list) else db.values()
        for entry in entries:
            assert "pins" in entry, f"Entry missing 'pins': {entry}"
            assert "package" in entry, f"Entry missing 'package': {entry}"

    def test_pin_types_are_valid(self):
        db = load_pinout_db(_DB_PATH)
        for part_name, entry in db.items():
            for pin_num, pin_info in entry.get("pins", {}).items():
                ptype = pin_info.get("type", "")
                assert ptype in VALID_PIN_TYPES, (
                    f"Invalid pin type '{ptype}' for pin {pin_num} in {part_name}")
