#!/usr/bin/env python3
"""
KiCad Schematic Validator — validates netlists and connectivity of .kicad_sch files.

Parses .kicad_sch files (or accepts in-memory KicadSchematic objects), builds a
connectivity graph from pin positions, wire endpoints, labels, junctions, and
no-connects, then runs a suite of checks to catch wiring errors.

CLI Usage (for validating .kicad_sch files):
    python validate_kicad_sch.py <path/to/schematic.kicad_sch>
    python validate_kicad_sch.py <path/to/schematic.kicad_sch> --json
    python validate_kicad_sch.py <path/to/schematic.kicad_sch> --netlist
    python validate_kicad_sch.py <path/to/schematic.kicad_sch> --json --netlist

Output Formats:
    Default:    Human-readable report with structured sections
    --json:     Machine-readable JSON (for Claude Code / automation)
    --netlist:  Include full netlist in output

Python API Usage (for in-memory validation during generation):
    from generate_kicad_sch import KicadSchematic
    from validate_kicad_sch import validate, extract_netlist, assert_connected

    sch = KicadSchematic("My Board")
    # ... build schematic ...
    result = validate(sch)
    # or: result = validate_file("output.kicad_sch")

Skill Integration:
    The skill should call this after generating a schematic:

        import subprocess, json
        result = subprocess.run(
            ["python", "validate_kicad_sch.py", output_path, "--json"],
            capture_output=True, text=True
        )
        report = json.loads(result.stdout)
        if not report["passed"]:
            # fix issues and regenerate
"""

import sys
import os
import json as json_module
import re
from dataclasses import dataclass, field
from typing import Optional

# Ensure this script's directory is on the path so sibling imports work
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from generate_kicad_sch import (
    KicadSchematic, Pin, LibSymbol, PlacedComponent, Wire, Label,
    GlobalLabel, HierarchicalLabel, Junction, NoConnect, snap_to_grid,
    Sheet, SheetPin,
)


# ─── S-expression parser ────────────────────────────────────────────

def _tokenize_sexpr(text):
    """Tokenize KiCad S-expression text into a list of tokens."""
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in ' \t\n\r':
            i += 1
        elif c == '(':
            tokens.append('(')
            i += 1
        elif c == ')':
            tokens.append(')')
            i += 1
        elif c == '"':
            # Quoted string
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == '\\':
                    j += 1  # skip escaped char
                j += 1
            tokens.append(text[i+1:j])
            i = j + 1
        else:
            # Unquoted atom
            j = i
            while j < n and text[j] not in ' \t\n\r()':
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


def _parse_sexpr(tokens, pos=0):
    """Parse tokens into nested lists. Returns (parsed, next_pos)."""
    if tokens[pos] == '(':
        result = []
        pos += 1
        while pos < len(tokens) and tokens[pos] != ')':
            item, pos = _parse_sexpr(tokens, pos)
            result.append(item)
        pos += 1  # skip ')'
        return result, pos
    else:
        return tokens[pos], pos + 1


def _parse_file(text):
    """Parse full S-expression text into a tree."""
    tokens = _tokenize_sexpr(text)
    tree, _ = _parse_sexpr(tokens)
    return tree


def _find_nodes(tree, tag):
    """Find all child nodes with given tag in a parsed S-expression list."""
    results = []
    for item in tree:
        if isinstance(item, list) and len(item) > 0 and item[0] == tag:
            results.append(item)
    return results


def _find_node(tree, tag):
    """Find first child node with given tag."""
    nodes = _find_nodes(tree, tag)
    return nodes[0] if nodes else None


def _get_atom(tree, tag, default=None):
    """Get the first atom value after a tag: (tag value) -> value."""
    node = _find_node(tree, tag)
    if node and len(node) > 1:
        return node[1]
    return default


def _get_float(tree, tag, default=0.0):
    """Get a float value from (tag value)."""
    val = _get_atom(tree, tag, default)
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _parse_at(node):
    """Parse (at X Y [rotation]) -> (x, y, rotation)."""
    at = _find_node(node, 'at')
    if at and len(at) >= 3:
        x = float(at[1])
        y = float(at[2])
        rot = float(at[3]) if len(at) > 3 else 0.0
        return x, y, rot
    return 0, 0, 0


def _parse_property(prop_node):
    """Parse (property "key" "value" ...) -> (key, value)."""
    if len(prop_node) >= 3:
        return prop_node[1], prop_node[2]
    return None, None


def _resolve_active_reference(comp_node, root_uuid, fallback_ref):
    """Resolve a placed symbol's reference from its (instances ...) block.

    KiCad 8 stores the authoritative reference per project/sheet-path inside
        (instances (project NAME (path P (reference R) (unit U))))
    The (property "Reference") field is only a cached display value and can go
    STALE after re-annotation, merges, or project renames. Sheets that were
    merged or copied (e.g. pulling in a block) often carry a LEFTOVER instance
    under an empty project name "" holding the OLD reference, while the live
    reference lives under the active project. Reading the property (or that
    stale instance) yields wrong and even apparently-"duplicate" refs, so
    resolve against the active project here — this is the value KiCad displays.

    Preference order:
      1. instance whose path is the root sheet ("/<root_uuid>") — authoritative
      2. instance under any non-empty project name
      3. the property "Reference" fallback (old format with no instances block)
    """
    instances = _find_node(comp_node, 'instances')
    if not instances:
        return fallback_ref
    root_path = "/" + root_uuid if root_uuid else None
    named_ref = None
    for project in _find_nodes(instances, 'project'):
        proj_name = project[1] if len(project) > 1 and isinstance(project[1], str) else ""
        for path in _find_nodes(project, 'path'):
            path_str = path[1] if len(path) > 1 else ""
            ref = _get_atom(path, 'reference')
            if ref is None:
                continue
            if root_path and path_str == root_path:
                return ref  # exact root-sheet placement wins
            if proj_name and named_ref is None:
                named_ref = ref  # first non-empty-project instance
    return named_ref if named_ref is not None else fallback_ref


# ─── .kicad_sch file loader ─────────────────────────────────────────

def _parse_lib_symbol_node(sym_node):
    """Parse one ``(symbol "…" …)`` definition node into (lib_id, LibSymbol).

    Shared by the embedded-cache parse in load_kicad_sch and the library
    fallback (which parses a block fetched from an installed library).
    """
    lib_id = sym_node[1] if len(sym_node) > 1 else ""

    is_power = _find_node(sym_node, 'power') is not None

    # Properties
    props = {}
    for prop in _find_nodes(sym_node, 'property'):
        key, val = _parse_property(prop)
        if key:
            props[key] = val

    # Pin names offset
    pn_node = _find_node(sym_node, 'pin_names')
    pin_names_offset = 1.016
    pin_names_hide = False
    if pn_node:
        offset_node = _find_node(pn_node, 'offset')
        if offset_node and len(offset_node) > 1:
            pin_names_offset = float(offset_node[1])
        pin_names_hide = 'hide' in pn_node

    pin_numbers_hide = _find_node(sym_node, 'pin_numbers') is not None and \
                       'hide' in (_find_node(sym_node, 'pin_numbers') or [])

    # Parse pins from sub-symbol nodes (like "R_1_1")
    pins = []
    for sub_sym in _find_nodes(sym_node, 'symbol'):
        for pin_node in _find_nodes(sub_sym, 'pin'):
            if len(pin_node) >= 3:
                pin_type = pin_node[1]  # passive, power_in, etc.
                # pin_style = pin_node[2]  # line, etc.

                px, py, prot = _parse_at(pin_node)
                length = _get_float(pin_node, 'length', 2.54)

                name_node = _find_node(pin_node, 'name')
                pin_name = name_node[1] if name_node and len(name_node) > 1 else "~"

                num_node = _find_node(pin_node, 'number')
                pin_number = num_node[1] if num_node and len(num_node) > 1 else "?"

                pins.append(Pin(
                    number=pin_number, name=pin_name,
                    pin_type=pin_type,
                    x=px, y=py, length=length,
                    rotation=int(prot),
                ))

    sym = LibSymbol(
        lib_id=lib_id,
        properties=props,
        pins=pins,
        is_power=is_power,
        pin_names_offset=pin_names_offset,
        pin_names_hide=pin_names_hide,
        pin_numbers_hide=pin_numbers_hide,
    )
    return lib_id, sym


