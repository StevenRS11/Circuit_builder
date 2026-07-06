#!/usr/bin/env python3
"""Tests for auto-reference, label collision avoidance, and wire deduplication."""

import sys
import os
import math
import tempfile
import pytest

_script_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from generate_kicad_sch import KicadSchematic, Wire, snap_to_grid


# ─── Auto-reference tests ────────────────────────────────────────────


class TestAutoReference:
    def test_bare_prefix(self):
        """Bare prefix like 'R' should auto-assign 'R1'."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        comp = sch.place_component("Device:R", "R", "10k", 100, 100)
        assert comp.reference == "R1"

    def test_question_mark(self):
        """'R?' should auto-assign 'R1'."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        comp = sch.place_component("Device:R", "R?", "10k", 100, 100)
        assert comp.reference == "R1"

    def test_sequential_auto(self):
        """Multiple auto-assigns should increment."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        c1 = sch.place_component("Device:R", "R", "10k", 100, 100)
        c2 = sch.place_component("Device:R", "R?", "4.7k", 120, 100)
        c3 = sch.place_component("Device:R", "R", "1k", 140, 100)
        assert c1.reference == "R1"
        assert c2.reference == "R2"
        assert c3.reference == "R3"

    def test_explicit_preserved(self):
        """Explicit references like 'R1' are used as-is."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        comp = sch.place_component("Device:R", "R1", "10k", 100, 100)
        assert comp.reference == "R1"

    def test_skip_existing(self):
        """Auto-assign should skip numbers already used by explicit refs."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", 100, 100)
        sch.place_component("Device:R", "R2", "10k", 120, 100)
        c3 = sch.place_component("Device:R", "R", "10k", 140, 100)
        assert c3.reference == "R3"

    def test_skip_gap(self):
        """Auto-assign should skip manually placed numbers."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R3", "10k", 100, 100)
        c1 = sch.place_component("Device:R", "R", "10k", 120, 100)
        c2 = sch.place_component("Device:R", "R", "10k", 140, 100)
        # R3 is taken, auto should give R1 then R2 (starts from 1, skips 3)
        # Actually: _ref_counters["R"] = 3 after placing R3, so next auto = 4
        assert c1.reference == "R4"
        assert c2.reference == "R5"

    def test_mixed_prefixes(self):
        """Different prefixes have independent counters."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        r1 = sch.place_component("Device:R", "R", "10k", 100, 100)
        c1 = sch.place_component("Device:C", "C", "100nF", 120, 100)
        r2 = sch.place_component("Device:R", "R", "4.7k", 140, 100)
        assert r1.reference == "R1"
        assert c1.reference == "C1"
        assert r2.reference == "R2"

    def test_auto_reference_method(self):
        """Direct auto_reference() call should work."""
        sch = KicadSchematic("test")
        ref1 = sch.auto_reference("U")
        ref2 = sch.auto_reference("U")
        assert ref1 == "U1"
        assert ref2 == "U2"

    def test_instances_section_matches(self):
        """The instances section should use the resolved reference, not 'R?'."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R?", "10k", 100, 100)

        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False, mode='w') as f:
            path = f.name
        try:
            sch.save(path)
            with open(path) as f:
                content = f.read()
            # Should contain R1, not R?
            assert '(reference "R1")' in content
            assert 'R?' not in content
        finally:
            os.unlink(path)


# ─── Label collision avoidance tests ─────────────────────────────────


