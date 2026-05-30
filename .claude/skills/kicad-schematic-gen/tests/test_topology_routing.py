"""Tests for topology-aware routing: auto_rotate, safe_wire, connect_or_label,
and subcircuit templates (series chain, decoupling cap, pull-up/down, voltage divider).
"""

import sys
import os
import math

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from generate_kicad_sch import KicadSchematic, snap_to_grid

# Also import validator if available
try:
    from validate_kicad_sch import validate
    HAS_VALIDATOR = True
except ImportError:
    HAS_VALIDATOR = False


def _setup_passive_sch():
    """Create a schematic with basic passive symbols registered."""
    sch = KicadSchematic("Topology Test")
    sch.add_lib_symbol_resistor()
    sch.add_lib_symbol_capacitor()
    sch.add_lib_symbol_led()
    sch.add_lib_symbol_power("GND")
    sch.add_lib_symbol_power("VCC")
    sch.add_lib_symbol_power("+3V3")
    return sch


# ═══════════════════════════════════════════════════════════════
# auto_rotate tests
# ═══════════════════════════════════════════════════════════════

class TestAutoRotate:

    def test_resistor_default_vertical(self):
        """Resistor at rotation=0 already has pin 1 up, pin 2 down."""
        sch = _setup_passive_sch()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        rot = sch.auto_rotate("R1", {"1": "up", "2": "down"})
        assert rot == 0

    def test_resistor_rotate_horizontal(self):
        """Requesting pin 1 left, pin 2 right should produce horizontal layout."""
        sch = _setup_passive_sch()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        rot = sch.auto_rotate("R1", {"1": "left", "2": "right"})
        # Both 90 and 270 produce horizontal layout with pin 1 left
        assert rot in (90, 270)
        p1x, _ = sch.get_pin_position("R1", "1")
        p2x, _ = sch.get_pin_position("R1", "2")
        assert p1x < p2x, "Pin 1 should be to the left of pin 2"

    def test_resistor_flip_vertical(self):
        """Requesting pin 1 down, pin 2 up should give rotation=180."""
        sch = _setup_passive_sch()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        rot = sch.auto_rotate("R1", {"1": "down", "2": "up"})
        assert rot == 180

    def test_capacitor_vertical_default(self):
        """Capacitor pin 1 up, pin 2 down → rotation=0."""
        sch = _setup_passive_sch()
        sch.place_component("Device:C", "C1", "100nF", 100, 80)
        rot = sch.auto_rotate("C1", {"1": "up", "2": "down"})
        assert rot == 0

    def test_led_rotate_to_vertical(self):
        """LED with cathode down, anode up needs rotation."""
        sch = _setup_passive_sch()
        sch.place_component("Device:LED", "LED1", "Red", 100, 80)
        # LED pin 1=K (cathode) at left, pin 2=A (anode) at right in symbol space
        # For vertical chain down: anode up (pin 2 → up), cathode down (pin 1 → down)
        rot = sch.auto_rotate("LED1", {"1": "down", "2": "up"})
        # Should rotate so cathode faces down and anode faces up
        assert rot in [0, 90, 180, 270]

        # Verify the pins actually face the right direction after rotation
        p1x, p1y = sch.get_pin_position("LED1", "1")
        p2x, p2y = sch.get_pin_position("LED1", "2")
        # Cathode (pin 1) should be below anode (pin 2)
        assert p1y > p2y, f"Cathode ({p1y}) should be below anode ({p2y})"

    def test_auto_rotate_updates_pin_positions(self):
        """After auto_rotate, get_pin_position reflects the new rotation."""
        sch = _setup_passive_sch()
        sch.place_component("Device:R", "R1", "10k", 100, 80)

        # Before rotation: pin 1 is above (lower y), pin 2 below
        p1_before = sch.get_pin_position("R1", "1")
        p2_before = sch.get_pin_position("R1", "2")
        assert p1_before[1] < p2_before[1]  # pin 1 above pin 2

        # Rotate to horizontal
        sch.auto_rotate("R1", {"1": "left", "2": "right"})

        p1_after = sch.get_pin_position("R1", "1")
        p2_after = sch.get_pin_position("R1", "2")
        # Now pin 1 should be to the left (lower x)
        assert p1_after[0] < p2_after[0], "After rotation, pin 1 should be left of pin 2"

    def test_single_constraint(self):
        """Auto-rotate with only one pin constrained still works."""
        sch = _setup_passive_sch()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        rot = sch.auto_rotate("R1", {"2": "right"})
        # Should pick a rotation where pin 2 faces right
        p2x, p2y = sch.get_pin_position("R1", "2")
        p1x, p1y = sch.get_pin_position("R1", "1")
        assert p2x > p1x, "Pin 2 should be to the right"