def _resolve_missing_lib_symbols(sch, project_dir=None, extra_sym=None):
    """Library fallback for a stale embedded lib_symbols cache.

    KiCad tolerates a placed symbol whose lib_id is missing from the file's
    embedded cache by falling back to the installed libraries; without this,
    the loader drops the component's pins and every wire touching them
    cascades into false dangling/disconnected/floating errors (observed:
    1 stale symbol → 14 false errors). Mirror KiCad: resolve the symbol from
    the registered libraries (built-in + user + project + explicit --sym-lib)
    and record a warning on ``sch.stale_lib_cache`` — the file should still be
    re-saved in KiCad to refresh its cache.
    """
    sch.stale_lib_cache = getattr(sch, 'stale_lib_cache', [])
    sch.unresolved_lib_ids = getattr(sch, 'unresolved_lib_ids', [])
    missing = sorted({c.lib_id for c in sch.components
                      if c.lib_id and c.lib_id not in sch.lib_symbols})
    if not missing:
        return

    try:
        from check_kicad_library import build_library_set, load_symbol_block
        libraries = build_library_set(project_dir=project_dir, extra_sym=extra_sym)
    except Exception:
        sch.unresolved_lib_ids.extend(missing)
        return

    for lib_id in missing:
        block = None
        try:
            block = load_symbol_block(lib_id, libraries=libraries)
        except Exception:
            block = None
        if not block:
            sch.unresolved_lib_ids.append(lib_id)
            continue
        _, sym = _parse_lib_symbol_node(_parse_file(block))
        # A library file names the symbol without a nickname — key the cache
        # by the lib_id the placed instances actually reference.
        sym.lib_id = lib_id
        sch.lib_symbols[lib_id] = sym
        sch.stale_lib_cache.append(lib_id)


