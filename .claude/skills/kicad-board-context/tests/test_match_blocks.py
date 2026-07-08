"""Tests for match_blocks.py — reviewer block recognition (ROADMAP W1c).

The board under test is built from the REAL registry bundle
(nau7802_dual_loadcell): its fragment is re-annotated (+100 refs), its nets
renamed to board-style names, and an MCU is attached to the port nets —
exactly what extract_netlist.py would produce from a board that composed the
block. Deviations are then injected per test case.
"""
import copy
import os
import re
import subprocess
import sys

import yaml

from match_blocks import (
    load_registry, match_board, match_block, parts_equal, DEFAULT_BLOCKS_DIR,
)
from verify_netlist import load_intended_netlist_from_string

BLOCK_NAME = "nau7802_dual_loadcell"
BLOCK_NETLIST = os.path.join(DEFAULT_BLOCKS_DIR, BLOCK_NAME, "netlist.yaml")

# Board-side renames: what the block's nets are called on this board.
NET_RENAME = {
    "3V3": "+3V3", "GND": "GND",
    "SDA": "I2C_SDA", "SCL": "I2C_SCL", "DRDY": "NAU_DRDY",
    "AVDD": "A_EXC", "VBG": "VBG_B",
    "N_U2_2": "NET_A2", "N_U2_3": "NET_A3",
    "N_U2_4": "NET_B4", "N_U2_5": "NET_B5",
    "LC_A_SIGN": "LCA_N", "LC_A_SIGP": "LCA_P",
    "LC_B_SIGN": "LCB_N", "LC_B_SIGP": "LCB_P",
}

_REF_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def _reref(ref):
    m = _REF_RE.match(ref)
    return f"{m.group(1)}{int(m.group(2)) + 100}"


def _board_raw():
    """The block fragment re-annotated (+100) + net renames + an MCU."""
    with open(BLOCK_NETLIST, "r", encoding="utf-8") as f:
        frag = yaml.safe_load(f)
    raw = {"project": "board_under_test", "components": {}, "nets": {},
           "no_connects": []}
    for ref, comp in frag["components"].items():
        raw["components"][_reref(ref)] = copy.deepcopy(comp)
    for net, data in frag["nets"].items():
        d = {"type": data.get("type", "signal"),
             "pins": [{"ref": _reref(p["ref"]), "pin": str(p["pin"])}
                      for p in data.get("pins", [])]}
        raw["nets"][NET_RENAME[net]] = d
    for nc in frag.get("no_connects", []):
        raw["no_connects"].append(
            {"ref": _reref(nc["ref"]), "pin": str(nc["pin"])})
    # The MCU the block hangs off (port nets + rails).
    raw["components"]["U1"] = {"part": "ESP32-S3", "pins": ["1", "2", "3", "4", "5"]}
    for pin, net in [("1", "+3V3"), ("2", "GND"), ("3", "I2C_SDA"),
                     ("4", "I2C_SCL"), ("5", "NAU_DRDY")]:
        raw["nets"][net]["pins"].append({"ref": "U1", "pin": pin})
    return raw


def _board(raw):
    return load_intended_netlist_from_string(yaml.dump(raw))


def _registry():
    return load_registry()


def _the_match(matches):
    assert len(matches) == 1, [m.block for m in matches]
    return matches[0]


# ─── Recognition ─────────────────────────────────────────────────────
def test_clean_composed_board_is_exact_match():
    matches, anchor_only = match_board(_board(_board_raw()), _registry())
    m = _the_match(matches)
    assert m.block == BLOCK_NAME
    assert m.anchor_board_ref == "U102"
    assert m.matched == m.total == 25
    assert m.quality == "exact"
    assert anchor_only == []
    # Ports resolve to the board's net names — the recognition payload.
    assert m.port_map == {"SDA": "I2C_SDA", "SCL": "I2C_SCL",
                          "DRDY": "NAU_DRDY"}
    assert m.rail_map == {"3V3": "+3V3", "GND": "GND"}
    # Every block component found its re-annotated counterpart.
    assert m.comp_map["U2"] == "U102"
    assert m.comp_map["J4"] == "J104"


def test_value_deviation_reported():
    raw = _board_raw()
    raw["components"]["C113"]["part"] = "10nF"   # block says C13 = 100nF
    matches, _ = match_board(_board(raw), _registry())
    m = _the_match(matches)
    assert m.quality == "match_with_deviations"
    dev = [d for d in m.deviations if d.kind == "value_mismatch"]
    assert len(dev) == 1
    assert dev[0].block_ref == "C13" and dev[0].board_ref == "C113"
    assert "10nF" in dev[0].message and "100nF" in dev[0].message