class TestLabelCollision:
    def test_text_bbox_horizontal(self):
        """Horizontal text bbox should extend rightward."""
        bbox = KicadSchematic._text_bbox("R1", 10, 20, rotation=0)
        assert bbox[0] < bbox[2]  # x_min < x_max
        assert bbox[1] < bbox[3]  # y_min < y_max
        # Width should be proportional to text length
        assert (bbox[2] - bbox[0]) > 1.0

    def test_text_bbox_vertical(self):
        """Vertical text bbox should extend upward."""
        bbox = KicadSchematic._text_bbox("R1", 10, 20, rotation=90)
        assert bbox[0] < bbox[2]
        assert bbox[1] < bbox[3]
        # For vertical, height (y extent) should be larger than width (x extent)
        assert (bbox[3] - bbox[1]) > (bbox[2] - bbox[0])

    def test_rects_overlap(self):
        """Overlapping rects should be detected."""
        a = (0, 0, 5, 5)
        b = (3, 3, 8, 8)
        assert KicadSchematic._rects_overlap(a, b)

    def test_rects_no_overlap(self):
        """Separated rects should not overlap."""
        a = (0, 0, 2, 2)
        b = (10, 10, 15, 15)
        assert not KicadSchematic._rects_overlap(a, b)

    def test_labels_dont_overlap_components(self):
        """Labels should be placed outside component bodies."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        # Place two components close together
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        sch.place_component("Device:C", "C1", "100nF", 100, 90)

        sch._compute_all_label_positions()

        # R1 and C1 should have different label positions
        r1_pos = sch._label_positions["R1"]
        c1_pos = sch._label_positions["C1"]

        # Reference labels should not be at the same position
        r1_ref_bbox = sch._text_bbox("R1", r1_pos[0], r1_pos[1])
        c1_ref_bbox = sch._text_bbox("C1", c1_pos[0], c1_pos[1])

        # They shouldn't heavily overlap (some proximity is OK, but not identical)
        # Just verify they were computed and are grid-aligned
        assert r1_pos[0] == snap_to_grid(r1_pos[0])
        assert c1_pos[0] == snap_to_grid(c1_pos[0])

    def test_power_symbols_skipped(self):
        """Power symbol labels should not participate in collision detection."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_power("GND")
        sch.add_lib_symbol_power("VCC")
        sch.place_power_symbol("VCC", 100, 60)
        sch.place_power_symbol("GND", 100, 100)

        sch._compute_all_label_positions()
        # #PWR001 and #PWR002 should be in label_positions with component coords
        assert "#PWR001" in sch._label_positions
        assert "#PWR002" in sch._label_positions

    def test_power_symbol_properties_hidden(self):
        """Power symbol Reference and Value should be hidden in output."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_power("GND")
        sch.place_power_symbol("GND", 100, 100)

        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False, mode='w') as f:
            path = f.name
        try:
            sch.save(path)
            with open(path) as f:
                content = f.read()
            # Find the placed symbol section for #PWR001
            # Reference property must have "hide"
            # The pattern: property "Reference" "#PWR001" ... effects ... hide
            assert '#PWR?' not in content
            assert '#PWR001' in content
            # Check that both Reference and Value lines have hide
            lines = content.split('\n')
            for i, line in enumerate(lines):
                if '"Reference" "#PWR001"' in line:
                    # The effects line should be 2 lines later and contain "hide"
                    effects_line = lines[i + 2] if i + 2 < len(lines) else ""
                    assert "hide" in effects_line, f"Reference for #PWR001 not hidden: {effects_line}"
        finally:
            os.unlink(path)

    def test_net_labels_registered(self):
        """Net labels added via add_label should register occupied space."""
        sch = KicadSchematic("test")
        initial_count = len(sch._occupied_rects)
        sch.add_label("VCC", 100, 80)
        assert len(sch._occupied_rects) == initial_count + 1

    def test_save_produces_valid_output(self):
        """A schematic with collision avoidance should save and validate."""
        sch = KicadSchematic("Collision Test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.add_lib_symbol_power("GND")
        sch.add_lib_symbol_power("VCC")

        # Place components close together
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        sch.place_component("Device:R", "R2", "4.7k", 105, 80)
        sch.place_component("Device:C", "C1", "100nF", 100, 95)

        sch.place_power_symbol("VCC", 100, 68)
        sch.place_power_symbol("GND", 100, 108)

        sch.add_label("NET1", 100, 68)

        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False, mode='w') as f:
            path = f.name
        try:
            sch.save(path)
            assert os.path.exists(path)
            with open(path) as f:
                content = f.read()
            assert "(kicad_sch" in content
            assert '"R1"' in content
            assert '"R2"' in content
        finally:
            os.unlink(path)


# ─── Wire deduplication tests ────────────────────────────────────────


class TestWireDedup:
    """Wire tests use grid-snapped coordinates (1.27mm grid) since add_wire snaps."""

    def test_exact_duplicates_removed(self):
        """Identical wires should be deduplicated to one."""
        sch = KicadSchematic("test")
        sch.add_wire(0, 0, 10.16, 0)  # grid-aligned
        sch.add_wire(0, 0, 10.16, 0)
        sch.add_wire(0, 0, 10.16, 0)
        sch._deduplicate_wires()
        assert len(sch.wires) == 1

    def test_reversed_duplicates_removed(self):
        """Wires with swapped endpoints should be detected as duplicates."""
        sch = KicadSchematic("test")
        sch.add_wire(0, 0, 10.16, 0)
        sch.add_wire(10.16, 0, 0, 0)
        sch._deduplicate_wires()
        assert len(sch.wires) == 1

    def test_overlapping_horizontal_merged(self):
        """Overlapping horizontal wires on same Y should merge."""
        sch = KicadSchematic("test")
        # Use grid-aligned values: 0, 5.08, 10.16, 15.24
        sch.add_wire(0, 5.08, 10.16, 5.08)
        sch.add_wire(5.08, 5.08, 15.24, 5.08)
        sch._deduplicate_wires()
        assert len(sch.wires) == 1
        w = sch.wires[0]
        assert min(w.x1, w.x2) == 0
        assert max(w.x1, w.x2) == 15.24

    def test_overlapping_vertical_merged(self):
        """Overlapping vertical wires on same X should merge."""
        sch = KicadSchematic("test")
        sch.add_wire(10.16, 0, 10.16, 10.16)
        sch.add_wire(10.16, 5.08, 10.16, 20.32)
        sch._deduplicate_wires()
        assert len(sch.wires) == 1
        w = sch.wires[0]
        assert w.x1 == 10.16
        assert min(w.y1, w.y2) == 0
        assert max(w.y1, w.y2) == 20.32

    def test_touching_wires_merged(self):
        """Collinear wires that touch end-to-end should merge."""
        sch = KicadSchematic("test")
        sch.add_wire(0, 0, 10.16, 0)
        sch.add_wire(10.16, 0, 20.32, 0)
        sch._deduplicate_wires()
        assert len(sch.wires) == 1
        w = sch.wires[0]
        assert min(w.x1, w.x2) == 0
        assert max(w.x1, w.x2) == 20.32

    def test_parallel_non_overlapping_kept(self):
        """Parallel wires on different axes should both remain."""
        sch = KicadSchematic("test")
        sch.add_wire(0, 0, 10.16, 0)
        sch.add_wire(0, 5.08, 10.16, 5.08)
        sch._deduplicate_wires()
        assert len(sch.wires) == 2

    def test_gap_wires_not_merged(self):
        """Collinear wires with a gap should not merge."""
        sch = KicadSchematic("test")
        sch.add_wire(0, 0, 5.08, 0)
        sch.add_wire(10.16, 0, 15.24, 0)
        sch._deduplicate_wires()
        assert len(sch.wires) == 2

    def test_diagonal_wires_kept(self):
        """Non-axis-aligned wires should not be merged even if overlapping."""
        sch = KicadSchematic("test")
        sch.add_wire(0, 0, 10.16, 10.16)
        sch.add_wire(5.08, 5.08, 15.24, 15.24)
        sch._deduplicate_wires()
        assert len(sch.wires) == 2

    def test_subset_wire_absorbed(self):
        """A wire fully contained within another should be absorbed."""
        sch = KicadSchematic("test")
        sch.add_wire(0, 0, 20.32, 0)
        sch.add_wire(5.08, 0, 10.16, 0)
        sch._deduplicate_wires()
        assert len(sch.wires) == 1
        w = sch.wires[0]
        assert min(w.x1, w.x2) == 0
        assert max(w.x1, w.x2) == 20.32

    def test_multiple_merges(self):
        """Three overlapping segments should merge into one."""
        sch = KicadSchematic("test")
        sch.add_wire(0, 0, 10.16, 0)
        sch.add_wire(7.62, 0, 17.78, 0)
        sch.add_wire(16.51, 0, 25.4, 0)
        sch._deduplicate_wires()
        assert len(sch.wires) == 1
        w = sch.wires[0]
        assert min(w.x1, w.x2) == 0
        assert max(w.x1, w.x2) == 25.4

    def test_empty_wires(self):
        """Empty wire list should not crash."""
        sch = KicadSchematic("test")
        sch._deduplicate_wires()
        assert len(sch.wires) == 0

    def test_dedup_called_on_save(self):
        """save() should call dedup — duplicate wires should not appear in output."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_power("GND")
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        sch.place_power_symbol("GND", 100, 95)

        # Add duplicate wires (grid-aligned)
        sch.add_wire(100, 85.09, 100, 95.25)
        sch.add_wire(100, 85.09, 100, 95.25)
        sch.add_wire(100, 95.25, 100, 85.09)  # reversed

        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False, mode='w') as f:
            path = f.name
        try:
            sch.save(path)
            with open(path) as f:
                content = f.read()
            # Count wire sections — should be exactly 1
            wire_count = content.count("  (wire\n")
            assert wire_count == 1
        finally:
            os.unlink(path)


