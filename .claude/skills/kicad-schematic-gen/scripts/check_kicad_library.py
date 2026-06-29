#!/usr/bin/env python3
"""
KiCad Library Checker — searches KiCad symbol and footprint libraries.

Given a part number, finds the matching KiCad symbol (lib_id) and its default
footprint. Given a footprint string, verifies it exists on disk. Returns
everything needed to place the part in a schematic without manual lookup.

Resolves symbols that use `extends` inheritance to return full pin data from
the parent.

Searches **every library the user has registered**, not just KiCad's built-ins.
Libraries are discovered, in increasing precedence, from:

  1. the bundled built-in install (``<kicad_root>/symbols/*.kicad_sym``),
  2. the **global** ``sym-lib-table`` / ``fp-lib-table`` in the KiCad config dir
     (this is where consolidated user libs like ``Custom`` and one-off libs like
     ``ESP32S3`` are registered — so anything imported via *kicad-import-lib*
     resolves here),
  3. the **project** ``sym-lib-table`` / ``fp-lib-table`` (``--project-dir``),
  4. any library pointed at explicitly (``--sym-lib`` / ``--fp-lib``).

URI variables (``${KICAD8_SYMBOL_DIR}``, ``${KIPRJMOD}``, env vars) are resolved.
Because each library keeps its registered nickname, the returned ``lib_id`` is the
real one KiCad uses (e.g. ``Custom:NAU7802``) — usable verbatim in a schematic.
A symbol pulled from a user library is the authoritative source for that part's
pins (the symbol *is* the truth), so its pinout can seed a fact card as verified.

CLI Usage:
    python check_kicad_library.py AP2112K
    python check_kicad_library.py NAU7802 --pins                 # incl. user libs
    python check_kicad_library.py --footprint "Package_TO_SOT_SMD:SOT-23-5"
    python check_kicad_library.py NAU7802 --lookup --json
    # point at a project and/or an arbitrary library:
    python check_kicad_library.py NAU7802 --lookup --project-dir /path/to/proj
    python check_kicad_library.py MYPART --lookup --sym-lib MyLib=/path/My.kicad_sym

Python API:
    from check_kicad_library import (
        find_symbol, check_footprint, find_kicad_root, build_library_set,
    )

    libs = build_library_set(project_dir="/path/to/proj")
    results = find_symbol("NAU7802", libraries=libs)
    for r in results:
        print(r["lib_id"], r["footprint"], r["description"])

    exists = check_footprint("Package_TO_SOT_SMD:SOT-23-5", libraries=libs)
"""

import sys
import os
import re
import json as json_module
import platform
from dataclasses import dataclass, field


# ─── KiCad root detection ────────────────────────────────────────

_KICAD_SEARCH_PATHS = []
if platform.system() == "Windows":
    _KICAD_SEARCH_PATHS = [
        os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "KiCad"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), "KiCad"),
    ]
elif platform.system() == "Darwin":
    _KICAD_SEARCH_PATHS = [
        "/Applications/KiCad/KiCad.app/Contents/SharedSupport",
        "/Applications/KiCad",
    ]
else:  # Linux
    _KICAD_SEARCH_PATHS = [
        "/usr/share/kicad",
        "/usr/local/share/kicad",
    ]


def find_kicad_root(kicad_path=None):
    """Locate the KiCad data directory containing symbols/ and footprints/.

    Args:
        kicad_path: Explicit path override. If None, searches standard locations.

    Returns:
        Path to the directory containing symbols/ and footprints/, or None.
    """
    if kicad_path and os.path.isdir(kicad_path):
        # Direct path — check if it has symbols/ directly or under share/kicad/
        if os.path.isdir(os.path.join(kicad_path, "symbols")):
            return kicad_path
        # Check versioned subdirs (e.g., KiCad/8.0/share/kicad/)
        for entry in sorted(os.listdir(kicad_path), reverse=True):
            candidate = os.path.join(kicad_path, entry, "share", "kicad")
            if os.path.isdir(os.path.join(candidate, "symbols")):
                return candidate
        return None

    for base in _KICAD_SEARCH_PATHS:
        if not os.path.isdir(base):
            continue
        # Direct: base/symbols/
        if os.path.isdir(os.path.join(base, "symbols")):
            return base
        # Versioned: base/8.0/share/kicad/symbols/
        for entry in sorted(os.listdir(base), reverse=True):
            candidate = os.path.join(base, entry, "share", "kicad")
            if os.path.isdir(os.path.join(candidate, "symbols")):
                return candidate
    return None