def load_kicad_sch(filepath, resolve_from_libraries=True,
                   project_dir=None, extra_sym=None,
                   load_children=True, _seen=None):
    """Parse a .kicad_sch file into a KicadSchematic object.

    This reads the S-expression file and reconstructs the in-memory
    representation needed by the validator. Not all fields are preserved
    (graphics, UUIDs, etc.) — only what's needed for validation.

    If a placed symbol's lib_id is missing from the file's embedded
    lib_symbols cache (stale cache — cross-project paste, hand-edit), the
    symbol is resolved from the installed KiCad libraries like KiCad itself
    does (``resolve_from_libraries=True``), recording a `stale_lib_cache`
    warning instead of dropping the component. Pass ``project_dir`` /
    ``extra_sym`` ("[NICK=]PATH" specs) to search project/explicit libraries.

    Hierarchical sheets (ROADMAP W1b): each ``(sheet)`` node is parsed into
    ``sch.sheets``, and its child file (Sheetfile, relative to this file's
    directory) is loaded recursively into ``Sheet.child``. A missing child
    file leaves ``child`` None (the ``sheet_integrity`` check errors on it);
    a sheet-file cycle raises ValueError. ``load_children=False`` restores
    the flat, single-file parse.
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        text = f.read()

    tree = _parse_file(text)

    # Root sheet UUID — used to resolve the active per-project instance reference
    root_uuid = _get_atom(tree, 'uuid')

    # Title block
    title_block = _find_node(tree, 'title_block')
    title = _get_atom(title_block, 'title', 'Untitled') if title_block else 'Untitled'
    date = _get_atom(title_block, 'date', '') if title_block else ''
    rev = _get_atom(title_block, 'rev', '') if title_block else ''

    sch = KicadSchematic(title=title, date=date, rev=rev)

    # ── Parse lib_symbols ──
    lib_symbols_node = _find_node(tree, 'lib_symbols')
    if lib_symbols_node:
        for sym_node in _find_nodes(lib_symbols_node, 'symbol'):
            lib_id, sym = _parse_lib_symbol_node(sym_node)
            sch.lib_symbols[lib_id] = sym

    # ── Parse placed components (symbol nodes at top level) ──
    for comp_node in _find_nodes(tree, 'symbol'):
        lib_id = _get_atom(comp_node, 'lib_id', '')
        if not lib_id:
            continue  # Skip if no lib_id (it's a lib_symbols sub-symbol)

        cx, cy, crot = _parse_at(comp_node)
        unit = int(_get_atom(comp_node, 'unit', '1'))

        # Properties
        ref = ""
        value = ""
        footprint = ""
        datasheet = "~"
        extra_props = {}
        for prop in _find_nodes(comp_node, 'property'):
            key, val = _parse_property(prop)
            if key == "Reference":
                ref = val
            elif key == "Value":
                value = val
            elif key == "Footprint":
                footprint = val
            elif key == "Datasheet":
                datasheet = val
            elif key:
                extra_props[key] = val

        # The property "Reference" is a stale cache; the authoritative reference
        # lives in the (instances) block under the active project. Resolve it so
        # re-annotated / merged schematics report the refs KiCad actually uses.
        ref = _resolve_active_reference(comp_node, root_uuid, ref)

        in_bom = _get_atom(comp_node, 'in_bom', 'yes') == 'yes'
        on_board = _get_atom(comp_node, 'on_board', 'yes') == 'yes'

        # Pin UUIDs
        pin_uuids = {}
        for pin_ref in _find_nodes(comp_node, 'pin'):
            if len(pin_ref) >= 2:
                pin_num = pin_ref[1]
                uuid_node = _find_node(pin_ref, 'uuid')
                if uuid_node and len(uuid_node) > 1:
                    pin_uuids[pin_num] = uuid_node[1]

        comp = PlacedComponent(
            lib_id=lib_id, reference=ref, value=value,
            x=cx, y=cy, rotation=crot,
            footprint=footprint, datasheet=datasheet,
            unit=unit, in_bom=in_bom, on_board=on_board,
            extra_properties=extra_props,
            pin_uuids=pin_uuids,
        )
        sch.components.append(comp)

    # ── Parse wires ──
    for wire_node in _find_nodes(tree, 'wire'):
        pts_node = _find_node(wire_node, 'pts')
        if pts_node:
            xy_nodes = _find_nodes(pts_node, 'xy')
            if len(xy_nodes) >= 2:
                x1, y1 = float(xy_nodes[0][1]), float(xy_nodes[0][2])
                x2, y2 = float(xy_nodes[1][1]), float(xy_nodes[1][2])
                sch.wires.append(Wire(x1, y1, x2, y2))

    # ── Parse labels ──
    for lbl_node in _find_nodes(tree, 'label'):
        text = lbl_node[1] if len(lbl_node) > 1 else ""
        lx, ly, lrot = _parse_at(lbl_node)
        sch.labels.append(Label(text, lx, ly, lrot))

    # ── Parse global labels ──
    for gl_node in _find_nodes(tree, 'global_label'):
        text = gl_node[1] if len(gl_node) > 1 else ""
        gx, gy, grot = _parse_at(gl_node)
        shape = _get_atom(gl_node, 'shape', 'bidirectional')
        sch.global_labels.append(GlobalLabel(text, gx, gy, shape, grot))

    # ── Parse hierarchical labels (sheet ports) ──
    for hl_node in _find_nodes(tree, 'hierarchical_label'):
        text = hl_node[1] if len(hl_node) > 1 else ""
        hx, hy, hrot = _parse_at(hl_node)
        shape = _get_atom(hl_node, 'shape', 'bidirectional')
        sch.hierarchical_labels.append(
            HierarchicalLabel(text, hx, hy, shape, hrot))

    # ── Parse junctions ──
    for j_node in _find_nodes(tree, 'junction'):
        jx, jy, _ = _parse_at(j_node)
        sch.junctions.append(Junction(jx, jy))

    # ── Parse no-connects ──
    for nc_node in _find_nodes(tree, 'no_connect'):
        nx, ny, _ = _parse_at(nc_node)
        sch.no_connects.append(NoConnect(nx, ny))

    # ── Parse hierarchical sheets (and load their child files) ──
    sch.missing_sheet_files = []
    this_file = os.path.abspath(filepath)
    seen = set(_seen or ()) | {this_file}
    for sheet_node in _find_nodes(tree, 'sheet'):
        sx, sy, _ = _parse_at(sheet_node)
        size_node = _find_node(sheet_node, 'size')
        sw = float(size_node[1]) if size_node and len(size_node) > 2 else 0.0
        sh = float(size_node[2]) if size_node and len(size_node) > 2 else 0.0
        sheet_name, sheet_file = "", ""
        for prop in _find_nodes(sheet_node, 'property'):
            key, val = _parse_property(prop)
            if key in ("Sheetname", "Sheet name"):
                sheet_name = val
            elif key in ("Sheetfile", "Sheet file"):
                sheet_file = val
        pins = []
        for pin_node in _find_nodes(sheet_node, 'pin'):
            pname = pin_node[1] if len(pin_node) > 1 else ""
            pshape = pin_node[2] if len(pin_node) > 2 and \
                isinstance(pin_node[2], str) else "bidirectional"
            px, py, prot = _parse_at(pin_node)
            pins.append(SheetPin(name=pname, shape=pshape,
                                 x=px, y=py, rotation=prot))
        sheet = Sheet(name=sheet_name, filename=sheet_file,
                      x=sx, y=sy, width=sw, height=sh, pins=pins,
                      uuid=_get_atom(sheet_node, 'uuid', ''))
        if load_children and sheet_file:
            child_path = os.path.normpath(
                os.path.join(os.path.dirname(this_file), sheet_file))
            if os.path.abspath(child_path) in seen:
                raise ValueError(
                    f"sheet-file cycle: '{sheet_file}' (from {filepath}) "
                    f"recurses into an ancestor sheet")
            if os.path.isfile(child_path):
                sheet.child = load_kicad_sch(
                    child_path, resolve_from_libraries=resolve_from_libraries,
                    project_dir=project_dir, extra_sym=extra_sym,
                    load_children=True, _seen=seen)
            else:
                sch.missing_sheet_files.append(sheet_file)
        sch.sheets.append(sheet)

    # ── Library fallback for lib_ids missing from the embedded cache ──
    if resolve_from_libraries:
        _resolve_missing_lib_symbols(sch, project_dir=project_dir,
                                     extra_sym=extra_sym)

    return sch


def iter_all_components(sch, _prefix=""):
    """Yield (component, lib_symbol, sheet_prefix) across the hierarchy.

    ``sheet_prefix`` is "" for the root and "instance/" (nested:
    "a/b/") for components inside hierarchical sheets. The shared iteration
    for every consumer that reasons about components regardless of which
    sheet they live on (verify_netlist, cross_check_bom, BOM extraction).
    """
    for comp in sch.components:
        yield comp, sch.lib_symbols.get(comp.lib_id), _prefix
    for sheet in getattr(sch, 'sheets', []):
        if sheet.child is not None:
            yield from iter_all_components(sheet.child,
                                           _prefix + sheet.name + "/")


# ─── Coordinate key helper ──────────────────────────────────────────

def _coord_key(x, y):
    """Canonical coordinate tuple, rounded to avoid float drift."""
    return (round(x, 4), round(y, 4))


# ─── Union-Find ─────────────────────────────────────────────────────

class UnionFind:
    """Disjoint-set data structure keyed on (x, y) coordinate tuples."""

    def __init__(self):
        self._parent = {}
        self._rank = {}

    def _ensure(self, key):
        if key not in self._parent:
            self._parent[key] = key
            self._rank[key] = 0

    def find(self, key):
        self._ensure(key)
        while self._parent[key] != key:
            self._parent[key] = self._parent[self._parent[key]]
            key = self._parent[key]
        return key

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1

    def connected(self, a, b):
        return self.find(a) == self.find(b)

    def all_keys(self):
        return set(self._parent.keys())

    def groups(self):
        """Return dict mapping root -> set of all members."""
        result = {}
        for key in self._parent:
            root = self.find(key)
            result.setdefault(root, set()).add(key)
        return result


# ─── Data structures ────────────────────────────────────────────────

@dataclass
class NetlistEntry:
    name: str
    pins: set  # set of (reference, pin_number)
    has_label: bool = False
    is_power: bool = False
    # Hierarchy bookkeeping (ROADMAP W1b):
    hlabel_names: set = field(default_factory=set)  # hierarchical labels on this net
    global_names: set = field(default_factory=set)  # global labels on this net
    from_sheet: bool = False   # net merged in from a child sheet (not root geometry)


@dataclass
class Netlist:
    nets: dict = field(default_factory=dict)  # name -> NetlistEntry

    def get_net_for_pin(self, reference: str, pin_number: str) -> Optional[str]:
        for name, entry in self.nets.items():
            if (reference, pin_number) in entry.pins:
                return name
        return None

    def get_pins_on_net(self, net_name: str) -> set:
        entry = self.nets.get(net_name)
        return entry.pins if entry else set()

    def are_connected(self, ref1: str, pin1: str, ref2: str, pin2: str) -> bool:
        net1 = self.get_net_for_pin(ref1, pin1)
        net2 = self.get_net_for_pin(ref2, pin2)
        return net1 is not None and net1 == net2


@dataclass
class ValidationIssue:
    severity: str  # "error", "warning"
    check_name: str
    message: str
    references: list = field(default_factory=list)
    coordinates: Optional[tuple] = None


@dataclass
class ValidationResult:
    passed: bool
    issues: list = field(default_factory=list)
    netlist: Optional[Netlist] = None


# ─── Geometry extraction ────────────────────────────────────────────

def _is_point_on_segment(px, py, x1, y1, x2, y2):
    """Check if point (px,py) lies on the axis-aligned segment (x1,y1)-(x2,y2).
    Does NOT match endpoints — only interior points."""
    px, py = round(px, 4), round(py, 4)
    x1, y1 = round(x1, 4), round(y1, 4)
    x2, y2 = round(x2, 4), round(y2, 4)

    # Skip if it's an endpoint
    if (px, py) == (x1, y1) or (px, py) == (x2, y2):
        return False

    # Horizontal segment
    if y1 == y2 and py == y1:
        lo, hi = min(x1, x2), max(x1, x2)
        return lo < px < hi

    # Vertical segment
    if x1 == x2 and px == x1:
        lo, hi = min(y1, y2), max(y1, y2)
        return lo < py < hi

    return False


def _is_point_on_segment_inclusive(px, py, x1, y1, x2, y2):
    """Check if point (px,py) lies anywhere on segment (x1,y1)-(x2,y2),
    INCLUDING endpoints. Used for pin-to-wire connectivity."""
    px, py = round(px, 4), round(py, 4)
    x1, y1 = round(x1, 4), round(y1, 4)
    x2, y2 = round(x2, 4), round(y2, 4)

    # Horizontal segment
    if y1 == y2 and py == y1:
        lo, hi = min(x1, x2), max(x1, x2)
        return lo <= px <= hi

    # Vertical segment
    if x1 == x2 and px == x1:
        lo, hi = min(y1, y2), max(y1, y2)
        return lo <= py <= hi

    return False


def _extract_all_pin_positions(sch: KicadSchematic):
    """Returns list of (reference, pin_number, x, y, pin_type, is_power_symbol)."""
    pins = []
    for comp in sch.components:
        lib_sym = sch.lib_symbols.get(comp.lib_id)
        if lib_sym is None:
            continue
        is_pwr = lib_sym.is_power
        for pin in lib_sym.pins:
            try:
                x, y = sch.get_pin_position(comp.reference, pin.number)
                pins.append((comp.reference, pin.number, x, y, pin.pin_type, is_pwr))
            except ValueError:
                pass
    return pins


# ─── Netlist extraction ─────────────────────────────────────────────

def extract_netlist(sch: KicadSchematic) -> Netlist:
    """Build a netlist from the schematic's geometric connectivity."""
    uf = UnionFind()

    # 1. Register all pin positions
    pin_positions = _extract_all_pin_positions(sch)
    # Map coord -> list of (ref, pin_num)
    coord_to_pins = {}
    for ref, pnum, x, y, ptype, is_pwr in pin_positions:
        key = _coord_key(x, y)
        uf._ensure(key)
        coord_to_pins.setdefault(key, []).append((ref, pnum))

    # 2. Register and union wire endpoints
    for w in sch.wires:
        k1 = _coord_key(w.x1, w.y1)
        k2 = _coord_key(w.x2, w.y2)
        uf.union(k1, k2)

    # 2b. Union pins that lie anywhere on a wire segment (KiCad connects
    #     pins to wires at any point, not just endpoints)
    for ref, pnum, x, y, ptype, is_pwr in pin_positions:
        pin_key = _coord_key(x, y)
        for w in sch.wires:
            if _is_point_on_segment_inclusive(x, y, w.x1, w.y1, w.x2, w.y2):
                uf.union(pin_key, _coord_key(w.x1, w.y1))
                break  # One wire is enough to connect

    # 2c. Union labels that lie on wire segments
    for lbl in sch.labels:
        lbl_key = _coord_key(lbl.x, lbl.y)
        for w in sch.wires:
            if _is_point_on_segment_inclusive(lbl.x, lbl.y, w.x1, w.y1, w.x2, w.y2):
                uf.union(lbl_key, _coord_key(w.x1, w.y1))
                break
    for gl in sch.global_labels:
        gl_key = _coord_key(gl.x, gl.y)
        for w in sch.wires:
            if _is_point_on_segment_inclusive(gl.x, gl.y, w.x1, w.y1, w.x2, w.y2):
                uf.union(gl_key, _coord_key(w.x1, w.y1))
                break
    for hl in getattr(sch, 'hierarchical_labels', []):
        hl_key = _coord_key(hl.x, hl.y)
        for w in sch.wires:
            if _is_point_on_segment_inclusive(hl.x, hl.y, w.x1, w.y1, w.x2, w.y2):
                uf.union(hl_key, _coord_key(w.x1, w.y1))
                break

    # 2d. Sheet pins connect wires at their position (the parent-side end
    #     of the hierarchy — the child side is merged in step 7).
    sheet_pin_key = {}    # (sheet_index, pin_name) -> coord key
    sheet_pin_coords = set()
    for si, sheet in enumerate(getattr(sch, 'sheets', [])):
        for spin in sheet.pins:
            key = _coord_key(spin.x, spin.y)
            uf._ensure(key)
            sheet_pin_key[(si, spin.name)] = key
            sheet_pin_coords.add(key)
            for w in sch.wires:
                if _is_point_on_segment_inclusive(spin.x, spin.y,
                                                  w.x1, w.y1, w.x2, w.y2):
                    uf.union(key, _coord_key(w.x1, w.y1))
                    break

    # 3. Register label positions
    for lbl in sch.labels:
        key = _coord_key(lbl.x, lbl.y)
        uf._ensure(key)

    for gl in sch.global_labels:
        key = _coord_key(gl.x, gl.y)
        uf._ensure(key)

    for hl in getattr(sch, 'hierarchical_labels', []):
        key = _coord_key(hl.x, hl.y)
        uf._ensure(key)

    # 4. Handle junctions — union with any wire segment passing through
    for j in sch.junctions:
        jkey = _coord_key(j.x, j.y)
        uf._ensure(jkey)
        for w in sch.wires:
            k1 = _coord_key(w.x1, w.y1)
            k2 = _coord_key(w.x2, w.y2)
            # If junction is at a wire endpoint, union
            if jkey == k1 or jkey == k2:
                uf.union(jkey, k1)
                uf.union(jkey, k2)
            # If junction is on the interior of a wire segment
            elif _is_point_on_segment(j.x, j.y, w.x1, w.y1, w.x2, w.y2):
                uf.union(jkey, k1)
                uf.union(jkey, k2)

    # 5. Merge sets that share label names
    label_groups = {}  # label_text -> list of coord keys
    global_texts = {}  # global-label text -> coord keys (globals span sheets)
    hlabel_texts = {}  # hierarchical-label text -> coord keys (sheet ports)
    for lbl in sch.labels:
        key = _coord_key(lbl.x, lbl.y)
        label_groups.setdefault(lbl.text, []).append(key)
    for gl in sch.global_labels:
        key = _coord_key(gl.x, gl.y)
        label_groups.setdefault(gl.text, []).append(key)
        global_texts.setdefault(gl.text, []).append(key)
    # Hierarchical labels name/unify nets within this sheet exactly like
    # labels do (cross-sheet connection happens via the parent's sheet pins).
    for hl in getattr(sch, 'hierarchical_labels', []):
        key = _coord_key(hl.x, hl.y)
        label_groups.setdefault(hl.text, []).append(key)
        hlabel_texts.setdefault(hl.text, []).append(key)

    # Power symbols act like global labels
    for comp in sch.components:
        lib_sym = sch.lib_symbols.get(comp.lib_id)
        if lib_sym and lib_sym.is_power:
            power_name = comp.value
            for pin in lib_sym.pins:
                try:
                    x, y = sch.get_pin_position(comp.reference, pin.number)
                    key = _coord_key(x, y)
                    label_groups.setdefault(power_name, []).append(key)
                except ValueError:
                    pass

    for text, keys in label_groups.items():
        for i in range(1, len(keys)):
            uf.union(keys[0], keys[i])

    # 6. Build nets from union-find groups
    groups = uf.groups()

    # Map each group root to a net name
    root_to_name = {}
    root_is_power = {}
    root_has_label = {}

    # Assign names from labels/power symbols
    for text, keys in label_groups.items():
        if keys:
            root = uf.find(keys[0])
            # Check if this is a power net
            is_power = False
            for comp in sch.components:
                lib_sym = sch.lib_symbols.get(comp.lib_id)
                if lib_sym and lib_sym.is_power and comp.value == text:
                    is_power = True
                    break
            # Power nets and labels both name the net
            if root not in root_to_name:
                root_to_name[root] = text
                root_is_power[root] = is_power
                root_has_label[root] = True

    # Auto-name unnamed nets — never colliding with an existing labeled net
    # (a schematic can legitimately carry a label literally named "_NET_1",
    # e.g. a block sheet extracted from a board's auto-named nets; without
    # this guard the auto-named group silently CLOBBERS the labeled net).
    auto_idx = 0
    used_names = set(root_to_name.values())
    for root in groups:
        if root not in root_to_name:
            # Only name it if it has pins (a sheet pin counts — a wire joining
            # two sheet pins is a real net even with no component pin on it)
            group_coords = groups[root]
            has_pins = any(c in coord_to_pins for c in group_coords) or \
                any(c in sheet_pin_coords for c in group_coords)
            if has_pins:
                auto_idx += 1
                name = f"_NET_{auto_idx}"
                while name in used_names:
                    auto_idx += 1
                    name = f"_NET_{auto_idx}"
                used_names.add(name)
                root_to_name[root] = name
                root_is_power[root] = False
                root_has_label[root] = False

    # Build NetlistEntry objects
    netlist = Netlist()
    for root, name in root_to_name.items():
        group_coords = groups[root]
        pins_on_net = set()
        for coord in group_coords:
            if coord in coord_to_pins:
                for ref, pnum in coord_to_pins[coord]:
                    pins_on_net.add((ref, pnum))
        netlist.nets[name] = NetlistEntry(
            name=name,
            pins=pins_on_net,
            has_label=root_has_label.get(root, False),
            is_power=root_is_power.get(root, False),
        )

    # Record which hierarchical/global label texts sit on each net — the
    # hierarchy merge below (and the one a parent runs on THIS sheet's
    # netlist) keys off these.
    for texts, attr in ((hlabel_texts, "hlabel_names"),
                        (global_texts, "global_names")):
        for text, keys in texts.items():
            for key in keys:
                name = root_to_name.get(uf.find(key))
                if name and name in netlist.nets:
                    getattr(netlist.nets[name], attr).add(text)

    # 7. Merge child-sheet netlists through the sheet pins (ROADMAP W1b).
    _merge_sheet_netlists(sch, netlist, uf, root_to_name, sheet_pin_key)

    return netlist


