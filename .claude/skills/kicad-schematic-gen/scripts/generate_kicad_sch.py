#!/usr/bin/env python3
"""
KiCad Schematic Generator — generates valid .kicad_sch files from Python.

No KiCad installation required. Outputs S-expression files compatible with KiCad 7/8.

Usage:
    from generate_kicad_sch import KicadSchematic
    
    sch = KicadSchematic("My Board")
    sch.add_lib_symbol_resistor()
    sch.add_lib_symbol_capacitor()
    sch.place_component("Device:R", "R1", "10k", x=100, y=100,
                        footprint="Resistor_SMD:R_0603_1608Metric")
    sch.add_wire(100, 94.92, 100, 80)
    sch.add_label("VCC", 100, 80)
    sch.save("output.kicad_sch")
"""

import uuid
import math
import re
import random
from dataclasses import dataclass, field
from typing import Optional


# Module-level RNG for UUID generation. When None (the default), _uuid() returns
# real random uuid4 values. When seeded via seed_uuids(seed), _uuid() emits
# deterministic uuid4-shaped values so the same inputs produce byte-stable output
# — required for golden-file snapshot tests. Global state is acceptable here: the
# generator is single-threaded, and the seed is scoped by callers (set then reset).
_uuid_rng: Optional[random.Random] = None


def seed_uuids(seed):
    """Seed UUID generation for reproducible output. Pass None to restore random."""
    global _uuid_rng
    _uuid_rng = random.Random(seed) if seed is not None else None


def _uuid():
    if _uuid_rng is None:
        return str(uuid.uuid4())
    b = bytearray(_uuid_rng.getrandbits(8) for _ in range(16))
    b[6] = (b[6] & 0x0F) | 0x40  # version 4
    b[8] = (b[8] & 0x3F) | 0x80  # variant RFC 4122
    return str(uuid.UUID(bytes=bytes(b)))


def snap_to_grid(val, grid=1.27):
    """Snap a value to the nearest grid point."""
    return round(round(val / grid) * grid, 4)


def fmt(val):
    """Format a float for KiCad output — remove trailing zeros but keep precision."""
    if val == int(val):
        return str(int(val))
    return f"{val:.4f}".rstrip('0').rstrip('.')


@dataclass
class Pin:
    number: str
    name: str
    pin_type: str  # passive, input, output, power_in, power_out, etc.
    x: float = 0
    y: float = 0
    length: float = 2.54
    rotation: int = 0  # degrees: 0=right, 90=up, 180=left, 270=down


@dataclass
class LibSymbol:
    lib_id: str  # e.g. "Device:R"
    properties: dict = field(default_factory=dict)
    pins: list = field(default_factory=list)
    graphics_sexpr: str = ""
    is_power: bool = False
    pin_names_offset: float = 1.016
    pin_names_hide: bool = False
    pin_numbers_hide: bool = False


@dataclass
class PlacedComponent:
    lib_id: str
    reference: str
    value: str
    x: float
    y: float
    rotation: float = 0
    footprint: str = ""
    datasheet: str = "~"
    unit: int = 1
    in_bom: bool = True
    on_board: bool = True
    dnp: bool = False
    extra_properties: dict = field(default_factory=dict)
    pin_uuids: dict = field(default_factory=dict)  # pin_number -> uuid


@dataclass
class Wire:
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass 
class Label:
    text: str
    x: float
    y: float
    rotation: float = 0


@dataclass
class GlobalLabel:
    text: str
    x: float
    y: float
    shape: str = "bidirectional"
    rotation: float = 0


@dataclass
class HierarchicalLabel:
    """A hierarchical label — a port of a child sheet. When the sheet is
    placed on a parent schematic, each hierarchical label becomes a sheet pin
    on the sheet symbol's border. The interface mechanism of the block
    library (ROADMAP W1)."""
    text: str
    x: float
    y: float
    shape: str = "bidirectional"  # input | output | bidirectional | tri_state | passive
    rotation: float = 0


@dataclass
class Junction:
    x: float
    y: float


@dataclass
class NoConnect:
    x: float
    y: float


@dataclass
class SheetPin:
    """A pin on a hierarchical sheet symbol — the parent-side end of a child
    sheet's hierarchical label of the same name."""
    name: str
    shape: str = "bidirectional"  # input | output | bidirectional | tri_state | passive
    x: float = 0.0                # absolute schematic coordinates (on the sheet border)
    y: float = 0.0
    rotation: float = 0           # 180 = left edge, 0 = right edge


@dataclass
class Sheet:
    """A hierarchical sheet symbol placed on a parent schematic (ROADMAP W1b).

    ``name`` is the instance name (Sheetname property), ``filename`` the child
    .kicad_sch (Sheetfile property, relative to the parent's directory). The
    loader populates ``child`` with the parsed child KicadSchematic; builder-
    created sheets leave it None until attached."""
    name: str
    filename: str
    x: float
    y: float
    width: float
    height: float
    pins: list = field(default_factory=list)   # list of SheetPin
    uuid: str = ""
    child: object = None                       # loader: child KicadSchematic


