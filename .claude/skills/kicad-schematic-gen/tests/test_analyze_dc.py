#!/usr/bin/env python3
"""Tests for the DC analysis engine."""

import sys
import os
import pytest

# Ensure scripts directory is importable
_script_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from analyze_dc import (
    analyze, Design, AnalysisResult, _parse_value,
    _analyze_voltage_divider, _analyze_ldo_regulator,
    _analyze_led_circuit, _analyze_pullup_network,
    _analyze_current_budget, _analyze_cap_sizing,
    _analyze_feedback_divider, load_design_from_string,
    format_result_text, format_result_json,
)


# ─── Value parser tests ──────────────────────────────────────────────────


class TestParseValue:
    def test_plain_int(self):
        assert _parse_value(470) == 470.0

    def test_plain_float(self):
        assert _parse_value(4.7) == 4.7

    def test_string_int(self):
        assert _parse_value("470") == 470.0

    def test_k_suffix(self):
        assert _parse_value("10k") == 10000.0

    def test_k_suffix_decimal(self):
        assert _parse_value("4.7k") == 4700.0

    def test_M_suffix(self):
        assert _parse_value("1M") == 1e6

    def test_nF(self):
        assert abs(_parse_value("100nF") - 1e-7) < 1e-15

    def test_uF(self):
        assert abs(_parse_value("10uF") - 1e-5) < 1e-13

    def test_pF(self):
        assert _parse_value("100pF") == 1e-10

    def test_mA(self):
        assert _parse_value("20mA") == 0.020

    def test_ohm(self):
        assert _parse_value("100ohm") == 100.0

    def test_invalid(self):
        with pytest.raises(ValueError):
            _parse_value("abc")


# ─── Voltage divider tests ──────────────────────────────────────────────


class TestVoltageDivider:
    def test_correct_divider(self):
        """50/50 divider from 5V should give 2.5V."""
        vd = {"name": "test", "vin": 5.0, "r_top": "10k", "r_bot": "10k",
              "target_vout": 2.5}
        issues = _analyze_voltage_divider(vd)
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == 0

    def test_wrong_ratio(self):
        """10k/10k divider targeting 3.3V should error."""
        vd = {"name": "test", "vin": 5.0, "r_top": "10k", "r_bot": "10k",
              "target_vout": 3.3, "tolerance_pct": 5}
        issues = _analyze_voltage_divider(vd)
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == 1
        assert "off target" in errors[0].message

    def test_high_quiescent_current(self):
        """Low-value divider should warn about current draw."""
        vd = {"name": "test", "vin": 12.0, "r_top": "1k", "r_bot": "1k",
              "target_vout": 6.0}
        issues = _analyze_voltage_divider(vd)
        warnings = [i for i in issues if i.severity == "warning"]
        assert any("quiescent" in w.message.lower() or "draws" in w.message.lower()
                    for w in warnings)

    def test_loaded_divider(self):
        """Loaded divider should account for load current pulling output down."""
        vd = {"name": "test", "vin": 5.0, "r_top": "100k", "r_bot": "100k",
              "target_vout": 2.5, "load_current_ua": 100}
        issues = _analyze_voltage_divider(vd)
        # Load will pull output below 2.5V — find the info/error
        info = [i for i in issues if i.details.get("vout_loaded")]
        assert info[0].details["vout_loaded"] < 2.5

    def test_missing_param(self):
        """Missing vin should error."""
        vd = {"name": "test", "r_top": "10k", "r_bot": "10k", "target_vout": 2.5}
        issues = _analyze_voltage_divider(vd)
        assert issues[0].severity == "error"


# ─── LDO regulator tests ────────────────────────────────────────────────