def _merge_sheet_netlists(sch, netlist, uf, root_to_name, sheet_pin_key):
    """Fold each child sheet's netlist into the parent's.

    KiCad hierarchy semantics: a child net carrying a hierarchical label
    joins the parent net wired to the sheet pin of the same name; power
    symbols and global labels are global across the hierarchy (merge by
    name); everything else is sheet-local and gets an ``instance/`` prefix.
    """
    merged_into = {}   # parent net name -> the name it was folded into

    def _resolve(name):
        while name in merged_into:
            name = merged_into[name]
        return name

    def _net_for_global(gname):
        """The net carrying global label ``gname`` (its primary name may
        differ), else ``gname`` itself (a fresh net will be created)."""
        for name, entry in netlist.nets.items():
            if gname in entry.global_names or name == gname:
                return name
        return gname

    for si, sheet in enumerate(getattr(sch, 'sheets', [])):
        if sheet.child is None:
            continue
        child_nl = extract_netlist(sheet.child)  # grandchildren already merged

        # Parent net wired to each sheet pin (None = pin floats in parent).
        port_net = {}
        for spin in sheet.pins:
            key = sheet_pin_key.get((si, spin.name))
            if key is None:
                continue
            pname = root_to_name.get(uf.find(key))
            if pname:
                port_net[spin.name] = pname

        for cname, centry in child_nl.nets.items():
            if centry.is_power:
                target = cname
            elif centry.global_names:
                target = _resolve(_net_for_global(sorted(centry.global_names)[0]))
            else:
                parents = sorted({_resolve(port_net[h])
                                  for h in centry.hlabel_names
                                  if h in port_net})
                if parents:
                    target = parents[0]
                    # One child net touching several parent nets CONNECTS
                    # them (e.g. a block internally ties two ports).
                    for other in parents[1:]:
                        if other == target or other not in netlist.nets:
                            continue
                        tgt = netlist.nets[target]
                        oth = netlist.nets.pop(other)
                        tgt.pins |= oth.pins
                        tgt.has_label = tgt.has_label or oth.has_label
                        tgt.is_power = tgt.is_power or oth.is_power
                        tgt.hlabel_names |= oth.hlabel_names
                        tgt.global_names |= oth.global_names
                        merged_into[other] = target
                else:
                    # Sheet-local net (or a port left unwired in the parent).
                    target = f"{sheet.name}/{cname}"
            entry = netlist.nets.get(target)
            if entry is None:
                entry = NetlistEntry(
                    name=target, pins=set(), has_label=True,
                    is_power=centry.is_power, from_sheet=True)
                netlist.nets[target] = entry
            entry.pins |= centry.pins
            entry.is_power = entry.is_power or centry.is_power
            entry.global_names |= centry.global_names