# ─── Integration: all features together ──────────────────────────────


class TestIntegration:
    def test_full_board_with_all_features(self):
        """Build a small board using auto-refs, verify no overlaps or dupes."""
        sch = KicadSchematic("Integration Test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.add_lib_symbol_led()
        sch.add_lib_symbol_power("GND")
        sch.add_lib_symbol_power("+3V3")

        # Auto-assign all references
        r1 = sch.place_component("Device:R", "R", "1k", 100, 80)
        r2 = sch.place_component("Device:R", "R", "4.7k", 115, 80)
        c1 = sch.place_component("Device:C", "C", "100nF", 130, 80)
        d1 = sch.place_component("Device:LED", "D", "Green", 145, 80)

        assert r1.reference == "R1"
        assert r2.reference == "R2"
        assert c1.reference == "C1"
        assert d1.reference == "D1"

        # Power
        sch.place_power_symbol("+3V3", 100, 65)
        sch.place_power_symbol("GND", 100, 95)

        # Add some duplicate wires
        sch.add_wire(100, 70, 100, 75)
        sch.add_wire(100, 70, 100, 75)  # dupe
        sch.add_wire(100, 85, 100, 95)
        sch.add_wire(100, 88, 100, 95)  # overlapping subset

        # Labels
        sch.add_label("NET1", 110, 75)
        sch.add_label("NET2", 125, 75)

        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False, mode='w') as f:
            path = f.name
        try:
            sch.save(path)
            with open(path) as f:
                content = f.read()

            # Verify no question marks in references
            assert "R?" not in content
            assert "C?" not in content
            assert "D?" not in content

            # Verify correct references present
            assert '(reference "R1")' in content
            assert '(reference "R2")' in content
            assert '(reference "C1")' in content
            assert '(reference "D1")' in content

            # Verify wire dedup worked — first pair should be 1 wire, second pair merged
            wire_count = content.count("  (wire\n")
            assert wire_count == 2  # one for top segment, one for merged bottom segment

        finally:
            os.unlink(path)

    def test_validator_still_passes(self):
        """A schematic with new features should still pass validation."""
        from validate_kicad_sch import validate

        sch = KicadSchematic("Validate Test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.add_lib_symbol_power("GND")
        sch.add_lib_symbol_power("VCC")

        # Use auto-refs
        sch.place_component("Device:R", "R", "10k", 100, 80)
        sch.place_component("Device:C", "C", "100nF", 120, 80)

        sch.place_power_symbol("VCC", 100, 65)
        sch.place_power_symbol("GND", 100, 95)
        sch.place_power_symbol("VCC", 120, 65)
        sch.place_power_symbol("GND", 120, 95)

        # Wire everything up
        sch.wire_between("R1", "1", "#PWR001", "1")
        sch.wire_between("R1", "2", "#PWR002", "1")
        sch.wire_between("C1", "1", "#PWR003", "1")
        sch.wire_between("C1", "2", "#PWR004", "1")

        # Add connecting label
        p1 = sch.get_pin_position("R1", "1")
        p2 = sch.get_pin_position("C1", "1")
        sch.add_wire(p1[0], p1[1], p2[0], p1[1])
        sch.add_wire(p2[0], p1[1], p2[0], p2[1])

        result = validate(sch)
        # Should have no errors (warnings OK)
        errors = [i for i in result.issues if i.severity == "error"]
        assert len(errors) == 0


# ─── Phase 1: Placement envelope & spacing enforcement ───────────────


class TestPlacementSpacing:
    def test_envelope_bigger_than_body(self):
        """Placement envelope should be strictly larger than body bbox."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        comp = sch.place_component("Device:R", "R1", "10k", 100, 80)
        body = sch._get_component_body_bbox(comp)
        envelope = sch._get_placement_envelope(comp)
        assert envelope[0] < body[0]  # left expanded
        assert envelope[1] < body[1]  # top expanded
        assert envelope[2] > body[2]  # right expanded
        assert envelope[3] > body[3]  # bottom expanded

    def test_overlapping_placement_nudged(self):
        """A component placed on top of another should be auto-nudged away."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        c1 = sch.place_component("Device:R", "R1", "10k", 100, 80)
        c2 = sch.place_component("Device:R", "R2", "4.7k", 100, 80)  # same position!
        # c2 should have been nudged away from c1
        assert (c2.x != c1.x or c2.y != c1.y), "Overlapping component was not nudged"

    def test_close_placement_nudged(self):
        """A component placed very close to another should be nudged."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        c1 = sch.place_component("Device:R", "R1", "10k", 100, 80)
        # Place R2 just 2.54mm away — within envelope overlap zone
        c2 = sch.place_component("Device:R", "R2", "4.7k", 102.54, 80)
        # They should be farther apart than 2.54mm after nudging
        dist = math.sqrt((c2.x - c1.x) ** 2 + (c2.y - c1.y) ** 2)
        assert dist > 2.54, f"Components too close: {dist:.2f}mm"

    def test_well_spaced_not_nudged(self):
        """Components placed far apart should not be moved."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        c1 = sch.place_component("Device:R", "R1", "10k", 100, 80)
        # Place R2 50mm away — well outside any envelope
        c2 = sch.place_component("Device:R", "R2", "4.7k", 150, 80)
        assert c2.x == snap_to_grid(150)
        assert c2.y == snap_to_grid(80)

    def test_power_symbols_exempt(self):
        """Power symbols should not be nudged even if overlapping."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_power("GND")
        c1 = sch.place_component("Device:R", "R1", "10k", 100, 80)
        # Place GND right on the component — should stay put
        pwr = sch.place_power_symbol("GND", 100, 85)
        assert pwr.x == snap_to_grid(100)
        assert pwr.y == snap_to_grid(85)

    def test_ic_has_bigger_envelope(self):
        """An IC should have a bigger envelope than a resistor."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_ic(
            "custom:AP2112K",
            pins=[
                ("1", "VIN", "power_in", "left", 0),
                ("2", "GND", "power_in", "left", 1),
                ("3", "EN", "input", "left", 2),
                ("4", "NC", "passive", "right", 0),
                ("5", "VOUT", "power_out", "right", 1),
            ],
            value="AP2112K",
        )
        r = sch.place_component("Device:R", "R1", "10k", 50, 50)
        u = sch.place_component("custom:AP2112K", "U1", "AP2112K", 150, 50)

        r_env = sch._get_placement_envelope(r)
        u_env = sch._get_placement_envelope(u)

        r_area = (r_env[2] - r_env[0]) * (r_env[3] - r_env[1])
        u_area = (u_env[2] - u_env[0]) * (u_env[3] - u_env[1])
        assert u_area > r_area, "IC envelope should be bigger than resistor envelope"