class KicadSchematic:
    """Builder for .kicad_sch files."""
    
    def __init__(self, title="Untitled", date=None, rev="1.0", uuid_seed=None):
        if uuid_seed is not None:
            seed_uuids(uuid_seed)
        self.title = title
        self.date = date or "2026-03-14"
        self.rev = rev
        self.root_uuid = _uuid()
        self.lib_symbols: dict[str, LibSymbol] = {}
        self.components: list[PlacedComponent] = []
        self.wires: list[Wire] = []
        self.labels: list[Label] = []
        self.global_labels: list[GlobalLabel] = []
        self.hierarchical_labels: list[HierarchicalLabel] = []
        self.junctions: list[Junction] = []
        self.no_connects: list[NoConnect] = []
        self.sheets: list[Sheet] = []
        self._pwr_counter = 0
        self._ref_counters: dict[str, int] = {}   # prefix -> highest assigned number
        self._occupied_rects: list[tuple[float, float, float, float]] = []  # (x_min, y_min, x_max, y_max)
        self._label_rects: list[tuple[float, float, float, float]] = []  # net label bboxes only (for label-vs-label collision)
        self._label_positions: dict[str, tuple[float, float, float, float]] = {}  # ref -> (ref_x, ref_y, val_x, val_y)
        self._pin_keepout_zones: dict[str, list[tuple[float, float, float, float]]] = {}  # ref -> list of (x_min, y_min, x_max, y_max)
        self._placement_groups: dict[str, list[str]] = {}  # group_id -> list of component references
    
    # ─── Predefined symbol helpers ───────────────────────────────────
    
    def add_lib_symbol_resistor(self):
        """Add the Device:R symbol definition."""
        sym = LibSymbol(
            lib_id="Device:R",
            properties={"Reference": "R", "Value": "R", "Footprint": "", "Datasheet": "~"},
            pin_numbers_hide=True,
            pin_names_hide=True,
            pin_names_offset=0,
            pins=[
                Pin("1", "~", "passive", x=0, y=3.81, length=1.27, rotation=270),
                Pin("2", "~", "passive", x=0, y=-3.81, length=1.27, rotation=90),
            ],
            graphics_sexpr="""      (symbol "R_0_1"
        (rectangle
          (start -1.016 -2.54)
          (end 1.016 2.54)
          (stroke (width 0.254) (type default))
          (fill (type none))
        )
      )""",
        )
        self.lib_symbols["Device:R"] = sym
    
    def add_lib_symbol_capacitor(self):
        """Add the Device:C symbol definition."""
        sym = LibSymbol(
            lib_id="Device:C",
            properties={"Reference": "C", "Value": "C", "Footprint": "", "Datasheet": "~"},
            pin_numbers_hide=True,
            pin_names_hide=True,
            pin_names_offset=0,
            pins=[
                Pin("1", "~", "passive", x=0, y=2.54, length=1.27, rotation=270),
                Pin("2", "~", "passive", x=0, y=-2.54, length=1.27, rotation=90),
            ],
            graphics_sexpr="""      (symbol "C_0_1"
        (polyline
          (pts (xy -1.524 -0.508) (xy 1.524 -0.508))
          (stroke (width 0.3048) (type default))
          (fill (type none))
        )
        (polyline
          (pts (xy -1.524 0.508) (xy 1.524 0.508))
          (stroke (width 0.3048) (type default))
          (fill (type none))
        )
      )""",
        )
        self.lib_symbols["Device:C"] = sym

    def add_lib_symbol_capacitor_polarized(self):
        """Add the Device:C_Polarized symbol definition."""
        sym = LibSymbol(
            lib_id="Device:C_Polarized",
            properties={"Reference": "C", "Value": "C_Polarized", "Footprint": "", "Datasheet": "~"},
            pin_numbers_hide=True,
            pin_names_hide=True,
            pin_names_offset=0,
            pins=[
                Pin("1", "+", "passive", x=0, y=2.54, length=1.27, rotation=270),
                Pin("2", "-", "passive", x=0, y=-2.54, length=1.27, rotation=90),
            ],
            graphics_sexpr="""      (symbol "C_Polarized_0_1"
        (polyline
          (pts (xy -1.524 -0.508) (xy 1.524 -0.508))
          (stroke (width 0.3048) (type default))
          (fill (type none))
        )
        (polyline
          (pts (xy -1.524 0.508) (xy 1.524 0.508))
          (stroke (width 0.3048) (type default))
          (fill (type none))
        )
        (polyline
          (pts (xy -0.508 1.27) (xy 0.508 1.27))
          (stroke (width 0.254) (type default))
          (fill (type none))
        )
        (polyline
          (pts (xy 0 0.762) (xy 0 1.778))
          (stroke (width 0.254) (type default))
          (fill (type none))
        )
      )""",
        )
        self.lib_symbols["Device:C_Polarized"] = sym

    def add_lib_symbol_led(self):
        """Add the Device:LED symbol definition."""
        sym = LibSymbol(
            lib_id="Device:LED",
            properties={"Reference": "D", "Value": "LED", "Footprint": "", "Datasheet": "~"},
            pin_numbers_hide=True,
            pin_names_hide=True,
            pin_names_offset=0,
            pins=[
                Pin("1", "K", "passive", x=-2.54, y=0, length=1.27, rotation=0),
                Pin("2", "A", "passive", x=2.54, y=0, length=1.27, rotation=180),
            ],
            graphics_sexpr="""      (symbol "LED_0_1"
        (polyline
          (pts (xy -1.27 -1.27) (xy -1.27 1.27))
          (stroke (width 0.254) (type default))
          (fill (type none))
        )
        (polyline
          (pts (xy -1.27 0) (xy 1.27 0))
          (stroke (width 0) (type default))
          (fill (type none))
        )
        (polyline
          (pts (xy 1.27 -1.27) (xy 1.27 1.27) (xy -1.27 0) (xy 1.27 -1.27))
          (stroke (width 0.254) (type default))
          (fill (type outline))
        )
      )""",
        )
        self.lib_symbols["Device:LED"] = sym

    def add_lib_symbol_inductor(self):
        """Add the Device:L symbol definition."""
        sym = LibSymbol(
            lib_id="Device:L",
            properties={"Reference": "L", "Value": "L", "Footprint": "", "Datasheet": "~"},
            pin_numbers_hide=True,
            pin_names_hide=True,
            pin_names_offset=0,
            pins=[
                Pin("1", "~", "passive", x=0, y=2.54, length=1.27, rotation=270),
                Pin("2", "~", "passive", x=0, y=-2.54, length=1.27, rotation=90),
            ],
            graphics_sexpr="""      (symbol "L_0_1"
        (arc (start 0 -2.54) (mid 0.6323 -1.905) (end 0 -1.27)
          (stroke (width 0) (type default)) (fill (type none)))
        (arc (start 0 -1.27) (mid 0.6323 -0.635) (end 0 0)
          (stroke (width 0) (type default)) (fill (type none)))
        (arc (start 0 0) (mid 0.6323 0.635) (end 0 1.27)
          (stroke (width 0) (type default)) (fill (type none)))
        (arc (start 0 1.27) (mid 0.6323 1.905) (end 0 2.54)
          (stroke (width 0) (type default)) (fill (type none)))
      )""",
        )
        self.lib_symbols["Device:L"] = sym

    def add_lib_symbol_diode(self):
        """Add the Device:D symbol definition."""
        sym = LibSymbol(
            lib_id="Device:D",
            properties={"Reference": "D", "Value": "D", "Footprint": "", "Datasheet": "~"},
            pin_numbers_hide=True,
            pin_names_hide=True,
            pin_names_offset=0,
            pins=[
                Pin("1", "K", "passive", x=-2.54, y=0, length=1.27, rotation=0),
                Pin("2", "A", "passive", x=2.54, y=0, length=1.27, rotation=180),
            ],
            graphics_sexpr="""      (symbol "D_0_1"
        (polyline
          (pts (xy -1.27 1.27) (xy -1.27 -1.27))
          (stroke (width 0.254) (type default))
          (fill (type none))
        )
        (polyline
          (pts (xy 1.27 -1.27) (xy 1.27 1.27) (xy -1.27 0) (xy 1.27 -1.27))
          (stroke (width 0.254) (type default))
          (fill (type outline))
        )
      )""",
        )
        self.lib_symbols["Device:D"] = sym

    def add_lib_symbol_mosfet_n(self):
        """Add the Device:Q_NMOS_GSD symbol definition (N-channel MOSFET).

        Pins: 1=G (gate, left), 2=S (source, bottom), 3=D (drain, top).
        """
        sym = LibSymbol(
            lib_id="Device:Q_NMOS_GSD",
            properties={"Reference": "Q", "Value": "Q_NMOS_GSD", "Footprint": "", "Datasheet": "~"},
            pin_names_offset=0,
            pin_names_hide=False,
            pin_numbers_hide=False,
            pins=[
                Pin("1", "G", "input", x=-2.54, y=0, length=1.27, rotation=0),
                Pin("2", "S", "passive", x=0, y=-2.54, length=1.27, rotation=90),
                Pin("3", "D", "passive", x=0, y=2.54, length=1.27, rotation=270),
            ],
            graphics_sexpr="""      (symbol "Q_NMOS_GSD_0_1"
        (circle (center 0 0) (radius 2.54)
          (stroke (width 0.254) (type default)) (fill (type none)))
        (polyline (pts (xy -1.27 -1.524) (xy -1.27 1.524))
          (stroke (width 0.254) (type default)) (fill (type none)))
        (polyline (pts (xy -0.508 -0.762) (xy -0.508 -1.524) (xy 0.762 -1.524))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy -0.508 0) (xy -0.508 0.762) (xy 0.762 0.762))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy -0.508 0.762) (xy -0.508 1.524) (xy 0.762 1.524))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy 0.762 -1.524) (xy 0.762 -2.54))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy 0.762 1.524) (xy 0.762 2.54))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy 0.762 -0.762) (xy 0.762 0.762))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy 0.254 0) (xy 0.762 0.381) (xy 0.762 -0.381) (xy 0.254 0))
          (stroke (width 0) (type default)) (fill (type outline)))
      )""",
        )
        self.lib_symbols["Device:Q_NMOS_GSD"] = sym

    def add_lib_symbol_mosfet_p(self):
        """Add the Device:Q_PMOS_GSD symbol definition (P-channel MOSFET).

        Pins: 1=G (gate, left), 2=S (source, top), 3=D (drain, bottom).
        Note: S and D are swapped vs N-channel (source on top for P-channel convention).
        """
        sym = LibSymbol(
            lib_id="Device:Q_PMOS_GSD",
            properties={"Reference": "Q", "Value": "Q_PMOS_GSD", "Footprint": "", "Datasheet": "~"},
            pin_names_offset=0,
            pin_names_hide=False,
            pin_numbers_hide=False,
            pins=[
                Pin("1", "G", "input", x=-2.54, y=0, length=1.27, rotation=0),
                Pin("2", "S", "passive", x=0, y=2.54, length=1.27, rotation=270),
                Pin("3", "D", "passive", x=0, y=-2.54, length=1.27, rotation=90),
            ],
            graphics_sexpr="""      (symbol "Q_PMOS_GSD_0_1"
        (circle (center 0 0) (radius 2.54)
          (stroke (width 0.254) (type default)) (fill (type none)))
        (polyline (pts (xy -1.27 -1.524) (xy -1.27 1.524))
          (stroke (width 0.254) (type default)) (fill (type none)))
        (polyline (pts (xy -0.508 -0.762) (xy -0.508 -1.524) (xy 0.762 -1.524))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy -0.508 0) (xy -0.508 0.762) (xy 0.762 0.762))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy -0.508 0.762) (xy -0.508 1.524) (xy 0.762 1.524))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy 0.762 -1.524) (xy 0.762 -2.54))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy 0.762 1.524) (xy 0.762 2.54))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy 0.762 -0.762) (xy 0.762 0.762))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy 0.254 0) (xy 0.762 -0.381) (xy 0.762 0.381) (xy 0.254 0))
          (stroke (width 0) (type default)) (fill (type outline)))
      )""",
        )
        self.lib_symbols["Device:Q_PMOS_GSD"] = sym

    def add_lib_symbol_bjt_npn(self):
        """Add the Device:Q_NPN_BCE symbol definition (NPN BJT).

        Pins: 1=B (base, left), 2=C (collector, top), 3=E (emitter, bottom).
        """
        sym = LibSymbol(
            lib_id="Device:Q_NPN_BCE",
            properties={"Reference": "Q", "Value": "Q_NPN_BCE", "Footprint": "", "Datasheet": "~"},
            pin_names_offset=0,
            pin_names_hide=False,
            pin_numbers_hide=False,
            pins=[
                Pin("1", "B", "input", x=-2.54, y=0, length=1.27, rotation=0),
                Pin("2", "C", "passive", x=0, y=2.54, length=1.27, rotation=270),
                Pin("3", "E", "passive", x=0, y=-2.54, length=1.27, rotation=90),
            ],
            graphics_sexpr="""      (symbol "Q_NPN_BCE_0_1"
        (circle (center 0 0) (radius 2.54)
          (stroke (width 0.254) (type default)) (fill (type none)))
        (polyline (pts (xy -1.27 -0.762) (xy -1.27 0.762))
          (stroke (width 0.254) (type default)) (fill (type none)))
        (polyline (pts (xy -1.27 0) (xy 0.762 1.524))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy 0.762 1.524) (xy 0.762 2.54))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy -1.27 0) (xy 0.762 -1.524))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy 0.762 -1.524) (xy 0.762 -2.54))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy 0.254 -1.016) (xy 0.762 -1.524) (xy 0.381 -0.635) (xy 0.254 -1.016))
          (stroke (width 0) (type default)) (fill (type outline)))
      )""",
        )
        self.lib_symbols["Device:Q_NPN_BCE"] = sym

    def add_lib_symbol_bjt_pnp(self):
        """Add the Device:Q_PNP_BCE symbol definition (PNP BJT).

        Pins: 1=B (base, left), 2=C (collector, bottom), 3=E (emitter, top).
        Note: C and E are swapped vs NPN (collector on bottom for PNP convention).
        """
        sym = LibSymbol(
            lib_id="Device:Q_PNP_BCE",
            properties={"Reference": "Q", "Value": "Q_PNP_BCE", "Footprint": "", "Datasheet": "~"},
            pin_names_offset=0,
            pin_names_hide=False,
            pin_numbers_hide=False,
            pins=[
                Pin("1", "B", "input", x=-2.54, y=0, length=1.27, rotation=0),
                Pin("2", "C", "passive", x=0, y=-2.54, length=1.27, rotation=90),
                Pin("3", "E", "passive", x=0, y=2.54, length=1.27, rotation=270),
            ],
            graphics_sexpr="""      (symbol "Q_PNP_BCE_0_1"
        (circle (center 0 0) (radius 2.54)
          (stroke (width 0.254) (type default)) (fill (type none)))
        (polyline (pts (xy -1.27 -0.762) (xy -1.27 0.762))
          (stroke (width 0.254) (type default)) (fill (type none)))
        (polyline (pts (xy -1.27 0) (xy 0.762 1.524))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy 0.762 1.524) (xy 0.762 2.54))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy -1.27 0) (xy 0.762 -1.524))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy 0.762 -1.524) (xy 0.762 -2.54))
          (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy -0.762 0.508) (xy -1.27 0) (xy -0.381 -0.127) (xy -0.762 0.508))
          (stroke (width 0) (type default)) (fill (type outline)))
      )""",
        )
        self.lib_symbols["Device:Q_PNP_BCE"] = sym

    def add_lib_symbol_crystal(self):
        """Add the Device:Crystal symbol definition (2-pin crystal).

        Pins: 1, 2 (both passive, horizontal like a capacitor). Prefix "Y".
        """
        sym = LibSymbol(
            lib_id="Device:Crystal",
            properties={"Reference": "Y", "Value": "Crystal", "Footprint": "", "Datasheet": "~"},
            pin_numbers_hide=True,
            pin_names_hide=True,
            pin_names_offset=0,
            pins=[
                Pin("1", "~", "passive", x=-2.54, y=0, length=1.27, rotation=0),
                Pin("2", "~", "passive", x=2.54, y=0, length=1.27, rotation=180),
            ],
            graphics_sexpr="""      (symbol "Crystal_0_1"
        (polyline (pts (xy -0.635 -1.016) (xy -0.635 1.016))
          (stroke (width 0.254) (type default)) (fill (type none)))
        (polyline (pts (xy 0.635 -1.016) (xy 0.635 1.016))
          (stroke (width 0.254) (type default)) (fill (type none)))
        (rectangle (start -0.254 -1.27) (end 0.254 1.27)
          (stroke (width 0.254) (type default)) (fill (type none)))
      )""",
        )
        self.lib_symbols["Device:Crystal"] = sym

    def add_lib_symbol_crystal_4pin(self):
        """Add the Device:Crystal_GND24 symbol definition (4-pin crystal with case ground).

        Pins: 1 (in, left), 3 (out, right), 2 (GND, bottom), 4 (GND, bottom).
        Standard 4-pin crystal footprint with ground on pins 2 and 4. Prefix "Y".
        """
        sym = LibSymbol(
            lib_id="Device:Crystal_GND24",
            properties={"Reference": "Y", "Value": "Crystal_GND24", "Footprint": "", "Datasheet": "~"},
            pin_names_offset=0,
            pin_names_hide=False,
            pin_numbers_hide=False,
            pins=[
                Pin("1", "IN", "passive", x=-3.81, y=0, length=1.27, rotation=0),
                Pin("3", "OUT", "passive", x=3.81, y=0, length=1.27, rotation=180),
                Pin("2", "GND", "passive", x=-1.27, y=-2.54, length=1.27, rotation=90),
                Pin("4", "GND", "passive", x=1.27, y=-2.54, length=1.27, rotation=90),
            ],
            graphics_sexpr="""      (symbol "Crystal_GND24_0_1"
        (polyline (pts (xy -1.016 -1.016) (xy -1.016 1.016))
          (stroke (width 0.254) (type default)) (fill (type none)))
        (polyline (pts (xy 1.016 -1.016) (xy 1.016 1.016))
          (stroke (width 0.254) (type default)) (fill (type none)))
        (rectangle (start -0.635 -1.27) (end 0.635 1.27)
          (stroke (width 0.254) (type default)) (fill (type none)))
      )""",
        )
        self.lib_symbols["Device:Crystal_GND24"] = sym

    def add_lib_symbol_power(self, name, pin_name=None, graphics=None):
        """Add a power symbol (GND, VCC, +3V3, +5V, etc.).
        
        Args:
            name: Power symbol name (e.g., "GND", "VCC", "+3V3", "+5V")
            pin_name: Pin name (defaults to symbol name)
            graphics: Custom graphics S-expression (uses defaults for GND/VCC/+3V3/+5V)
        """
        if pin_name is None:
            pin_name = name
        
        lib_id = f"power:{name}"
        
        # Default graphics for common power symbols
        if graphics is None:
            if name == "GND":
                graphics = f"""      (symbol "{name}_0_1"
        (polyline
          (pts (xy 0 0) (xy 0 -1.27) (xy 1.27 -1.27) (xy 0 -2.54) (xy -1.27 -1.27) (xy 0 -1.27))
          (stroke (width 0) (type default))
          (fill (type none))
        )
      )"""
                pin_rotation = 90  # points up
                pin_y = 0
            else:
                # VCC, +3V3, +5V, etc. — bar with line
                graphics = f"""      (symbol "{name}_0_1"
        (polyline
          (pts (xy -0.762 1.27) (xy 0 2.54) (xy 0.762 1.27))
          (stroke (width 0) (type default))
          (fill (type none))
        )
        (polyline
          (pts (xy 0 0) (xy 0 1.27))
          (stroke (width 0) (type default))
          (fill (type none))
        )
      )"""
                pin_rotation = 270  # points down
                pin_y = 0
        else:
            pin_rotation = 90 if name == "GND" else 270
            pin_y = 0
        
        sym = LibSymbol(
            lib_id=lib_id,
            is_power=True,
            properties={
                "Reference": "#PWR",
                "Value": name,
                "Footprint": "",
                "Datasheet": "",
            },
            pin_names_offset=0,
            pin_names_hide=False,
            pin_numbers_hide=True,
            pins=[
                Pin("1", pin_name, "power_in", x=0, y=pin_y, length=0, rotation=pin_rotation),
            ],
            graphics_sexpr=graphics,
        )
        self.lib_symbols[lib_id] = sym

    def add_lib_symbol_connector(self, num_pins, lib_id=None):
        """Add a generic connector symbol.
        
        Args:
            num_pins: Number of pins (1-40)
            lib_id: Override library ID (default: Connector_Generic:Conn_01xNN)
        """
        if lib_id is None:
            lib_id = f"Connector_Generic:Conn_01x{num_pins:02d}"
        
        pins = []
        half_height = (num_pins - 1) * 2.54 / 2
        for i in range(num_pins):
            pin_y = half_height - i * 2.54
            pins.append(Pin(
                str(i + 1), f"Pin_{i+1}", "passive",
                x=-3.81, y=pin_y, length=2.54, rotation=0
            ))
        
        # Build graphics — rectangle
        rect_top = half_height + 1.27
        rect_bottom = -half_height - 1.27
        graphics = f"""      (symbol "{lib_id.split(':')[1]}_1_1"
        (rectangle
          (start -1.27 {fmt(rect_top)})
          (end 1.27 {fmt(rect_bottom)})
          (stroke (width 0.254) (type default))
          (fill (type background))
        )
      )"""
        
        sym = LibSymbol(
            lib_id=lib_id,
            properties={"Reference": "J", "Value": lib_id.split(':')[1], "Footprint": "", "Datasheet": "~"},
            pins=pins,
            pin_names_offset=1.016,
            graphics_sexpr=graphics,
        )
        self.lib_symbols[lib_id] = sym

    def add_lib_symbol_ic(self, lib_id, pins, ref_prefix="U", value=None,
                          width=10.16, properties=None):
        """Add a custom IC symbol definition.
        
        Args:
            lib_id: Library identifier (e.g., "custom:AP2112K")
            pins: List of tuples: (pin_number, pin_name, pin_type, side, position_index)
                  side: "left", "right", "top", "bottom"
                  position_index: 0-based index from top/left of that side
            ref_prefix: Reference prefix (default "U")
            value: Default value text
            width: Width of the IC rectangle in mm (default 10.16 = 4 grid units)
            properties: Additional properties dict
        """
        if value is None:
            value = lib_id.split(':')[-1]
        
        # Organize pins by side
        sides = {"left": [], "right": [], "top": [], "bottom": []}
        for pin_num, pin_name, pin_type, side, idx in pins:
            sides[side].append((pin_num, pin_name, pin_type, idx))
        
        # Sort each side by index
        for side in sides:
            sides[side].sort(key=lambda p: p[3])
        
        # Calculate dimensions
        max_pins_vertical = max(len(sides["left"]), len(sides["right"]), 1)
        max_pins_horizontal = max(len(sides["top"]), len(sides["bottom"]), 0)
        
        half_w = width / 2
        height = max(max_pins_vertical * 2.54 + 2.54, 5.08)
        half_h = height / 2
        
        pin_objects = []
        
        # Left side pins (face right, rotation=0)
        for pin_num, pin_name, pin_type, idx in sides["left"]:
            py = half_h - 2.54 - idx * 2.54
            pin_objects.append(Pin(pin_num, pin_name, pin_type,
                                  x=-half_w - 2.54, y=py, length=2.54, rotation=0))
        
        # Right side pins (face left, rotation=180)
        for pin_num, pin_name, pin_type, idx in sides["right"]:
            py = half_h - 2.54 - idx * 2.54
            pin_objects.append(Pin(pin_num, pin_name, pin_type,
                                  x=half_w + 2.54, y=py, length=2.54, rotation=180))
        
        # Top side pins (face down, rotation=270)
        for pin_num, pin_name, pin_type, idx in sides["top"]:
            px = -half_w + 2.54 + idx * 2.54
            pin_objects.append(Pin(pin_num, pin_name, pin_type,
                                  x=px, y=half_h + 2.54, length=2.54, rotation=270))
        
        # Bottom side pins (face up, rotation=90)
        for pin_num, pin_name, pin_type, idx in sides["bottom"]:
            px = -half_w + 2.54 + idx * 2.54
            pin_objects.append(Pin(pin_num, pin_name, pin_type,
                                  x=px, y=-half_h - 2.54, length=2.54, rotation=90))
        
        symbol_name = lib_id.split(':')[-1]
        graphics = f"""      (symbol "{symbol_name}_0_1"
        (rectangle
          (start {fmt(-half_w)} {fmt(half_h)})
          (end {fmt(half_w)} {fmt(-half_h)})
          (stroke (width 0.254) (type default))
          (fill (type background))
        )
      )"""
        
        props = {"Reference": ref_prefix, "Value": value, "Footprint": "", "Datasheet": "~"}
        if properties:
            props.update(properties)
        
        sym = LibSymbol(
            lib_id=lib_id,
            properties=props,
            pins=pin_objects,
            pin_names_offset=1.016,
            graphics_sexpr=graphics,
        )
        self.lib_symbols[lib_id] = sym

    # ─── Embed a real library symbol verbatim (use as-is) ────────────

    @staticmethod
    def _balanced_blocks(text, keyword):
        """Yield each balanced ``(keyword ...)`` group in *text*.

        String- and depth-aware so quoted parens and nesting don't confuse the
        scan. ``keyword`` is matched on a word boundary, so ``pin`` does not match
        ``pin_names`` / ``pin_numbers``.
        """
        out = []
        for m in re.finditer(r'\(' + re.escape(keyword) + r'\b', text):
            i = m.start()
            depth = 0
            in_str = esc = False
            while i < len(text):
                c = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif c == '\\':
                        esc = True
                    elif c == '"':
                        in_str = False
                else:
                    if c == '"':
                        in_str = True
                    elif c == '(':
                        depth += 1
                    elif c == ')':
                        depth -= 1
                        if depth == 0:
                            out.append(text[m.start():i + 1])
                            break
                i += 1
        return out

    @classmethod
    def parse_symbol_block(cls, block):
        """Parse a top-level ``.kicad_sym`` symbol block.

        Returns (pins, graphics, meta):
          * pins — list[Pin] copied **verbatim** from the symbol (number/name/type
            and the real symbol-space at/length/angle). These are authoritative.
          * graphics — list[str] of graphic-primitive s-exprs (rectangle, polyline,
            circle, arc, bezier, text) defining the symbol's drawn shape.
          * meta — dict: name, ref_prefix, value, footprint, pin_names_offset,
            pin_names_hide, pin_numbers_hide, units (distinct unit count).
        """
        m = re.search(r'\(symbol\s+"([^"]+)"', block)
        name = m.group(1) if m else ""

        pins = []
        for pb in cls._balanced_blocks(block, "pin"):
            tm = re.match(r'\(pin\s+(\S+)\s+(\S+)', pb)
            at = re.search(r'\(at\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\)', pb)
            ln = re.search(r'\(length\s+(-?[\d.]+)\)', pb)
            nm = re.search(r'\(name\s+"((?:[^"\\]|\\.)*)"', pb)
            num = re.search(r'\(number\s+"((?:[^"\\]|\\.)*)"', pb)
            if not (tm and at and num):
                continue
            pins.append(Pin(
                number=num.group(1),
                name=nm.group(1) if nm else "~",
                pin_type=tm.group(1),
                x=float(at.group(1)),
                y=float(at.group(2)),
                length=float(ln.group(1)) if ln else 2.54,
                rotation=int(round(float(at.group(3)))),
            ))

        graphics = []
        for kw in ("arc", "circle", "rectangle", "polyline", "bezier", "text"):
            graphics.extend(cls._balanced_blocks(block, kw))

        def _prop(key):
            mm = re.search(r'\(property\s+"' + key + r'"\s+"((?:[^"\\]|\\.)*)"', block)
            return mm.group(1) if mm else None

        pn = cls._balanced_blocks(block, "pin_names")
        pin_names_offset = 1.016
        pin_names_hide = False
        if pn:
            om = re.search(r'\(offset\s+(-?[\d.]+)\)', pn[0])
            if om:
                pin_names_offset = float(om.group(1))
            pin_names_hide = bool(re.search(r'\bhide\b', pn[0]) or
                                  re.search(r'\(hide\s+yes\)', pn[0]))
        pnum = cls._balanced_blocks(block, "pin_numbers")
        pin_numbers_hide = bool(pnum and (re.search(r'\bhide\b', pnum[0]) or
                                          re.search(r'\(hide\s+yes\)', pnum[0])))

        # distinct unit count from sub-symbol names "<name>_<unit>_<style>"
        units = set()
        for um in re.finditer(r'\(symbol\s+"' + re.escape(name) + r'_(\d+)_\d+"', block):
            units.add(int(um.group(1)))
        units.discard(0)  # unit 0 holds common (shared) graphics

        # The Reference property may carry a baked-in instance number (e.g. "U2");
        # the lib-symbol wants just the prefix.
        ref_prefix = re.sub(r'\d+$', '', _prop("Reference") or "U") or "U"
        meta = {
            "name": name,
            "ref_prefix": ref_prefix,
            "value": _prop("Value"),
            "footprint": _prop("Footprint"),
            "pin_names_offset": pin_names_offset,
            "pin_names_hide": pin_names_hide,
            "pin_numbers_hide": pin_numbers_hide,
            "units": len(units) if units else 1,
        }
        return pins, graphics, meta

    def add_lib_symbol_from_block(self, lib_id, block, ref_prefix=None,
                                  value=None, footprint=None):
        """Embed a real library symbol **as-is** instead of synthesizing one.

        Use this when the part already exists in a registered library (built-in or
        a user/imported lib): the symbol's own drawing and **real pin geometry** are
        preserved, so the result matches what KiCad draws when you place the part —
        no ``side``/``index`` arrangement needed (that judgment only applies to
        symbols Claude must invent).

        ``block`` is the top-level ``(symbol "..." ...)`` text from the ``.kicad_sym``
        file (e.g. from ``check_kicad_library.load_symbol_block``). Returns the
        LibSymbol. Raises ValueError if no pins parse out.
        """
        pins, graphics, meta = self.parse_symbol_block(block)
        if not pins:
            raise ValueError(f"no pins parsed from symbol block for {lib_id}")
        name = lib_id.split(':')[-1] if ':' in lib_id else lib_id

        gfx_lines = [f'      (symbol "{name}_0_1"']
        for g in graphics:
            g = g.replace('\t', '  ')
            gfx_lines.extend('        ' + ln for ln in g.split('\n'))
        gfx_lines.append('      )')
        graphics_sexpr = '\n'.join(gfx_lines) if graphics else ""

        props = {
            "Reference": ref_prefix or meta["ref_prefix"],
            "Value": value or meta["value"] or name,
            "Footprint": footprint or meta["footprint"] or "",
            "Datasheet": "~",
        }
        sym = LibSymbol(
            lib_id=lib_id,
            properties=props,
            pins=pins,
            graphics_sexpr=graphics_sexpr,
            pin_names_offset=meta["pin_names_offset"],
            pin_names_hide=meta["pin_names_hide"],
            pin_numbers_hide=meta["pin_numbers_hide"],
        )
        self.lib_symbols[lib_id] = sym
        return sym

    # ─── Reference auto-assignment ───────────────────────────────────

    def auto_reference(self, prefix):
        """Return the next available reference for a given prefix.

        Example: auto_reference("R") → "R1", then "R2", etc.
        Skips numbers already used by manually-placed components.
        """
        existing_nums = set()
        for c in self.components:
            m = re.match(r'^' + re.escape(prefix) + r'(\d+)$', c.reference)
            if m:
                existing_nums.add(int(m.group(1)))

        counter = self._ref_counters.get(prefix, 0)
        counter += 1
        while counter in existing_nums:
            counter += 1
        self._ref_counters[prefix] = counter
        return f"{prefix}{counter}"

    def _resolve_reference(self, reference):
        """Resolve a reference string, auto-assigning if needed.

        - "R1", "C3", "U2" → used as-is, registered in counters
        - "R?", "C?", "U?" → auto-assigned (question mark stripped)
        - "R", "C", "U"   → auto-assigned (bare prefix)
        """
        # Match: alpha prefix, optional digits, optional trailing ?
        m = re.match(r'^([A-Za-z_#]+?)(\d+)?(\?)?$', reference)
        if not m:
            return reference  # can't parse, use as-is

        prefix = m.group(1)
        num_str = m.group(2)
        has_q = m.group(3)

        if num_str is not None and not has_q:
            # Explicit reference like "R1" — register and use as-is
            num = int(num_str)
            self._ref_counters[prefix] = max(
                self._ref_counters.get(prefix, 0), num
            )
            return reference
        else:
            # Auto-assign: bare prefix ("R") or question mark ("R?")
            return self.auto_reference(prefix)

    # ─── Label collision avoidance ────────────────────────────────────

    @staticmethod
    def _text_bbox(text, x, y, rotation=0):
        """Estimate axis-aligned bounding box for a text string.

        Returns (x_min, y_min, x_max, y_max).
        Assumes KiCad default font: 1.27mm size, ~1.0mm per character width.
        """
        w = len(text) * 1.0 + 0.5  # small padding
        h = 1.8  # height with descenders

        rot = rotation % 360
        if rot == 0 or rot == 180:
            # horizontal text
            return (x - 0.25, y - h / 2, x + w, y + h / 2)
        else:
            # vertical text (90/270)
            return (x - h / 2, y - w, x + h / 2, y + 0.25)

    @staticmethod
    def _rects_overlap(a, b, margin=0.5):
        """Test if two axis-aligned rects overlap (with margin)."""
        return not (a[2] + margin <= b[0] or b[2] + margin <= a[0] or
                    a[3] + margin <= b[1] or b[3] + margin <= a[1])

    def _register_rect(self, bbox):
        """Register a bounding box as occupied space."""
        self._occupied_rects.append(bbox)

    def _find_clear_position(self, text, preferred_x, preferred_y, rotation=0):
        """Find a position for text that doesn't overlap existing items.

        Tries the preferred position first, then nudges by grid increments.
        Returns (x, y) snapped to grid.
        """
        px = snap_to_grid(preferred_x)
        py = snap_to_grid(preferred_y)

        bbox = self._text_bbox(text, px, py, rotation)
        if not any(self._rects_overlap(bbox, r) for r in self._occupied_rects):
            self._register_rect(bbox)
            return px, py

        # Try nudging in a grid-aligned spiral
        grid = 1.27
        offsets = [
            (0, -grid), (0, grid),          # above, below
            (0, -2*grid), (0, 2*grid),
            (grid, 0), (-grid, 0),           # right, left
            (grid, -grid), (-grid, -grid),   # diagonals
            (grid, grid), (-grid, grid),
            (0, -3*grid), (0, 3*grid),
            (2*grid, 0), (-2*grid, 0),
            (0, -4*grid), (0, 4*grid),
        ]
        for dx, dy in offsets:
            cx = snap_to_grid(px + dx)
            cy = snap_to_grid(py + dy)
            bbox = self._text_bbox(text, cx, cy, rotation)
            if not any(self._rects_overlap(bbox, r) for r in self._occupied_rects):
                self._register_rect(bbox)
                return cx, cy

        # All nudge positions taken — just use preferred and register
        bbox = self._text_bbox(text, px, py, rotation)
        self._register_rect(bbox)
        return px, py

    def _get_component_body_bbox(self, comp):
        """Estimate the bounding box of a component's body in schematic coords.

        Returns (x_min, y_min, x_max, y_max).
        """
        lib_sym = self.lib_symbols.get(comp.lib_id)
        if lib_sym is None:
            # Fallback: small box around placement point
            return (comp.x - 2.54, comp.y - 2.54, comp.x + 2.54, comp.y + 2.54)

        # Try to extract rectangle from graphics_sexpr
        rect_match = re.search(
            r'\(start\s+([-\d.]+)\s+([-\d.]+)\).*?\(end\s+([-\d.]+)\s+([-\d.]+)\)',
            lib_sym.graphics_sexpr, re.DOTALL
        )

        if rect_match:
            sx = float(rect_match.group(1))
            sy = float(rect_match.group(2))
            ex = float(rect_match.group(3))
            ey = float(rect_match.group(4))
        else:
            # Estimate from pin positions
            if lib_sym.pins:
                xs = [p.x for p in lib_sym.pins]
                ys = [p.y for p in lib_sym.pins]
                sx, ex = min(xs), max(xs)
                sy, ey = min(ys), max(ys)
            else:
                sx, sy, ex, ey = -2.54, -2.54, 2.54, 2.54

        # Normalize min/max
        x_min_s, x_max_s = min(sx, ex), max(sx, ex)
        y_min_s, y_max_s = min(sy, ey), max(sy, ey)

        # Transform corners through rotation + Y-inversion
        corners_sym = [
            (x_min_s, y_min_s), (x_max_s, y_min_s),
            (x_min_s, y_max_s), (x_max_s, y_max_s),
        ]
        comp_rad = math.radians(comp.rotation)
        cos_r = math.cos(comp_rad)
        sin_r = math.sin(comp_rad)

        schem_xs = []
        schem_ys = []
        for sx_c, sy_c in corners_sym:
            # Y-inversion then rotation (same transform as get_pin_position)
            ix, iy = sx_c, -sy_c
            rx = ix * cos_r - iy * sin_r
            ry = ix * sin_r + iy * cos_r
            schem_xs.append(comp.x + rx)
            schem_ys.append(comp.y + ry)

        pad = 1.27  # padding around body
        return (min(schem_xs) - pad, min(schem_ys) - pad,
                max(schem_xs) + pad, max(schem_ys) + pad)

    def _compute_all_label_positions(self):
        """Pre-compute Reference/Value positions for all components.

        Called at the start of save(). Populates self._label_positions
        and registers occupied areas for collision avoidance.
        """
        # Reset label positions but keep label/body rects from building phase
        self._label_positions.clear()

        # Register component body areas first (so text avoids bodies)
        body_rects = []
        for comp in self.components:
            bbox = self._get_component_body_bbox(comp)
            body_rects.append(bbox)

        # Start with label areas already registered during add_label/add_global_label,
        # plus body areas
        save_rects = list(self._occupied_rects) + body_rects

        # Temporarily replace _occupied_rects for the computation
        orig_rects = self._occupied_rects
        self._occupied_rects = save_rects

        for comp in self.components:
            if comp.reference.startswith("#PWR"):
                # Power symbols — text is hidden, use component position
                self._label_positions[comp.reference] = (
                    comp.x, comp.y, comp.x, comp.y
                )
                continue

            body = self._get_component_body_bbox(comp)
            body_cx = (body[0] + body[2]) / 2
            body_cy = (body[1] + body[3]) / 2
            body_w = body[2] - body[0]
            body_h = body[3] - body[1]

            rot = comp.rotation % 360

            # Choose preferred positions based on rotation and body shape
            if rot == 0 or rot == 180:
                # Component is in default or flipped orientation
                # For tall components (passives vertical): ref above, val below
                # For wide components (ICs horizontal): ref above-right, val below-right
                if body_h >= body_w:
                    # Tall (vertical passive)
                    ref_px = body[2] + 1.27
                    ref_py = body_cy - 1.27
                    val_px = body[2] + 1.27
                    val_py = body_cy + 1.27
                else:
                    # Wide (IC)
                    ref_px = body_cx
                    ref_py = body[1] - 1.0
                    val_px = body_cx
                    val_py = body[3] + 1.8
            elif rot == 90 or rot == 270:
                # Rotated 90/270 — body axes are swapped
                if body_w >= body_h:
                    # Now wide (was tall passive, rotated horizontal)
                    ref_px = body_cx
                    ref_py = body[1] - 1.0
                    val_px = body_cx
                    val_py = body[3] + 1.8
                else:
                    # Now tall (was wide IC, rotated vertical)
                    ref_px = body[2] + 1.27
                    ref_py = body_cy - 1.27
                    val_px = body[2] + 1.27
                    val_py = body_cy + 1.27
            else:
                ref_px = body[2] + 1.27
                ref_py = body_cy - 1.27
                val_px = body[2] + 1.27
                val_py = body_cy + 1.27

            ref_x, ref_y = self._find_clear_position(
                comp.reference, ref_px, ref_py
            )
            val_x, val_y = self._find_clear_position(
                comp.value, val_px, val_py
            )

            self._label_positions[comp.reference] = (ref_x, ref_y, val_x, val_y)

        # Restore original rects
        self._occupied_rects = orig_rects

    # ─── Wire deduplication ───────────────────────────────────────────

    def _deduplicate_wires(self):
        """Remove overlapping and redundant parallel wire segments.

        Merges collinear segments that share an axis and overlap/touch.
        Removes exact duplicates. Operates on self.wires in-place.
        """
        if not self.wires:
            return

        # Normalize wire direction: ensure x1<=x2 (or y1<=y2 for vertical)
        def _normalize(w):
            if w.x1 == w.x2:
                # Vertical wire — sort by Y
                if w.y1 > w.y2:
                    return Wire(w.x1, w.y2, w.x2, w.y1)
            else:
                # Horizontal or diagonal — sort by X
                if w.x1 > w.x2:
                    return Wire(w.x2, w.y2, w.x1, w.y1)
            return Wire(w.x1, w.y1, w.x2, w.y2)

        wires = [_normalize(w) for w in self.wires]

        # Remove exact duplicates
        seen = set()
        unique = []
        for w in wires:
            key = (round(w.x1, 4), round(w.y1, 4), round(w.x2, 4), round(w.y2, 4))
            if key not in seen:
                seen.add(key)
                unique.append(w)
        wires = unique

        # Merge collinear overlapping segments
        # Group by axis: horizontal (same y) or vertical (same x)
        merged = True
        while merged:
            merged = False
            horizontal = {}  # y -> list of (x_min, x_max) wires
            vertical = {}    # x -> list of (y_min, y_max) wires
            diagonal = []    # non-axis-aligned wires (can't merge)

            for w in wires:
                y1r = round(w.y1, 4)
                y2r = round(w.y2, 4)
                x1r = round(w.x1, 4)
                x2r = round(w.x2, 4)
                if y1r == y2r:
                    horizontal.setdefault(y1r, []).append((min(x1r, x2r), max(x1r, x2r)))
                elif x1r == x2r:
                    vertical.setdefault(x1r, []).append((min(y1r, y2r), max(y1r, y2r)))
                else:
                    diagonal.append(w)

            new_wires = list(diagonal)

            for y_coord, segments in horizontal.items():
                merged_segs = self._merge_1d_segments(segments)
                if len(merged_segs) != len(segments):
                    merged = True
                for a, b in merged_segs:
                    new_wires.append(Wire(a, y_coord, b, y_coord))

            for x_coord, segments in vertical.items():
                merged_segs = self._merge_1d_segments(segments)
                if len(merged_segs) != len(segments):
                    merged = True
                for a, b in merged_segs:
                    new_wires.append(Wire(x_coord, a, x_coord, b))

            wires = new_wires

        self.wires = wires

    @staticmethod
    def _merge_1d_segments(segments):
        """Merge overlapping/touching 1D segments.

        Input: list of (min, max) tuples.
        Output: list of merged (min, max) tuples.
        """
        if not segments:
            return []
        segments = sorted(segments)
        merged = [segments[0]]
        for lo, hi in segments[1:]:
            prev_lo, prev_hi = merged[-1]
            if lo <= prev_hi + 0.01:  # touching or overlapping (with small tolerance)
                merged[-1] = (prev_lo, max(prev_hi, hi))
            else:
                merged.append((lo, hi))
        return merged

    # ─── Placement envelopes & spacing ──────────────────────────────

    # Minimum clearance between placement envelopes (mm).
    # 5.08mm = 2 grid units — leaves a routing channel between components.
    PLACEMENT_CLEARANCE = 5.08

    def _get_placement_envelope(self, comp):
        """Compute the full placement envelope for a component.

        The envelope is the body bbox expanded to include:
        - Pin stub extensions (2.54mm outward from body per side with pins)
        - Label clearance (space for Reference/Value text, ~3mm)
        - Routing channel (PLACEMENT_CLEARANCE around the whole thing)

        Returns (x_min, y_min, x_max, y_max).
        """
        body = self._get_component_body_bbox(comp)
        lib_sym = self.lib_symbols.get(comp.lib_id)

        # Start with body
        x_min, y_min, x_max, y_max = body

        # Expand for pin stubs — find which sides have pins
        if lib_sym and lib_sym.pins:
            for pin in lib_sym.pins:
                px, py = self._transform_point_to_schematic(
                    pin.x, pin.y, comp.x, comp.y, comp.rotation
                )
                # Expand envelope to include pin tips
                x_min = min(x_min, px)
                y_min = min(y_min, py)
                x_max = max(x_max, px)
                y_max = max(y_max, py)

        # Add label clearance (above/below or left/right)
        label_pad = 3.0
        y_min -= label_pad
        y_max += label_pad

        # Add routing channel clearance
        cl = self.PLACEMENT_CLEARANCE
        return (x_min - cl, y_min - cl, x_max + cl, y_max + cl)

    # ─── Pin keepout zones ─────────────────────────────────────────

    # Keepout corridor length (mm) extending outward from each pin tip.
    # 5.08mm = 2 grid units — enough room for a wire approach + label.
    PIN_KEEPOUT_LENGTH = 5.08

    # Half-width of the keepout corridor (mm) perpendicular to pin direction.
    # 1.27mm = 1 grid unit on each side of the pin centerline.
    PIN_KEEPOUT_HALF_WIDTH = 1.27

    def _compute_pin_keepouts(self, comp):
        """Compute keepout corridors for every pin on a placed component.

        Each pin gets a rectangular keepout zone extending outward from its
        tip in the pin's facing direction.  The corridor is
        PIN_KEEPOUT_LENGTH long and 2 * PIN_KEEPOUT_HALF_WIDTH wide.

        Returns a list of (x_min, y_min, x_max, y_max) rectangles in
        schematic coordinates.
        """
        lib_sym = self.lib_symbols.get(comp.lib_id)
        if not lib_sym or not lib_sym.pins:
            return []

        keepouts = []
        length = self.PIN_KEEPOUT_LENGTH
        hw = self.PIN_KEEPOUT_HALF_WIDTH

        comp_rad = math.radians(comp.rotation)
        cos_r = math.cos(comp_rad)
        sin_r = math.sin(comp_rad)

        for pin in lib_sym.pins:
            # Pin tip position in schematic space
            tip_x, tip_y = self._transform_point_to_schematic(
                pin.x, pin.y, comp.x, comp.y, comp.rotation
            )

            # Outward direction: opposite to the direction pin stub points
            # into the body.  Pin rotation is the direction from tip toward
            # body, so outward = pin.rotation + 180°.
            out_deg = (pin.rotation + 180) % 360
            out_rad = math.radians(out_deg)

            # Direction vector in symbol space
            dx_sym = math.cos(out_rad)
            dy_sym = math.sin(out_rad)

            # Y-inversion then component rotation
            dx_inv, dy_inv = dx_sym, -dy_sym
            dx_sch = dx_inv * cos_r - dy_inv * sin_r
            dy_sch = dx_inv * sin_r + dy_inv * cos_r

            # Round to axis-aligned (components only use 0/90/180/270)
            dx_sch = round(dx_sch)
            dy_sch = round(dy_sch)

            # Build corridor rectangle
            if dx_sch != 0 and dy_sch == 0:
                # Horizontal corridor
                if dx_sch > 0:
                    rect = (tip_x, tip_y - hw, tip_x + length, tip_y + hw)
                else:
                    rect = (tip_x - length, tip_y - hw, tip_x, tip_y + hw)
            elif dy_sch != 0 and dx_sch == 0:
                # Vertical corridor
                if dy_sch > 0:
                    rect = (tip_x - hw, tip_y, tip_x + hw, tip_y + length)
                else:
                    rect = (tip_x - hw, tip_y - length, tip_x + hw, tip_y)
            else:
                # Fallback: small square around tip
                rect = (tip_x - hw, tip_y - hw, tip_x + hw, tip_y + hw)

            keepouts.append(rect)

        return keepouts

    def get_pin_keepouts(self, reference=None):
        """Return pin keepout zones for a component or all components.

        Args:
            reference: Component reference (e.g. "R1").  If None, returns
                       a dict mapping every reference to its keepout list.

        Returns:
            If reference is given: list of (x_min, y_min, x_max, y_max).
            If None: dict[str, list[tuple]].
        """
        if reference is not None:
            return list(self._pin_keepout_zones.get(reference, []))
        return {ref: list(zones) for ref, zones in self._pin_keepout_zones.items()}

    def _point_in_any_keepout(self, px, py, exclude_ref=None):
        """Check if a point falls inside any registered pin keepout zone.

        Args:
            px, py: Point to test.
            exclude_ref: Optional reference whose keepouts to skip (e.g.
                         to allow a wire to start at a pin's own keepout).

        Returns True if the point is inside a keepout.
        """
        for ref, zones in self._pin_keepout_zones.items():
            if ref == exclude_ref:
                continue
            for rect in zones:
                if rect[0] <= px <= rect[2] and rect[1] <= py <= rect[3]:
                    return True
        return False

    def _rect_overlaps_any_keepout(self, rect, exclude_ref=None):
        """Check if a rectangle overlaps any registered pin keepout zone.

        Used during placement to ensure a new component's body doesn't
        block existing pins' approach corridors.
        """
        for ref, zones in self._pin_keepout_zones.items():
            if ref == exclude_ref:
                continue
            for kz in zones:
                if self._rects_overlap(rect, kz, margin=0):
                    return True
        return False

    def _transform_point_to_schematic(self, sym_x, sym_y, comp_x, comp_y, rotation):
        """Transform a point from symbol space to schematic space."""
        comp_rad = math.radians(rotation)
        cos_r = math.cos(comp_rad)
        sin_r = math.sin(comp_rad)
        ix, iy = sym_x, -sym_y  # Y-inversion
        rx = ix * cos_r - iy * sin_r
        ry = ix * sin_r + iy * cos_r
        return snap_to_grid(comp_x + rx), snap_to_grid(comp_y + ry)

    def _find_clear_placement(self, envelope, x, y, body_rect=None):
        """Find the nearest grid-aligned position where an envelope doesn't overlap
        any existing placement envelope and the body doesn't block pin keepouts.

        Returns (new_x, new_y) — the offset to apply to the component position.
        The envelope is centered at (x, y); we shift both together.

        Args:
            envelope: The component's placement envelope at (x, y).
            x, y: Preferred placement coordinates.
            body_rect: Optional body bbox at (x, y).  When provided, the
                       search also rejects positions where the body overlaps
                       any existing pin keepout zone.
        """
        # Check existing component envelopes (skip power symbols — they're small and
        # often intentionally placed at pin locations)
        existing_envelopes = []
        for c in self.components:
            if c.reference.startswith("#PWR"):
                continue
            existing_envelopes.append(self._get_placement_envelope(c))

        # Collect all existing pin keepout zones into a flat list
        all_keepouts = []
        if body_rect is not None:
            for zones in self._pin_keepout_zones.values():
                all_keepouts.extend(zones)

        def _position_ok(shifted_env, shifted_body):
            if any(self._rects_overlap(shifted_env, e, margin=0) for e in existing_envelopes):
                return False
            if shifted_body is not None and all_keepouts:
                if any(self._rects_overlap(shifted_body, kz, margin=0) for kz in all_keepouts):
                    return False
            return True

        if not existing_envelopes and not all_keepouts:
            return x, y

        # Test the preferred position
        if _position_ok(envelope, body_rect):
            return x, y

        # Nudge on grid — try rightward first, then downward, then diagonals
        grid = 2.54  # nudge in 2.54mm steps (100mil)
        best = None
        best_dist = float('inf')

        for step in range(1, 20):  # up to ~50mm nudge
            offsets = [
                (step * grid, 0),           # right
                (0, step * grid),            # down
                (-step * grid, 0),           # left
                (0, -step * grid),           # up
                (step * grid, step * grid),  # down-right
                (-step * grid, step * grid), # down-left
                (step * grid, -step * grid), # up-right
            ]
            for dx, dy in offsets:
                nx = snap_to_grid(x + dx)
                ny = snap_to_grid(y + dy)
                shifted_env = (
                    envelope[0] + dx, envelope[1] + dy,
                    envelope[2] + dx, envelope[3] + dy,
                )
                shifted_body = None
                if body_rect is not None:
                    shifted_body = (
                        body_rect[0] + dx, body_rect[1] + dy,
                        body_rect[2] + dx, body_rect[3] + dy,
                    )
                if _position_ok(shifted_env, shifted_body):
                    dist = dx * dx + dy * dy
                    if dist < best_dist:
                        best = (nx, ny)
                        best_dist = dist
            if best is not None:
                return best

        # Couldn't find clear space — use original position
        return x, y

    # ─── Layout suggestion ──────────────────────────────────────────

    # Default starting position when no components exist yet
    _LAYOUT_ORIGIN_X = 100.0
    _LAYOUT_ORIGIN_Y = 80.0

    # Offsets for different relationship types (mm)
    _OFFSET_NEARBY = 20.0       # general spacing between component groups
    _OFFSET_DECOUPLING = 10.0   # cap near its IC
    _OFFSET_SERIES = 15.0       # in-line signal flow
    _OFFSET_PARALLEL = 7.62     # stacked vertically (3 grid units)

    def suggest_placement(self, lib_id, near_x=None, near_y=None,
                          relative_to=None, relationship="nearby",
                          group_id=None):
        """Suggest optimal placement coordinates for a new component.

        Returns (x, y) grid-snapped and collision-free. Does NOT place
        the component — call place_component() with the returned coords.

        Args:
            lib_id: Library symbol ID (used to determine component size).
            near_x, near_y: Preferred location hint.
            relative_to: Dict {"ref": "U1", "pin": "5"} — anchor to a
                         specific pin of an existing component.
            relationship: How to position relative to the anchor.
                "nearby"     — offset by component width + routing channel
                "decoupling" — cap placement near IC pin (vertically offset)
                "series"     — in-line with horizontal signal flow
                "parallel"   — stacked vertically below anchor
            group_id: Optional group name. Components in the same group
                      cluster near the group centroid.

        Returns:
            (x, y) tuple snapped to 1.27mm grid.
        """
        # Step 1: Determine anchor point
        anchor_x, anchor_y = self._suggest_anchor(
            near_x, near_y, relative_to, group_id
        )

        # Step 2: Apply relationship-based offset
        offset_x, offset_y = self._suggest_offset(
            lib_id, relationship, relative_to
        )

        target_x = snap_to_grid(anchor_x + offset_x)
        target_y = snap_to_grid(anchor_y + offset_y)

        # Step 3: Collision avoidance
        lib_sym = self.lib_symbols.get(lib_id)
        if lib_sym and not (lib_sym.is_power):
            # Build a temporary component to compute its envelope
            temp = PlacedComponent(
                lib_id=lib_id, reference="_TEMP", value="",
                x=target_x, y=target_y, rotation=0,
            )
            envelope = self._get_placement_envelope(temp)
            body = self._get_component_body_bbox(temp)
            target_x, target_y = self._find_clear_placement(
                envelope, target_x, target_y, body_rect=body
            )

        return target_x, target_y

    def _suggest_anchor(self, near_x, near_y, relative_to, group_id):
        """Determine the anchor point for placement suggestion."""
        # Priority 1: relative_to a specific pin
        if relative_to is not None:
            ref = relative_to.get("ref")
            pin = relative_to.get("pin")
            if ref and pin:
                try:
                    return self.get_pin_position(ref, str(pin))
                except (ValueError, KeyError):
                    pass
            # Fall back to component center
            if ref:
                for comp in self.components:
                    if comp.reference == ref:
                        return comp.x, comp.y
                        break

        # Priority 2: explicit coordinates
        if near_x is not None and near_y is not None:
            return float(near_x), float(near_y)

        # Priority 3: group centroid
        if group_id and group_id in self._placement_groups:
            refs = self._placement_groups[group_id]
            group_comps = [c for c in self.components if c.reference in refs]
            if group_comps:
                cx = sum(c.x for c in group_comps) / len(group_comps)
                cy = sum(c.y for c in group_comps) / len(group_comps)
                return cx, cy

        # Priority 4: next to rightmost existing component
        non_pwr = [c for c in self.components if not c.reference.startswith("#PWR")]
        if non_pwr:
            rightmost = max(non_pwr, key=lambda c: c.x)
            return rightmost.x, rightmost.y

        # Priority 5: layout origin
        return self._LAYOUT_ORIGIN_X, self._LAYOUT_ORIGIN_Y

    def _suggest_offset(self, lib_id, relationship, relative_to):
        """Compute offset from anchor based on relationship type."""
        if relationship == "decoupling":
            # Place cap below and slightly right of the IC pin
            return 2.54, self._OFFSET_DECOUPLING

        if relationship == "series":
            # Horizontal offset for signal flow
            return self._OFFSET_SERIES, 0.0

        if relationship == "parallel":
            # Stack vertically below
            return 0.0, self._OFFSET_PARALLEL

        # "nearby" — default: offset right by component group spacing
        # Use a smaller offset if anchored to a specific pin
        if relative_to is not None:
            return self._OFFSET_SERIES, 0.0
        return self._OFFSET_NEARBY, 0.0

    def register_group(self, group_id, reference):
        """Register a component reference as part of a placement group.

        Call after place_component() to track group membership.
        Future suggest_placement() calls with the same group_id will
        cluster near the group centroid.
        """
        if group_id not in self._placement_groups:
            self._placement_groups[group_id] = []
        if reference not in self._placement_groups[group_id]:
            self._placement_groups[group_id].append(reference)

    # ─── Place elements ──────────────────────────────────────────────

    def place_component(self, lib_id, reference, value, x, y,
                        rotation=0, footprint="", datasheet="~",
                        in_bom=True, on_board=True, dnp=False, **extra_props):
        """Place a component instance on the schematic.

        The lib_id must have been previously added via add_lib_symbol_*.
        Coordinates are snapped to 1.27mm grid.

        Spacing enforcement:
            Non-power components are auto-nudged if they would overlap an
            existing component's placement envelope (body + pins + routing
            channel). Power symbols are exempt.

        Reference auto-assignment:
            - "R1", "C3" → used as-is
            - "R?", "C?" → auto-assigned next available number
            - "R", "C"   → auto-assigned next available number
        """
        x = snap_to_grid(x)
        y = snap_to_grid(y)

        # Auto-assign reference if needed
        reference = self._resolve_reference(reference)

        # Generate pin UUIDs
        lib_sym = self.lib_symbols.get(lib_id)
        pin_uuids = {}
        if lib_sym:
            for pin in lib_sym.pins:
                pin_uuids[pin.number] = _uuid()

        # Build a temporary component to compute its envelope
        comp = PlacedComponent(
            lib_id=lib_id, reference=reference, value=value,
            x=x, y=y, rotation=rotation,
            footprint=footprint, datasheet=datasheet,
            in_bom=in_bom, on_board=on_board, dnp=dnp,
            extra_properties=extra_props,
            pin_uuids=pin_uuids,
        )

        # Auto-nudge non-power components to avoid overlapping envelopes
        # and pin keepout zones
        if not reference.startswith("#PWR"):
            envelope = self._get_placement_envelope(comp)
            body = self._get_component_body_bbox(comp)
            new_x, new_y = self._find_clear_placement(envelope, x, y, body_rect=body)
            if new_x != x or new_y != y:
                comp.x = new_x
                comp.y = new_y

        self.components.append(comp)

        # Register component body as occupied space (for label collision)
        body_bbox = self._get_component_body_bbox(comp)
        self._register_rect(body_bbox)

        # Register pin keepout zones (skip power symbols — they sit on pins)
        if not reference.startswith("#PWR"):
            keepouts = self._compute_pin_keepouts(comp)
            if keepouts:
                self._pin_keepout_zones[reference] = keepouts

        return comp
    
    def place_power_symbol(self, name, x, y, rotation=0):
        """Place a power symbol (GND, VCC, +3V3, etc.).
        
        Ensure add_lib_symbol_power(name) was called first.
        """
        lib_id = f"power:{name}"
        self._pwr_counter += 1
        ref = f"#PWR{self._pwr_counter:03d}"
        return self.place_component(
            lib_id, ref, name, x, y,
            rotation=rotation, in_bom=False, on_board=False
        )
    
    def add_wire(self, x1, y1, x2, y2):
        """Add a wire segment. Coordinates snapped to grid."""
        self.wires.append(Wire(
            snap_to_grid(x1), snap_to_grid(y1),
            snap_to_grid(x2), snap_to_grid(y2)
        ))
    
    def add_label(self, text, x, y, rotation=0):
        """Add a local net label with label-vs-label collision avoidance.

        Checks if the proposed position overlaps any previously placed net label.
        If so, nudges along the label's direction to find a clear spot.
        Does NOT avoid component bodies — labels on pin tips are expected.
        """
        sx, sy = snap_to_grid(x), snap_to_grid(y)
        bbox = self._text_bbox(text, sx, sy, rotation)

        # Only check against other net labels (not component bodies)
        if any(self._rects_overlap(bbox, r) for r in self._label_rects):
            grid = 1.27
            rot = rotation % 360
            if rot == 0:
                nudges = [(i * grid, 0) for i in range(1, 5)]
            elif rot == 180:
                nudges = [(-i * grid, 0) for i in range(1, 5)]
            elif rot == 90:
                nudges = [(0, -i * grid) for i in range(1, 5)]
            elif rot == 270:
                nudges = [(0, i * grid) for i in range(1, 5)]
            else:
                nudges = [(i * grid, 0) for i in range(1, 5)]

            for dx, dy in nudges:
                cx = snap_to_grid(sx + dx)
                cy = snap_to_grid(sy + dy)
                bbox = self._text_bbox(text, cx, cy, rotation)
                if not any(self._rects_overlap(bbox, r) for r in self._label_rects):
                    sx, sy = cx, cy
                    break

        self.labels.append(Label(text, sx, sy, rotation))
        lbl_bbox = self._text_bbox(text, sx, sy, rotation)
        self._register_rect(lbl_bbox)
        self._label_rects.append(lbl_bbox)

    def add_global_label(self, text, x, y, shape="bidirectional", rotation=0):
        """Add a global (cross-sheet) label."""
        sx, sy = snap_to_grid(x), snap_to_grid(y)
        self.global_labels.append(GlobalLabel(text, sx, sy, shape, rotation))
        # Global labels have a shape box — wider than plain text
        bbox = self._text_bbox(text + "  ", sx, sy, rotation)  # extra padding for shape
        self._register_rect(bbox)

    def add_hierarchical_label(self, text, x, y, shape="bidirectional", rotation=0):
        """Add a hierarchical label — a port of this sheet (block interface).

        Connects only through the parent's sheet pins (never leaks like a
        global label). shape: input | output | bidirectional | tri_state |
        passive — the direction shown on the parent's sheet symbol.
        """
        sx, sy = snap_to_grid(x), snap_to_grid(y)
        self.hierarchical_labels.append(
            HierarchicalLabel(text, sx, sy, shape, rotation))
        bbox = self._text_bbox(text + "  ", sx, sy, rotation)
        self._register_rect(bbox)

    def add_junction(self, x, y):
        """Add a wire junction."""
        self.junctions.append(Junction(snap_to_grid(x), snap_to_grid(y)))
    
    def add_no_connect(self, x, y):
        """Add a no-connect marker."""
        self.no_connects.append(NoConnect(snap_to_grid(x), snap_to_grid(y)))

    # ─── Pin direction query ──────────────────────────────────────────

    def get_pin_stub_direction(self, reference, pin_number):
        """Get the outward direction from a component pin (away from body).

        Returns (dx, dy) as one of: (1,0) right, (-1,0) left, (0,1) down, (0,-1) up.
        Accounts for component rotation and Y-inversion.

        This is the direction a wire stub should extend to reach a label or
        power symbol. The logic mirrors _compute_pin_keepouts but returns
        the unit direction vector instead of a keepout rectangle.
        """
        comp = None
        for c in self.components:
            if c.reference == reference:
                comp = c
                break
        if comp is None:
            raise ValueError(f"Component {reference} not found")

        lib_sym = self.lib_symbols.get(comp.lib_id)
        if lib_sym is None:
            raise ValueError(f"Library symbol {comp.lib_id} not found")

        pin = None
        for p in lib_sym.pins:
            if p.number == pin_number:
                pin = p
                break
        if pin is None:
            raise ValueError(f"Pin {pin_number} not found on {comp.lib_id}")

        # Outward = opposite of the direction from tip toward body
        out_deg = (pin.rotation + 180) % 360
        out_rad = math.radians(out_deg)

        dx_sym = math.cos(out_rad)
        dy_sym = math.sin(out_rad)

        # Y-inversion then component rotation
        comp_rad = math.radians(comp.rotation)
        cos_r = math.cos(comp_rad)
        sin_r = math.sin(comp_rad)

        dx_inv, dy_inv = dx_sym, -dy_sym
        dx_sch = dx_inv * cos_r - dy_inv * sin_r
        dy_sch = dx_inv * sin_r + dy_inv * cos_r

        # Round to nearest cardinal direction
        dx_sch = round(dx_sch)
        dy_sch = round(dy_sch)

        # Safety: ensure at least one axis is nonzero
        if dx_sch == 0 and dy_sch == 0:
            dx_sch = 1  # fallback: right

        return dx_sch, dy_sch

    # ─── Convenience helpers: GND and label placement ────────────────

    _DIRECTION_TO_ROTATION = {
        (1, 0): 0,      # right
        (-1, 0): 180,   # left
        (0, -1): 90,    # up
        (0, 1): 270,    # down
    }

    def gnd_below(self, x, y, wire_len=5.08):
        """Place a GND symbol below (x, y) with a connecting wire.

        Default wire_len=5.08mm (2 grid units) gives clearance between
        the component pin and the GND triangle graphic.
        """
        self.add_wire(x, y, x, y + wire_len)
        self.place_power_symbol("GND", x, y + wire_len)

    def gnd_at_pin(self, reference, pin, wire_len=5.08):
        """Place a GND power symbol connected to a component pin.

        Automatically determines the pin's outward direction:
        - If the pin faces down: straight down to GND (cleanest).
        - Otherwise: short stub outward, then vertical down to GND.
        """
        x, y = self.get_pin_position(reference, pin)
        dx, dy = self.get_pin_stub_direction(reference, pin)

        if dy > 0:
            # Pin already faces down — go straight down
            self.add_wire(x, y, x, y + wire_len)
            self.place_power_symbol("GND", x, y + wire_len)
        elif dy < 0:
            # Pin faces up — stub up, then over, then down
            stub = snap_to_grid(2.54)
            end_y = y - stub
            offset_x = snap_to_grid(x + 2.54)
            self.add_wire(x, y, x, end_y)
            self.add_wire(x, end_y, offset_x, end_y)
            self.add_wire(offset_x, end_y, offset_x, end_y + wire_len + stub)
            self.place_power_symbol("GND", offset_x, end_y + wire_len + stub)
        else:
            # Pin faces left or right — stub outward, then down
            stub = snap_to_grid(2.54)
            mid_x = snap_to_grid(x + dx * stub)
            self.add_wire(x, y, mid_x, y)
            self.add_wire(mid_x, y, mid_x, y + wire_len)
            self.place_power_symbol("GND", mid_x, y + wire_len)

    def label_at_pin(self, reference, pin, name, direction="auto", length=5.08):
        """Add a net label connected to a component pin via a wire stub.

        Args:
            reference: Component reference (e.g., "U1")
            pin: Pin number (e.g., "1")
            name: Label text (e.g., "VBUS")
            direction: "auto" (default — computed from pin geometry),
                       or explicit "right", "left", "up", "down"
            length: Wire stub length in mm (default 5.08 = 2 grid units)
        """
        x, y = self.get_pin_position(reference, pin)

        if direction == "auto":
            dx, dy = self.get_pin_stub_direction(reference, pin)
        else:
            _dir_map = {"right": (1, 0), "left": (-1, 0),
                        "up": (0, -1), "down": (0, 1)}
            dx, dy = _dir_map.get(direction, (1, 0))

        end_x = snap_to_grid(x + dx * length)
        end_y = snap_to_grid(y + dy * length)
        rotation = self._DIRECTION_TO_ROTATION.get((dx, dy), 0)

        self.add_wire(x, y, end_x, end_y)
        self.add_label(name, end_x, end_y, rotation)

    def hlabel_at_pin(self, reference, pin, name, shape="bidirectional",
                      direction="auto", length=5.08):
        """Add a hierarchical label (sheet port) connected to a component pin
        via a wire stub — the block-interface counterpart of label_at_pin.

        A hierarchical label anchored at a wire end points *into* the wire, so
        its rotation is the opposite of a plain label's for the same stub
        direction (KiCad convention: rotation 0 = text extends left of the
        anchor for hierarchical labels).
        """
        x, y = self.get_pin_position(reference, pin)

        if direction == "auto":
            dx, dy = self.get_pin_stub_direction(reference, pin)
        else:
            _dir_map = {"right": (1, 0), "left": (-1, 0),
                        "up": (0, -1), "down": (0, 1)}
            dx, dy = _dir_map.get(direction, (1, 0))

        end_x = snap_to_grid(x + dx * length)
        end_y = snap_to_grid(y + dy * length)
        rotation = self._DIRECTION_TO_ROTATION.get((dx, dy), 0)

        self.add_wire(x, y, end_x, end_y)
        self.add_hierarchical_label(name, end_x, end_y, shape, rotation)

    def power_at_pin(self, reference, pin, power_name, wire_len=5.08):
        """Place a named power symbol connected to a component pin.

        For non-GND power symbols (VCC, +3V3, +5V, etc.), the symbol
        graphic points upward. The stub routes the wire so the symbol
        can be placed above the pin.

        For GND, delegates to gnd_at_pin().

        Args:
            reference: Component reference (e.g., "U1")
            pin: Pin number (e.g., "4")
            power_name: Power net name (e.g., "+5V", "VCC")
            wire_len: Wire stub length in mm (default 5.08)
        """
        if power_name == "GND":
            self.gnd_at_pin(reference, pin, wire_len)
            return

        x, y = self.get_pin_position(reference, pin)
        dx, dy = self.get_pin_stub_direction(reference, pin)

        if dy < 0:
            # Pin faces up — go straight up (cleanest for non-GND)
            self.add_wire(x, y, x, y - wire_len)
            self.place_power_symbol(power_name, x, y - wire_len)
        elif dy > 0:
            # Pin faces down — stub down, then over, then up
            stub = snap_to_grid(2.54)
            end_y = y + stub
            offset_x = snap_to_grid(x + 2.54)
            self.add_wire(x, y, x, end_y)
            self.add_wire(x, end_y, offset_x, end_y)
            self.add_wire(offset_x, end_y, offset_x, end_y - wire_len - stub)
            self.place_power_symbol(power_name, offset_x, end_y - wire_len - stub)
        else:
            # Pin faces left or right — stub outward, then up
            stub = snap_to_grid(2.54)
            mid_x = snap_to_grid(x + dx * stub)
            self.add_wire(x, y, mid_x, y)
            self.add_wire(mid_x, y, mid_x, y - wire_len)
            self.place_power_symbol(power_name, mid_x, y - wire_len)

    def nc_at_pin(self, reference, pin):
        """Place a no-connect marker at the specified component pin."""
        x, y = self.get_pin_position(reference, pin)
        self.add_no_connect(x, y)

    # ─── Hierarchical sheets (block composition, ROADMAP W1b) ─────────

    def add_sheet(self, name, filename, x, y, ports, width=25.4):
        """Place a hierarchical sheet symbol referencing a child .kicad_sch.

        Args:
            name: instance name (Sheetname) — must be unique on this schematic.
            filename: the child file (Sheetfile), relative to the parent's dir.
            x, y: top-left corner (snapped to grid).
            ports: ordered list of (port_name, shape) — one sheet pin each,
                   stacked down the LEFT edge (shape: input | output |
                   bidirectional | tri_state | passive). Must mirror the child
                   sheet's hierarchical labels exactly.
            width: sheet body width in mm.

        Returns the Sheet. Wire the pins with label_at_sheet_pin() /
        power_at_sheet_pin() — connectivity, as everywhere else, is by name.
        """
        if any(s.name == name for s in self.sheets):
            raise ValueError(f"Sheet instance name '{name}' already placed")
        seen = set()
        for pname, _shape in ports:
            if pname in seen:
                raise ValueError(f"Sheet '{name}': duplicate port '{pname}'")
            seen.add(pname)
        sx, sy = snap_to_grid(x), snap_to_grid(y)
        w = snap_to_grid(width)
        h = snap_to_grid(max(2.54 * (len(ports) + 1), 12.7))
        pins = []
        for i, (pname, shape) in enumerate(ports):
            pins.append(SheetPin(
                name=pname, shape=shape,
                x=sx, y=snap_to_grid(sy + 2.54 * (i + 1)),
                rotation=180,  # left edge
            ))
        sheet = Sheet(name=name, filename=filename, x=sx, y=sy,
                      width=w, height=h, pins=pins, uuid=_uuid())
        self.sheets.append(sheet)
        # The body is occupied space for label collision avoidance.
        self._register_rect((sx, sy, sx + w, sy + h))
        return sheet

    def _find_sheet_pin(self, sheet_name, pin_name):
        for sheet in self.sheets:
            if sheet.name == sheet_name:
                for pin in sheet.pins:
                    if pin.name == pin_name:
                        return sheet, pin
                raise ValueError(f"Sheet '{sheet_name}' has no pin '{pin_name}'")
        raise ValueError(f"Sheet '{sheet_name}' not found")

    def get_sheet_pin_position(self, sheet_name, pin_name):
        """Absolute (x, y) of a sheet pin on the sheet border."""
        _sheet, pin = self._find_sheet_pin(sheet_name, pin_name)
        return pin.x, pin.y

    def label_at_sheet_pin(self, sheet_name, pin_name, net_name, length=5.08):
        """Net label connected to a sheet pin via a wire stub (leftward —
        pins sit on the sheet's left edge). The sheet-pin counterpart of
        label_at_pin: matching the netlist name makes the connection."""
        x, y = self.get_sheet_pin_position(sheet_name, pin_name)
        end_x = snap_to_grid(x - length)
        self.add_wire(x, y, end_x, y)
        self.add_label(net_name, end_x, y, rotation=180)

    def power_at_sheet_pin(self, sheet_name, pin_name, power_name,
                           wire_len=5.08):
        """Power symbol (GND down, rails up) connected to a sheet pin via a
        leftward stub — for the rare port that maps onto a power net."""
        x, y = self.get_sheet_pin_position(sheet_name, pin_name)
        stub = snap_to_grid(2.54)
        mid_x = snap_to_grid(x - stub)
        self.add_wire(x, y, mid_x, y)
        if power_name == "GND":
            self.add_wire(mid_x, y, mid_x, y + wire_len)
            self.place_power_symbol("GND", mid_x, y + wire_len)
        else:
            self.add_wire(mid_x, y, mid_x, y - wire_len)
            self.place_power_symbol(power_name, mid_x, y - wire_len)

    # ─── Pre-save audits ──────────────────────────────────────────────

    def ensure_all_pins_assigned(self):
        """Audit that every pin on every non-power component is connected.

        A pin is 'assigned' if its endpoint touches a wire, label, power
        symbol, or no-connect marker. Returns a list of (reference, pin_number)
        tuples for any unassigned pins. Empty list = all good.
        """
        unassigned = []

        # Collect all wire endpoints, label positions, NC positions, and
        # power symbol pin positions as a set of (x, y) grid-snapped points.
        connected_points = set()

        for w in self.wires:
            connected_points.add((round(w.x1, 4), round(w.y1, 4)))
            connected_points.add((round(w.x2, 4), round(w.y2, 4)))

        for nc in self.no_connects:
            connected_points.add((round(nc.x, 4), round(nc.y, 4)))

        for comp in self.components:
            if comp.reference.startswith("#PWR"):
                # Power symbols — they connect via their pin position
                lib_sym = self.lib_symbols.get(comp.lib_id)
                if lib_sym:
                    for pin in lib_sym.pins:
                        px, py = self._transform_point_to_schematic(
                            pin.x, pin.y, comp.x, comp.y, comp.rotation
                        )
                        connected_points.add((round(px, 4), round(py, 4)))

        # Now check every pin on every non-power component
        for comp in self.components:
            if comp.reference.startswith("#PWR"):
                continue

            lib_sym = self.lib_symbols.get(comp.lib_id)
            if not lib_sym:
                continue

            for pin in lib_sym.pins:
                px, py = self.get_pin_position(comp.reference, pin.number)
                point = (round(px, 4), round(py, 4))
                if point not in connected_points:
                    unassigned.append((comp.reference, pin.number))

        return unassigned

    def ensure_footprints(self):
        """Audit that every non-power component has a footprint assigned.

        Returns a list of reference strings for components missing footprints.
        Empty list = all good.
        """
        missing = []
        for comp in self.components:
            if comp.reference.startswith("#PWR"):
                continue
            if not comp.footprint or comp.footprint.strip() == "":
                missing.append(comp.reference)
        return missing

    # ─── Phase 1: Topology-aware primitives ─────────────────────────

    def auto_rotate(self, reference, pin_constraints):
        """Compute and apply the rotation that satisfies pin-direction constraints.

        For 2-pin passives, finds the rotation (0/90/180/270) that best aligns
        each pin's outward direction with the desired schematic direction.

        Args:
            reference: Component reference (e.g., "R1")
            pin_constraints: Dict mapping pin_number -> desired direction.
                Direction is one of "up", "down", "left", "right".
                Example: {"1": "up", "2": "down"}

        Returns:
            The applied rotation in degrees.
        """
        comp = None
        for c in self.components:
            if c.reference == reference:
                comp = c
                break
        if comp is None:
            raise ValueError(f"Component {reference} not found")

        lib_sym = self.lib_symbols.get(comp.lib_id)
        if lib_sym is None:
            raise ValueError(f"Library symbol {comp.lib_id} not found")

        # Direction name → schematic unit vector (x, y) where Y+ is down
        dir_to_vec = {
            "right": (1, 0),
            "left":  (-1, 0),
            "up":    (0, -1),
            "down":  (0, 1),
        }

        best_rot = 0
        best_score = -1

        for candidate_rot in [0, 90, 180, 270]:
            score = 0
            rot_rad = math.radians(candidate_rot)
            cos_r = math.cos(rot_rad)
            sin_r = math.sin(rot_rad)

            for pin in lib_sym.pins:
                if pin.number not in pin_constraints:
                    continue

                desired = pin_constraints[pin.number]
                dx_want, dy_want = dir_to_vec[desired]

                # Pin's outward direction in symbol space:
                # pin.rotation points from tip INTO body, so outward = +180
                out_deg = (pin.rotation + 180) % 360
                out_rad = math.radians(out_deg)
                dx_sym = math.cos(out_rad)
                dy_sym = math.sin(out_rad)

                # Transform through Y-inversion + candidate rotation
                dx_inv, dy_inv = dx_sym, -dy_sym
                dx_sch = dx_inv * cos_r - dy_inv * sin_r
                dy_sch = dx_inv * sin_r + dy_inv * cos_r

                # Dot product: 1.0 = perfect alignment
                dot = round(dx_sch) * dx_want + round(dy_sch) * dy_want
                if dot > 0:
                    score += 1

            if score > best_score:
                best_score = score
                best_rot = candidate_rot

        # Apply the rotation
        old_rot = comp.rotation
        comp.rotation = best_rot

        # Re-register body bbox and pin keepouts with new rotation
        if not comp.reference.startswith("#PWR"):
            body_bbox = self._get_component_body_bbox(comp)
            # Replace old body rect (approximate: remove last matching, add new)
            self._occupied_rects = [r for r in self._occupied_rects
                                    if r != self._get_component_body_bbox(
                                        PlacedComponent(comp.lib_id, comp.reference,
                                                        comp.value, comp.x, comp.y,
                                                        old_rot))]
            self._register_rect(body_bbox)
            keepouts = self._compute_pin_keepouts(comp)
            if keepouts:
                self._pin_keepout_zones[comp.reference] = keepouts

        return best_rot

    def safe_wire(self, x1, y1, x2, y2):
        """Add a wire only if it doesn't pass through any component body.

        Returns True if the wire was added, False if it would cause a
        through-body conflict (caller should use a label instead).
        """
        sx1, sy1 = snap_to_grid(x1), snap_to_grid(y1)
        sx2, sy2 = snap_to_grid(x2), snap_to_grid(y2)

        if self._segment_intersects_body(sx1, sy1, sx2, sy2):
            return False

        self.add_wire(sx1, sy1, sx2, sy2)
        return True

    def connect_or_label(self, ref1, pin1, ref2, pin2, label_name=None):
        """Try to wire two pins; fall back to net labels if routing fails.

        Uses wire_between's smart routing. If no clean route is found
        (fallback would cross bodies), places matching labels on both pins
        instead.

        Args:
            ref1, pin1: First component pin
            ref2, pin2: Second component pin
            label_name: Net label name for fallback. If None, auto-generates
                        from the pin references.

        Returns:
            "wired" if a clean wire route was used,
            "labeled" if labels were placed instead.
        """
        x1, y1 = self.get_pin_position(ref1, pin1)
        x2, y2 = self.get_pin_position(ref2, pin2)
        ep_refs = (ref1, ref2)

        if x1 == x2 and y1 == y2:
            return "wired"  # same point, no connection needed

        # Try all wire_between strategies (without the fallback)
        if self._try_clean_route(x1, y1, x2, y2, ep_refs):
            return "wired"

        # No clean route found — use labels
        if label_name is None:
            label_name = f"_NET_{ref1}_{pin1}_{ref2}_{pin2}"

        # Determine label directions based on pin facing
        d1 = self._get_pin_outward_direction(ref1, pin1)
        d2 = self._get_pin_outward_direction(ref2, pin2)

        self.label_at_pin(ref1, pin1, label_name, d1)
        self.label_at_pin(ref2, pin2, label_name, d2)
        return "labeled"

    def _try_clean_route(self, x1, y1, x2, y2, ep_refs):
        """Attempt all clean routing strategies. Returns True if one succeeds."""
        # Strategy 1: Straight line
        if x1 == x2 or y1 == y2:
            route = [(x1, y1, x2, y2)]
            if self._route_is_clean(route, ep_refs):
                self.add_wire(x1, y1, x2, y2)
                return True

        # Strategy 2: L-route vertical-first
        if x1 != x2 and y1 != y2:
            route_vf = [(x1, y1, x1, y2), (x1, y2, x2, y2)]
            if self._route_is_clean(route_vf, ep_refs):
                self.add_wire(x1, y1, x1, y2)
                self.add_wire(x1, y2, x2, y2)
                return True

        # Strategy 3: L-route horizontal-first
        if x1 != x2 and y1 != y2:
            route_hf = [(x1, y1, x2, y1), (x2, y1, x2, y2)]
            if self._route_is_clean(route_hf, ep_refs):
                self.add_wire(x1, y1, x2, y1)
                self.add_wire(x2, y1, x2, y2)
                return True

        # Strategy 4: Z-routes
        grid = 2.54
        if x1 != x2 and y1 != y2:
            for jog_step in range(1, 8):
                for direction in [1, -1]:
                    jog = snap_to_grid(jog_step * grid * direction)

                    mid_y = snap_to_grid((y1 + y2) / 2 + jog)
                    route_hz = [(x1, y1, x1, mid_y), (x1, mid_y, x2, mid_y),
                                (x2, mid_y, x2, y2)]
                    if self._route_is_clean(route_hz, ep_refs):
                        for seg in route_hz:
                            self.add_wire(*seg)
                        return True

                    mid_x = snap_to_grid((x1 + x2) / 2 + jog)
                    route_vz = [(x1, y1, mid_x, y1), (mid_x, y1, mid_x, y2),
                                (mid_x, y2, x2, y2)]
                    if self._route_is_clean(route_vz, ep_refs):
                        for seg in route_vz:
                            self.add_wire(*seg)
                        return True

        # Strategy 5: Offset parallel for blocked straight lines
        if x1 == x2 or y1 == y2:
            for jog_step in range(1, 6):
                for direction in [1, -1]:
                    jog = snap_to_grid(jog_step * grid * direction)
                    if x1 == x2:
                        mx = snap_to_grid(x1 + jog)
                        route = [(x1, y1, mx, y1), (mx, y1, mx, y2), (mx, y2, x2, y2)]
                    else:
                        my = snap_to_grid(y1 + jog)
                        route = [(x1, y1, x1, my), (x1, my, x2, my), (x2, my, x2, y2)]
                    if self._route_is_clean(route, ep_refs):
                        for seg in route:
                            self.add_wire(*seg)
                        return True

        return False

    def _get_pin_outward_direction(self, reference, pin_number):
        """Get the outward-facing direction of a pin in schematic space.

        Returns one of "right", "left", "up", "down".
        """
        comp = None
        for c in self.components:
            if c.reference == reference:
                comp = c
                break
        if comp is None:
            return "right"

        lib_sym = self.lib_symbols.get(comp.lib_id)
        if lib_sym is None:
            return "right"

        pin = None
        for p in lib_sym.pins:
            if p.number == pin_number:
                pin = p
                break
        if pin is None:
            return "right"

        # Outward = pin.rotation + 180 (pin.rotation points inward)
        out_deg = (pin.rotation + 180) % 360
        out_rad = math.radians(out_deg)
        dx_sym = math.cos(out_rad)
        dy_sym = math.sin(out_rad)

        # Y-inversion + component rotation
        comp_rad = math.radians(comp.rotation)
        cos_r = math.cos(comp_rad)
        sin_r = math.sin(comp_rad)
        dx_inv, dy_inv = dx_sym, -dy_sym
        dx_sch = round(dx_inv * cos_r - dy_inv * sin_r)
        dy_sch = round(dx_inv * sin_r + dy_inv * cos_r)

        if dx_sch > 0:
            return "right"
        elif dx_sch < 0:
            return "left"
        elif dy_sch < 0:
            return "up"
        else:
            return "down"

    def vcc_above(self, x, y, rail="VCC", wire_len=5.08):
        """Place a power symbol above (x, y) with a connecting wire."""
        self.add_wire(x, y, x, y - wire_len)
        self.place_power_symbol(rail, x, y - wire_len)

    def vcc_at_pin(self, reference, pin, rail="VCC", wire_len=5.08):
        """Place a power symbol above the specified component pin."""
        x, y = self.get_pin_position(reference, pin)
        self.vcc_above(x, y, rail, wire_len)

    # ─── Phase 2: Subcircuit templates ───────────────────────────────

    def place_series_chain(self, components, direction="down",
                           anchor_x=None, anchor_y=None,
                           gnd_at_end=True, vcc_at_start=False,
                           vcc_rail="VCC", start_label=None, end_label=None,
                           spacing=None):
        """Place a chain of 2-pin components in series with correct rotation.

        Components are oriented so that pin 2 of each component faces
        pin 1 of the next, with straight wires connecting them.

        Args:
            components: List of dicts, each with keys:
                "lib_id": e.g., "Device:R"
                "ref": e.g., "R4"
                "value": e.g., "1k"
                "footprint": optional footprint string
            direction: Chain direction — "down" (default), "up", "right", "left"
            anchor_x, anchor_y: Position of the first component.
                If None, uses suggest_placement.
            gnd_at_end: If True, place a GND symbol after the last component.
            vcc_at_start: If True, place a VCC symbol before the first component.
            vcc_rail: Power rail name for vcc_at_start (default "VCC").
            start_label: If set, place this net label at the chain's input pin.
            end_label: If set, place this net label at the chain's output pin
                       (before GND if gnd_at_end is True).
            spacing: Override spacing between components in mm.
                If None, uses body height + 5.08mm.

        Returns:
            List of placed component references.
        """
        if not components:
            return []

        # Direction → pin constraints and position deltas
        # For a chain going "down": pin 1 should face up (input from above),
        # pin 2 should face down (output toward next component below)
        dir_config = {
            "down":  {"pin1": "up",    "pin2": "down",  "dx": 0,  "dy": 1},
            "up":    {"pin1": "down",  "pin2": "up",    "dx": 0,  "dy": -1},
            "right": {"pin1": "left",  "pin2": "right", "dx": 1,  "dy": 0},
            "left":  {"pin1": "right", "pin2": "left",  "dx": -1, "dy": 0},
        }
        cfg = dir_config.get(direction, dir_config["down"])

        # Determine rotation for 2-pin components in this direction
        pin1_dir = cfg["pin1"]
        pin2_dir = cfg["pin2"]

        # Determine starting position
        if anchor_x is None or anchor_y is None:
            ax, ay = self.suggest_placement(
                components[0]["lib_id"], near_x=anchor_x, near_y=anchor_y
            )
            anchor_x = anchor_x if anchor_x is not None else ax
            anchor_y = anchor_y if anchor_y is not None else ay

        cur_x = snap_to_grid(anchor_x)
        cur_y = snap_to_grid(anchor_y)

        placed_refs = []
        wire_gap = 5.08  # gap between components for wiring

        for i, comp_def in enumerate(components):
            lib_id = comp_def["lib_id"]
            ref = comp_def["ref"]
            value = comp_def["value"]
            fp = comp_def.get("footprint", "")

            # Place the component
            comp = self.place_component(lib_id, ref, value, cur_x, cur_y,
                                        footprint=fp)
            # Auto-rotate to align with chain direction
            self.auto_rotate(ref, {"1": pin1_dir, "2": pin2_dir})
            placed_refs.append(ref)

            # Wire to previous component's pin 2
            if i > 0:
                prev_ref = placed_refs[i - 1]
                self.wire_between(prev_ref, "2", ref, "1")

            # Compute spacing for next component
            if spacing is not None:
                step = spacing
            else:
                # Use body size + wire gap
                lib_sym = self.lib_symbols.get(lib_id)
                if lib_sym:
                    body_size = self._estimate_body_size(lib_id, direction)
                else:
                    body_size = 7.62
                step = body_size + wire_gap

            cur_x += cfg["dx"] * step
            cur_y += cfg["dy"] * step

        # Terminal connections
        first_ref = placed_refs[0]
        last_ref = placed_refs[-1]

        if vcc_at_start:
            p1x, p1y = self.get_pin_position(first_ref, "1")
            if direction in ("down", "up"):
                self.vcc_above(p1x, p1y, vcc_rail)
            else:
                self.vcc_at_pin(first_ref, "1", vcc_rail)

        if start_label:
            self.label_at_pin(first_ref, "1", start_label,
                              direction=pin1_dir)

        if end_label:
            self.label_at_pin(last_ref, "2", end_label,
                              direction=pin2_dir)

        if gnd_at_end:
            p2x, p2y = self.get_pin_position(last_ref, "2")
            self.gnd_below(p2x, p2y)

        return placed_refs

    def _estimate_body_size(self, lib_id, direction):
        """Estimate a component's body size along the chain direction."""
        lib_sym = self.lib_symbols.get(lib_id)
        if not lib_sym:
            return 7.62

        # Use pin positions to estimate size
        if lib_sym.pins:
            if direction in ("down", "up"):
                ys = [p.y for p in lib_sym.pins]
                return abs(max(ys) - min(ys)) if len(ys) > 1 else 5.08
            else:
                xs = [p.x for p in lib_sym.pins]
                return abs(max(xs) - min(xs)) if len(xs) > 1 else 5.08
        return 7.62

    def place_decoupling_cap(self, signal_x, signal_y, cap_ref, cap_value,
                             footprint="", offset_x=0, offset_y=10.16):
        """Place a decoupling capacitor as a T-junction off a signal line.

        The cap is placed vertically: pin 1 connects to the signal wire,
        pin 2 connects to GND below.

        Args:
            signal_x, signal_y: Point on the signal/power bus wire.
            cap_ref: Reference for the capacitor (e.g., "C1").
            cap_value: Value string (e.g., "100nF").
            footprint: KiCad footprint string.
            offset_x: Horizontal offset from signal point (default 0).
            offset_y: Vertical offset for cap placement (default 10.16mm).

        Returns:
            The placed component reference.
        """
        cap_x = snap_to_grid(signal_x + offset_x)
        cap_y = snap_to_grid(signal_y + offset_y)

        comp = self.place_component("Device:C", cap_ref, cap_value,
                                    cap_x, cap_y, footprint=footprint)

        # Ensure cap is vertical: pin 1 up, pin 2 down
        self.auto_rotate(cap_ref, {"1": "up", "2": "down"})

        # Wire from signal point down to cap pin 1
        p1x, p1y = self.get_pin_position(cap_ref, "1")
        self.add_wire(signal_x, signal_y, signal_x, p1y)
        if signal_x != p1x:
            self.add_wire(signal_x, p1y, p1x, p1y)

        # Junction at signal bus if there's a horizontal bus
        self.add_junction(signal_x, signal_y)

        # GND below cap pin 2
        self.gnd_at_pin(cap_ref, "2")

        return cap_ref

    def place_bypass_cap(self, ic_ref, power_pin, cap_ref, cap_value,
                         footprint="", gnd_pin=None):
        """Place a bypass/decoupling cap near an IC's power pin.

        If gnd_pin is given, wires cap pin 2 to that IC GND pin.
        Otherwise, places a GND symbol below.

        Args:
            ic_ref: IC reference (e.g., "U1").
            power_pin: Power pin number on the IC (e.g., "8").
            cap_ref: Reference for the capacitor.
            cap_value: Value string.
            footprint: KiCad footprint string.
            gnd_pin: Optional IC GND pin number. If given, wires cap
                     to the IC's GND pin instead of placing a GND symbol.

        Returns:
            The placed component reference.
        """
        px, py = self.get_pin_position(ic_ref, power_pin)

        # Place cap to the right and below the power pin
        cap_x = snap_to_grid(px + 5.08)
        cap_y = snap_to_grid(py + 7.62)

        comp = self.place_component("Device:C", cap_ref, cap_value,
                                    cap_x, cap_y, footprint=footprint)

        # Orient vertically
        self.auto_rotate(cap_ref, {"1": "up", "2": "down"})

        # Wire power pin to cap pin 1
        self.connect_or_label(ic_ref, power_pin, cap_ref, "1",
                              label_name=f"_PWR_{ic_ref}")

        # GND connection
        if gnd_pin:
            self.connect_or_label(ic_ref, gnd_pin, cap_ref, "2",
                                  label_name="GND")
        else:
            self.gnd_at_pin(cap_ref, "2")

        return cap_ref

    def place_pullup(self, signal_ref, signal_pin, res_ref, res_value,
                     rail="VCC", footprint="",
                     anchor_x=None, anchor_y=None):
        """Place a pull-up resistor between a signal pin and a power rail.

        Resistor is vertical: pin 1 goes to VCC above, pin 2 wires to signal.

        Args:
            signal_ref: Component reference for the signal (e.g., "U1").
            signal_pin: Pin number on the signal component.
            res_ref: Resistor reference (e.g., "R1").
            res_value: Resistor value (e.g., "4.7k").
            rail: Power rail name (default "VCC").
            footprint: KiCad footprint string.
            anchor_x, anchor_y: Override position. If None, places near the signal pin.

        Returns:
            The placed component reference.
        """
        sx, sy = self.get_pin_position(signal_ref, signal_pin)

        if anchor_x is None:
            anchor_x = sx + 5.08
        if anchor_y is None:
            anchor_y = sy - 10.16

        comp = self.place_component("Device:R", res_ref, res_value,
                                    snap_to_grid(anchor_x),
                                    snap_to_grid(anchor_y),
                                    footprint=footprint)

        # Orient: pin 1 up (to VCC), pin 2 down (to signal)
        self.auto_rotate(res_ref, {"1": "up", "2": "down"})

        # VCC above pin 1
        self.vcc_at_pin(res_ref, "1", rail)

        # Wire pin 2 to signal pin
        self.connect_or_label(res_ref, "2", signal_ref, signal_pin,
                              label_name=f"_PU_{res_ref}")

        return res_ref

    def place_pulldown(self, signal_ref, signal_pin, res_ref, res_value,
                       footprint="",
                       anchor_x=None, anchor_y=None):
        """Place a pull-down resistor between a signal pin and GND.

        Resistor is vertical: pin 1 wires to signal, pin 2 goes to GND below.

        Args:
            signal_ref: Component reference for the signal (e.g., "U1").
            signal_pin: Pin number on the signal component.
            res_ref: Resistor reference (e.g., "R1").
            res_value: Resistor value (e.g., "10k").
            footprint: KiCad footprint string.
            anchor_x, anchor_y: Override position.

        Returns:
            The placed component reference.
        """
        sx, sy = self.get_pin_position(signal_ref, signal_pin)

        if anchor_x is None:
            anchor_x = sx + 5.08
        if anchor_y is None:
            anchor_y = sy + 10.16

        comp = self.place_component("Device:R", res_ref, res_value,
                                    snap_to_grid(anchor_x),
                                    snap_to_grid(anchor_y),
                                    footprint=footprint)

        # Orient: pin 1 up (to signal), pin 2 down (to GND)
        self.auto_rotate(res_ref, {"1": "up", "2": "down"})

        # Wire pin 1 to signal pin
        self.connect_or_label(res_ref, "1", signal_ref, signal_pin,
                              label_name=f"_PD_{res_ref}")

        # GND below pin 2
        self.gnd_at_pin(res_ref, "2")

        return res_ref

    def place_voltage_divider(self, r_top_ref, r_top_value,
                              r_bottom_ref, r_bottom_value,
                              tap_label, top_rail="VCC", bottom_gnd=True,
                              anchor_x=None, anchor_y=None,
                              fp_top="", fp_bottom=""):
        """Place a voltage divider: two resistors in series with a tap point.

        Layout:  VCC → R_top (pin1=up, pin2=down) → tap label → R_bottom (pin1=up, pin2=down) → GND

        Args:
            r_top_ref, r_top_value: Top resistor reference and value.
            r_bottom_ref, r_bottom_value: Bottom resistor reference and value.
            tap_label: Net label at the midpoint junction.
            top_rail: Power rail for the top (default "VCC"). Set None to skip.
            bottom_gnd: If True, place GND below bottom resistor.
            anchor_x, anchor_y: Position of top resistor.
            fp_top, fp_bottom: Footprint strings.

        Returns:
            Tuple of (r_top_ref, r_bottom_ref).
        """
        if anchor_x is None:
            anchor_x = self._LAYOUT_ORIGIN_X
        if anchor_y is None:
            anchor_y = self._LAYOUT_ORIGIN_Y

        # Place top resistor
        self.place_component("Device:R", r_top_ref, r_top_value,
                             snap_to_grid(anchor_x), snap_to_grid(anchor_y),
                             footprint=fp_top)
        self.auto_rotate(r_top_ref, {"1": "up", "2": "down"})

        # Place bottom resistor below
        r_top_p2x, r_top_p2y = self.get_pin_position(r_top_ref, "2")
        bottom_y = snap_to_grid(r_top_p2y + 10.16)

        self.place_component("Device:R", r_bottom_ref, r_bottom_value,
                             snap_to_grid(anchor_x), bottom_y,
                             footprint=fp_bottom)
        self.auto_rotate(r_bottom_ref, {"1": "up", "2": "down"})

        # Wire R_top pin 2 to R_bottom pin 1
        self.wire_between(r_top_ref, "2", r_bottom_ref, "1")

        # Tap label at the junction between the two resistors
        r_bot_p1x, r_bot_p1y = self.get_pin_position(r_bottom_ref, "1")
        # Place label stub at the top resistor's pin 2 (the junction point)
        self.add_wire(r_top_p2x, r_top_p2y, r_top_p2x + 5.08, r_top_p2y)
        self.add_label(tap_label, r_top_p2x + 5.08, r_top_p2y)

        # Top rail
        if top_rail:
            self.vcc_at_pin(r_top_ref, "1", top_rail)

        # Bottom GND
        if bottom_gnd:
            self.gnd_at_pin(r_bottom_ref, "2")

        return r_top_ref, r_bottom_ref

    # ─── Pin position calculator ─────────────────────────────────────

    def get_pin_position(self, reference, pin_number):
        """Calculate the absolute position of a pin on a placed component.
        
        Returns (x, y) in schematic coordinates, accounting for component
        position and rotation. This is where wires should connect.
        """
        comp = None
        for c in self.components:
            if c.reference == reference:
                comp = c
                break
        if comp is None:
            raise ValueError(f"Component {reference} not found")
        
        lib_sym = self.lib_symbols.get(comp.lib_id)
        if lib_sym is None:
            raise ValueError(f"Library symbol {comp.lib_id} not found")
        
        pin = None
        for p in lib_sym.pins:
            if p.number == pin_number:
                pin = p
                break
        if pin is None:
            raise ValueError(f"Pin {pin_number} not found on {comp.lib_id}")
        
        # Pin tip position in symbol space
        # The pin extends from (x,y) in the direction of rotation for 'length' mm
        # The connection point (tip) is at (x,y) itself in symbol coords
        rad = math.radians(pin.rotation)
        # Pin endpoint (where the wire connects) = pin position + length in direction
        tip_x = pin.x + pin.length * math.cos(rad)
        tip_y = pin.y + pin.length * math.sin(rad)
        
        # Wait — in KiCad symbol space, pin (at X Y) is the END (connection point)
        # and it extends inward by 'length'. So the tip IS at (pin.x, pin.y).
        tip_x = pin.x
        tip_y = pin.y
        
        # Transform by component rotation (in schematic space, Y is inverted)
        comp_rad = math.radians(comp.rotation)
        cos_r = math.cos(comp_rad)
        sin_r = math.sin(comp_rad)
        
        # In schematic space, Y is inverted relative to symbol space
        # Symbol Y+ is up, Schematic Y+ is down
        sym_x = tip_x
        sym_y = -tip_y  # Y inversion
        
        # Apply rotation
        rot_x = sym_x * cos_r - sym_y * sin_r
        rot_y = sym_x * sin_r + sym_y * cos_r
        
        abs_x = snap_to_grid(comp.x + rot_x)
        abs_y = snap_to_grid(comp.y + rot_y)
        
        return abs_x, abs_y
    
    # ─── Smart wire routing ──────────────────────────────────────────

    def _segment_intersects_body(self, sx1, sy1, sx2, sy2):
        """Check if a wire segment passes through any component body.

        Tests against the body bbox (not envelope) of all placed components.
        Returns True if the segment interior intersects any body.
        Wire endpoints are excluded (they're typically at pin positions
        which are on the body edge).
        """
        for comp in self.components:
            body = self._get_component_body_bbox(comp)
            if self._segment_crosses_rect(sx1, sy1, sx2, sy2, body):
                return True
        return False

    @staticmethod
    def _segment_crosses_rect(sx1, sy1, sx2, sy2, rect):
        """Check if the interior of an axis-aligned segment crosses a rect.

        The segment must be axis-aligned (horizontal or vertical).
        Endpoints are shrunk inward by a small epsilon so that wires
        terminating at a body edge (pin location) are not flagged.
        """
        x_min, y_min, x_max, y_max = rect
        eps = 0.5  # shrink segment ends to avoid false positives at pins

        if sy1 == sy2:
            # Horizontal segment
            y = sy1
            if y < y_min or y > y_max:
                return False
            seg_lo = min(sx1, sx2) + eps
            seg_hi = max(sx1, sx2) - eps
            if seg_lo >= seg_hi:
                return False  # segment too short after shrink
            return seg_hi > x_min and seg_lo < x_max
        elif sx1 == sx2:
            # Vertical segment
            x = sx1
            if x < x_min or x > x_max:
                return False
            seg_lo = min(sy1, sy2) + eps
            seg_hi = max(sy1, sy2) - eps
            if seg_lo >= seg_hi:
                return False
            return seg_hi > y_min and seg_lo < y_max
        else:
            # Diagonal — shouldn't happen in KiCad schematics
            return False

    def _bend_on_existing_wire(self, bx, by):
        """Check if a bend point would land on an existing wire segment.

        A wire must never change direction on top of another wire.
        Returns True if (bx, by) lies on any existing wire's interior.
        """
        bx_r = round(bx, 4)
        by_r = round(by, 4)
        for w in self.wires:
            wx1, wy1 = round(w.x1, 4), round(w.y1, 4)
            wx2, wy2 = round(w.x2, 4), round(w.y2, 4)
            # Check if point is on segment interior (not endpoints)
            if wy1 == wy2 == by_r:
                # Horizontal wire — check if bx is between endpoints
                lo = min(wx1, wx2)
                hi = max(wx1, wx2)
                if lo < bx_r < hi:
                    return True
            elif wx1 == wx2 == bx_r:
                # Vertical wire — check if by is between endpoints
                lo = min(wy1, wy2)
                hi = max(wy1, wy2)
                if lo < by_r < hi:
                    return True
        return False

    def _route_is_clean(self, segments, endpoint_refs=None):
        """Check if a list of wire segments is free of collisions.

        A route is clean if:
        1. No segment interior crosses a component body
        2. No bend point (junction between consecutive segments) sits on
           an existing wire
        3. No bend point falls inside a pin keepout zone (unless it belongs
           to one of the endpoint components)

        Args:
            segments: list of (x1, y1, x2, y2) tuples
            endpoint_refs: optional set/tuple of component references whose
                           keepout zones should be excluded (the wire is
                           allowed to enter its own pins' keepouts).

        Returns True if the route is collision-free.
        """
        for sx1, sy1, sx2, sy2 in segments:
            if self._segment_intersects_body(sx1, sy1, sx2, sy2):
                return False

        # Check bend points (where consecutive segments meet)
        exclude = set(endpoint_refs) if endpoint_refs else set()
        for i in range(len(segments) - 1):
            # The end of segment i = start of segment i+1 = the bend point
            bx, by = segments[i][2], segments[i][3]
            if self._bend_on_existing_wire(bx, by):
                return False
            # Check keepout zones (skip the components being wired)
            for ref, zones in self._pin_keepout_zones.items():
                if ref in exclude:
                    continue
                for rect in zones:
                    if rect[0] <= bx <= rect[2] and rect[1] <= by <= rect[3]:
                        return False

        return True

    def wire_between(self, ref1, pin1, ref2, pin2):
        """Add a wire between two component pins with smart routing.

        Tries multiple routing strategies in order of preference:
        1. Straight line (if pins are axis-aligned)
        2. L-route vertical-then-horizontal
        3. L-route horizontal-then-vertical
        4. Z-route with horizontal jog (for vertical separation)
        5. Z-route with vertical jog (for horizontal separation)
        6. Fallback: L-route vertical-first (always works, may cross things)

        Rules enforced:
        - Wire segments never pass through component bodies
        - Bend points never land on existing wire segments
        - Bend points avoid pin keepout zones of uninvolved components
        - All coordinates are grid-aligned
        """
        x1, y1 = self.get_pin_position(ref1, pin1)
        x2, y2 = self.get_pin_position(ref2, pin2)
        ep_refs = (ref1, ref2)  # endpoint refs — their keepouts are OK

        if x1 == x2 and y1 == y2:
            return  # same point, no wire needed

        # Strategy 1: Straight line
        if x1 == x2 or y1 == y2:
            route = [(x1, y1, x2, y2)]
            if self._route_is_clean(route, ep_refs):
                self.add_wire(x1, y1, x2, y2)
                return

        # Strategy 2: L-route vertical-first (go to y2, then across to x2)
        if x1 != x2 and y1 != y2:
            route_vf = [(x1, y1, x1, y2), (x1, y2, x2, y2)]
            if self._route_is_clean(route_vf, ep_refs):
                self.add_wire(x1, y1, x1, y2)
                self.add_wire(x1, y2, x2, y2)
                return

        # Strategy 3: L-route horizontal-first (go to x2, then down to y2)
        if x1 != x2 and y1 != y2:
            route_hf = [(x1, y1, x2, y1), (x2, y1, x2, y2)]
            if self._route_is_clean(route_hf, ep_refs):
                self.add_wire(x1, y1, x2, y1)
                self.add_wire(x2, y1, x2, y2)
                return

        # Strategy 4: Z-route — try jogs at various offsets
        # A Z-route has 3 segments: out from pin1, jog, into pin2
        grid = 2.54
        if x1 != x2 and y1 != y2:
            for jog_step in range(1, 8):
                for direction in [1, -1]:
                    jog = snap_to_grid(jog_step * grid * direction)

                    # Z-route with horizontal jog:
                    # pin1 → down to mid_y → across to x2 at mid_y → down to pin2
                    mid_y = snap_to_grid((y1 + y2) / 2 + jog)
                    route_hz = [
                        (x1, y1, x1, mid_y),
                        (x1, mid_y, x2, mid_y),
                        (x2, mid_y, x2, y2),
                    ]
                    if self._route_is_clean(route_hz, ep_refs):
                        for seg in route_hz:
                            self.add_wire(*seg)
                        return

                    # Z-route with vertical jog:
                    # pin1 → across to mid_x → down to y2 at mid_x → across to pin2
                    mid_x = snap_to_grid((x1 + x2) / 2 + jog)
                    route_vz = [
                        (x1, y1, mid_x, y1),
                        (mid_x, y1, mid_x, y2),
                        (mid_x, y2, x2, y2),
                    ]
                    if self._route_is_clean(route_vz, ep_refs):
                        for seg in route_vz:
                            self.add_wire(*seg)
                        return

        # Strategy 5 (straight line with obstacles): try offset parallel paths
        if x1 == x2 or y1 == y2:
            for jog_step in range(1, 6):
                for direction in [1, -1]:
                    jog = snap_to_grid(jog_step * grid * direction)
                    if x1 == x2:
                        # Vertical line blocked — jog out and back
                        mx = snap_to_grid(x1 + jog)
                        route = [
                            (x1, y1, mx, y1),
                            (mx, y1, mx, y2),
                            (mx, y2, x2, y2),
                        ]
                    else:
                        # Horizontal line blocked — jog out and back
                        my = snap_to_grid(y1 + jog)
                        route = [
                            (x1, y1, x1, my),
                            (x1, my, x2, my),
                            (x2, my, x2, y2),
                        ]
                    if self._route_is_clean(route, ep_refs):
                        for seg in route:
                            self.add_wire(*seg)
                        return

        # Fallback: L-route vertical-first (may cross things, but at least connects)
        if x1 == x2 or y1 == y2:
            self.add_wire(x1, y1, x2, y2)
        else:
            self.add_wire(x1, y1, x1, y2)
            self.add_wire(x1, y2, x2, y2)
    
    # ─── Serialization ───────────────────────────────────────────────
    
    def _render_lib_symbol(self, sym: LibSymbol) -> str:
        """Render a lib_symbols entry."""
        name = sym.lib_id.split(':')[-1] if ':' in sym.lib_id else sym.lib_id
        full_id = sym.lib_id.replace(':', ':') 
        
        lines = []
        lines.append(f'    (symbol "{full_id}"')
        
        if sym.is_power:
            lines.append(f'      (power)')
        
        if sym.pin_numbers_hide:
            lines.append(f'      (pin_numbers hide)')
        
        pn_line = '      (pin_names'
        if sym.pin_names_offset is not None:
            pn_line += f' (offset {fmt(sym.pin_names_offset)})'
        if sym.pin_names_hide:
            pn_line += ' hide'
        pn_line += ')'
        lines.append(pn_line)
        
        bom = "yes" if not sym.is_power else "no"
        board = "yes" if not sym.is_power else "no"
        lines.append(f'      (in_bom {bom})')
        lines.append(f'      (on_board {board})')
        
        # Properties
        prop_positions = {
            "Reference": (2.54, 0, 0),
            "Value": (-2.54, 0, 0),
            "Footprint": (0, 0, 0),
            "Datasheet": (0, 0, 0),
        }
        for key, val in sym.properties.items():
            px, py, pr = prop_positions.get(key, (0, 0, 0))
            hide = " hide" if key in ("Footprint", "Datasheet") else ""
            if sym.is_power and key == "Reference":
                hide = " hide"
            lines.append(f'      (property "{key}" "{val}"')
            lines.append(f'        (at {fmt(px)} {fmt(py)} {fmt(pr)})')
            lines.append(f'        (effects (font (size 1.27 1.27)){hide})')
            lines.append(f'      )')
        
        # Graphics
        if sym.graphics_sexpr:
            lines.append(sym.graphics_sexpr)
        
        # Pins (in a _1_1 sub-symbol)
        lines.append(f'      (symbol "{name}_1_1"')
        for pin in sym.pins:
            rot_deg = pin.rotation
            lines.append(f'        (pin {pin.pin_type} line')
            lines.append(f'          (at {fmt(pin.x)} {fmt(pin.y)} {rot_deg})')
            lines.append(f'          (length {fmt(pin.length)})')
            lines.append(f'          (name "{pin.name}" (effects (font (size 1.27 1.27))))')
            lines.append(f'          (number "{pin.number}" (effects (font (size 1.27 1.27))))')
            lines.append(f'        )')
        lines.append(f'      )')
        
        lines.append(f'    )')
        return '\n'.join(lines)
    
    def _render_placed_component(self, comp: PlacedComponent) -> str:
        """Render a placed symbol instance."""
        lines = []
        uid = _uuid()

        bom = "yes" if comp.in_bom else "no"
        board = "yes" if comp.on_board else "no"

        lines.append(f'  (symbol')
        lines.append(f'    (lib_id "{comp.lib_id}")')
        lines.append(f'    (at {fmt(comp.x)} {fmt(comp.y)} {fmt(comp.rotation)})')
        lines.append(f'    (unit {comp.unit})')
        lines.append(f'    (in_bom {bom})')
        lines.append(f'    (on_board {board})')
        lines.append(f'    (dnp {"yes" if comp.dnp else "no"})')
        lines.append(f'    (uuid "{uid}")')

        # Properties — use precomputed label positions if available
        if comp.reference in self._label_positions:
            ref_x, ref_y, val_x, val_y = self._label_positions[comp.reference]
        else:
            # Fallback for power symbols or if compute wasn't called
            ref_x = comp.x + 2.54
            ref_y = comp.y
            val_x = comp.x - 2.54
            val_y = comp.y
        
        # Power symbols: hide both Reference (#PWR) and Value (redundant with symbol graphic)
        is_power = comp.reference.startswith("#PWR")
        ref_hide = " hide" if is_power else ""
        val_hide = " hide" if is_power else ""

        lines.append(f'    (property "Reference" "{comp.reference}"')
        lines.append(f'      (at {fmt(ref_x)} {fmt(ref_y)} 0)')
        lines.append(f'      (effects (font (size 1.27 1.27)){ref_hide})')
        lines.append(f'    )')
        lines.append(f'    (property "Value" "{comp.value}"')
        lines.append(f'      (at {fmt(val_x)} {fmt(val_y)} 0)')
        lines.append(f'      (effects (font (size 1.27 1.27)){val_hide})')
        lines.append(f'    )')
        
        hide = " hide" if comp.footprint else ""
        lines.append(f'    (property "Footprint" "{comp.footprint}"')
        lines.append(f'      (at {fmt(comp.x)} {fmt(comp.y)} 0)')
        lines.append(f'      (effects (font (size 1.27 1.27)) hide)')
        lines.append(f'    )')
        lines.append(f'    (property "Datasheet" "{comp.datasheet}"')
        lines.append(f'      (at {fmt(comp.x)} {fmt(comp.y)} 0)')
        lines.append(f'      (effects (font (size 1.27 1.27)) hide)')
        lines.append(f'    )')
        
        # Extra properties
        for key, val in comp.extra_properties.items():
            lines.append(f'    (property "{key}" "{val}"')
            lines.append(f'      (at {fmt(comp.x)} {fmt(comp.y)} 0)')
            lines.append(f'      (effects (font (size 1.27 1.27)) hide)')
            lines.append(f'    )')
        
        # Pin UUIDs
        for pin_num, pin_uuid in comp.pin_uuids.items():
            lines.append(f'    (pin "{pin_num}" (uuid "{pin_uuid}"))')
        
        # Instances
        lines.append(f'    (instances')
        lines.append(f'      (project ""')
        lines.append(f'        (path "/{self.root_uuid}/"')
        lines.append(f'          (reference "{comp.reference}")')
        lines.append(f'          (unit {comp.unit})')
        lines.append(f'        )')
        lines.append(f'      )')
        lines.append(f'    )')
        lines.append(f'  )')
        
        return '\n'.join(lines)
    
    def _render_sheet(self, sheet: Sheet, page=2) -> str:
        """Render a hierarchical sheet symbol with its pins."""
        lines = []
        lines.append(f'  (sheet')
        lines.append(f'    (at {fmt(sheet.x)} {fmt(sheet.y)})')
        lines.append(f'    (size {fmt(sheet.width)} {fmt(sheet.height)})')
        lines.append(f'    (stroke (width 0.1524) (type solid))')
        lines.append(f'    (fill (color 0 0 0 0.0000))')
        lines.append(f'    (uuid "{sheet.uuid}")')
        lines.append(f'    (property "Sheetname" "{sheet.name}"')
        lines.append(f'      (at {fmt(sheet.x)} {fmt(sheet.y - 0.7)} 0)')
        lines.append(f'      (effects (font (size 1.27 1.27)) (justify left bottom))')
        lines.append(f'    )')
        lines.append(f'    (property "Sheetfile" "{sheet.filename}"')
        lines.append(f'      (at {fmt(sheet.x)} {fmt(sheet.y + sheet.height + 0.7)} 0)')
        lines.append(f'      (effects (font (size 1.27 1.27)) (justify left top) hide)')
        lines.append(f'    )')
        for pin in sheet.pins:
            lines.append(f'    (pin "{pin.name}" {pin.shape}')
            lines.append(f'      (at {fmt(pin.x)} {fmt(pin.y)} {fmt(pin.rotation)})')
            lines.append(f'      (effects (font (size 1.27 1.27)) (justify right))')
            lines.append(f'      (uuid "{_uuid()}")')
            lines.append(f'    )')
        lines.append(f'    (instances')
        lines.append(f'      (project ""')
        lines.append(f'        (path "/{self.root_uuid}"')
        lines.append(f'          (page "{page}")')
        lines.append(f'        )')
        lines.append(f'      )')
        lines.append(f'    )')
        lines.append(f'  )')
        return '\n'.join(lines)

    def save(self, filepath):
        """Write the complete .kicad_sch file."""
        # Pre-compute label positions with collision avoidance
        self._compute_all_label_positions()

        # Remove overlapping/parallel wire segments
        self._deduplicate_wires()

        lines = []

        # Header
        lines.append(f'(kicad_sch')
        lines.append(f'  (version 20230121)')
        lines.append(f'  (generator "kicad_schematic_gen_skill")')
        lines.append(f'  (generator_version "1.0")')
        lines.append(f'  (uuid "{self.root_uuid}")')
        lines.append(f'  (paper "A4")')
        lines.append(f'')
        
        # Title block
        lines.append(f'  (title_block')
        lines.append(f'    (title "{self.title}")')
        lines.append(f'    (date "{self.date}")')
        lines.append(f'    (rev "{self.rev}")')
        lines.append(f'  )')
        lines.append(f'')
        
        # Lib symbols
        lines.append(f'  (lib_symbols')
        for sym in self.lib_symbols.values():
            lines.append(self._render_lib_symbol(sym))
        lines.append(f'  )')
        lines.append(f'')
        
        # Junctions
        for j in self.junctions:
            lines.append(f'  (junction (at {fmt(j.x)} {fmt(j.y)}) (diameter 0) (color 0 0 0 0) (uuid "{_uuid()}"))')
        
        # No connects
        for nc in self.no_connects:
            lines.append(f'  (no_connect (at {fmt(nc.x)} {fmt(nc.y)}) (uuid "{_uuid()}"))')
        
        # Wires
        for w in self.wires:
            lines.append(f'  (wire')
            lines.append(f'    (pts (xy {fmt(w.x1)} {fmt(w.y1)}) (xy {fmt(w.x2)} {fmt(w.y2)}))')
            lines.append(f'    (stroke (width 0) (type default))')
            lines.append(f'    (uuid "{_uuid()}")')
            lines.append(f'  )')
        
        # Labels
        for lbl in self.labels:
            lines.append(f'  (label "{lbl.text}"')
            lines.append(f'    (at {fmt(lbl.x)} {fmt(lbl.y)} {fmt(lbl.rotation)})')
            lines.append(f'    (effects (font (size 1.27 1.27)))')
            lines.append(f'    (uuid "{_uuid()}")')
            lines.append(f'  )')
        
        # Global labels
        for gl in self.global_labels:
            lines.append(f'  (global_label "{gl.text}"')
            lines.append(f'    (shape {gl.shape})')
            lines.append(f'    (at {fmt(gl.x)} {fmt(gl.y)} {fmt(gl.rotation)})')
            lines.append(f'    (effects (font (size 1.27 1.27)))')
            lines.append(f'    (uuid "{_uuid()}")')
            lines.append(f'    (property "Intersheetrefs" "${{INTERSHEET_REFS}}"')
            lines.append(f'      (at {fmt(gl.x)} {fmt(gl.y)} 0)')
            lines.append(f'      (effects (font (size 1.27 1.27)) hide)')
            lines.append(f'    )')
            lines.append(f'  )')

        # Hierarchical labels (sheet ports — the block interface)
        for hl in self.hierarchical_labels:
            lines.append(f'  (hierarchical_label "{hl.text}"')
            lines.append(f'    (shape {hl.shape})')
            lines.append(f'    (at {fmt(hl.x)} {fmt(hl.y)} {fmt(hl.rotation)})')
            lines.append(f'    (effects (font (size 1.27 1.27)))')
            lines.append(f'    (uuid "{_uuid()}")')
            lines.append(f'  )')

        lines.append(f'')

        # Placed components
        for comp in self.components:
            lines.append(self._render_placed_component(comp))
            lines.append(f'')

        # Hierarchical sheet symbols (block instances — ROADMAP W1b)
        for i, sheet in enumerate(self.sheets):
            lines.append(self._render_sheet(sheet, page=i + 2))
            lines.append(f'')

        # Sheet instances (required)
        lines.append(f'  (sheet_instances')
        lines.append(f'    (path "/{self.root_uuid}/"')
        lines.append(f'      (page "1")')
        lines.append(f'    )')
        lines.append(f'  )')
        lines.append(f')')
        
        with open(filepath, 'w') as f:
            f.write('\n'.join(lines))
        
        return filepath