# ─── Validation checks ──────────────────────────────────────────────

def _check_duplicate_references(sch):
    """Duplicate refs across the WHOLE hierarchy — two sheets each carrying a
    'U2' is exactly the collision the per-instance refdes ranges exist to
    prevent, and it must be caught at the root."""
    issues = []
    seen = {}
    for comp, lib_sym, prefix in iter_all_components(sch):
        if lib_sym and lib_sym.is_power:
            continue  # Power refs like #PWR001 are allowed to overlap in name prefix
        if comp.reference.startswith("#"):
            continue  # power-symbol refs are per-sheet by design
        where = prefix.rstrip("/") if prefix else "root"
        if comp.reference in seen:
            issues.append(ValidationIssue(
                "error", "duplicate_reference",
                f"Duplicate reference designator '{comp.reference}' "
                f"(in {seen[comp.reference]} and {where})",
                references=[comp.reference],
                coordinates=(comp.x, comp.y),
            ))
        else:
            seen[comp.reference] = where
    return issues


def _check_missing_lib_symbols(sch):
    issues = []
    for comp in sch.components:
        if comp.lib_id not in sch.lib_symbols:
            issues.append(ValidationIssue(
                "error", "missing_lib_symbol",
                f"Component '{comp.reference}' references unknown lib_id '{comp.lib_id}'",
                references=[comp.reference],
                coordinates=(comp.x, comp.y),
            ))
    return issues


def _check_stale_lib_cache(sch):
    """Warn for symbols the loader had to resolve from installed libraries
    because the file's embedded lib_symbols cache doesn't contain them.
    Connectivity is checked with the real pins (matching KiCad's fallback),
    but the file should be re-saved in KiCad to refresh its cache."""
    issues = []
    for lib_id in getattr(sch, 'stale_lib_cache', []):
        refs = sorted(c.reference for c in sch.components if c.lib_id == lib_id)
        issues.append(ValidationIssue(
            "warning", "stale_lib_cache",
            f"'{lib_id}' ({', '.join(refs)}) missing from the file's embedded "
            f"lib_symbols cache — resolved from installed libraries; re-save "
            f"the schematic in KiCad to refresh its cache",
            references=refs,
        ))
    return issues


def _check_floating_pins(sch, netlist):
    """Pins not connected to any wire, label, or other pin (and not marked no-connect)."""
    issues = []
    nc_coords = {_coord_key(nc.x, nc.y) for nc in sch.no_connects}

    # Build set of all "connected" coordinates (wire endpoints, labels, junctions)
    connected_coords = set()
    for w in sch.wires:
        connected_coords.add(_coord_key(w.x1, w.y1))
        connected_coords.add(_coord_key(w.x2, w.y2))
    for lbl in sch.labels:
        connected_coords.add(_coord_key(lbl.x, lbl.y))
    for gl in sch.global_labels:
        connected_coords.add(_coord_key(gl.x, gl.y))
    for j in sch.junctions:
        connected_coords.add(_coord_key(j.x, j.y))

    # Also count other component pins at the same coordinate as connections
    all_pin_coords = {}  # coord_key -> count of pins at that point
    for comp in sch.components:
        lib_sym = sch.lib_symbols.get(comp.lib_id)
        if lib_sym is None:
            continue
        for pin in lib_sym.pins:
            try:
                x, y = sch.get_pin_position(comp.reference, pin.number)
                key = _coord_key(x, y)
                all_pin_coords[key] = all_pin_coords.get(key, 0) + 1
            except ValueError:
                pass

    for comp in sch.components:
        lib_sym = sch.lib_symbols.get(comp.lib_id)
        if lib_sym is None or lib_sym.is_power:
            continue
        for pin in lib_sym.pins:
            try:
                x, y = sch.get_pin_position(comp.reference, pin.number)
            except ValueError:
                continue
            key = _coord_key(x, y)
            if key in nc_coords:
                continue
            # Check if pin lies on any wire segment (KiCad connects pins
            # to wires at any point along the wire, not just endpoints)
            on_wire = False
            for w in sch.wires:
                if _is_point_on_segment_inclusive(x, y, w.x1, w.y1, w.x2, w.y2):
                    on_wire = True
                    break

            # Pin is floating if nothing touches its coordinate and it's not on a wire
            if not on_wire and key not in connected_coords and all_pin_coords.get(key, 0) <= 1:
                issues.append(ValidationIssue(
                    "error", "floating_pin",
                    f"Pin {pin.number} ({pin.name}) of {comp.reference} "
                    f"at ({x}, {y}) is not connected",
                    references=[comp.reference],
                    coordinates=(x, y),
                ))
    return issues