class TestLDORegulator:
    def test_normal_operation(self):
        """AP2112K-3.3 from 5V with light load should pass."""
        ldo = {
            "name": "U1", "part": "AP2112K-3.3",
            "vin": 5.0, "vout": 3.3, "dropout_v": 0.25,
            "max_current_ma": 600, "load_current_ma": 50,
            "cin_uf": 1, "cout_uf": 1,
            "cin_min_uf": 0.7, "cout_min_uf": 0.7,
            "package": "SOT-23-5", "enable_pin": "tied_high",
        }
        issues = _analyze_ldo_regulator(ldo)
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == 0

    def test_dropout_violation(self):
        """3.3V out from 3.4V in with 0.25V dropout should error."""
        ldo = {
            "name": "U1", "vin": 3.4, "vout": 3.3, "dropout_v": 0.25,
            "max_current_ma": 600, "load_current_ma": 100,
            "package": "SOT-23-5",
        }
        issues = _analyze_ldo_regulator(ldo)
        errors = [i for i in issues if i.severity == "error"]
        assert any("dropout" in e.message.lower() or "headroom" in e.message.lower()
                    for e in errors)

    def test_overcurrent(self):
        """Load exceeding max rating should error."""
        ldo = {
            "name": "U1", "vin": 5.0, "vout": 3.3, "dropout_v": 0.25,
            "max_current_ma": 600, "load_current_ma": 700,
            "package": "SOT-23-5",
        }
        issues = _analyze_ldo_regulator(ldo)
        errors = [i for i in issues if i.severity == "error"]
        assert any("exceeds" in e.message for e in errors)

    def test_thermal_overload(self):
        """12V-to-3.3V at 500mA in SOT-23-5 should overheat."""
        ldo = {
            "name": "U1", "vin": 12.0, "vout": 3.3, "dropout_v": 0.25,
            "max_current_ma": 600, "load_current_ma": 500,
            "package": "SOT-23-5",
        }
        issues = _analyze_ldo_regulator(ldo)
        errors = [i for i in issues if i.severity == "error"]
        # P = (12-3.3) * 0.5 = 4.35W, Tj = 25 + 4.35*250 = 1112°C
        assert any("junction temp" in e.message.lower() or "thermal" in e.message.lower()
                    for e in errors)

    def test_undersized_cap(self):
        """Output cap below datasheet minimum should error."""
        ldo = {
            "name": "U1", "vin": 5.0, "vout": 3.3, "dropout_v": 0.25,
            "max_current_ma": 600, "load_current_ma": 100,
            "cout_uf": 0.1, "cout_min_uf": 1.0,
            "package": "SOT-23-5",
        }
        issues = _analyze_ldo_regulator(ldo)
        errors = [i for i in issues if i.severity == "error"]
        assert any("output cap" in e.message.lower() for e in errors)

    def test_missing_enable_pin(self):
        """No enable_pin specified should warn."""
        ldo = {
            "name": "U1", "vin": 5.0, "vout": 3.3, "dropout_v": 0.25,
            "max_current_ma": 600, "load_current_ma": 100,
            "package": "SOT-23-5",
        }
        issues = _analyze_ldo_regulator(ldo)
        warnings = [i for i in issues if i.severity == "warning"]
        assert any("enable" in w.message.lower() for w in warnings)


# ─── LED circuit tests ──────────────────────────────────────────────────


class TestLEDCircuit:
    def test_normal_led(self):
        """1k resistor on 3.3V with green LED (Vf=2.2) → ~1.1mA."""
        led = {"name": "D1", "vsource": 3.3, "vf": 2.2, "resistor": "1k",
               "target_current_ma": 1, "max_current_ma": 20}
        issues = _analyze_led_circuit(led)
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == 0
        info = [i for i in issues if i.details.get("current_mA")]
        assert abs(info[0].details["current_mA"] - 1.1) < 0.01

    def test_overcurrent(self):
        """100 ohm on 5V with red LED (Vf=2.0) → 30mA, exceeds 20mA max."""
        led = {"name": "D1", "vsource": 5.0, "vf": 2.0, "resistor": "100",
               "max_current_ma": 20}
        issues = _analyze_led_circuit(led)
        errors = [i for i in issues if i.severity == "error"]
        assert any("exceeds" in e.message for e in errors)

    def test_led_wont_turn_on(self):
        """Vsource < Vf should error."""
        led = {"name": "D1", "vsource": 1.8, "vf": 2.2, "resistor": "1k"}
        issues = _analyze_led_circuit(led)
        errors = [i for i in issues if i.severity == "error"]
        assert any("will not turn on" in e.message for e in errors)

    def test_very_dim(self):
        """100k resistor → tiny current, should warn."""
        led = {"name": "D1", "vsource": 3.3, "vf": 2.2, "resistor": "100k"}
        issues = _analyze_led_circuit(led)
        warnings = [i for i in issues if i.severity == "warning"]
        assert any("low" in w.message.lower() or "visible" in w.message.lower()
                    for w in warnings)


# ─── Pull-up network tests ──────────────────────────────────────────────


