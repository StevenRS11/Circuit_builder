"""Tests for embedding a real library symbol verbatim (use-as-is).

Covers the pure builder path (parse_symbol_block / add_lib_symbol_from_block) and
the engine integration (from_library symbols). Deterministic and offline: the
symbol is supplied inline via the layout's `block:` field, so no KiCad install is
touched.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from generate_kicad_sch import KicadSchematic
from generate_from_data import (
    EngineError, build_schematic, self_verify, load_layout_from_string,
)
from verify_netlist import load_intended_netlist_from_string
from cross_check_bom import load_bom_from_markdown


# A small but realistic KiCad-format symbol: a 3-pin IC "FOO" with its own
# drawing (rectangle) and real pin geometry (symbol space, y-up).
FOO_BLOCK = '''(symbol "FOO"
	(pin_names (offset 1.016))
	(in_bom yes)
	(on_board yes)
	(property "Reference" "U2" (at 0 0 0) (effects (font (size 1.27 1.27))))
	(property "Value" "FOO" (at 0 0 0) (effects (font (size 1.27 1.27))))
	(property "Footprint" "Package_SO:SOIC-8" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
	(symbol "FOO_0_1"
		(rectangle (start -5.08 5.08) (end 5.08 -5.08)
			(stroke (width 0.254) (type default)) (fill (type background)))
	)
	(symbol "FOO_1_1"
		(pin power_in line (at -7.62 2.54 0) (length 2.54)
			(name "VDD" (effects (font (size 1.27 1.27))))
			(number "1" (effects (font (size 1.27 1.27)))))
		(pin input line (at -7.62 0 0) (length 2.54)
			(name "IN" (effects (font (size 1.27 1.27))))
			(number "2" (effects (font (size 1.27 1.27)))))
		(pin output line (at 7.62 0 180) (length 2.54)
			(name "OUT" (effects (font (size 1.27 1.27))))
			(number "3" (effects (font (size 1.27 1.27)))))
	)
)'''


# ─── Pure builder path ───────────────────────────────────────────────
def test_parse_symbol_block_pins_verbatim():
    pins, gfx, meta = KicadSchematic.parse_symbol_block(FOO_BLOCK)
    by_num = {p.number: p for p in pins}
    assert set(by_num) == {"1", "2", "3"}
    assert by_num["1"].name == "VDD" and by_num["1"].pin_type == "power_in"
    # real coordinates copied straight from the symbol (symbol space)
    assert (by_num["1"].x, by_num["1"].y, by_num["1"].rotation) == (-7.62, 2.54, 0)
    assert (by_num["3"].x, by_num["3"].y, by_num["3"].rotation) == (7.62, 0.0, 180)
    assert len(gfx) == 1 and gfx[0].startswith("(rectangle")


def test_parse_symbol_block_meta():
    _pins, _gfx, meta = KicadSchematic.parse_symbol_block(FOO_BLOCK)
    assert meta["ref_prefix"] == "U"          # "U2" → stripped to prefix
    assert meta["footprint"] == "Package_SO:SOIC-8"
    assert meta["pin_names_offset"] == 1.016
    assert meta["units"] == 1


def test_add_lib_symbol_from_block_renders_real_pins():
    sch = KicadSchematic(title="t")
    sym = sch.add_lib_symbol_from_block("Custom:FOO", FOO_BLOCK)
    assert len(sym.pins) == 3
    out = sch._render_lib_symbol(sym)
    assert '(symbol "Custom:FOO"' in out
    assert "(rectangle" in out         # the real drawing is preserved
    assert out.count("(pin ") == 3
    assert '(number "3"' in out


def test_add_lib_symbol_from_block_no_pins_raises():
    sch = KicadSchematic(title="t")
    with pytest.raises(ValueError):
        sch.add_lib_symbol_from_block("Custom:EMPTY", '(symbol "EMPTY")')


# ─── Engine integration (from_library) ───────────────────────────────
NETLIST = """
project: "embed"
components:
  U1: { part: "FOO", pins: ["1", "2", "3"] }
  R1: { part: "10k", pins: ["1", "2"] }
  J1: { part: "conn", pins: ["1", "2"] }