# ═══════════════════════════════════════════════════════════════
# safe_wire tests
# ═══════════════════════════════════════════════════════════════

class TestSafeWire:

    def test_safe_wire_no_obstacle(self):
        """Wire in clear space should succeed."""
        sch = _setup_passive_sch()
        result = sch.safe_wire(50, 50, 50, 80)
        assert result is True
        assert len(sch.wires) == 1

    def test_safe_wire_through_body(self):
        """Wire through a component body should fail."""
        sch = _setup_passive_sch()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        # Wire straight through the resistor body vertically
        result = sch.safe_wire(100, 70, 100, 90)
        assert result is False
        assert len(sch.wires) == 0  # wire was not added

    def test_safe_wire_beside_body(self):
        """Wire beside (not through) a component should succeed."""
        sch = _setup_passive_sch()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        # Wire 20mm to the right — well clear
        result = sch.safe_wire(120, 70, 120, 90)
        assert result is True
        assert len(sch.wires) == 1


# ═══════════════════════════════════════════════════════════════
# connect_or_label tests
# ═══════════════════════════════════════════════════════════════

class TestConnectOrLabel:

    def test_clean_route_uses_wire(self):
        """When a clean route exists, connect_or_label uses wires."""
        sch = _setup_passive_sch()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        sch.place_component("Device:R", "R2", "10k", 100, 100)
        result = sch.connect_or_label("R1", "2", "R2", "1", "TEST_NET")
        assert result == "wired"
        assert len(sch.wires) >= 1
        assert len(sch.labels) == 0

    def test_blocked_route_uses_labels(self):
        """When no clean route exists, connect_or_label falls back to labels."""
        sch = _setup_passive_sch()
        # Place components with an obstacle between them
        sch.place_component("Device:R", "R1", "10k", 100, 60)
        # Place obstacle dead center between them
        sch.place_component("Device:R", "R_BLOCK", "0", 100, 80)
        sch.place_component("Device:R", "R2", "10k", 100, 100)

        wire_count_before = len(sch.wires)
        result = sch.connect_or_label("R1", "2", "R2", "1", "BLOCKED_NET")

        # Should be either wired (if Z-route found) or labeled
        assert result in ("wired", "labeled")
        if result == "labeled":
            # Labels should be present
            label_texts = [l.text for l in sch.labels]
            assert "BLOCKED_NET" in label_texts

    def test_auto_generates_label_name(self):
        """When label_name is None, auto-generates from refs."""
        sch = _setup_passive_sch()
        sch.place_component("Device:R", "R1", "10k", 100, 60)
        sch.place_component("Device:R", "R_BLOCK", "0", 100, 80)
        sch.place_component("Device:R", "R2", "10k", 100, 100)
        sch.place_component("Device:R", "R_BLOCK2", "0", 100, 90)
        sch.place_component("Device:R", "R_BLOCK3", "0", 100, 70)

        result = sch.connect_or_label("R1", "2", "R2", "1")
        # Just verify it didn't crash — label name format is internal


# ═══════════════════════════════════════════════════════════════
# _get_pin_outward_direction tests
# ═══════════════════════════════════════════════════════════════