def _check_single_pin_nets(sch, netlist):
    """Nets with only one pin — usually a broken connection."""
    issues = []
    nc_coords = {_coord_key(nc.x, nc.y) for nc in sch.no_connects}

    def _is_nc(ref, pnum):
        try:
            x, y = sch.get_pin_position(ref, pnum)
        except ValueError:
            return False
        return _coord_key(x, y) in nc_coords

    for name, entry in netlist.nets.items():
        if entry.from_sheet:
            continue  # child-sheet nets are checked by the child's own validate()
        # Filter out power symbol-only pins
        real_pins = {(r, p) for r, p in entry.pins if not r.startswith("#PWR")}
        pwr_pins = {(r, p) for r, p in entry.pins if r.startswith("#PWR")}

        if len(real_pins) == 1 and not entry.has_label:
            ref, pnum = next(iter(real_pins))
            if _is_nc(ref, pnum):
                continue  # an explicitly NC'd pin is not a broken net
            issues.append(ValidationIssue(
                "warning", "single_pin_net",
                f"Net '{name}' has only one non-power pin: {ref}.{pnum}",
                references=[ref],
            ))
        elif len(real_pins) == 0 and len(pwr_pins) <= 1:
            # A power symbol alone with no connections
            if pwr_pins:
                ref, pnum = next(iter(pwr_pins))
                issues.append(ValidationIssue(
                    "warning", "isolated_power_symbol",
                    f"Power symbol {ref} on net '{name}' is not connected to anything",
                    references=[ref],
                ))
    return issues


def _check_dangling_wires(sch):
    """Wire endpoints that don't touch any pin, label, other wire endpoint, or junction."""
    issues = []

    pin_positions = _extract_all_pin_positions(sch)
    occupied = set()

    # Pin positions
    for ref, pnum, x, y, ptype, is_pwr in pin_positions:
        occupied.add(_coord_key(x, y))

    # Label positions
    for lbl in sch.labels:
        occupied.add(_coord_key(lbl.x, lbl.y))
    for gl in sch.global_labels:
        occupied.add(_coord_key(gl.x, gl.y))
    for hl in getattr(sch, 'hierarchical_labels', []):
        occupied.add(_coord_key(hl.x, hl.y))

    # Sheet pins are connection points too
    for sheet in getattr(sch, 'sheets', []):
        for spin in sheet.pins:
            occupied.add(_coord_key(spin.x, spin.y))

    # Junction positions
    for j in sch.junctions:
        occupied.add(_coord_key(j.x, j.y))

    # Collect all wire endpoints
    wire_endpoints = {}  # coord -> count of wire endpoints at that point
    for w in sch.wires:
        for key in [_coord_key(w.x1, w.y1), _coord_key(w.x2, w.y2)]:
            wire_endpoints[key] = wire_endpoints.get(key, 0) + 1

    for coord, count in wire_endpoints.items():
        # Connected if: touches a pin/label/junction, or 2+ wire endpoints meet here
        if coord not in occupied and count < 2:
            issues.append(ValidationIssue(
                "error", "dangling_wire",
                f"Wire endpoint at ({coord[0]}, {coord[1]}) doesn't connect to anything",
                coordinates=coord,
            ))
    return issues


def _check_missing_junctions(sch):
    """Wire endpoint landing on the interior of another wire without a junction."""
    issues = []
    junction_coords = {_coord_key(j.x, j.y) for j in sch.junctions}

    wire_endpoints = set()
    for w in sch.wires:
        wire_endpoints.add(_coord_key(w.x1, w.y1))
        wire_endpoints.add(_coord_key(w.x2, w.y2))

    for endpoint in wire_endpoints:
        if endpoint in junction_coords:
            continue
        ex, ey = endpoint
        for w in sch.wires:
            if _is_point_on_segment(ex, ey, w.x1, w.y1, w.x2, w.y2):
                issues.append(ValidationIssue(
                    "warning", "missing_junction",
                    f"Wire endpoint at ({ex}, {ey}) touches the middle of another wire "
                    f"but has no junction — KiCad won't connect them",
                    coordinates=endpoint,
                ))
                break  # One warning per endpoint is enough
    return issues


def _check_overlapping_components(sch):
    """Two non-power components at the same position."""
    issues = []
    seen = {}
    for comp in sch.components:
        lib_sym = sch.lib_symbols.get(comp.lib_id)
        if lib_sym and lib_sym.is_power:
            continue
        key = _coord_key(comp.x, comp.y)
        if key in seen:
            issues.append(ValidationIssue(
                "warning", "overlapping_components",
                f"Components '{seen[key]}' and '{comp.reference}' are at the same position "
                f"({comp.x}, {comp.y})",
                references=[seen[key], comp.reference],
                coordinates=key,
            ))
        else:
            seen[key] = comp.reference
    return issues


def _check_no_connect_conflicts(sch):
    """No-connect marker at a position that also has a wire."""
    issues = []
    wire_points = set()
    for w in sch.wires:
        wire_points.add(_coord_key(w.x1, w.y1))
        wire_points.add(_coord_key(w.x2, w.y2))

    for nc in sch.no_connects:
        key = _coord_key(nc.x, nc.y)
        if key in wire_points:
            issues.append(ValidationIssue(
                "warning", "no_connect_conflict",
                f"No-connect marker at ({nc.x}, {nc.y}) conflicts with a wire endpoint",
                coordinates=key,
            ))
    return issues


def _check_missing_power_connections(sch, netlist):
    """Power-input pins not on a net with a power source."""
    issues = []
    pin_positions = _extract_all_pin_positions(sch)

    # Build set of nets that have a power source
    powered_nets = set()
    for ref, pnum, x, y, ptype, is_pwr in pin_positions:
        if ptype in ("power_out",) or is_pwr:
            net = netlist.get_net_for_pin(ref, pnum)
            if net:
                powered_nets.add(net)

    # Also count power-named nets (GND, VCC, etc.) as powered
    for name, entry in netlist.nets.items():
        if entry.is_power:
            powered_nets.add(name)

    # Check power_in pins on non-power components
    for ref, pnum, x, y, ptype, is_pwr in pin_positions:
        if is_pwr:
            continue
        if ptype == "power_in":
            net = netlist.get_net_for_pin(ref, pnum)
            if net is None:
                continue  # Already caught by floating_pin
            if net not in powered_nets:
                issues.append(ValidationIssue(
                    "warning", "missing_power_source",
                    f"Power-input pin {pnum} of {ref} is on net '{net}' "
                    f"which has no power source",
                    references=[ref],
                    coordinates=(x, y),
                ))
    return issues


