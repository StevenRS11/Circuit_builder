#!/usr/bin/env python3
"""Tests for the analog front-end completeness analyzer (Stage 5)."""

import sys
import os
import pytest

_script_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from analyze_analog import (
    analyze_netlist_from_string, match_recipe, load_recipes, _Index,
    format_result_text, format_result_json,
)
from verify_netlist import load_intended_netlist_from_string


# A NAU7802 block with BARE differential inputs (the canonical defect).
BARE = """
project: "bare"
components:
  U2: { part: "NAU7802SGI", pins: ["1","2","3","4","5","6","7","8","9","10","11","12","13","14","15","16"] }
  C9:  { part: "1uF",   pins: ["1","2"] }
  C13: { part: "100nF", pins: ["1","2"] }
  J4:  { part: "LoadCell_A", pins: ["1","2","3","4"] }
  J5:  { part: "LoadCell_B", pins: ["1","2","3","4"] }
nets:
  AVDD:
    type: power
    pins:
      - { ref: U2, pin: "16" }
      - { ref: U2, pin: "1" }
      - { ref: C9, pin: "1" }
      - { ref: J4, pin: "1" }
      - { ref: J5, pin: "1" }
  GND:
    type: power
    pins:
      - { ref: U2, pin: "7" }
      - { ref: U2, pin: "8" }
      - { ref: U2, pin: "9" }
      - { ref: U2, pin: "15" }
      - { ref: C9, pin: "2" }
      - { ref: C13, pin: "2" }
      - { ref: J4, pin: "4" }
      - { ref: J5, pin: "4" }
    power_symbols: ["GND"]
  VBG:
    type: signal
    pins:
      - { ref: U2, pin: "6" }
      - { ref: C13, pin: "1" }
  LC_A_SIGP: { type: signal, pins: [ { ref: J4, pin: "2" }, { ref: U2, pin: "3" } ] }
  LC_A_SIGN: { type: signal, pins: [ { ref: J4, pin: "3" }, { ref: U2, pin: "2" } ] }
  LC_B_SIGP: { type: signal, pins: [ { ref: J5, pin: "2" }, { ref: U2, pin: "5" } ] }
  LC_B_SIGN: { type: signal, pins: [ { ref: J5, pin: "3" }, { ref: U2, pin: "4" } ] }
no_connects:
  - { ref: U2, pin: "10" }
  - { ref: U2, pin: "11" }
  - { ref: U2, pin: "12" }
  - { ref: U2, pin: "13" }
  - { ref: U2, pin: "14" }
"""

# Same board with differential filter caps (CdA, CdB) added across each pair.
FILTERED = """
project: "filtered"
components:
  U2: { part: "NAU7802SGI", pins: ["1","2","3","4","5","6","7","8","9","10","11","12","13","14","15","16"] }
  C9:  { part: "1uF",   pins: ["1","2"] }
  C13: { part: "100nF", pins: ["1","2"] }
  CdA: { part: "330pF", pins: ["1","2"] }
  CdB: { part: "330pF", pins: ["1","2"] }
  J4:  { part: "LoadCell_A", pins: ["1","2","3","4"] }
  J5:  { part: "LoadCell_B", pins: ["1","2","3","4"] }
nets:
  AVDD:
    type: power
    pins:
      - { ref: U2, pin: "16" }
      - { ref: U2, pin: "1" }
      - { ref: C9, pin: "1" }
      - { ref: J4, pin: "1" }
      - { ref: J5, pin: "1" }
  GND:
    type: power
    pins:
      - { ref: U2, pin: "7" }
      - { ref: U2, pin: "8" }
      - { ref: U2, pin: "9" }
      - { ref: U2, pin: "15" }
      - { ref: C9, pin: "2" }
      - { ref: C13, pin: "2" }
      - { ref: J4, pin: "4" }
      - { ref: J5, pin: "4" }
    power_symbols: ["GND"]
  VBG:
    type: signal
    pins:
      - { ref: U2, pin: "6" }
      - { ref: C13, pin: "1" }
  LC_A_SIGP: { type: signal, class: analog_differential, pair: LC_A, polarity: P, source_z: low, pins: [ { ref: J4, pin: "2" }, { ref: U2, pin: "3" }, { ref: CdA, pin: "1" } ] }
  LC_A_SIGN: { type: signal, class: analog_differential, pair: LC_A, polarity: N, source_z: low, pins: [ { ref: J4, pin: "3" }, { ref: U2, pin: "2" }, { ref: CdA, pin: "2" } ] }
  LC_B_SIGP: { type: signal, pins: [ { ref: J5, pin: "2" }, { ref: U2, pin: "5" }, { ref: CdB, pin: "1" } ] }
  LC_B_SIGN: { type: signal, pins: [ { ref: J5, pin: "3" }, { ref: U2, pin: "4" }, { ref: CdB, pin: "2" } ] }
no_connects:
  - { ref: U2, pin: "10" }
  - { ref: U2, pin: "11" }
  - { ref: U2, pin: "12" }
  - { ref: U2, pin: "13" }
  - { ref: U2, pin: "14" }
"""