def test_missing_component_reported():
    raw = _board_raw()
    # Remove an anti-alias cap entirely (the canonical "filter got dropped").
    del raw["components"]["C115"]
    for net in raw["nets"].values():
        net["pins"] = [p for p in net["pins"] if p["ref"] != "C115"]
    matches, _ = match_board(_board(raw), _registry())
    m = _the_match(matches)
    assert m.quality == "partial"
    assert any(d.kind == "missing_component" and d.block_ref == "C15"
               for d in m.deviations)


def test_extra_attachment_on_internal_net_reported():
    raw = _board_raw()
    raw["components"]["C90"] = {"part": "100nF", "pins": ["1", "2"]}
    raw["nets"]["A_EXC"]["pins"].append({"ref": "C90", "pin": "1"})
    raw["nets"]["GND"]["pins"].append({"ref": "C90", "pin": "2"})
    matches, _ = match_board(_board(raw), _registry())
    m = _the_match(matches)
    extras = [d for d in m.deviations if d.kind == "extra_attachment"]
    assert any("C90" in d.message and d.net == "A_EXC" for d in extras)


def test_nc_violation_reported():
    raw = _board_raw()
    # Board wires a crystal to pin 10 — the block declares XIN/XOUT no-connect
    # (and its constraints forbid a crystal without revalidation).
    raw["components"]["Y1"] = {"part": "32.768kHz", "pins": ["1", "2"]}
    raw["nets"]["XTAL"] = {"type": "signal", "pins": [
        {"ref": "U102", "pin": "10"}, {"ref": "Y1", "pin": "1"}]}
    raw["no_connects"] = [nc for nc in raw["no_connects"]
                          if not (nc["ref"] == "U102" and nc["pin"] == "10")]
    matches, _ = match_board(_board(raw), _registry())
    m = _the_match(matches)
    assert any(d.kind == "nc_violated" and "pin 10" in d.message
               for d in m.deviations)


def test_board_without_block_reports_nothing():
    raw = {"project": "bare", "components": {
        "U1": {"part": "ESP32-S3", "pins": ["1", "2"]},
        "C1": {"part": "100nF", "pins": ["1", "2"]}},
        "nets": {
            "+3V3": {"type": "power", "pins": [
                {"ref": "U1", "pin": "1"}, {"ref": "C1", "pin": "1"}]},
            "GND": {"type": "power", "pins": [
                {"ref": "U1", "pin": "2"}, {"ref": "C1", "pin": "2"}]}},
        "no_connects": []}
    matches, anchor_only = match_board(_board(raw), _registry())
    assert matches == [] and anchor_only == []


def test_same_silicon_different_circuit_is_anchor_only():
    # A lone NAU7802 with none of the block's surrounding circuit.
    raw = {"project": "lone", "components": {
        "U9": {"part": "NAU7802",
               "pins": [str(i) for i in range(1, 17)]}},
        "nets": {"+3V3": {"type": "power",
                          "pins": [{"ref": "U9", "pin": "15"}]},
                 "GND": {"type": "power",
                         "pins": [{"ref": "U9", "pin": str(p)}
                                  for p in (7, 8, 9)]}},
        "no_connects": []}
    matches, anchor_only = match_board(_board(raw), _registry())
    assert matches == []
    assert any(a["board_ref"] == "U9" for a in anchor_only)


def test_parts_equal_fuzzy():
    assert parts_equal("NAU7802", "NAU7802SGI-REEL")
    assert parts_equal("nau7802", "NAU7802")
    assert not parts_equal("NAU7802", "ESP32-S3")
    assert not parts_equal("", "NAU7802")


# ─── Determinism + CLI ───────────────────────────────────────────────
def test_match_is_deterministic():
    raw = _board_raw()
    raw["components"]["C113"]["part"] = "10nF"
    r1, _ = match_board(_board(raw), _registry())
    r2, _ = match_board(_board(raw), _registry())
    assert [m.comp_map for m in r1] == [m.comp_map for m in r2]
    assert [[d.message for d in m.deviations] for m in r1] == \
           [[d.message for d in m.deviations] for m in r2]


def test_cli_json_smoke(tmp_path):
    board_yaml = tmp_path / "netlist.yaml"
    board_yaml.write_text(yaml.dump(_board_raw()), encoding="utf-8")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "scripts", "match_blocks.py")
    out = subprocess.run(
        [sys.executable, script, str(board_yaml), "--json"],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    import json
    data = json.loads(out.stdout)
    assert data["matches"][0]["block"] == BLOCK_NAME
    assert data["matches"][0]["port_map"]["SDA"] == "I2C_SDA"
    # Constraints ride along for Claude to judge.
    assert any("0x2A" in c for c in data["matches"][0]["constraints"])