class TestPullupNetwork:
    def test_i2c_4k7_at_3v3(self):
        """Standard 4.7k I2C pull-up at 3.3V should pass."""
        pu = {"name": "SDA", "vsource": 3.3, "resistor": "4.7k",
              "bus_type": "i2c", "bus_speed": "fast", "bus_capacitance_pf": 50}
        issues = _analyze_pullup_network(pu)
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == 0

    def test_i2c_too_strong(self):
        """100 ohm I2C pull-up should warn (too low)."""
        pu = {"name": "SDA", "vsource": 3.3, "resistor": "100",
              "bus_type": "i2c", "bus_speed": "fast"}
        issues = _analyze_pullup_network(pu)
        problems = [i for i in issues if i.severity in ("error", "warning")]
        assert len(problems) > 0

    def test_i2c_high_capacitance_slow_rise(self):
        """Large bus capacitance with weak pull-up → slow rise time."""
        pu = {"name": "SDA", "vsource": 3.3, "resistor": "10k",
              "bus_type": "i2c", "bus_speed": "fast", "bus_capacitance_pf": 200}
        issues = _analyze_pullup_network(pu)
        warnings = [i for i in issues if i.severity == "warning"]
        assert any("rise time" in w.message.lower() for w in warnings)

    def test_generic_pullup(self):
        """Generic pull-up should just report current."""
        pu = {"name": "RST", "vsource": 3.3, "resistor": "10k",
              "bus_type": "generic"}
        issues = _analyze_pullup_network(pu)
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == 0


# ─── Current budget tests ───────────────────────────────────────────────


class TestCurrentBudget:
    def test_within_budget(self):
        rails = [{"name": "+3V3", "voltage": 3.3, "source_ma": 600,
                  "loads": [{"name": "IC", "current_ma": 50},
                            {"name": "LED", "current_ma": 1}]}]
        issues = _analyze_current_budget(rails)
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == 0

    def test_over_budget(self):
        rails = [{"name": "+3V3", "voltage": 3.3, "source_ma": 100,
                  "loads": [{"name": "IC1", "current_ma": 60},
                            {"name": "IC2", "current_ma": 50}]}]
        issues = _analyze_current_budget(rails)
        errors = [i for i in issues if i.severity == "error"]
        assert any("exceeds" in e.message for e in errors)

    def test_tight_margin(self):
        rails = [{"name": "+3V3", "voltage": 3.3, "source_ma": 100,
                  "loads": [{"name": "IC", "current_ma": 85}]}]
        issues = _analyze_current_budget(rails)
        warnings = [i for i in issues if i.severity == "warning"]
        assert any("margin" in w.message.lower() or "85%" in w.message
                    for w in warnings)


# ─── Capacitor sizing tests ─────────────────────────────────────────────


class TestCapSizing:
    def test_adequate_cap(self):
        checks = [{"name": "C1", "value_uf": 1.0, "min_uf": 0.7}]
        issues = _analyze_cap_sizing(checks)
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == 0

    def test_undersized_cap(self):
        checks = [{"name": "C1", "value_uf": 0.1, "min_uf": 1.0}]
        issues = _analyze_cap_sizing(checks)
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == 1

    def test_mlcc_voltage_derating(self):
        """MLCC at operating voltage with <2x rated voltage should warn."""
        checks = [{"name": "C1", "value_uf": 1.0, "min_uf": 0.7,
                    "voltage_rating": 6.3, "operating_voltage": 5.0,
                    "type": "MLCC"}]
        issues = _analyze_cap_sizing(checks)
        warnings = [i for i in issues if i.severity == "warning"]
        assert any("DC bias" in w.message or "derating" in w.message.lower()
                    for w in warnings)


# ─── Feedback divider tests ─────────────────────────────────────────────


class TestFeedbackDivider:
    def test_correct_fb(self):
        """Vref=0.6V, R_top=47k, R_bot=10k → Vout=3.42V."""
        fb = {"name": "test", "vref": 0.6, "r_top": "47k", "r_bot": "10k",
              "target_vout": 3.42}
        issues = _analyze_feedback_divider(fb)
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == 0

    def test_wrong_fb(self):
        """Swapped resistors should give wrong output."""
        fb = {"name": "test", "vref": 0.6, "r_top": "10k", "r_bot": "47k",
              "target_vout": 3.42}
        issues = _analyze_feedback_divider(fb)
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == 1


# ─── Integration tests ──────────────────────────────────────────────────