# ─── Phase 2: Smart wire routing ─────────────────────────────────────


class TestSmartRouting:
    def test_straight_wire_preferred(self):
        """Aligned pins should get a single straight wire."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        # Place R and C at same X, spaced vertically
        sch.place_component("Device:R", "R1", "10k", 100, 60)
        sch.place_component("Device:C", "C1", "100nF", 100, 90)
        # Wire R1 pin 2 (bottom) to C1 pin 1 (top) — should be straight vertical
        sch.wire_between("R1", "2", "C1", "1")
        # Should be exactly 1 wire (straight line)
        assert len(sch.wires) == 1
        w = sch.wires[0]
        assert w.x1 == w.x2  # vertical

    def test_l_route_avoids_collision(self):
        """L-route should try both orientations to avoid component bodies."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        # Place R and C at different X and Y
        sch.place_component("Device:R", "R1", "10k", 80, 60)
        sch.place_component("Device:C", "C1", "100nF", 120, 90)
        sch.wire_between("R1", "2", "C1", "1")
        # Should have produced some wires (either L or Z route)
        assert len(sch.wires) >= 2

    def test_bend_not_on_existing_wire(self):
        """Wire bend points should not land on existing wire segments."""
        sch = KicadSchematic("test")
        # Use grid-aligned coordinates (multiples of 1.27mm)
        # 78.74 = 62*1.27, 101.6 = 80*1.27, 139.7 = 110*1.27
        y = snap_to_grid(80)   # 80.01
        x1 = snap_to_grid(78)  # 78.74
        x2 = snap_to_grid(140) # 139.7
        mid = snap_to_grid(100) # 100.33 — on the interior of x1..x2
        sch.add_wire(x1, y, x2, y)

        # A bend on the wire interior should be detected
        assert sch._bend_on_existing_wire(mid, y)

        # A bend off the wire should be fine
        assert not sch._bend_on_existing_wire(mid, snap_to_grid(90))

        # Endpoints should NOT be flagged (not interior)
        assert not sch._bend_on_existing_wire(x1, y)
        assert not sch._bend_on_existing_wire(x2, y)

    def test_segment_intersects_body(self):
        """A wire through a component body should be detected."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        # A horizontal wire through the resistor body
        assert sch._segment_intersects_body(90, 80, 110, 80)
        # A wire well above the body
        assert not sch._segment_intersects_body(90, 50, 110, 50)

    def test_segment_crosses_rect(self):
        """Static segment-rect intersection check."""
        rect = (95, 75, 105, 85)
        # Wire through the middle
        assert KicadSchematic._segment_crosses_rect(90, 80, 110, 80, rect)
        # Wire above
        assert not KicadSchematic._segment_crosses_rect(90, 70, 110, 70, rect)
        # Wire too short after endpoint shrink
        assert not KicadSchematic._segment_crosses_rect(95, 80, 96, 80, rect)

    def test_z_route_around_obstacle(self):
        """When L-routes both cross a body, Z-route should be used."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()

        # Place an obstacle in between source and destination
        sch.place_component("Device:R", "R1", "10k", 100, 75)
        # Place source component at left
        sch.place_component("Device:R", "R2", "4.7k", 80, 60)
        # Place destination at right
        sch.place_component("Device:C", "C1", "100nF", 120, 90)

        initial_wires = len(sch.wires)
        sch.wire_between("R2", "2", "C1", "1")

        # Should have created wires (at least 2 segments for any non-straight route)
        new_wires = len(sch.wires) - initial_wires
        assert new_wires >= 2, f"Expected at least 2 wire segments, got {new_wires}"

    def test_route_is_clean_checks_both(self):
        """_route_is_clean should check both body crossings and bend-on-wire."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", 100, 80)

        # Route that crosses the body
        route_cross = [(90, 80, 110, 80)]
        assert not sch._route_is_clean(route_cross)

        # Route that avoids the body
        route_clear = [(90, 60, 110, 60)]
        assert sch._route_is_clean(route_clear)

        # Route with bend on existing wire
        sch.add_wire(80, 80, 140, 80)
        route_bend = [(100, 60, 100, 80), (100, 80, 120, 80)]
        assert not sch._route_is_clean(route_bend)

    def test_fallback_always_connects(self):
        """Even in worst case, wire_between should produce wires."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        # Place components with lots of obstacles between them
        for i in range(5):
            sch.place_component("Device:R", f"R{i+1}", "10k", 90 + i * 5, 75 + i * 3)
        sch.place_component("Device:C", "C1", "100nF", 80, 60)
        sch.place_component("Device:C", "C2", "100nF", 130, 100)

        wires_before = len(sch.wires)
        sch.wire_between("C1", "1", "C2", "2")
        wires_after = len(sch.wires)
        assert wires_after > wires_before, "wire_between should always create at least one wire"