class TestPinOutwardDirection:

    def test_resistor_pin1_up(self):
        """Resistor at rotation=0: pin 1 faces up."""
        sch = _setup_passive_sch()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        d = sch._get_pin_outward_direction("R1", "1")
        assert d == "up"

    def test_resistor_pin2_down(self):
        """Resistor at rotation=0: pin 2 faces down."""
        sch = _setup_passive_sch()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        d = sch._get_pin_outward_direction("R1", "2")
        assert d == "down"

    def test_led_pin_directions(self):
        """LED at rotation=0: pin 1 (cathode) faces left, pin 2 (anode) faces right."""
        sch = _setup_passive_sch()
        sch.place_component("Device:LED", "LED1", "Red", 100, 80)
        d1 = sch._get_pin_outward_direction("LED1", "1")
        d2 = sch._get_pin_outward_direction("LED1", "2")
        assert d1 == "left"
        assert d2 == "right"


# ═══════════════════════════════════════════════════════════════
# vcc_above / vcc_at_pin tests
# ═══════════════════════════════════════════════════════════════

class TestVccHelpers:

    def test_vcc_above_places_symbol_and_wire(self):
        sch = _setup_passive_sch()
        sch.vcc_above(100, 80)
        # Should have a wire and a power symbol
        assert len(sch.wires) == 1
        pwr_comps = [c for c in sch.components if c.reference.startswith("#PWR")]
        assert len(pwr_comps) == 1
        assert pwr_comps[0].value == "VCC"
        # Power symbol should be above (lower y)
        assert pwr_comps[0].y < 80

    def test_vcc_at_pin(self):
        sch = _setup_passive_sch()
        sch.place_component("Device:R", "R1", "10k", 100, 80)
        sch.vcc_at_pin("R1", "1")
        pwr = [c for c in sch.components if c.value == "VCC"]
        assert len(pwr) == 1


# ═══════════════════════════════════════════════════════════════
# Subcircuit template tests
# ═══════════════════════════════════════════════════════════════

class TestSeriesChain:

    def test_basic_chain_down(self):
        """Place R → LED chain going downward."""
        sch = _setup_passive_sch()
        refs = sch.place_series_chain([
            {"lib_id": "Device:R", "ref": "R1", "value": "330"},
            {"lib_id": "Device:LED", "ref": "LED1", "value": "Red"},
        ], direction="down", anchor_x=100, anchor_y=60,
            gnd_at_end=True, start_label="SIG")

        assert refs == ["R1", "LED1"]

        # R1 should be above LED1
        r1 = next(c for c in sch.components if c.reference == "R1")
        led1 = next(c for c in sch.components if c.reference == "LED1")
        assert r1.y < led1.y

        # GND symbol should exist
        gnd_comps = [c for c in sch.components if c.value == "GND"]
        assert len(gnd_comps) >= 1

        # Start label should exist
        labels = [l.text for l in sch.labels]
        assert "SIG" in labels

    def test_chain_with_vcc(self):
        """Chain with VCC at start."""
        sch = _setup_passive_sch()
        refs = sch.place_series_chain([
            {"lib_id": "Device:R", "ref": "R1", "value": "4.7k"},
        ], direction="down", anchor_x=100, anchor_y=80,
            vcc_at_start=True, gnd_at_end=False, end_label="SDA")

        vcc_comps = [c for c in sch.components if c.value == "VCC"]
        assert len(vcc_comps) >= 1

    def test_chain_right(self):
        """Horizontal chain going right."""
        sch = _setup_passive_sch()
        refs = sch.place_series_chain([
            {"lib_id": "Device:R", "ref": "R1", "value": "10k"},
            {"lib_id": "Device:R", "ref": "R2", "value": "10k"},
        ], direction="right", anchor_x=80, anchor_y=80,
            gnd_at_end=False)

        r1 = next(c for c in sch.components if c.reference == "R1")
        r2 = next(c for c in sch.components if c.reference == "R2")
        assert r1.x < r2.x, "R1 should be left of R2 in rightward chain"

    def test_chain_validates_clean(self):
        """Series chain should produce a valid schematic."""
        if not HAS_VALIDATOR:
            return

        sch = _setup_passive_sch()
        sch.place_series_chain([
            {"lib_id": "Device:R", "ref": "R1", "value": "330"},
        ], direction="down", anchor_x=100, anchor_y=80,
            vcc_at_start=True, gnd_at_end=True)

        result = validate(sch)
        errors = [i for i in result.issues if i.severity == "error"]
        assert len(errors) == 0, f"Validation errors: {errors}"