nets:
  "+3V3":
    type: power
    pins:
      - { ref: U1, pin: "1" }
    power_symbols: ["+3V3"]
  SIG_IN:
    type: signal
    pins:
      - { ref: U1, pin: "2" }
      - { ref: J1, pin: "1" }
  OUT_NET:
    type: signal
    pins:
      - { ref: U1, pin: "3" }
      - { ref: R1, pin: "1" }
  GND:
    type: power
    pins:
      - { ref: R1, pin: "2" }
      - { ref: J1, pin: "2" }
    power_symbols: ["GND"]
"""

BOM = """
| Ref | Value | Footprint |
|-----|-------|-----------|
| U1 | FOO | Package_SO:SOIC-8 |
| R1 | 10k | Resistor_SMD:R_0805_2012Metric |
| J1 | conn | Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical |
"""


def _layout(block=FOO_BLOCK, extra_sym=""):
    # block is embedded inline so the test is offline + deterministic
    indented = "\n".join("      " + ln for ln in block.split("\n"))
    return f"""
project: "embed"
title: "embed test"
power_nets: ["+3V3"]
placements:
  U1: {{ lib_id: "Custom:FOO", x: 100, y: 100 }}
  R1: {{ lib_id: "Device:R", x: 130, y: 100 }}
  J1: {{ lib_id: "Connector_Generic:Conn_01x02", x: 160, y: 100, rotation: 270 }}
symbols:
  "Custom:FOO":
    from_library: true
    ref_prefix: U
    block: |
{indented}
{extra_sym}
"""


def test_embed_end_to_end_passes_verifiers():
    netlist = load_intended_netlist_from_string(NETLIST)
    bom = load_bom_from_markdown(BOM)
    layout = load_layout_from_string(_layout())
    sch = build_schematic(netlist, bom, layout, uuid_seed=42)
    # The embedded symbol's real pins drive the wiring; everything must verify.
    errors, _warnings = self_verify(netlist, bom, sch)
    assert errors == [], errors
    # The embedded drawing made it into the file.
    assert "Custom:FOO" in sch.lib_symbols
    assert sch.lib_symbols["Custom:FOO"].graphics_sexpr.count("rectangle") == 1


def test_embed_pinset_gate_uses_real_pins():
    # Netlist declares a pin (9) the real symbol does not have → gate must fire,
    # proving the gate checks the embedded symbol's actual pins (not side/index).
    bad = NETLIST.replace('pins: ["1", "2", "3"] }', 'pins: ["1", "2", "9"] }')
    netlist = load_intended_netlist_from_string(bad)
    bom = load_bom_from_markdown(BOM)
    layout = load_layout_from_string(_layout())
    with pytest.raises(EngineError) as ei:
        build_schematic(netlist, bom, layout, uuid_seed=42)
    assert any("pin-set" in m for m in ei.value.messages)


def test_from_library_unresolved_raises():
    # from_library with no inline block and no library that has it → clear error.
    layout_text = """
project: "embed"
title: "embed test"
power_nets: ["+3V3"]
placements:
  U1: { lib_id: "Nope:DOES_NOT_EXIST", x: 100, y: 100 }
  R1: { lib_id: "Device:R", x: 130, y: 100 }
  J1: { lib_id: "Connector_Generic:Conn_01x02", x: 160, y: 100 }
symbols:
  "Nope:DOES_NOT_EXIST":
    from_library: true
"""
    netlist = load_intended_netlist_from_string(NETLIST)
    bom = load_bom_from_markdown(BOM)
    layout = load_layout_from_string(layout_text)
    with pytest.raises(EngineError) as ei:
        build_schematic(netlist, bom, layout, uuid_seed=42)
    assert any("from_library" in m for m in ei.value.messages)