class TestIntegration:
    def test_full_design_passes(self):
        """A well-designed board should pass analysis."""
        d = Design(
            project_name="test_board",
            rails=[{
                "name": "+3V3", "voltage": 3.3, "source_ma": 600,
                "loads": [
                    {"name": "sensor", "current_ma": 5},
                    {"name": "pull-ups", "current_ma": 1.4},
                    {"name": "LED", "current_ma": 1},
                ],
            }],
            ldo_regulators=[{
                "name": "U1", "vin": 5.0, "vout": 3.3, "dropout_v": 0.25,
                "max_current_ma": 600, "load_current_ma": 7.4,
                "cin_uf": 1, "cout_uf": 1,
                "cin_min_uf": 0.7, "cout_min_uf": 0.7,
                "package": "SOT-23-5", "enable_pin": "tied_high",
            }],
            led_circuits=[{
                "name": "D1", "vsource": 3.3, "vf": 2.2, "resistor": "1k",
                "target_current_ma": 1, "max_current_ma": 20,
            }],
            pullup_networks=[
                {"name": "SDA", "vsource": 3.3, "resistor": "4.7k",
                 "bus_type": "i2c", "bus_speed": "fast",
                 "bus_capacitance_pf": 50},
                {"name": "SCL", "vsource": 3.3, "resistor": "4.7k",
                 "bus_type": "i2c", "bus_speed": "fast",
                 "bus_capacitance_pf": 50},
            ],
            cap_checks=[
                {"name": "C1", "value_uf": 1, "min_uf": 0.7,
                 "voltage_rating": 16, "operating_voltage": 5.0, "type": "MLCC"},
                {"name": "C2", "value_uf": 1, "min_uf": 0.7,
                 "voltage_rating": 10, "operating_voltage": 3.3, "type": "MLCC"},
            ],
        )
        result = analyze(d)
        assert result.passed
        assert result.subcircuits_analyzed > 0
        assert len(result.errors) == 0

    def test_broken_design_fails(self):
        """A design with multiple problems should fail."""
        d = Design(
            project_name="broken_board",
            rails=[{
                "name": "+3V3", "voltage": 3.3, "source_ma": 100,
                "loads": [{"name": "IC", "current_ma": 150}],
            }],
            ldo_regulators=[{
                "name": "U1", "vin": 3.4, "vout": 3.3, "dropout_v": 0.25,
                "max_current_ma": 600, "load_current_ma": 150,
                "cout_uf": 0.1, "cout_min_uf": 1.0,
                "package": "SOT-23-5",
            }],
            led_circuits=[{
                "name": "D1", "vsource": 1.5, "vf": 2.2, "resistor": "100",
            }],
        )
        result = analyze(d)
        assert not result.passed
        assert len(result.errors) >= 3  # overcurrent, dropout, LED won't turn on

    def test_strict_mode(self):
        """Strict mode should fail on warnings."""
        d = Design(
            project_name="test",
            ldo_regulators=[{
                "name": "U1", "vin": 5.0, "vout": 3.3, "dropout_v": 0.25,
                "max_current_ma": 600, "load_current_ma": 100,
                "package": "SOT-23-5",
                # No enable_pin → warning
            }],
        )
        result_normal = analyze(d, strict=False)
        result_strict = analyze(d, strict=True)
        assert result_normal.passed  # warnings don't fail normal mode
        assert not result_strict.passed  # warnings fail strict mode

    def test_yaml_load(self):
        """Loading a design from YAML string should work."""
        yaml_str = """
project_name: "yaml_test"
rails: []
voltage_dividers:
  - name: "test divider"
    vin: 5.0
    r_top: "10k"
    r_bot: "10k"
    target_vout: 2.5
ldo_regulators: []
led_circuits: []
pullup_networks: []
cap_checks: []
feedback_dividers: []
custom_checks: []
"""
        d = load_design_from_string(yaml_str)
        assert d.project_name == "yaml_test"
        result = analyze(d)
        assert result.passed

    def test_output_formats(self):
        """Both text and JSON formatters should work without crashing."""
        d = Design(
            project_name="format_test",
            led_circuits=[{
                "name": "D1", "vsource": 3.3, "vf": 2.2, "resistor": "1k",
            }],
        )
        result = analyze(d)
        text = format_result_text(result, "test.yaml")
        assert "DC ANALYSIS REPORT" in text

        json_str = format_result_json(result, "test.yaml")
        import json
        data = json.loads(json_str)
        assert "passed" in data
        assert "issues" in data

    def test_custom_checks_unanalyzed(self):
        """Custom checks should be counted as unanalyzed."""
        d = Design(
            project_name="test",
            custom_checks=[{"name": "EMI filter", "notes": "manual review"}],
        )
        result = analyze(d)
        assert result.subcircuits_unanalyzed == 1
        assert result.passed  # custom checks don't cause failures

    def test_empty_design(self):
        """Empty design should pass with nothing to analyze."""
        d = Design(project_name="empty")
        result = analyze(d)
        assert result.passed
        assert result.subcircuits_analyzed == 0
