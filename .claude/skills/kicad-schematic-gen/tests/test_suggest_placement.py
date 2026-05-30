#!/usr/bin/env python3
"""Tests for suggest_placement() API."""

import sys
import os
_tests_dir = os.path.dirname(os.path.abspath(__file__))
_scripts_dir = os.path.join(_tests_dir, "..", "scripts")
sys.path.insert(0, os.path.abspath(_scripts_dir))

from generate_kicad_sch import KicadSchematic, snap_to_grid
from validate_kicad_sch import validate
import pytest


# ─── Helpers ───────────────────────────────────────────────────────

def _is_grid_aligned(val, grid=1.27):
    """Check if value is on 1.27mm grid."""
    remainder = abs(val / grid - round(val / grid))
    return remainder < 0.001


def _rects_overlap(a, b):
    """Check if two (x_min, y_min, x_max, y_max) rects overlap."""
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


# ─── Basic suggestion ─────────────────────────────────────────────

class TestBasicSuggestion:
    def test_returns_grid_aligned(self):
        """Suggested coordinates should be on the 1.27mm grid."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        x, y = sch.suggest_placement("Device:R")
        assert _is_grid_aligned(x), f"x={x} not grid-aligned"
        assert _is_grid_aligned(y), f"y={y} not grid-aligned"

    def test_empty_schematic_returns_origin(self):
        """With no components, suggestion should be near the layout origin."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        x, y = sch.suggest_placement("Device:R")
        # Should be offset from _LAYOUT_ORIGIN (100, 80) by _OFFSET_NEARBY
        assert 90 <= x <= 130
        assert 70 <= y <= 100

    def test_near_x_y_hint(self):
        """When near_x/near_y given, suggestion should be close to it."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        x, y = sch.suggest_placement("Device:R", near_x=200, near_y=150)
        # Should be offset from (200, 150), not the origin
        assert abs(x - 200) < 30
        assert abs(y - 150) < 30


# ─── Relative-to placement ────────────────────────────────────────

class TestRelativeTo:
    def test_relative_to_pin(self):
        """Placement relative to a specific IC pin."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.add_lib_symbol_ic(
            "custom:U1", pins=[
                ("1", "VIN", "power_in", "left", 0),
                ("2", "GND", "power_in", "left", 1),
                ("3", "VOUT", "power_out", "right", 0),
            ], ref_prefix="U", value="LDO",
        )
        sch.place_component("custom:U1", "U1", "LDO", x=100, y=80)

        # Suggest cap near U1 pin 3 (VOUT)
        pin3_x, pin3_y = sch.get_pin_position("U1", "3")
        x, y = sch.suggest_placement(
            "Device:C", relative_to={"ref": "U1", "pin": "3"}
        )
        # Should be near pin 3, not at the origin
        assert abs(x - pin3_x) < 25
        assert abs(y - pin3_y) < 25

    def test_relative_to_component_center(self):
        """When pin not specified, anchor to component center."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)

        x, y = sch.suggest_placement(
            "Device:R", relative_to={"ref": "R1"}
        )
        # Should be near R1 (100, 80)
        assert abs(x - 100) < 30
        assert abs(y - 80) < 30


# ─── Relationship types ───────────────────────────────────────────

class TestRelationships:
    def test_decoupling_places_below_ic(self):
        """Decoupling relationship offsets vertically (cap below IC pin)."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_capacitor()
        sch.add_lib_symbol_ic(
            "custom:U1", pins=[
                ("1", "VIN", "power_in", "left", 0),
                ("2", "VOUT", "power_out", "right", 0),
            ], ref_prefix="U",
        )
        sch.place_component("custom:U1", "U1", "IC", x=100, y=80)

        pin1_x, pin1_y = sch.get_pin_position("U1", "1")
        x, y = sch.suggest_placement(
            "Device:C",
            relative_to={"ref": "U1", "pin": "1"},
            relationship="decoupling",
        )
        # Decoupling should be below the pin (larger Y in KiCad)
        assert y > pin1_y

    def test_series_places_rightward(self):
        """Series relationship offsets horizontally."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)

        x, y = sch.suggest_placement(
            "Device:R",
            relative_to={"ref": "R1"},
            relationship="series",
        )
        assert x > 100  # Should be to the right

    def test_parallel_places_below(self):
        """Parallel relationship stacks vertically."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)

        x, y = sch.suggest_placement(
            "Device:R",
            relative_to={"ref": "R1"},
            relationship="parallel",
        )
        assert y > 80  # Should be below R1
        assert abs(x - 100) < 20  # Roughly same column (may nudge for clearance)