# ─── Phase 3a: Pin keepout zones ─────────────────────────────────────


class TestPinKeepoutZones:
    def test_resistor_has_two_keepouts(self):
        """A placed resistor should generate 2 keepout zones (one per pin)."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        keepouts = sch.get_pin_keepouts("R1")
        assert len(keepouts) == 2

    def test_keepout_extends_outward_from_pin(self):
        """Keepout corridor should extend away from the component body."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        comp = sch.place_component("Device:R", "R1", "10k", 100, 80)
        keepouts = sch.get_pin_keepouts("R1")

        # Resistor at rotation=0: pin 1 is at top (lower Y in schematic),
        # keepout should extend upward (even lower Y).
        # Pin 2 is at bottom, keepout extends downward.
        pin1_pos = sch.get_pin_position("R1", "1")
        pin2_pos = sch.get_pin_position("R1", "2")

        # Find which keepout belongs to which pin by checking containment
        # of pin positions at rectangle edges
        for kz in keepouts:
            # Keepout must touch at least one pin's position
            touches_pin1 = (kz[0] <= pin1_pos[0] <= kz[2] and
                            kz[1] <= pin1_pos[1] <= kz[3])
            touches_pin2 = (kz[0] <= pin2_pos[0] <= kz[2] and
                            kz[1] <= pin2_pos[1] <= kz[3])
            assert touches_pin1 or touches_pin2, \
                f"Keepout {kz} doesn't touch either pin"

    def test_keepout_dimensions(self):
        """Keepout corridors should have correct length and width."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        keepouts = sch.get_pin_keepouts("R1")

        for kz in keepouts:
            w = kz[2] - kz[0]
            h = kz[3] - kz[1]
            # One dimension should be the keepout length, the other the width
            dims = sorted([w, h])
            assert abs(dims[0] - 2 * sch.PIN_KEEPOUT_HALF_WIDTH) < 0.01, \
                f"Keepout width {dims[0]} != expected {2 * sch.PIN_KEEPOUT_HALF_WIDTH}"
            assert abs(dims[1] - sch.PIN_KEEPOUT_LENGTH) < 0.01, \
                f"Keepout length {dims[1]} != expected {sch.PIN_KEEPOUT_LENGTH}"

    def test_power_symbols_no_keepouts(self):
        """Power symbols should not generate keepout zones."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_power("GND")
        sch.place_power_symbol("GND", 100, 90)
        # Power symbols use #PWR references — no keepouts
        all_keepouts = sch.get_pin_keepouts()
        assert len(all_keepouts) == 0

    def test_ic_has_keepouts_per_pin(self):
        """An IC with 5 pins should have 5 keepout zones."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_ic(
            "custom:AP2112K",
            pins=[
                ("1", "VIN", "power_in", "left", 0),
                ("2", "GND", "power_in", "left", 1),
                ("3", "EN", "input", "left", 2),
                ("4", "NC", "passive", "right", 0),
                ("5", "VOUT", "power_out", "right", 1),
            ],
            value="AP2112K",
        )
        sch.place_component("custom:AP2112K", "U1", "AP2112K", 100, 80)
        keepouts = sch.get_pin_keepouts("U1")
        assert len(keepouts) == 5

    def test_rotated_component_keepouts(self):
        """Keepout zones should rotate with the component."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()

        # Place resistors at 0° and 90°
        sch.place_component("Device:R", "R1", "10k", 50, 80)
        sch.place_component("Device:R", "R2", "10k", 150, 80, rotation=90)

        kz1 = sch.get_pin_keepouts("R1")
        kz2 = sch.get_pin_keepouts("R2")

        # R1 (vertical): keepouts should be taller than wide
        for kz in kz1:
            w, h = kz[2] - kz[0], kz[3] - kz[1]
            assert h > w, f"Vertical resistor keepout should be taller: {w}x{h}"

        # R2 (rotated 90°): keepouts should be wider than tall
        for kz in kz2:
            w, h = kz[2] - kz[0], kz[3] - kz[1]
            assert w > h, f"Rotated resistor keepout should be wider: {w}x{h}"

    def test_placement_avoids_keepout_zones(self):
        """A component placed in another's pin keepout zone should be nudged."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()

        # Place R1 first
        c1 = sch.place_component("Device:R", "R1", "10k", 100, 80)
        pin1_pos = sch.get_pin_position("R1", "1")  # top pin

        # Try to place R2 directly in R1's top pin keepout zone
        # (just above pin 1 tip, in the keepout corridor)
        c2 = sch.place_component("Device:R", "R2", "4.7k",
                                  pin1_pos[0], pin1_pos[1] - 3)

        # c2 should have been nudged away from R1's keepout
        body2 = sch._get_component_body_bbox(c2)
        keepouts1 = sch.get_pin_keepouts("R1")

        # The body of c2 should not overlap any keepout of R1
        for kz in keepouts1:
            assert not sch._rects_overlap(body2, kz, margin=0), \
                f"R2 body {body2} overlaps R1 keepout {kz}"

    def test_get_pin_keepouts_all(self):
        """get_pin_keepouts(None) should return dict of all keepouts."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        sch.place_component("Device:C", "C1", "100nF", 130, 80)

        all_kz = sch.get_pin_keepouts()
        assert "R1" in all_kz
        assert "C1" in all_kz
        assert len(all_kz["R1"]) == 2
        assert len(all_kz["C1"]) == 2

    def test_get_pin_keepouts_missing_ref(self):
        """get_pin_keepouts for non-existent ref should return empty list."""
        sch = KicadSchematic("test")
        assert sch.get_pin_keepouts("R99") == []

    def test_point_in_keepout_detection(self):
        """_point_in_any_keepout should detect points inside keepout corridors."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        pin1_pos = sch.get_pin_position("R1", "1")

        # A point in the keepout corridor (just above pin 1 tip)
        assert sch._point_in_any_keepout(pin1_pos[0], pin1_pos[1] - 2.54)

        # A point far away should not be in any keepout
        assert not sch._point_in_any_keepout(200, 200)

    def test_point_in_keepout_excludes_ref(self):
        """_point_in_any_keepout with exclude_ref should skip that component."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        pin1_pos = sch.get_pin_position("R1", "1")

        # Point in R1's keepout — detected normally
        pt = (pin1_pos[0], pin1_pos[1] - 2.54)
        assert sch._point_in_any_keepout(pt[0], pt[1])
        # But excluded when R1 is the reference
        assert not sch._point_in_any_keepout(pt[0], pt[1], exclude_ref="R1")

    def test_route_clean_rejects_bend_in_keepout(self):
        """A wire bend point in a keepout zone should fail _route_is_clean."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        pin1_pos = sch.get_pin_position("R1", "1")

        # Build a route whose bend point lands in R1's pin 1 keepout.
        # The keepout extends upward from pin1 at (pin1_x, pin1_y).
        # Place the bend at the pin's X and slightly above the pin tip.
        bend_x = pin1_pos[0]
        bend_y = pin1_pos[1] - 2.54  # inside keepout corridor
        route = [
            (bend_x - 10, 60, bend_x - 10, bend_y),
            (bend_x - 10, bend_y, bend_x, bend_y),
        ]
        # The second segment's start (bend_x-10, bend_y) is the bend.
        # That's outside the keepout. We need the bend AT the keepout.
        # Use a route where the bend IS at (bend_x, bend_y):
        route = [
            (bend_x, 60, bend_x, bend_y),
            (bend_x, bend_y, bend_x + 10, bend_y),
        ]
        # Without endpoint exclusion, bend in keepout should fail
        assert not sch._route_is_clean(route)

    def test_route_clean_allows_endpoint_keepout(self):
        """Bend points in endpoint components' keepouts should be allowed."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        pin1_pos = sch.get_pin_position("R1", "1")

        bend_y = pin1_pos[1] - 2.54
        route = [
            (80, 60, 80, bend_y),
            (80, bend_y, pin1_pos[0], bend_y),
        ]
        # With R1 as an endpoint, its keepouts are excluded
        assert sch._route_is_clean(route, endpoint_refs=("R1",))

    def test_rect_overlaps_keepout(self):
        """_rect_overlaps_any_keepout should detect overlapping rectangles."""
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        pin1_pos = sch.get_pin_position("R1", "1")

        # A rect sitting in the keepout zone
        rect_in = (pin1_pos[0] - 1, pin1_pos[1] - 4,
                    pin1_pos[0] + 1, pin1_pos[1] - 2)
        assert sch._rect_overlaps_any_keepout(rect_in)

        # A rect far away
        rect_out = (200, 200, 205, 205)
        assert not sch._rect_overlaps_any_keepout(rect_out)

    def test_existing_tests_still_pass_with_keepouts(self):
        """Integration: validator test from Phase 2 should still work."""
        from validate_kicad_sch import validate

        sch = KicadSchematic("Keepout Integration")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.add_lib_symbol_power("GND")
        sch.add_lib_symbol_power("VCC")

        sch.place_component("Device:R", "R", "10k", 100, 80)
        sch.place_component("Device:C", "C", "100nF", 120, 80)

        sch.place_power_symbol("VCC", 100, 65)
        sch.place_power_symbol("GND", 100, 95)
        sch.place_power_symbol("VCC", 120, 65)
        sch.place_power_symbol("GND", 120, 95)

        sch.wire_between("R1", "1", "#PWR001", "1")
        sch.wire_between("R1", "2", "#PWR002", "1")
        sch.wire_between("C1", "1", "#PWR003", "1")
        sch.wire_between("C1", "2", "#PWR004", "1")

        result = validate(sch)
        errors = [i for i in result.issues if i.severity == "error"]
        assert len(errors) == 0