# ─── Library discovery (lib-tables + explicit) ───────────────────


@dataclass
class LibrarySet:
    """The set of symbol/footprint libraries to search.

    sym_libs / fp_libs map a KiCad library nickname → on-disk path. The nickname
    is what appears in a lib_id (``nick:Symbol``), so it is preserved end-to-end.
    For symbols the path is a ``.kicad_sym`` file; for footprints, a ``.pretty``
    directory.
    """
    sym_libs: dict = field(default_factory=dict)   # nick -> .kicad_sym path
    fp_libs: dict = field(default_factory=dict)     # nick -> .pretty dir
    notes: list = field(default_factory=list)


def find_kicad_config_dir(config_dir=None):
    """Locate the KiCad config dir holding sym-lib-table / fp-lib-table.

    Mirrors KiCad's per-user config location; highest version dir wins.
    """
    if config_dir and os.path.isfile(os.path.join(config_dir, "sym-lib-table")):
        return config_dir
    if platform.system() == "Windows":
        base = os.path.join(os.environ.get("APPDATA", ""), "kicad")
    elif platform.system() == "Darwin":
        base = os.path.expanduser("~/Library/Preferences/kicad")
    else:
        base = os.path.expanduser("~/.config/kicad")
    if not os.path.isdir(base):
        return None
    candidates = []
    for d in sorted(os.listdir(base), reverse=True):
        full = os.path.join(base, d)
        if os.path.isfile(os.path.join(full, "sym-lib-table")):
            candidates.append(full)
    return candidates[0] if candidates else None


def _kicad_uri_subs(kicad_root=None, project_dir=None):
    """Substitution map for ${VAR} tokens in lib-table URIs."""
    subs = {}
    if kicad_root:
        sym = os.path.join(kicad_root, "symbols")
        fp = os.path.join(kicad_root, "footprints")
        td = os.path.join(kicad_root, "3dmodels")
        # KiCad exposes these per major version; populate the common ones so a
        # table written by any of v6/7/8/9 resolves against this install.
        for ver in ("", "6", "7", "8", "9"):
            subs[f"KICAD{ver}_SYMBOL_DIR"] = sym
            subs[f"KICAD{ver}_FOOTPRINT_DIR"] = fp
            subs[f"KICAD{ver}_3DMODEL_DIR"] = td
    if project_dir:
        subs["KIPRJMOD"] = os.path.abspath(project_dir)
    return subs


def _resolve_uri(uri, subs):
    """Expand ${VAR} tokens using subs first, then the process environment."""
    def repl(m):
        var = m.group(1)
        if var in subs:
            return subs[var]
        return os.environ.get(var, m.group(0))
    return os.path.normpath(re.sub(r"\$\{([^}]+)\}", repl, uri))


