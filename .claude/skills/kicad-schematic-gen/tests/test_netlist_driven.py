"""Tests for netlist-driven schematic generation helpers.

Tests the new builder API methods: get_pin_stub_direction, label_at_pin (auto),
gnd_at_pin (auto), power_at_pin, nc_at_pin, ensure_all_pins_assigned,
ensure_footprints.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from generate_kicad_sch import KicadSchematic, snap_to_grid


# ─── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def sch():
    """Basic schematic with standard symbols registered."""
    s = KicadSchematic("Test Netlist Driven")
    s.add_lib_symbol_resistor()
    s.add_lib_symbol_capacitor()
    s.add_lib_symbol_led()
    s.add_lib_symbol_diode()
    s.add_lib_symbol_inductor()
    s.add_lib_symbol_power("GND")
    s.add_lib_symbol_power("+5V")
    return s


@pytest.fixture
def sch_with_ic(sch):
    """Schematic with a custom IC (4 pins: left/right/top/bottom)."""
    sch.add_lib_symbol_ic("custom:TestIC", ref_prefix="U", value="TestIC",
                          width=10.16, pins=[
        ("1", "VIN",  "power_in",  "left",   0),
        ("2", "GND",  "power_in",  "bottom", 0),
        ("3", "VOUT", "power_out", "right",  0),
        ("4", "EN",   "input",     "top",    0),
    ])
    sch.place_component("custom:TestIC", "U1", "TestIC", 100, 80,
                        footprint="Package_TO_SOT_SMD:SOT-23-5")
    return sch


# ─── get_pin_stub_direction ────────────────────────────────────────────

class TestGetPinStubDirection:
    def test_ic_left_pin_faces_left(self, sch_with_ic):
        dx, dy = sch_with_ic.get_pin_stub_direction("U1", "1")
        assert dx == -1 and dy == 0, f"Left pin should face left, got ({dx},{dy})"

    def test_ic_right_pin_faces_right(self, sch_with_ic):
        dx, dy = sch_with_ic.get_pin_stub_direction("U1", "3")
        assert dx == 1 and dy == 0, f"Right pin should face right, got ({dx},{dy})"

    def test_ic_bottom_pin_faces_down(self, sch_with_ic):
        dx, dy = sch_with_ic.get_pin_stub_direction("U1", "2")
        assert dx == 0 and dy == 1, f"Bottom pin should face down, got ({dx},{dy})"

    def test_ic_top_pin_faces_up(self, sch_with_ic):
        dx, dy = sch_with_ic.get_pin_stub_direction("U1", "4")
        assert dx == 0 and dy == -1, f"Top pin should face up, got ({dx},{dy})"

    def test_resistor_pin1_faces_up(self, sch):
        """Resistor pin 1 is at top, its stub should go up (away from body)."""
        sch.place_component("Device:R", "R1", "10k", 50, 50,
                            footprint="Resistor_SMD:R_0805_2012Metric")
        dx, dy = sch.get_pin_stub_direction("R1", "1")
        assert dx == 0 and dy == -1, f"R pin 1 should face up, got ({dx},{dy})"

    def test_resistor_pin2_faces_down(self, sch):
        """Resistor pin 2 is at bottom, its stub should go down."""
        sch.place_component("Device:R", "R1", "10k", 50, 50,
                            footprint="Resistor_SMD:R_0805_2012Metric")
        dx, dy = sch.get_pin_stub_direction("R1", "2")
        assert dx == 0 and dy == 1, f"R pin 2 should face down, got ({dx},{dy})"

    def test_resistor_rotated_90(self, sch):
        """Resistor rotated 90° CCW: pin 1 (top) rotates to the right, pin 2 to the left."""
        sch.place_component("Device:R", "R1", "10k", 50, 50, rotation=90,
                            footprint="Resistor_SMD:R_0805_2012Metric")
        dx1, dy1 = sch.get_pin_stub_direction("R1", "1")
        dx2, dy2 = sch.get_pin_stub_direction("R1", "2")
        assert (dx1, dy1) == (1, 0), f"R1 pin 1 @90° should face right, got ({dx1},{dy1})"
        assert (dx2, dy2) == (-1, 0), f"R1 pin 2 @90° should face left, got ({dx2},{dy2})"

    def test_led_pin_directions(self, sch):
        """LED: pin 1 (K) is left, pin 2 (A) is right."""
        sch.place_component("Device:LED", "LED1", "Red", 50, 50,
                            footprint="LED_SMD:LED_0805_2012Metric")
        dx1, dy1 = sch.get_pin_stub_direction("LED1", "1")
        dx2, dy2 = sch.get_pin_stub_direction("LED1", "2")
        assert (dx1, dy1) == (-1, 0), f"LED K should face left, got ({dx1},{dy1})"
        assert (dx2, dy2) == (1, 0), f"LED A should face right, got ({dx2},{dy2})"

    def test_nonexistent_component_raises(self, sch):
        with pytest.raises(ValueError, match="not found"):
            sch.get_pin_stub_direction("NOPE", "1")

    def test_nonexistent_pin_raises(self, sch_with_ic):
        with pytest.raises(ValueError, match="Pin.*not found"):
            sch_with_ic.get_pin_stub_direction("U1", "99")


# ─── label_at_pin (auto direction) ────────────────────────────────────

class TestLabelAtPinAuto:
    def test_creates_wire_and_label(self, sch_with_ic):
        initial_wires = len(sch_with_ic.wires)
        initial_labels = len(sch_with_ic.labels)
        sch_with_ic.label_at_pin("U1", "1", "VBUS")
        assert len(sch_with_ic.wires) == initial_wires + 1
        assert len(sch_with_ic.labels) == initial_labels + 1

    def test_label_text_matches(self, sch_with_ic):
        sch_with_ic.label_at_pin("U1", "1", "MY_NET")
        label = sch_with_ic.labels[-1]
        assert label.text == "MY_NET"

    def test_left_pin_label_goes_left(self, sch_with_ic):
        """Left pin stub extends leftward — wire end x < pin x."""
        pin_x, pin_y = sch_with_ic.get_pin_position("U1", "1")
        sch_with_ic.label_at_pin("U1", "1", "VBUS")
        wire = sch_with_ic.wires[-1]
        label = sch_with_ic.labels[-1]
        # The label should be to the left of the pin
        assert label.x < pin_x
        assert label.rotation == 180  # left-facing label

    def test_right_pin_label_goes_right(self, sch_with_ic):
        pin_x, _ = sch_with_ic.get_pin_position("U1", "3")
        sch_with_ic.label_at_pin("U1", "3", "VOUT")
        label = sch_with_ic.labels[-1]
        assert label.x > pin_x
        assert label.rotation == 0

    def test_top_pin_label_goes_up(self, sch_with_ic):
        _, pin_y = sch_with_ic.get_pin_position("U1", "4")
        sch_with_ic.label_at_pin("U1", "4", "EN")
        label = sch_with_ic.labels[-1]
        assert label.y < pin_y
        assert label.rotation == 90

    def test_bottom_pin_label_goes_down(self, sch_with_ic):
        _, pin_y = sch_with_ic.get_pin_position("U1", "2")
        sch_with_ic.label_at_pin("U1", "2", "GND_NET")
        label = sch_with_ic.labels[-1]
        assert label.y > pin_y
        assert label.rotation == 270

    def test_explicit_direction_overrides_auto(self, sch_with_ic):
        """When direction is specified, it overrides auto-detection."""
        pin_x, _ = sch_with_ic.get_pin_position("U1", "1")
        sch_with_ic.label_at_pin("U1", "1", "FORCED_RIGHT", direction="right")
        label = sch_with_ic.labels[-1]
        assert label.x > pin_x
        assert label.rotation == 0

    def test_passive_pin1_auto_direction(self, sch):
        """Resistor pin 1 auto-direction should go up."""
        sch.place_component("Device:R", "R1", "10k", 50, 50,
                            footprint="Resistor_SMD:R_0805_2012Metric")
        _, pin_y = sch.get_pin_position("R1", "1")
        sch.label_at_pin("R1", "1", "NET_A")
        label = sch.labels[-1]
        assert label.y < pin_y
        assert label.rotation == 90


# ─── gnd_at_pin (auto direction) ──────────────────────────────────────

class TestGndAtPinAuto:
    def test_places_gnd_symbol(self, sch_with_ic):
        initial_comps = len(sch_with_ic.components)
        sch_with_ic.gnd_at_pin("U1", "2")
        # Should have added a power symbol component
        pwr_comps = [c for c in sch_with_ic.components if c.reference.startswith("#PWR")]
        assert len(pwr_comps) >= 1

    def test_bottom_pin_goes_straight_down(self, sch_with_ic):
        """Bottom-facing pin: GND should be directly below."""
        pin_x, pin_y = sch_with_ic.get_pin_position("U1", "2")
        sch_with_ic.gnd_at_pin("U1", "2")
        # Find the GND power symbol
        gnd_comp = [c for c in sch_with_ic.components
                    if c.reference.startswith("#PWR") and c.value == "GND"][-1]
        assert gnd_comp.x == pin_x, "GND should be at same X as pin"
        assert gnd_comp.y > pin_y, "GND should be below pin"

    def test_left_pin_routes_to_gnd(self, sch_with_ic):
        """Left-facing pin: should stub left then go down."""
        sch_with_ic.gnd_at_pin("U1", "1")
        gnd_comp = [c for c in sch_with_ic.components
                    if c.reference.startswith("#PWR") and c.value == "GND"][-1]
        pin_x, pin_y = sch_with_ic.get_pin_position("U1", "1")
        # GND symbol should be below pin y
        assert gnd_comp.y > pin_y

    def test_resistor_pin2_gnd(self, sch):
        """Resistor pin 2 faces down — straight GND."""
        sch.place_component("Device:R", "R1", "10k", 50, 50,
                            footprint="Resistor_SMD:R_0805_2012Metric")
        pin_x, pin_y = sch.get_pin_position("R1", "2")
        sch.gnd_at_pin("R1", "2")
        gnd_comp = [c for c in sch.components
                    if c.reference.startswith("#PWR") and c.value == "GND"][-1]
        assert gnd_comp.x == pin_x
        assert gnd_comp.y > pin_y


# ─── power_at_pin ─────────────────────────────────────────────────────

class TestPowerAtPin:
    def test_places_power_symbol(self, sch_with_ic):
        sch_with_ic.power_at_pin("U1", "4", "+5V")
        pwr = [c for c in sch_with_ic.components
               if c.reference.startswith("#PWR") and c.value == "+5V"]
        assert len(pwr) >= 1

    def test_top_pin_goes_straight_up(self, sch_with_ic):
        """Top-facing pin: +5V should be directly above."""
        pin_x, pin_y = sch_with_ic.get_pin_position("U1", "4")
        sch_with_ic.power_at_pin("U1", "4", "+5V")
        pwr = [c for c in sch_with_ic.components
               if c.reference.startswith("#PWR") and c.value == "+5V"][-1]
        assert pwr.x == pin_x
        assert pwr.y < pin_y

    def test_gnd_delegates_to_gnd_at_pin(self, sch_with_ic):
        """power_at_pin("GND") should delegate to gnd_at_pin."""
        sch_with_ic.power_at_pin("U1", "2", "GND")
        gnd = [c for c in sch_with_ic.components
               if c.reference.startswith("#PWR") and c.value == "GND"]
        assert len(gnd) >= 1

    def test_right_pin_routes_up(self, sch_with_ic):
        """Right-facing pin: should stub right then go up for VCC."""
        sch_with_ic.power_at_pin("U1", "3", "+5V")
        pwr = [c for c in sch_with_ic.components
               if c.reference.startswith("#PWR") and c.value == "+5V"][-1]
        pin_x, pin_y = sch_with_ic.get_pin_position("U1", "3")
        assert pwr.y < pin_y, "+5V should be above pin"


# ─── nc_at_pin ─────────────────────────────────────────────────────────

class TestNcAtPin:
    def test_places_no_connect(self, sch_with_ic):
        initial_nc = len(sch_with_ic.no_connects)
        sch_with_ic.nc_at_pin("U1", "4")
        assert len(sch_with_ic.no_connects) == initial_nc + 1

    def test_no_connect_at_pin_position(self, sch_with_ic):
        pin_x, pin_y = sch_with_ic.get_pin_position("U1", "4")
        sch_with_ic.nc_at_pin("U1", "4")
        nc = sch_with_ic.no_connects[-1]
        assert nc.x == pin_x and nc.y == pin_y


# ─── ensure_all_pins_assigned ──────────────────────────────────────────

class TestEnsureAllPinsAssigned:
    def test_fully_wired_returns_empty(self, sch_with_ic):
        """All pins labeled → no unassigned pins."""
        sch_with_ic.label_at_pin("U1", "1", "VIN")
        sch_with_ic.gnd_at_pin("U1", "2")
        sch_with_ic.label_at_pin("U1", "3", "VOUT")
        sch_with_ic.nc_at_pin("U1", "4")
        result = sch_with_ic.ensure_all_pins_assigned()
        assert result == [], f"Expected empty, got {result}"

    def test_missing_pin_detected(self, sch_with_ic):
        """One pin not wired → should appear in result."""
        sch_with_ic.label_at_pin("U1", "1", "VIN")
        sch_with_ic.gnd_at_pin("U1", "2")
        # pin 3 and 4 not wired
        result = sch_with_ic.ensure_all_pins_assigned()
        refs_pins = set(result)
        assert ("U1", "3") in refs_pins
        assert ("U1", "4") in refs_pins

    def test_nc_counts_as_assigned(self, sch_with_ic):
        """No-connect marker should count as assigned."""
        sch_with_ic.label_at_pin("U1", "1", "VIN")
        sch_with_ic.gnd_at_pin("U1", "2")
        sch_with_ic.label_at_pin("U1", "3", "VOUT")
        sch_with_ic.nc_at_pin("U1", "4")
        result = sch_with_ic.ensure_all_pins_assigned()
        assert result == []

    def test_power_symbols_ignored(self, sch):
        """Power symbol components should not be audited."""
        sch.place_power_symbol("GND", 50, 50)
        result = sch.ensure_all_pins_assigned()
        assert result == []


# ─── ensure_footprints ─────────────────────────────────────────────────

class TestEnsureFootprints:
    def test_all_have_footprints(self, sch_with_ic):
        result = sch_with_ic.ensure_footprints()
        assert result == []

    def test_missing_footprint_detected(self, sch):
        sch.place_component("Device:R", "R1", "10k", 50, 50, footprint="")
        result = sch.ensure_footprints()
        assert "R1" in result

    def test_power_symbols_excluded(self, sch):
        sch.place_power_symbol("GND", 50, 50)
        result = sch.ensure_footprints()
        assert result == []


# ─── Integration: full netlist-driven circuit ──────────────────────────

class TestNetlistDrivenIntegration:
    """Build a small circuit entirely with label-based wiring and validate."""

    def test_rc_circuit_label_only(self, sch):
        """R1 + C1 circuit using only label_at_pin — should validate clean."""
        sch.place_component("Device:R", "R1", "10k", 60, 50,
                            footprint="Resistor_SMD:R_0805_2012Metric")
        sch.place_component("Device:C", "C1", "100nF", 80, 50,
                            footprint="Capacitor_SMD:C_0805_2012Metric")

        # Net "VCC": R1.1, C1.1
        sch.label_at_pin("R1", "1", "VCC")
        sch.label_at_pin("C1", "1", "VCC")

        # Net "GND": R1.2, C1.2
        sch.gnd_at_pin("R1", "2")
        sch.gnd_at_pin("C1", "2")

        # Pre-save audit
        assert sch.ensure_all_pins_assigned() == []
        assert sch.ensure_footprints() == []

        # Validate with the validator
        import tempfile
        from validate_kicad_sch import validate
        result = validate(sch)
        errors = [i for i in result.issues if i.severity == "error"]
        assert len(errors) == 0, f"Errors: {[i.message for i in errors]}"

    def test_ic_circuit_label_only(self):
        """IC with passives, fully label-driven."""
        sch = KicadSchematic("IC Test")
        sch.add_lib_symbol_resistor()
        sch.add_lib_symbol_capacitor()
        sch.add_lib_symbol_power("GND")
        sch.add_lib_symbol_power("+5V")
        sch.add_lib_symbol_ic("custom:LDO", ref_prefix="U", value="LDO",
                              width=10.16, pins=[
            ("1", "VIN",  "power_in",  "left",  0),
            ("2", "GND",  "power_in",  "bottom", 0),
            ("3", "VOUT", "power_out", "right", 0),
            ("4", "EN",   "input",     "left",  1),
        ])

        sch.place_component("custom:LDO", "U1", "LDO", 100, 80,
                            footprint="Package_TO_SOT_SMD:SOT-23-5")
        sch.place_component("Device:C", "C1", "100nF", 75, 55,
                            footprint="Capacitor_SMD:C_0805_2012Metric")
        sch.place_component("Device:C", "C2", "10uF", 125, 55,
                            footprint="Capacitor_SMD:C_0805_2012Metric")
        sch.place_component("Device:R", "R1", "100k", 75, 90,
                            footprint="Resistor_SMD:R_0805_2012Metric")

        # VIN net: U1.1, U1.4, C1.1 — with power symbol on first pin
        sch.power_at_pin("U1", "1", "+5V")
        sch.label_at_pin("U1", "4", "VIN")
        sch.label_at_pin("C1", "1", "VIN")

        # VOUT net: U1.3, C2.1, R1.1
        sch.label_at_pin("U1", "3", "VOUT")
        sch.label_at_pin("C2", "1", "VOUT")
        sch.label_at_pin("R1", "1", "VOUT")

        # GND net: U1.2, C1.2, C2.2, R1.2
        sch.gnd_at_pin("U1", "2")
        sch.gnd_at_pin("C1", "2")
        sch.gnd_at_pin("C2", "2")
        sch.gnd_at_pin("R1", "2")

        assert sch.ensure_all_pins_assigned() == []
        assert sch.ensure_footprints() == []

        from validate_kicad_sch import validate
        result = validate(sch)
        errors = [i for i in result.issues if i.severity == "error"]
        assert len(errors) == 0, f"Errors: {[i.message for i in errors]}"