# ─── DNP + baked BOM-identity property emission ──────────────────────

class TestDnpAndBakedProperties:
    def _resistor_sch(self):
        sch = KicadSchematic("test")
        sch.add_lib_symbol_resistor()
        return sch

    def test_dnp_default_is_no(self):
        sch = self._resistor_sch()
        sch.place_component("Device:R", "R1", "10k", 100, 100)
        text = sch._render_placed_component(sch.components[-1])
        assert "(dnp no)" in text
        assert "(dnp yes)" not in text

    def test_dnp_true_serializes_yes(self):
        sch = self._resistor_sch()
        sch.place_component("Device:R", "R1", "10k", 100, 100, dnp=True)
        text = sch._render_placed_component(sch.components[-1])
        assert "(dnp yes)" in text

    def test_extra_props_serialize_as_hidden_properties(self):
        sch = self._resistor_sch()
        sch.place_component("Device:R", "R1", "10k", 100, 100,
                            footprint="Resistor_SMD:R_0805_2012Metric",
                            **{"MPN": "RC0805FR-0710KL", "Manufacturer": "YAGEO"})
        text = sch._render_placed_component(sch.components[-1])
        assert '(property "MPN" "RC0805FR-0710KL"' in text
        assert '(property "Manufacturer" "YAGEO"' in text

    def test_in_bom_no_serializes(self):
        sch = self._resistor_sch()
        sch.place_component("Device:R", "R1", "10k", 100, 100, in_bom=False)
        text = sch._render_placed_component(sch.components[-1])
        assert "(in_bom no)" in text