def _check_disconnected_labels(sch):
    """Labels placed at coordinates that don't touch any wire endpoint or pin."""
    issues = []

    # Build set of all wire endpoints and pin positions
    touchable = set()
    for w in sch.wires:
        touchable.add(_coord_key(w.x1, w.y1))
        touchable.add(_coord_key(w.x2, w.y2))
    for j in sch.junctions:
        touchable.add(_coord_key(j.x, j.y))
    pin_positions = _extract_all_pin_positions(sch)
    for ref, pnum, x, y, ptype, is_pwr in pin_positions:
        touchable.add(_coord_key(x, y))
    for sheet in getattr(sch, 'sheets', []):
        for spin in sheet.pins:
            touchable.add(_coord_key(spin.x, spin.y))

    def _label_touches_something(lx, ly):
        key = _coord_key(lx, ly)
        if key in touchable:
            return True
        # Also check if label lies on a wire segment
        for w in sch.wires:
            if _is_point_on_segment_inclusive(lx, ly, w.x1, w.y1, w.x2, w.y2):
                return True
        return False

    for lbl in sch.labels:
        if not _label_touches_something(lbl.x, lbl.y):
            issues.append(ValidationIssue(
                "error", "disconnected_label",
                f"Label '{lbl.text}' at ({lbl.x}, {lbl.y}) is not connected to "
                f"any wire or pin",
                coordinates=_coord_key(lbl.x, lbl.y),
            ))
    for gl in sch.global_labels:
        if not _label_touches_something(gl.x, gl.y):
            issues.append(ValidationIssue(
                "error", "disconnected_label",
                f"Global label '{gl.text}' at ({gl.x}, {gl.y}) is not connected to "
                f"any wire or pin",
                coordinates=_coord_key(gl.x, gl.y),
            ))
    return issues


def _check_sheet_integrity(sch):
    """Hierarchical-sheet contract checks (ROADMAP W1b).

    Errors: duplicate sheet instance names, duplicate pin names on one
    sheet, a Sheetfile that doesn't exist, and pin↔hierarchical-label parity
    with the loaded child (a pin with no matching child port connects
    nothing; a child port with no pin is an interface the parent ignores).
    Warning: a sheet pin left unwired in the parent.
    """
    issues = []
    missing_files = set(getattr(sch, 'missing_sheet_files', []))

    # Connection points a sheet pin can legitimately touch
    label_coords = set()
    for lbl in sch.labels:
        label_coords.add(_coord_key(lbl.x, lbl.y))
    for gl in sch.global_labels:
        label_coords.add(_coord_key(gl.x, gl.y))

    seen_names = {}
    for sheet in getattr(sch, 'sheets', []):
        if sheet.name in seen_names:
            issues.append(ValidationIssue(
                "error", "sheet_integrity",
                f"Duplicate sheet instance name '{sheet.name}' — instance "
                f"names must be unique (they scope refs and net names)",
                coordinates=(sheet.x, sheet.y),
            ))
        seen_names[sheet.name] = sheet

        pin_names = [p.name for p in sheet.pins]
        for dup in sorted({n for n in pin_names if pin_names.count(n) > 1}):
            issues.append(ValidationIssue(
                "error", "sheet_integrity",
                f"Sheet '{sheet.name}': duplicate sheet pin '{dup}'",
                coordinates=(sheet.x, sheet.y),
            ))

        if sheet.child is None:
            if sheet.filename in missing_files:
                issues.append(ValidationIssue(
                    "error", "sheet_integrity",
                    f"Sheet '{sheet.name}': child file '{sheet.filename}' "
                    f"not found next to the parent schematic",
                    coordinates=(sheet.x, sheet.y),
                ))
            continue

        # Pin set ↔ child hierarchical-label parity
        hlabels = {hl.text for hl in sheet.child.hierarchical_labels}
        for pname in sorted(set(pin_names) - hlabels):
            issues.append(ValidationIssue(
                "error", "sheet_integrity",
                f"Sheet '{sheet.name}': pin '{pname}' has no matching "
                f"hierarchical label in '{sheet.filename}' — it connects "
                f"nothing",
                coordinates=(sheet.x, sheet.y),
            ))
        for hname in sorted(hlabels - set(pin_names)):
            issues.append(ValidationIssue(
                "error", "sheet_integrity",
                f"Sheet '{sheet.name}': child '{sheet.filename}' declares "
                f"port '{hname}' but the sheet symbol has no such pin",
                coordinates=(sheet.x, sheet.y),
            ))

        # Unwired sheet pins (parent side)
        for spin in sheet.pins:
            key = _coord_key(spin.x, spin.y)
            wired = key in label_coords or any(
                _is_point_on_segment_inclusive(spin.x, spin.y,
                                               w.x1, w.y1, w.x2, w.y2)
                for w in sch.wires)
            if not wired:
                issues.append(ValidationIssue(
                    "warning", "sheet_integrity",
                    f"Sheet '{sheet.name}': pin '{spin.name}' is not wired "
                    f"to anything in the parent",
                    coordinates=(spin.x, spin.y),
                ))
    return issues


def _check_similar_net_names(sch):
    """Net names that differ only in case — almost always a wiring bug."""
    issues = []

    # Collect all net names from labels, global labels, and power symbols
    net_names = {}  # lowercase -> list of (original_name, source_description)
    for lbl in sch.labels:
        net_names.setdefault(lbl.text.lower(), []).append(
            (lbl.text, f"label at ({lbl.x}, {lbl.y})")
        )
    for gl in sch.global_labels:
        net_names.setdefault(gl.text.lower(), []).append(
            (gl.text, f"global label at ({gl.x}, {gl.y})")
        )
    for comp in sch.components:
        lib_sym = sch.lib_symbols.get(comp.lib_id)
        if lib_sym and lib_sym.is_power:
            net_names.setdefault(comp.value.lower(), []).append(
                (comp.value, f"power symbol {comp.reference}")
            )

    for lower_name, entries in net_names.items():
        distinct_names = set(name for name, _ in entries)
        if len(distinct_names) > 1:
            name_list = ", ".join(sorted(distinct_names))
            issues.append(ValidationIssue(
                "warning", "similar_net_names",
                f"Net names differ only in case: {name_list} — "
                f"these are separate nets in KiCad",
            ))
    return issues


# ─── Main validation entry point ────────────────────────────────────

ALL_CHECKS = [
    "duplicate_reference",
    "missing_lib_symbol",
    "stale_lib_cache",
    "floating_pin",
    "single_pin_net",
    "dangling_wire",
    "missing_junction",
    "overlapping_components",
    "no_connect_conflict",
    "missing_power_source",
    "disconnected_label",
    "similar_net_names",
    "sheet_integrity",
]

def validate(sch: KicadSchematic,
             checks: list = None,
             severity_threshold: str = "warning") -> ValidationResult:
    """Run validation checks on a schematic.

    Args:
        sch: The schematic to validate.
        checks: List of check names to run (default: all).
        severity_threshold: Minimum severity to include ("error" or "warning").
    """
    if checks is None:
        checks = ALL_CHECKS

    netlist = extract_netlist(sch)
    all_issues = []

    check_funcs = {
        "duplicate_reference": lambda: _check_duplicate_references(sch),
        "missing_lib_symbol": lambda: _check_missing_lib_symbols(sch),
        "stale_lib_cache": lambda: _check_stale_lib_cache(sch),
        "floating_pin": lambda: _check_floating_pins(sch, netlist),
        "single_pin_net": lambda: _check_single_pin_nets(sch, netlist),
        "dangling_wire": lambda: _check_dangling_wires(sch),
        "missing_junction": lambda: _check_missing_junctions(sch),
        "overlapping_components": lambda: _check_overlapping_components(sch),
        "no_connect_conflict": lambda: _check_no_connect_conflicts(sch),
        "missing_power_source": lambda: _check_missing_power_connections(sch, netlist),
        "disconnected_label": lambda: _check_disconnected_labels(sch),
        "similar_net_names": lambda: _check_similar_net_names(sch),
        "sheet_integrity": lambda: _check_sheet_integrity(sch),
    }

    severity_levels = {"warning": 0, "error": 1}
    threshold = severity_levels.get(severity_threshold, 0)

    for check_name in checks:
        func = check_funcs.get(check_name)
        if func:
            issues = func()
            for issue in issues:
                if severity_levels.get(issue.severity, 0) >= threshold:
                    all_issues.append(issue)

    # Recurse into loaded child sheets, prefixing their issues with the
    # instance name. duplicate_reference is skipped in children — the root
    # call already checks it across the whole hierarchy.
    child_checks = [c for c in checks if c != "duplicate_reference"]
    for sheet in getattr(sch, 'sheets', []):
        if sheet.child is None:
            continue
        child_result = validate(sheet.child, checks=child_checks,
                                severity_threshold=severity_threshold)
        for issue in child_result.issues:
            all_issues.append(ValidationIssue(
                issue.severity, issue.check_name,
                f"[sheet {sheet.name}] {issue.message}",
                references=issue.references,
                coordinates=issue.coordinates,
            ))

    has_errors = any(i.severity == "error" for i in all_issues)
    return ValidationResult(
        passed=not has_errors,
        issues=all_issues,
        netlist=netlist,
    )