# ─── Collision avoidance ──────────────────────────────────────────

class TestCollisionAvoidance:
    def test_avoids_existing_component(self):
        """Suggested position shouldn't overlap existing component."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()

        # Place R1 at (100, 80)
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)

        # Suggest placement near R1 — should be nudged away
        x, y = sch.suggest_placement("Device:R", near_x=100, near_y=80)

        # Should not be at exact same position
        assert not (abs(x - 100) < 1 and abs(y - 80) < 1), \
            "Suggested position overlaps existing component"

    def test_avoids_multiple_components(self):
        """Navigates around multiple existing components."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()

        # Place a row of resistors
        for i in range(5):
            sch.place_component("Device:R", f"R{i+1}", "10k",
                                x=100 + i * 20, y=80)

        # Suggest near the middle — should find a clear spot
        x, y = sch.suggest_placement("Device:R", near_x=140, near_y=80)
        assert _is_grid_aligned(x)
        assert _is_grid_aligned(y)

    def test_suggested_then_placed_validates(self):
        """Component placed at suggested position passes validation."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.add_lib_symbol_power("VCC")
        sch.add_lib_symbol_power("GND")

        # Place R1, then suggest and place C1
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        x, y = sch.suggest_placement("Device:C", near_x=100, near_y=80)
        sch.place_component("Device:C", "C1", "100nF", x=x, y=y)

        # Wire both to power
        for ref in ("R1", "C1"):
            p1 = sch.get_pin_position(ref, "1")
            sch.place_power_symbol("VCC", p1[0], p1[1] - 5)
            sch.add_wire(p1[0], p1[1], p1[0], p1[1] - 5)
            p2 = sch.get_pin_position(ref, "2")
            sch.place_power_symbol("GND", p2[0], p2[1] + 5)
            sch.add_wire(p2[0], p2[1], p2[0], p2[1] + 5)

        result = validate(sch)
        errors = [i for i in result.issues if i.severity == "error"]
        assert len(errors) == 0


# ─── Group placement ──────────────────────────────────────────────

class TestGroupPlacement:
    def test_register_group(self):
        """register_group tracks component membership."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()

        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.register_group("power_section", "R1")

        assert "power_section" in sch._placement_groups
        assert "R1" in sch._placement_groups["power_section"]

    def test_group_clusters_nearby(self):
        """Components in same group should cluster near the group centroid."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()

        # Place two components in a group
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.register_group("block_A", "R1")
        sch.place_component("Device:R", "R2", "4.7k", x=120, y=80)
        sch.register_group("block_A", "R2")

        # Suggest a third in the same group — should be near (110, 80)
        x, y = sch.suggest_placement("Device:C", group_id="block_A")
        centroid_x = 110  # midpoint of R1 (100) and R2 (120)
        assert abs(x - centroid_x) < 30, f"x={x} too far from group centroid {centroid_x}"

    def test_different_groups_separate(self):
        """Components in different groups should not cluster together."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()

        # Group A at x=100
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.register_group("group_A", "R1")

        # Group B at x=200
        sch.place_component("Device:R", "R2", "10k", x=200, y=80)
        sch.register_group("group_B", "R2")

        # Suggest for group A — should be near 100, not 200
        x, _ = sch.suggest_placement("Device:R", group_id="group_A")
        assert abs(x - 100) < abs(x - 200), \
            f"x={x} is closer to group_B (200) than group_A (100)"

    def test_duplicate_register_ignored(self):
        """Registering same ref twice in a group doesn't duplicate."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", x=100, y=80)
        sch.register_group("g", "R1")
        sch.register_group("g", "R1")
        assert sch._placement_groups["g"].count("R1") == 1


# ─── Edge cases ───────────────────────────────────────────────────

class TestEdgeCases:
    def test_unknown_lib_id(self):
        """suggest_placement with unknown lib_id still returns valid coords."""
        sch = KicadSchematic("test")
        x, y = sch.suggest_placement("Device:Unknown_Symbol")
        assert _is_grid_aligned(x)
        assert _is_grid_aligned(y)

    def test_relative_to_nonexistent_ref(self):
        """relative_to with missing ref falls back gracefully."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        x, y = sch.suggest_placement(
            "Device:R", relative_to={"ref": "U99", "pin": "1"}
        )
        # Should fall back to origin-based placement
        assert _is_grid_aligned(x)
        assert _is_grid_aligned(y)

    def test_new_group_id(self):
        """Group that doesn't exist yet falls back to normal placement."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        x, y = sch.suggest_placement("Device:R", group_id="nonexistent")
        assert _is_grid_aligned(x)
        assert _is_grid_aligned(y)


# ─── Integration ──────────────────────────────────────────────────

class TestIntegration:
    def test_full_board_with_suggest(self):
        """Build a small board using suggest_placement for all components."""
        sch = KicadSchematic("suggest_test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.add_lib_symbol_led()
        sch.add_lib_symbol_power("VCC")
        sch.add_lib_symbol_power("GND")

        # Place first component explicitly
        sch.place_component("Device:R", "R1", "1k", x=100, y=80)

        # Suggest and place the rest
        x, y = sch.suggest_placement("Device:LED",
                                      relative_to={"ref": "R1"},
                                      relationship="series")
        sch.place_component("Device:LED", "D1", "Red", x=x, y=y)

        x, y = sch.suggest_placement("Device:C",
                                      relative_to={"ref": "R1"},
                                      relationship="parallel")
        sch.place_component("Device:C", "C1", "100nF", x=x, y=y)

        # All components should be at distinct positions
        positions = [(c.x, c.y) for c in sch.components
                     if not c.reference.startswith("#PWR")]
        assert len(set(positions)) == len(positions), \
            "Some components overlap"

    def test_suggest_with_ic_decoupling(self):
        """Suggest decoupling caps around an IC."""
        sch = KicadSchematic("ic_test")
        sch.add_lib_symbol_capacitor()
        sch.add_lib_symbol_ic(
            "custom:MCU", pins=[
                ("1", "VDD", "power_in", "left", 0),
                ("2", "GND", "power_in", "left", 1),
                ("3", "IO1", "bidirectional", "right", 0),
                ("4", "IO2", "bidirectional", "right", 1),
            ], ref_prefix="U",
        )
        sch.place_component("custom:MCU", "U1", "MCU", x=100, y=80)

        # Place decoupling cap near VDD pin
        x, y = sch.suggest_placement(
            "Device:C",
            relative_to={"ref": "U1", "pin": "1"},
            relationship="decoupling",
        )
        sch.place_component("Device:C", "C1", "100nF", x=x, y=y)

        # Place another decoupling cap
        x2, y2 = sch.suggest_placement(
            "Device:C",
            relative_to={"ref": "U1", "pin": "1"},
            relationship="decoupling",
        )
        sch.place_component("Device:C", "C2", "10uF", x=x2, y=y2)

        # All three should be at distinct positions
        positions = [(c.x, c.y) for c in sch.components
                     if not c.reference.startswith("#PWR")]
        assert len(set(positions)) == len(positions)