def parse_lib_table(table_path, subs):
    """Parse a sym-lib-table / fp-lib-table → list of (nickname, resolved_uri).

    Only ``(type "KiCad")`` entries are returned (the on-disk kind this resolver
    can read); legacy/database/IPC libs are skipped with a note.
    """
    out, skipped = [], []
    if not table_path or not os.path.isfile(table_path):
        return out, skipped
    with open(table_path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    for m in re.finditer(
        r'\(lib\s+\(name\s+"([^"]+)"\)\s*\(type\s+"([^"]+)"\)\s*\(uri\s+"([^"]+)"\)',
        text,
    ):
        nick, kind, uri = m.group(1), m.group(2), m.group(3)
        if kind.lower() != "kicad":
            skipped.append(f"{nick} (type {kind})")
            continue
        out.append((nick, _resolve_uri(uri, subs)))
    return out, skipped


def _parse_extra(specs):
    """Parse --sym-lib / --fp-lib values into (nick, path).

    Accepts ``nick=path`` or a bare ``path`` (nickname derived from the file /
    directory stem, with a trailing ``.pretty`` stripped for footprints).
    """
    pairs = []
    for spec in specs or []:
        if "=" in spec and not os.path.exists(spec.split("=", 1)[0]):
            nick, path = spec.split("=", 1)
        else:
            path = spec
            stem = os.path.basename(os.path.normpath(path))
            for ext in (".kicad_sym", ".pretty"):
                if stem.endswith(ext):
                    stem = stem[: -len(ext)]
            nick = stem
        pairs.append((nick.strip(), os.path.abspath(path.strip())))
    return pairs


def build_library_set(
    kicad_root=None,
    project_dir=None,
    extra_sym=None,
    extra_fp=None,
    config_dir=None,
    include_builtin=True,
):
    """Discover every searchable library, honoring nickname precedence.

    Precedence (later overrides earlier on a nickname clash, matching KiCad):
    built-in install < global lib-table < project lib-table < explicit --*-lib.
    """
    if kicad_root is None:
        kicad_root = find_kicad_root()
    ls = LibrarySet()

    # 1. global lib-tables (resolve ${KICAD*_*_DIR} against this install)
    cfg = find_kicad_config_dir(config_dir)
    global_subs = _kicad_uri_subs(kicad_root, None)
    if cfg:
        syms, sk = parse_lib_table(os.path.join(cfg, "sym-lib-table"), global_subs)
        for nick, uri in syms:
            ls.sym_libs[nick] = uri
        fps, _ = parse_lib_table(os.path.join(cfg, "fp-lib-table"), global_subs)
        for nick, uri in fps:
            ls.fp_libs[nick] = uri
        ls.notes.extend(f"skipped global lib: {s}" for s in sk)

    # 2. built-in install fallback (fill gaps for nicknames not in the table)
    if include_builtin and kicad_root:
        sym_dir = os.path.join(kicad_root, "symbols")
        if os.path.isdir(sym_dir):
            for fn in os.listdir(sym_dir):
                if fn.endswith(".kicad_sym"):
                    ls.sym_libs.setdefault(fn[:-10], os.path.join(sym_dir, fn))
        fp_dir = os.path.join(kicad_root, "footprints")
        if os.path.isdir(fp_dir):
            for dn in os.listdir(fp_dir):
                if dn.endswith(".pretty"):
                    ls.fp_libs.setdefault(dn[:-7], os.path.join(fp_dir, dn))

    # 3. project lib-tables (override global/built-in)
    if project_dir:
        proj_subs = _kicad_uri_subs(kicad_root, project_dir)
        syms, sk = parse_lib_table(os.path.join(project_dir, "sym-lib-table"), proj_subs)
        for nick, uri in syms:
            ls.sym_libs[nick] = uri
        fps, _ = parse_lib_table(os.path.join(project_dir, "fp-lib-table"), proj_subs)
        for nick, uri in fps:
            ls.fp_libs[nick] = uri
        ls.notes.extend(f"skipped project lib: {s}" for s in sk)

    # 4. explicit overrides (highest precedence)
    for nick, path in _parse_extra(extra_sym):
        ls.sym_libs[nick] = path
    for nick, path in _parse_extra(extra_fp):
        ls.fp_libs[nick] = path

    return ls


# ─── Symbol search ───────────────────────────────────────────────

def _parse_symbol_entry(lib_name, sym_text):
    """Extract symbol metadata from a top-level symbol S-expression block.

    Returns dict with: name, lib_id, footprint, description, extends, pins.
    """
    result = {
        "name": "",
        "lib_id": "",
        "footprint": "",
        "description": "",
        "extends": "",
        "pins": [],
    }

    # Symbol name — first line
    m = re.search(r'\(symbol\s+"([^"]+)"', sym_text.split('\n')[0])
    if m:
        result["name"] = m.group(1)
        result["lib_id"] = f"{lib_name}:{m.group(1)}"

    # Extends
    m = re.search(r'\(extends\s+"([^"]+)"\)', sym_text)
    if m:
        result["extends"] = m.group(1)

    # Properties
    for prop_match in re.finditer(
        r'\(property\s+"(\w+)"\s+"([^"]*)"', sym_text
    ):
        key, val = prop_match.group(1), prop_match.group(2)
        if key == "Footprint":
            result["footprint"] = val
        elif key == "Description":
            result["description"] = val

    # Pins — from sub-symbol blocks (e.g., SymName_1_1)
    # Pattern: (pin <type> <shape>\n ... (name "X") ... (number "N"))
    pin_blocks = re.finditer(
        r'\(pin\s+(\w+)\s+\w+\s*\n'       # (pin power_in line
        r'(?:.*?\n)*?'                       #   ... position, length ...
        r'\s*\(name\s+"([^"]+)"'            #   (name "VIN"
        r'(?:.*?\n)*?'                       #   ... effects ...
        r'\s*\(number\s+"([^"]+)"',          #   (number "1"
        sym_text,
    )
    seen_pins = set()
    for pm in pin_blocks:
        pin_type, pin_name, pin_number = pm.group(1), pm.group(2), pm.group(3)
        if pin_number not in seen_pins:
            seen_pins.add(pin_number)
            result["pins"].append({
                "number": pin_number,
                "name": pin_name,
                "type": pin_type,
            })

    return result


def _split_symbols(file_text):
    """Split a .kicad_sym file into top-level symbol blocks.

    Top-level symbols start with a single-tab indent: \\t(symbol "Name"
    Sub-symbols have double-tab: \\t\\t(symbol "Name_0_1"
    """
    blocks = []
    current_lines = []
    depth = 0
    in_symbol = False

    for line in file_text.split('\n'):
        # Detect top-level symbol start (single tab)
        if re.match(r'^\t\(symbol\s+"', line) and not re.match(r'^\t\t', line):
            if in_symbol and current_lines:
                blocks.append('\n'.join(current_lines))
            current_lines = [line]
            in_symbol = True
            depth = 1
        elif in_symbol:
            current_lines.append(line)
            depth += line.count('(') - line.count(')')
            if depth <= 0:
                blocks.append('\n'.join(current_lines))
                current_lines = []
                in_symbol = False
                depth = 0

    if in_symbol and current_lines:
        blocks.append('\n'.join(current_lines))

    return blocks


def find_symbol(part_number, kicad_root=None, include_pins=False,
                libraries=None, project_dir=None, extra_sym=None):
    """Search registered KiCad symbol libraries for a part number.

    Searches by substring match against symbol names. Returns all matches
    ranked by relevance (exact > startswith > contains).

    For symbols that use `extends`, pin data is resolved from the parent symbol.

    Args:
        part_number: Part number to search (e.g., "AP2112K", "NAU7802").
        kicad_root: KiCad data directory. Auto-detected if None.
        include_pins: If True, include pin data in results.
        libraries: A pre-built LibrarySet. If None, one is built from
            kicad_root + project_dir + extra_sym (global + project lib-tables
            plus the built-in install).
        project_dir: Project dir whose lib-table to include (when libraries None).
        extra_sym: Explicit symbol libs ("nick=path" or "path"), highest priority.

    Returns:
        List of dicts, each with: lib_id, name, footprint, description, pins.
        Empty list if nothing found.
    """
    if libraries is None:
        libraries = build_library_set(
            kicad_root, project_dir=project_dir, extra_sym=extra_sym,
        )
    if not libraries.sym_libs:
        return []

    query = part_number.upper()
    exact = []
    prefix = []
    contains = []

    # Precedence: later-registered libraries win ties (built-in < global <
    # project < explicit), matching build_library_set's insertion order.
    prec = {nick: i for i, nick in enumerate(libraries.sym_libs)}

    for lib_name, fpath in libraries.sym_libs.items():
        if not os.path.isfile(fpath):
            continue

        # Quick pre-filter: does the file even contain this string?
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        if part_number.upper() not in content.upper():
            continue

        # Parse symbol blocks
        symbol_blocks = _split_symbols(content)
        # Index all symbols by name for extends resolution
        all_entries = {}
        for block in symbol_blocks:
            entry = _parse_symbol_entry(lib_name, block)
            if entry["name"]:
                all_entries[entry["name"]] = entry

        for name, entry in all_entries.items():
            name_upper = name.upper()
            if query not in name_upper:
                continue

            # Skip sub-symbol variants (handled via extends)
            if re.match(r'.+_\d+_\d+$', name):
                continue

            # Resolve extends — get pins from parent if this symbol inherits
            if include_pins and entry["extends"] and not entry["pins"]:
                parent = all_entries.get(entry["extends"])
                if parent:
                    entry["pins"] = parent["pins"]

            result_entry = {
                "lib_id": entry["lib_id"],
                "name": entry["name"],
                "footprint": entry["footprint"],
                "description": entry["description"],
                "_prec": prec.get(lib_name, -1),
            }
            if include_pins:
                result_entry["pins"] = sorted(
                    entry["pins"],
                    key=lambda p: int(p["number"]) if p["number"].isdigit() else p["number"],
                )

            # Rank by match quality
            if name_upper == query:
                exact.append(result_entry)
            elif name_upper.startswith(query):
                prefix.append(result_entry)
            else:
                contains.append(result_entry)

    # Within each group, sort alphabetically by name but break ties on identical
    # names by library precedence (higher wins). Return exact > prefix > contains.
    for group in (exact, prefix, contains):
        group.sort(key=lambda x: (x["name"], -x["_prec"]))

    return [{k: v for k, v in e.items() if k != "_prec"}
            for e in (exact + prefix + contains)]


def load_symbol_block(lib_id, libraries=None, kicad_root=None,
                      project_dir=None, extra_sym=None):
    """Return the raw top-level ``(symbol ...)`` block for a lib_id, or None.

    This is the source for embedding a real library symbol **verbatim** (its true
    drawing + pin geometry) via ``KicadSchematic.add_lib_symbol_from_block`` —
    rather than synthesizing a rectangle. ``extends`` is resolved to the parent's
    geometry block so the result always carries drawable pins/graphics.

    Args:
        lib_id: Full symbol ID, e.g. "Custom:NAU7802".
        libraries: Pre-built LibrarySet (built from the other args if None).
    """
    if ":" not in lib_id:
        return None
    if libraries is None:
        libraries = build_library_set(
            kicad_root, project_dir=project_dir, extra_sym=extra_sym,
        )
    nick, name = lib_id.split(":", 1)
    path = libraries.sym_libs.get(nick)
    if not path or not os.path.isfile(path):
        return None

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    blocks = {}
    for b in _split_symbols(content):
        nm = re.match(r'\s*\(symbol\s+"([^"]+)"', b)
        if nm:
            blocks[nm.group(1)] = b

    block = blocks.get(name)
    if block is None:
        return None

    # A symbol that `extends` a parent carries no geometry of its own; embed the
    # parent's block (which has the pins + drawing) instead.
    if "(pin " not in block:
        em = re.search(r'\(extends\s+"([^"]+)"\)', block)
        if em and em.group(1) in blocks:
            return blocks[em.group(1)]
    return block


# ─── Footprint check ─────────────────────────────────────────────

def check_footprint(footprint_id, kicad_root=None, libraries=None,
                    project_dir=None, extra_fp=None):
    """Verify a KiCad footprint exists in a registered footprint library.

    Resolves the footprint's library nickname against the LibrarySet (global +
    project lib-tables + explicit), falling back to the built-in install layout
    (``<kicad_root>/footprints/<nick>.pretty``) for nicknames not in any table.

    Args:
        footprint_id: Full footprint ID, e.g., "Package_TO_SOT_SMD:SOT-23-5".
        kicad_root: KiCad data directory. Auto-detected if None.
        libraries: Pre-built LibrarySet (built from the other args if None).

    Returns:
        Dict with: exists (bool), path (str or None), library (str), footprint (str).
    """
    if libraries is None:
        libraries = build_library_set(
            kicad_root, project_dir=project_dir, extra_fp=extra_fp,
        )

    result = {
        "footprint_id": footprint_id,
        "exists": False,
        "path": None,
        "library": "",
        "footprint": "",
    }

    if ":" not in footprint_id:
        return result

    lib_name, fp_name = footprint_id.split(":", 1)
    result["library"] = lib_name
    result["footprint"] = fp_name

    fp_dir = libraries.fp_libs.get(lib_name)
    if fp_dir is None and kicad_root:
        # built-in layout fallback for an unregistered nickname
        fp_dir = os.path.join(kicad_root, "footprints", f"{lib_name}.pretty")
    if not fp_dir:
        return result

    fp_path = os.path.join(fp_dir, f"{fp_name}.kicad_mod")
    if os.path.isfile(fp_path):
        result["exists"] = True
        result["path"] = fp_path

    return result


def search_footprint(query, kicad_root=None, limit=20, libraries=None,
                     project_dir=None, extra_fp=None):
    """Search for footprints by name substring across registered libraries.

    Args:
        query: Search string (e.g., "SOT-23", "QFN-32").
        kicad_root: KiCad data directory. Auto-detected if None.
        limit: Max results to return.
        libraries: Pre-built LibrarySet (built from the other args if None).

    Returns:
        List of footprint ID strings (e.g., "Package_TO_SOT_SMD:SOT-23-5").
    """
    if libraries is None:
        libraries = build_library_set(
            kicad_root, project_dir=project_dir, extra_fp=extra_fp,
        )

    query_upper = query.upper()
    results = []

    for lib_name in sorted(libraries.fp_libs):
        lib_path = libraries.fp_libs[lib_name]
        if not os.path.isdir(lib_path):
            continue
        for fp_file in sorted(os.listdir(lib_path)):
            if not fp_file.endswith(".kicad_mod"):
                continue
            fp_name = fp_file[:-len(".kicad_mod")]
            if query_upper in fp_name.upper():
                results.append(f"{lib_name}:{fp_name}")
                if len(results) >= limit:
                    return results

    return results


# ─── Combined lookup for BOM workflow ────────────────────────────

def lookup_part(part_number, kicad_root=None, libraries=None,
                project_dir=None, extra_sym=None, extra_fp=None):
    """Full lookup for a BOM part: find symbol, verify its footprint, get pins.

    This is the main entry point for the skill workflow. Given a part number,
    returns everything needed to place it in a schematic — searching the user's
    own libraries (Custom, project, explicit) as well as KiCad's built-ins.

    Args:
        part_number: Part number (e.g., "AP2112K-3.3", "CH340G", "NAU7802").
        kicad_root: KiCad data directory. Auto-detected if None.
        libraries: Pre-built LibrarySet (built from the other args if None).
        project_dir: Project dir whose lib-table to include.
        extra_sym / extra_fp: Explicit libraries ("nick=path" or "path").

    Returns:
        Dict with:
            found (bool): Whether a symbol was found.
            lib_id (str): Full symbol ID for place_component() (real nickname).
            footprint (str): Full footprint ID for place_component().
            footprint_exists (bool): Whether the footprint file exists on disk.
            from_user_library (bool): True if resolved from a non-built-in lib —
                its pins are authoritative and may seed a fact card as verified.
            description (str): Part description from KiCad library.
            pins (list): Pin data [{number, name, type}, ...].
            alternatives (list): Other matching symbols if multiple found.
            message (str): Human-readable summary.
    """
    if libraries is None:
        libraries = build_library_set(
            kicad_root, project_dir=project_dir,
            extra_sym=extra_sym, extra_fp=extra_fp,
        )

    result = {
        "found": False,
        "lib_id": "",
        "footprint": "",
        "footprint_exists": False,
        "from_user_library": False,
        "description": "",
        "pins": [],
        "alternatives": [],
        "message": "",
    }

    if not libraries.sym_libs:
        result["message"] = "No symbol libraries found (KiCad install not located)"
        return result

    symbols = find_symbol(part_number, libraries=libraries, include_pins=True)

    if not symbols:
        result["message"] = f"No symbol found for '{part_number}' in registered libraries"
        return result

    best = symbols[0]
    result["found"] = True
    result["lib_id"] = best["lib_id"]
    result["footprint"] = best["footprint"]
    result["description"] = best["description"]
    result["pins"] = best.get("pins", [])
    # A symbol whose library path is not under the built-in install is a
    # user-supplied symbol — its pins are the authoritative source for the part.
    best_nick = best["lib_id"].split(":", 1)[0]
    best_path = libraries.sym_libs.get(best_nick, "")
    builtin_syms = os.path.join(kicad_root or find_kicad_root() or "", "symbols")
    result["from_user_library"] = bool(
        best_path and (not builtin_syms or
                       os.path.normcase(os.path.dirname(os.path.abspath(best_path)))
                       != os.path.normcase(os.path.abspath(builtin_syms)))
    )

    if len(symbols) > 1:
        result["alternatives"] = [
            {"lib_id": s["lib_id"], "footprint": s["footprint"], "description": s["description"]}
            for s in symbols[1:10]  # cap at 10 alternatives
        ]

    # Verify footprint exists
    if best["footprint"]:
        fp_check = check_footprint(best["footprint"], kicad_root, libraries=libraries)
        result["footprint_exists"] = fp_check["exists"]
    else:
        result["footprint_exists"] = False

    # Build message
    parts = [f"Symbol: {result['lib_id']}"]
    if result["footprint"]:
        fp_status = "verified" if result["footprint_exists"] else "NOT FOUND on disk"
        parts.append(f"Footprint: {result['footprint']} ({fp_status})")
    else:
        parts.append("Footprint: none assigned in symbol")
    if result["alternatives"]:
        parts.append(f"{len(result['alternatives'])} other variants available")
    result["message"] = " | ".join(parts)

    return result


# ─── Output formatters ───────────────────────────────────────────

def format_symbol_results_text(results, query):
    lines = []
    lines.append("=" * 60)
    lines.append(f"SYMBOL SEARCH: {query}")
    lines.append("=" * 60)

    if not results:
        lines.append(f"No symbols found matching '{query}'")
        lines.append("")
        lines.append("The part may not be in KiCad's built-in libraries.")
        lines.append("Check SnapEDA, Ultra Librarian, or the manufacturer's site.")
    else:
        lines.append(f"Found {len(results)} match(es):")
        lines.append("")
        for r in results:
            lines.append(f"  {r['lib_id']}")
            if r["footprint"]:
                lines.append(f"    Footprint: {r['footprint']}")
            if r["description"]:
                lines.append(f"    {r['description']}")
            if r.get("pins"):
                lines.append(f"    Pins: {len(r['pins'])}")
                for pin in r["pins"]:
                    lines.append(f"      {pin['number']:>3}: {pin['name']} ({pin['type']})")
            lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def format_lookup_result_text(result, query):
    lines = []
    lines.append("=" * 60)
    lines.append(f"PART LOOKUP: {query}")
    lines.append("=" * 60)

    if not result["found"]:
        lines.append(f"NOT FOUND: {result['message']}")
        lines.append("")
        lines.append("Options:")
        lines.append("  1. Search SnapEDA (snapeda.com) for symbol + footprint")
        lines.append("  2. Check Ultra Librarian (ultralibrarian.com)")
        lines.append("  3. Download from the manufacturer's website")
        lines.append("  4. Use a generic symbol with custom pinout")
    else:
        lines.append(f"Symbol:      {result['lib_id']}")
        if result.get("from_user_library"):
            lines.append("Source:      user library (pins are authoritative - "
                         "may seed a fact card as verified)")
        if result["description"]:
            lines.append(f"Description: {result['description']}")
        if result["footprint"]:
            fp_status = "EXISTS" if result["footprint_exists"] else "NOT FOUND"
            lines.append(f"Footprint:   {result['footprint']} [{fp_status}]")
        else:
            lines.append("Footprint:   (none assigned in symbol)")
        lines.append(f"Pins:        {len(result['pins'])}")
        if result["pins"]:
            for pin in result["pins"]:
                lines.append(f"  {pin['number']:>3}: {pin['name']} ({pin['type']})")
        if result["alternatives"]:
            lines.append("")
            lines.append(f"Other variants ({len(result['alternatives'])}):")
            for alt in result["alternatives"]:
                lines.append(f"  {alt['lib_id']}")
                if alt["description"]:
                    lines.append(f"    {alt['description']}")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


# ─── CLI ──────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Search KiCad symbol/footprint libraries (built-in + user).",
    )
    parser.add_argument("part", nargs="?", help="Part number to search for")
    parser.add_argument("--footprint", "-f", help="Check/search a specific footprint ID")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--pins", action="store_true", help="Include pin data in results")
    parser.add_argument("--kicad-path", help="Override KiCad installation path")
    parser.add_argument("--lookup", action="store_true",
                        help="Full part lookup (symbol + footprint + pins)")
    parser.add_argument("--project-dir",
                        help="Project dir whose sym-lib-table/fp-lib-table to include")
    parser.add_argument("--sym-lib", action="append", metavar="[NICK=]PATH",
                        help="Extra symbol library (.kicad_sym); repeatable, highest priority")
    parser.add_argument("--fp-lib", action="append", metavar="[NICK=]PATH",
                        help="Extra footprint library (.pretty dir); repeatable")
    parser.add_argument("--config-dir",
                        help="KiCad config dir holding the global sym-lib-table")
    parser.add_argument("--no-builtin", action="store_true",
                        help="Skip KiCad's built-in libraries (search only user libs)")
    parser.add_argument("--list-libs", action="store_true",
                        help="List every discovered library and exit")
    args = parser.parse_args()

    kicad_root = find_kicad_root(args.kicad_path)

    libraries = build_library_set(
        kicad_root,
        project_dir=args.project_dir,
        extra_sym=args.sym_lib,
        extra_fp=args.fp_lib,
        config_dir=args.config_dir,
        include_builtin=not args.no_builtin,
    )

    if args.list_libs:
        payload = {
            "symbol_libs": libraries.sym_libs,
            "footprint_libs": libraries.fp_libs,
            "notes": libraries.notes,
        }
        if args.json:
            print(json_module.dumps(payload, indent=2))
        else:
            print(f"Symbol libraries ({len(libraries.sym_libs)}):")
            for nick, path in sorted(libraries.sym_libs.items()):
                print(f"  {nick}: {path}")
            print(f"Footprint libraries ({len(libraries.fp_libs)}):")
            for nick, path in sorted(libraries.fp_libs.items()):
                print(f"  {nick}: {path}")
            for n in libraries.notes:
                print(f"  note: {n}")
        sys.exit(0)

    if not libraries.sym_libs and not libraries.fp_libs:
        print("ERROR: No libraries found. Could not locate a KiCad install or any "
              "lib-table; pass --kicad-path / --sym-lib / --project-dir.",
              file=sys.stderr)
        sys.exit(2)

    # Footprint check/search mode
    if args.footprint:
        if ":" in args.footprint:
            result = check_footprint(args.footprint, kicad_root, libraries=libraries)
            if args.json:
                print(json_module.dumps(result, indent=2))
            else:
                status = "EXISTS" if result["exists"] else "NOT FOUND"
                print(f"Footprint: {args.footprint} [{status}]")
                if result["path"]:
                    print(f"Path: {result['path']}")
        else:
            results = search_footprint(args.footprint, kicad_root, libraries=libraries)
            if args.json:
                print(json_module.dumps(results, indent=2))
            else:
                if results:
                    print(f"Footprints matching '{args.footprint}':")
                    for fp in results:
                        print(f"  {fp}")
                else:
                    print(f"No footprints found matching '{args.footprint}'")
        sys.exit(0)

    # Part search mode
    if not args.part:
        parser.print_help()
        sys.exit(1)

    if args.lookup:
        result = lookup_part(args.part, kicad_root, libraries=libraries)
        if args.json:
            print(json_module.dumps(result, indent=2))
        else:
            print(format_lookup_result_text(result, args.part))
        sys.exit(0 if result["found"] else 1)

    # Default: symbol search
    results = find_symbol(args.part, libraries=libraries, include_pins=args.pins)
    if args.json:
        print(json_module.dumps(results, indent=2))
    else:
        print(format_symbol_results_text(results, args.part))
    sys.exit(0 if results else 1)


if __name__ == "__main__":
    main()