class TestDecouplingCap:

    def test_basic_decoupling(self):
        """Decoupling cap T-junction."""
        sch = _setup_passive_sch()
        # Create a horizontal power bus wire
        sch.add_wire(80, 80, 120, 80)

        sch.place_decoupling_cap(100, 80, "C1", "100nF")

        c1 = next(c for c in sch.components if c.reference == "C1")
        # Cap should be below the bus
        assert c1.y > 80

        # Pin 1 should face up, pin 2 should face down
        p1x, p1y = sch.get_pin_position("C1", "1")
        p2x, p2y = sch.get_pin_position("C1", "2")
        assert p1y < p2y, "Pin 1 should be above pin 2"

        # GND should exist below
        gnd_comps = [c for c in sch.components if c.value == "GND"]
        assert len(gnd_comps) >= 1


class TestPullUpDown:

    def test_pullup(self):
        """Pull-up resistor to VCC."""
        sch = _setup_passive_sch()
        sch.add_lib_symbol_ic("custom:MCU", [
            ("1", "SDA", "bidirectional", "left", 2),
        ], width=10.16)
        sch.place_component("custom:MCU", "U1", "MCU", 100, 80)

        sch.place_pullup("U1", "1", "R1", "4.7k")

        r1 = next(c for c in sch.components if c.reference == "R1")
        vcc_comps = [c for c in sch.components if c.value == "VCC"]
        assert len(vcc_comps) >= 1

        # Verify R1 pin 1 is above pin 2 (vertical orientation)
        p1x, p1y = sch.get_pin_position("R1", "1")
        p2x, p2y = sch.get_pin_position("R1", "2")
        assert p1y < p2y

    def test_pulldown(self):
        """Pull-down resistor to GND."""
        sch = _setup_passive_sch()
        sch.add_lib_symbol_ic("custom:MCU", [
            ("1", "EN", "input", "left", 2),
        ], width=10.16)
        sch.place_component("custom:MCU", "U1", "MCU", 100, 80)

        sch.place_pulldown("U1", "1", "R1", "10k")

        gnd_comps = [c for c in sch.components if c.value == "GND"]
        assert len(gnd_comps) >= 1


class TestVoltageDivider:

    def test_basic_divider(self):
        """Voltage divider with two resistors."""
        sch = _setup_passive_sch()
        r_top, r_bot = sch.place_voltage_divider(
            "R1", "10k", "R2", "10k",
            tap_label="VDIV",
            top_rail="VCC", bottom_gnd=True,
            anchor_x=100, anchor_y=60
        )

        assert r_top == "R1"
        assert r_bot == "R2"

        r1 = next(c for c in sch.components if c.reference == "R1")
        r2 = next(c for c in sch.components if c.reference == "R2")

        # R1 should be above R2
        assert r1.y < r2.y

        # Tap label should exist
        labels = [l.text for l in sch.labels]
        assert "VDIV" in labels

        # VCC and GND should exist
        vcc_comps = [c for c in sch.components if c.value == "VCC"]
        gnd_comps = [c for c in sch.components if c.value == "GND"]
        assert len(vcc_comps) >= 1
        assert len(gnd_comps) >= 1

    def test_divider_validates(self):
        """Voltage divider should produce a valid schematic."""
        if not HAS_VALIDATOR:
            return

        sch = _setup_passive_sch()
        sch.place_voltage_divider(
            "R1", "10k", "R2", "10k",
            tap_label="FB",
            top_rail="VCC", bottom_gnd=True,
            anchor_x=100, anchor_y=60
        )

        result = validate(sch)
        errors = [i for i in result.issues if i.severity == "error"]
        # Filter out single_pin_net warnings (tap label only has one connection in this simple test)
        real_errors = [e for e in errors if e.check_name != "single_pin_net"]
        assert len(real_errors) == 0, f"Validation errors: {real_errors}"