# ─── Convenience: quick test ─────────────────────────────────────────
if __name__ == "__main__":
    sch = KicadSchematic("Test Board", rev="0.1")
    
    # Add symbols
    sch.add_lib_symbol_resistor()
    sch.add_lib_symbol_capacitor()
    sch.add_lib_symbol_power("GND")
    sch.add_lib_symbol_power("VCC")
    
    # Place components
    sch.place_component("Device:R", "R1", "10k", x=100, y=80,
                        footprint="Resistor_SMD:R_0603_1608Metric")
    sch.place_component("Device:C", "C1", "100nF", x=120, y=80,
                        footprint="Capacitor_SMD:C_0603_1608Metric")
    
    # Power symbols
    sch.place_power_symbol("VCC", 100, 68)
    sch.place_power_symbol("GND", 100, 92)
    sch.place_power_symbol("GND", 120, 90)
    
    # Wires
    sch.add_wire(100, 74.93, 100, 68)   # R1 pin 1 to VCC
    sch.add_wire(100, 85.09, 100, 92)   # R1 pin 2 to GND
    sch.add_wire(120, 77.47, 120, 68)   # C1 pin 1 up
    sch.add_wire(120, 82.55, 120, 90)   # C1 pin 2 to GND
    
    # Labels
    sch.add_label("NET1", 110, 68)
    
    sch.save("/tmp/test_board.kicad_sch")
    print("Saved to /tmp/test_board.kicad_sch")
