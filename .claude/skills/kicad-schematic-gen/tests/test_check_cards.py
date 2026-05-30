"""Unit tests for the fact-card cross-check (check_cards.py).

Synthetic, fully offline. A tiny one-IC + one-passive design, with the fact card
as the verified intrinsic-fact source. Covers the join (on lib_id) and every
drift check.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from check_cards import (
    check_cards, load_card_from_string, load_cards_from_dir,
    FactCard, _card_pin_index, _is_intrinsic_ic,
)
from generate_from_data import load_layout_from_string
from cross_check_bom import load_bom_from_markdown


# ─── Synthetic fixtures ──────────────────────────────────────────────
LAYOUT = """
project: "unit"
placements:
  U1: { lib_id: "Custom:FOO", x: 100, y: 100 }
  R1: { lib_id: "Device:R", x: 120, y: 100 }
symbols:
  Custom:FOO:
    pins:
      - ["1", "VDD", "power_in", "top", 0]
      - ["2", "GND", "power_in", "bottom", 0]
      - ["3", "OUT", "output", "right", 0]
"""

BOM = """
| Ref | Value | Footprint |
|-----|-------|-----------|
| U1 | FOO | Package_TO_SOT_SMD:SOT-23 |
| R1 | 10k | Resistor_SMD:R_0805_2012Metric |
"""

CARD_OK = """
mpn: "FOO"
kicad:
  lib_id: "Custom:FOO"
  footprint: "Package_TO_SOT_SMD:SOT-23"
pins:
  - { number: "1", name: "VDD", type: "power_in" }
  - { number: "2", name: "GND", type: "power_in" }
  - { number: "3", name: "OUT", type: "output" }
pinout_verified: true
"""


def _run(layout=LAYOUT, bom=BOM, cards_text=(CARD_OK,), strict=False):
    layout_obj = load_layout_from_string(layout)
    bom_obj = load_bom_from_markdown(bom)
    cards = [load_card_from_string(t) for t in cards_text]
    return check_cards(cards, layout_obj, bom_obj, strict=strict)


def _checks(result):
    return {i.check_name for i in result.issues}


# ─── Happy path ──────────────────────────────────────────────────────

def test_clean_design_passes():
    result = _run()
    assert result.passed, [i.message for i in result.errors]
    assert not result.errors


def test_passive_not_required_to_have_card():
    # R1 (Device:R) is a passive — no symbols: entry, so no ic_without_card.
    result = _run()
    assert "ic_without_card" not in _checks(result)


# ─── Pinout verification gate ────────────────────────────────────────

def test_unverified_card_errors():
    card = CARD_OK.replace("pinout_verified: true", "pinout_verified: false")
    result = _run(cards_text=(card,))
    assert not result.passed
    assert "pinout_unverified" in _checks(result)


def test_missing_pinout_verified_defaults_false():
    card = CARD_OK.replace("pinout_verified: true\n", "")
    result = _run(cards_text=(card,))
    assert "pinout_unverified" in _checks(result)


# ─── Pin drift ───────────────────────────────────────────────────────

def test_pin_name_mismatch():
    card = CARD_OK.replace('name: "OUT"', 'name: "VOUT"')
    result = _run(cards_text=(card,))
    assert not result.passed
    assert "pin_name_mismatch" in _checks(result)


def test_pin_type_mismatch():
    card = CARD_OK.replace('name: "OUT", type: "output"',
                           'name: "OUT", type: "bidirectional"')
    result = _run(cards_text=(card,))
    assert not result.passed
    assert "pin_type_mismatch" in _checks(result)


def test_pin_set_mismatch_extra_in_symbol():
    # Card drops pin 3 → symbol has an extra pin number the card lacks.
    card = CARD_OK.replace('  - { number: "3", name: "OUT", type: "output" }\n', "")
    result = _run(cards_text=(card,))
    assert not result.passed
    assert "pin_set_mismatch" in _checks(result)


def test_pin_name_case_insensitive():
    card = CARD_OK.replace('name: "VDD"', 'name: "vdd"')
    result = _run(cards_text=(card,))
    assert "pin_name_mismatch" not in _checks(result)


# ─── Footprint drift ─────────────────────────────────────────────────

def test_footprint_mismatch():
    bom = BOM.replace("Package_TO_SOT_SMD:SOT-23", "Package_SO:SOIC-8")
    result = _run(bom=bom)
    assert not result.passed
    assert "footprint_mismatch" in _checks(result)


def test_empty_bom_footprint_skipped():
    bom = """
| Ref | Value | Footprint |
|-----|-------|-----------|
| U1 | FOO |  |
| R1 | 10k | Resistor_SMD:R_0805_2012Metric |
"""
    result = _run(bom=bom)
    assert "footprint_mismatch" not in _checks(result)


# ─── Missing / extra cards ───────────────────────────────────────────

def test_ic_without_card_warns_then_errors_strict():
    result = _run(cards_text=())          # no cards at all
    assert "ic_without_card" in _checks(result)
    assert result.passed                  # warning only by default
    strict = _run(cards_text=(), strict=True)
    assert "ic_without_card" in _checks(strict)
    assert not strict.passed              # error under --strict


def test_ambiguous_card():
    other = CARD_OK.replace('mpn: "FOO"', 'mpn: "FOO-ALT"')
    result = _run(cards_text=(CARD_OK, other))
    assert "ambiguous_card" in _checks(result)


def test_card_missing_lib_id_warns():
    card = """
mpn: "FOO"
kicad:
  footprint: "Package_TO_SOT_SMD:SOT-23"
pins:
  - { number: "1", name: "VDD", type: "power_in" }
pinout_verified: true
"""
    result = _run(cards_text=(card,))
    assert "card_missing_lib_id" in _checks(result)


def test_unused_card_is_info():
    extra = CARD_OK.replace('lib_id: "Custom:FOO"', 'lib_id: "Custom:BAR"') \
                   .replace('mpn: "FOO"', 'mpn: "BAR"')
    result = _run(cards_text=(CARD_OK, extra))
    infos = {i.check_name for i in result.infos}
    assert "unused_card" in infos
    assert result.passed                  # info never fails


# ─── Helpers ─────────────────────────────────────────────────────────

def test_is_intrinsic_ic():
    assert _is_intrinsic_ic("Custom:NAU7802")
    assert _is_intrinsic_ic("Regulator_Linear:AP2112K-3.3")
    assert not _is_intrinsic_ic("Device:R")
    assert not _is_intrinsic_ic("Device:C")
    assert not _is_intrinsic_ic("Connector_Generic:Conn_01x04")


def test_card_pin_index_tolerates_list_form():
    card = FactCard(mpn="X", pins=[["1", "A", "input"], {"number": "2", "name": "B", "type": "output"}])
    idx = _card_pin_index(card)
    assert idx["1"] == {"name": "A", "type": "input"}
    assert idx["2"] == {"name": "B", "type": "output"}


def test_load_cards_from_dir(tmp_path):
    (tmp_path / "FOO.facts.yaml").write_text(CARD_OK, encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
    cards = load_cards_from_dir(str(tmp_path))
    assert len(cards) == 1
    assert cards[0].mpn == "FOO"
    assert cards[0].lib_id == "Custom:FOO"
    assert cards[0].pinout_verified is True