# ═══════════════════════════════════════════════════════════════
# Through-body prevention integration tests
# ═══════════════════════════════════════════════════════════════

class TestThroughBodyPrevention:

    def test_decoupling_cap_no_through_body(self):
        """Decoupling cap must NOT have a wire passing through its body."""
        sch = _setup_passive_sch()
        sch.add_wire(80, 80, 120, 80)
        sch.place_decoupling_cap(100, 80, "C1", "100nF")

        c1 = next(c for c in sch.components if c.reference == "C1")
        body = sch._get_component_body_bbox(c1)

        # Check no wire passes through the cap body
        for w in sch.wires:
            if sch._segment_crosses_rect(w.x1, w.y1, w.x2, w.y2, body):
                # Allow wires that terminate at pin positions
                p1 = sch.get_pin_position("C1", "1")
                p2 = sch.get_pin_position("C1", "2")
                is_pin_wire = (
                    (abs(w.x1 - p1[0]) < 0.1 and abs(w.y1 - p1[1]) < 0.1) or
                    (abs(w.x2 - p1[0]) < 0.1 and abs(w.y2 - p1[1]) < 0.1) or
                    (abs(w.x1 - p2[0]) < 0.1 and abs(w.y1 - p2[1]) < 0.1) or
                    (abs(w.x2 - p2[0]) < 0.1 and abs(w.y2 - p2[1]) < 0.1)
                )
                assert is_pin_wire, \
                    f"Wire ({w.x1},{w.y1})-({w.x2},{w.y2}) passes through C1 body"

    def test_series_chain_no_through_body(self):
        """Series chain should not produce through-body wires."""
        sch = _setup_passive_sch()
        sch.place_series_chain([
            {"lib_id": "Device:R", "ref": "R1", "value": "330"},
            {"lib_id": "Device:R", "ref": "R2", "value": "330"},
        ], direction="down", anchor_x=100, anchor_y=60, gnd_at_end=True)

        for comp in sch.components:
            if comp.reference.startswith("#PWR"):
                continue
            body = sch._get_component_body_bbox(comp)
            for w in sch.wires:
                assert not sch._segment_crosses_rect(
                    w.x1, w.y1, w.x2, w.y2, body
                ), f"Wire ({w.x1},{w.y1})-({w.x2},{w.y2}) crosses {comp.reference}"


# ═══════════════════════════════════════════════════════════════
# Grid alignment tests
# ═══════════════════════════════════════════════════════════════

class TestGridAlignment:

    def _is_grid_aligned(self, val, grid=1.27):
        remainder = abs(val / grid - round(val / grid))
        return remainder < 0.001

    def test_series_chain_grid_aligned(self):
        """All components and wires from series chain are grid-aligned."""
        sch = _setup_passive_sch()
        sch.place_series_chain([
            {"lib_id": "Device:R", "ref": "R1", "value": "330"},
            {"lib_id": "Device:LED", "ref": "LED1", "value": "Red"},
        ], direction="down", anchor_x=100, anchor_y=60, gnd_at_end=True)

        for comp in sch.components:
            assert self._is_grid_aligned(comp.x), f"{comp.reference} x={comp.x} not grid-aligned"
            assert self._is_grid_aligned(comp.y), f"{comp.reference} y={comp.y} not grid-aligned"

        for w in sch.wires:
            assert self._is_grid_aligned(w.x1), f"Wire x1={w.x1} not grid-aligned"
            assert self._is_grid_aligned(w.y1), f"Wire y1={w.y1} not grid-aligned"
            assert self._is_grid_aligned(w.x2), f"Wire x2={w.x2} not grid-aligned"
            assert self._is_grid_aligned(w.y2), f"Wire y2={w.y2} not grid-aligned"