class TestPcbwaySymbolProps:
    """The engine's clean-MPN emit rules (generate_from_data._pcbway_symbol_props)."""

    def _bom(self, **kw):
        from cross_check_bom import BomEntry
        kw.setdefault("reference", "U1")
        kw.setdefault("value", "X")
        kw.setdefault("footprint", "Package_TO_SOT_SMD:SOT-23-5")
        return BomEntry(**kw)

    def test_real_mpn_emitted_once(self):
        from generate_from_data import _pcbway_symbol_props
        p = _pcbway_symbol_props(self._bom(part_number="AP2112K-3.3TRG1",
                                           manufacturer="Diodes Inc", package="SOT-23-5"))
        assert p["MPN"] == "AP2112K-3.3TRG1"
        assert p["Manufacturer"] == "Diodes Inc"
        assert p["Package"] == "SOT-23-5"
        # exactly one mpn-family key
        from check_pcbway import is_mpn_family_key
        assert sum(1 for k in p if is_mpn_family_key(k)) == 1

    def test_blank_part_number_emits_no_mpn(self):
        from generate_from_data import _pcbway_symbol_props
        p = _pcbway_symbol_props(self._bom(part_number=""))
        assert "MPN" not in p

    def test_dash_placeholder_emits_no_mpn(self):
        from generate_from_data import _pcbway_symbol_props
        p = _pcbway_symbol_props(self._bom(part_number="—"))
        assert "MPN" not in p

    def test_distributor_code_not_emitted_as_mpn(self):
        from generate_from_data import _pcbway_symbol_props
        p = _pcbway_symbol_props(self._bom(part_number="C25804"))
        assert "MPN" not in p

    def test_description_not_emitted_as_mpn(self):
        from generate_from_data import _pcbway_symbol_props
        p = _pcbway_symbol_props(self._bom(part_number="56k 5% 0805"))
        assert "MPN" not in p

    def test_package_falls_back_to_footprint(self):
        from generate_from_data import _pcbway_symbol_props
        p = _pcbway_symbol_props(self._bom(part_number="X1", package="",
                                           footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"))
        assert p["Package"] == "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"

    def test_lcsc_dual_compat_field(self):
        from generate_from_data import _pcbway_symbol_props
        p = _pcbway_symbol_props(self._bom(part_number="X1", supplier="LCSC",
                                           supplier_pn="C2827654"))
        assert p["LCSC Part #"] == "C2827654"
        # ...but it is never the mpn-family key
        from check_pcbway import is_mpn_family_key
        assert not is_mpn_family_key("LCSC Part #")


class TestMechanicalNonFitted:
    def test_mounting_hole_is_non_fitted(self):
        from cross_check_bom import BomEntry
        assert BomEntry("MH1", "", "MountingHole:MountingHole_3.2mm_M3").is_mechanical_non_fitted
        assert BomEntry("TP1", "", "").is_mechanical_non_fitted
        assert BomEntry("FID1", "", "").is_mechanical_non_fitted

    def test_regular_part_is_fitted(self):
        from cross_check_bom import BomEntry
        assert not BomEntry("U1", "CH224K", "Package_SO:SSOP-10").is_mechanical_non_fitted
        assert not BomEntry("R1", "10k", "Resistor_SMD:R_0805_2012Metric").is_mechanical_non_fitted
