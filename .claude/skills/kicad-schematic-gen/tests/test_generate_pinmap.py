#!/usr/bin/env python3
"""Tests for generate_pinmap.py — the firmware pinmap handoff (roadmap A2).

Fixture board is built with the real KicadSchematic builder (labels wired via
label_at_pin), saved, and re-loaded through the normal file path — so the test
exercises exactly the loader + netlist-extraction pipeline a real board uses.
"""

import os
import sys

_tests_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_tests_dir, "..", "scripts")))

import pytest

from generate_kicad_sch import KicadSchematic
from validate_kicad_sch import load_kicad_sch
from generate_pinmap import (
    build_pinmap, emit_header, emit_sketch, _gpio_of, _macro_name,
)


def _build_board(tmp_path):
    sch = KicadSchematic("PinmapTest")
    sch.add_lib_symbol_ic("Custom:ESP32TEST", pins=[
        ("1", "3V3", "power_in", "left", 0),
        ("2", "EN", "input", "left", 1),
        ("3", "IO4", "bidirectional", "right", 0),
        ("4", "IO5", "bidirectional", "right", 1),
        ("5", "IO6/ADC1_CH5", "bidirectional", "right", 2),
        ("6", "IO7", "bidirectional", "right", 3),
        ("7", "IO8", "bidirectional", "right", 4),
        ("8", "GND", "power_in", "bottom", 0),
    ])
    sch.place_component("Custom:ESP32TEST", "U1", "ESP32-S3-TEST", 100, 100,
                        footprint="Package_DFN_QFN:QFN-56")
    sch.label_at_pin("U1", "3", "I2C_SDA")
    sch.label_at_pin("U1", "4", "I2C_SCL")
    sch.label_at_pin("U1", "5", "NAU_DRDY")
    sch.label_at_pin("U1", "6", "STATUS_LED")
    sch.nc_at_pin("U1", "7")                    # unconnected GPIO
    sch.power_at_pin("U1", "1", "+3V3")
    sch.gnd_at_pin("U1", "8")
    sch.label_at_pin("U1", "2", "EN_PULLUP")    # netted, but not a GPIO name
    path = str(tmp_path / "pinmap_test.kicad_sch")
    sch.save(path)
    return path


@pytest.fixture()
def board(tmp_path):
    return _build_board(tmp_path)


class TestGpioParsing:
    def test_numeric_forms(self):
        assert _gpio_of("IO4") == 4
        assert _gpio_of("GPIO21") == 21
        assert _gpio_of("GP15") == 15
        assert _gpio_of("IO6/ADC1_CH5") == 6

    def test_symbolic_port(self):
        assert _gpio_of("PA5") == "PA5"
        assert _gpio_of("PB12/TIM1_BKIN") == "PB12"

    def test_non_gpio(self):
        for name in ("3V3", "GND", "EN", "XTAL_P", "VDDA", "~"):
            assert _gpio_of(name) is None

    def test_macro_sanitization(self):
        assert _macro_name("I2C_SDA") == "PIN_I2C_SDA"
        assert _macro_name("NAU-DRDY!") == "PIN_NAU_DRDY"
        assert _macro_name("3V3_EN") == "PIN__3V3_EN"


class TestBuildPinmap:
    def test_maps_named_gpio_nets(self, board):
        pinmap = build_pinmap(load_kicad_sch(board))
        assert pinmap["mcu_ref"] == "U1"           # auto-detected by part name
        got = {e["macro"]: e["gpio"] for e in pinmap["entries"]}
        assert got == {
            "PIN_I2C_SDA": 4,
            "PIN_I2C_SCL": 5,
            "PIN_NAU_DRDY": 6,
            "PIN_STATUS_LED": 7,
        }

    def test_skips_carry_reasons(self, board):
        pinmap = build_pinmap(load_kicad_sch(board))
        reasons = {s["pin"]: s["reason"] for s in pinmap["skipped"]}
        assert reasons["1"] == "not a GPIO"        # 3V3
        assert reasons["2"] == "not a GPIO"        # EN (netted, but not GPIO)
        assert reasons["7"] == "unconnected"       # NC'd IO8
        assert reasons["8"] == "not a GPIO"        # GND

    def test_explicit_mcu_ref_and_missing_ref(self, board):
        sch = load_kicad_sch(board)
        assert build_pinmap(sch, mcu_ref="U1")["mcu_ref"] == "U1"
        with pytest.raises(ValueError, match="no such reference"):
            build_pinmap(sch, mcu_ref="U9")


class TestEmission:
    def test_header_contents_and_determinism(self, board):
        sch = load_kicad_sch(board)
        pinmap = build_pinmap(sch)
        h1 = emit_header(pinmap, board)
        h2 = emit_header(build_pinmap(load_kicad_sch(board)), board)
        assert h1 == h2                            # byte-deterministic
        assert "#pragma once" in h1
        assert "#define PIN_NAU_DRDY" in h1
        assert "-> net NAU_DRDY" in h1
        assert "sha256:" in h1
        # audit trail: skipped pins are listed, not silently dropped
        assert '"3V3"' in h1 and "not a GPIO" in h1

    def test_sketch_uses_i2c_and_led_pins(self, board):
        pinmap = build_pinmap(load_kicad_sch(board))
        ino = emit_sketch(pinmap, board)
        assert "Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL)" in ino
        assert "pinMode(PIN_STATUS_LED, OUTPUT)" in ino
        assert '#include "board_pins.h"' in ino