class TestRecipeMatching:
    def test_match_nau7802(self):
        recipes = load_recipes()
        key, recipe = match_recipe("NAU7802SGI", recipes)
        assert key == "NAU7802"
        assert recipe["family"] == "precision_bridge_adc"

    def test_no_match(self):
        recipes = load_recipes()
        key, recipe = match_recipe("STM32F405", recipes)
        assert key is None

    def test_recipes_file_loads(self):
        recipes = load_recipes()
        assert "NAU7802" in recipes


class TestBareInputDetection:
    def test_bare_inputs_fail(self):
        result = analyze_netlist_from_string(BARE)
        assert not result.passed
        assert len(result.errors) == 2  # both channels bare

    def test_bare_message_mentions_filter(self):
        result = analyze_netlist_from_string(BARE)
        errs = " ".join(i.message for i in result.errors)
        assert "BARE" in errs
        assert "filter" in errs.lower()

    def test_recipe_matched(self):
        result = analyze_netlist_from_string(BARE)
        infos = " ".join(i.message for i in result.infos)
        assert "NAU7802" in infos


class TestFilteredInputPasses:
    def test_filtered_inputs_no_bare_error(self):
        result = analyze_netlist_from_string(FILTERED)
        bare = [i for i in result.errors if "BARE" in i.message]
        assert bare == []

    def test_diff_cap_detected(self):
        result = analyze_netlist_from_string(FILTERED)
        msgs = " ".join(i.message for i in result.issues)
        assert "differential filter cap" in msgs


class TestDecouplingAndRatiometric:
    def test_avdd_decoupled_ok(self):
        result = analyze_netlist_from_string(BARE)
        msgs = " ".join(i.message for i in result.infos)
        assert "AVDD" in msgs and "OK" in msgs

    def test_ratiometric_confirmed(self):
        # AVDD net carries REFP (pin 1) and excitation (J4.1/J5.1) -> ratiometric OK
        result = analyze_netlist_from_string(BARE)
        msgs = " ".join(i.message for i in result.issues)
        assert "ratiometric reference OK" in msgs

    def test_missing_decoupling_warns(self):
        # Drop the VBG bypass cap -> warning about bandgap decoupling
        nl = BARE.replace('      - { ref: C13, pin: "1" }\n', "")
        result = analyze_netlist_from_string(nl)
        warns = " ".join(i.message for i in result.warnings)
        assert "VBG" in warns or "bandgap" in warns.lower()


class TestClassDrivenNoRecipe:
    def test_classified_pair_without_recipe(self):
        # A generic part with classified differential nets but no recipe.
        nl = """
project: "generic"
components:
  U9: { part: "SOMEADC", pins: ["1","2","3"] }
  J1: { part: "Conn", pins: ["1","2"] }
nets:
  AINP: { type: signal, class: analog_differential, pair: X, polarity: P, source_z: low, pins: [ { ref: U9, pin: "1" }, { ref: J1, pin: "1" } ] }
  AINN: { type: signal, class: analog_differential, pair: X, polarity: N, source_z: low, pins: [ { ref: U9, pin: "2" }, { ref: J1, pin: "2" } ] }
  GND:  { type: power, pins: [ { ref: U9, pin: "3" } ], power_symbols: ["GND"] }
"""
        result = analyze_netlist_from_string(nl)
        assert not result.passed  # bare classified pair -> error
        assert any("pair X" in i.message for i in result.errors)


class TestFormatters:
    def test_text(self):
        result = analyze_netlist_from_string(BARE)
        txt = format_result_text(result, "x.yaml")
        assert "ANALOG FRONT-END" in txt
        assert "FAIL" in txt

    def test_json(self):
        import json
        result = analyze_netlist_from_string(BARE)
        data = json.loads(format_result_json(result, "x.yaml"))
        assert data["passed"] is False
        assert data["error_count"] == 2