def validate_file(filepath, project_dir=None, extra_sym=None,
                  resolve_from_libraries=True, **kwargs):
    """Validate a .kicad_sch file. Convenience wrapper around load + validate."""
    sch = load_kicad_sch(filepath, resolve_from_libraries=resolve_from_libraries,
                         project_dir=project_dir, extra_sym=extra_sym)
    return validate(sch, **kwargs)


# ─── Assertion helpers ──────────────────────────────────────────────

def assert_connected(sch: KicadSchematic,
                     ref1: str, pin1: str,
                     ref2: str, pin2: str) -> None:
    """Assert two pins are on the same net. Raises AssertionError if not."""
    netlist = extract_netlist(sch)
    net1 = netlist.get_net_for_pin(ref1, pin1)
    net2 = netlist.get_net_for_pin(ref2, pin2)

    if net1 is None:
        raise AssertionError(f"Pin {ref1}.{pin1} is not on any net")
    if net2 is None:
        raise AssertionError(f"Pin {ref2}.{pin2} is not on any net")
    if net1 != net2:
        raise AssertionError(
            f"{ref1}.{pin1} is on net '{net1}' but {ref2}.{pin2} is on net '{net2}'"
        )


def assert_net_contains(sch: KicadSchematic,
                        net_name: str,
                        expected_pins: list) -> None:
    """Assert a named net contains exactly these pins.

    Args:
        expected_pins: List of (reference, pin_number) tuples.
    """
    netlist = extract_netlist(sch)
    entry = netlist.nets.get(net_name)
    if entry is None:
        available = sorted(netlist.nets.keys())
        raise AssertionError(
            f"Net '{net_name}' not found. Available nets: {available}"
        )

    expected = set(tuple(p) for p in expected_pins)
    actual = entry.pins
    missing = expected - actual
    extra = actual - expected

    msgs = []
    if missing:
        msgs.append(f"Missing pins: {sorted(missing)}")
    if extra:
        msgs.append(f"Extra pins: {sorted(extra)}")
    if msgs:
        raise AssertionError(
            f"Net '{net_name}' mismatch. {'; '.join(msgs)}"
        )


# ─── Output formatters ──────────────────────────────────────────────

def format_result_text(result, filepath=None, show_netlist=False):
    """Format validation result as human-readable text."""
    lines = []
    lines.append("=" * 60)
    lines.append("SCHEMATIC VALIDATION REPORT")
    lines.append("=" * 60)
    if filepath:
        lines.append(f"File: {filepath}")

    if result.netlist:
        lines.append(f"Components: {sum(1 for n, e in result.netlist.nets.items() for _ in [])  or ''}")
    lines.append("")

    # Summary
    errors = [i for i in result.issues if i.severity == "error"]
    warnings = [i for i in result.issues if i.severity == "warning"]
    if result.passed:
        lines.append(f"RESULT: PASSED ({len(warnings)} warnings)")
    else:
        lines.append(f"RESULT: FAILED ({len(errors)} errors, {len(warnings)} warnings)")
    lines.append("")

    # Issues grouped by severity
    if errors:
        lines.append("ERRORS:")
        for i in errors:
            coord = f" at ({i.coordinates[0]}, {i.coordinates[1]})" if i.coordinates else ""
            lines.append(f"  [{i.check_name}] {i.message}")
        lines.append("")

    if warnings:
        lines.append("WARNINGS:")
        for i in warnings:
            lines.append(f"  [{i.check_name}] {i.message}")
        lines.append("")

    # Netlist
    if show_netlist and result.netlist:
        lines.append("NETLIST:")
        for name in sorted(result.netlist.nets.keys()):
            entry = result.netlist.nets[name]
            flags = []
            if entry.is_power:
                flags.append("power")
            if entry.has_label:
                flags.append("labeled")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            pins_str = ", ".join(f"{r}.{p}" for r, p in sorted(entry.pins))
            lines.append(f"  {name}{flag_str}: {pins_str}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def format_result_json(result, filepath=None, show_netlist=False):
    """Format validation result as JSON for machine consumption."""
    output = {
        "file": filepath,
        "passed": result.passed,
        "error_count": sum(1 for i in result.issues if i.severity == "error"),
        "warning_count": sum(1 for i in result.issues if i.severity == "warning"),
        "issues": [
            {
                "severity": i.severity,
                "check": i.check_name,
                "message": i.message,
                "references": i.references,
                "coordinates": list(i.coordinates) if i.coordinates else None,
            }
            for i in result.issues
        ],
    }

    if show_netlist and result.netlist:
        output["netlist"] = {
            name: {
                "pins": sorted([f"{r}.{p}" for r, p in entry.pins]),
                "is_power": entry.is_power,
                "has_label": entry.has_label,
            }
            for name, entry in sorted(result.netlist.nets.items())
        }

    return json_module.dumps(output, indent=2)


def print_netlist(sch: KicadSchematic) -> None:
    """Print a human-readable netlist to stdout."""
    netlist = extract_netlist(sch)
    for name in sorted(netlist.nets.keys()):
        entry = netlist.nets[name]
        flags = []
        if entry.is_power:
            flags.append("power")
        if entry.has_label:
            flags.append("labeled")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        pins_str = ", ".join(f"{r}.{p}" for r, p in sorted(entry.pins))
        print(f"  {name}{flag_str}: {pins_str}")


# ─── CLI entry point ────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Validate a KiCad .kicad_sch schematic file.",
        epilog="""
Examples:
  python validate_kicad_sch.py board.kicad_sch
  python validate_kicad_sch.py board.kicad_sch --json
  python validate_kicad_sch.py board.kicad_sch --json --netlist
  python validate_kicad_sch.py board.kicad_sch --errors-only
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("file", help="Path to .kicad_sch file to validate")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON (for automation / Claude Code)")
    parser.add_argument("--netlist", action="store_true",
                        help="Include full netlist in output")
    parser.add_argument("--errors-only", action="store_true",
                        help="Only show errors, suppress warnings")
    parser.add_argument("--project-dir", default=None,
                        help="KiCad project dir — searches its sym-lib-table "
                             "when resolving stale-cache symbols")
    parser.add_argument("--sym-lib", action="append", default=None,
                        metavar="[NICK=]PATH",
                        help="Extra symbol library for stale-cache resolution "
                             "(repeatable)")
    parser.add_argument("--no-lib-fallback", action="store_true",
                        help="Do not resolve cache-missing symbols from "
                             "installed libraries (pre-fallback behavior)")

    args = parser.parse_args()

    filepath = os.path.abspath(args.file)
    if not os.path.isfile(filepath):
        print(f"Error: File not found: {filepath}", file=sys.stderr)
        sys.exit(2)

    threshold = "error" if args.errors_only else "warning"
    result = validate_file(filepath, severity_threshold=threshold,
                           project_dir=args.project_dir, extra_sym=args.sym_lib,
                           resolve_from_libraries=not args.no_lib_fallback)

    if args.json:
        print(format_result_json(result, filepath=filepath, show_netlist=args.netlist))
    else:
        print(format_result_text(result, filepath=filepath, show_netlist=args.netlist))

    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
