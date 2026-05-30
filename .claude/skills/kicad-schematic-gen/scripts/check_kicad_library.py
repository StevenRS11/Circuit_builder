#!/usr/bin/env python3
"""
KiCad Library Checker — searches installed KiCad symbol and footprint libraries.

Given a part number, finds the matching KiCad symbol (lib_id) and its default
footprint. Given a footprint string, verifies it exists on disk. Returns
everything needed to place the part in a schematic without manual lookup.

Searches the standard KiCad installation path and resolves symbols that use
`extends` inheritance to return full pin data from the parent.

CLI Usage:
    python check_kicad_library.py AP2112K
    python check_kicad_library.py AP2112K --json
    python check_kicad_library.py --footprint "Package_TO_SOT_SMD:SOT-23-5"
    python check_kicad_library.py --footprint "Package_TO_SOT_SMD:SOT-23-5" --json
    python check_kicad_library.py AP2112K --pins
    python check_kicad_library.py AP2112K --pins --json

Python API:
    from check_kicad_library import find_symbol, check_footprint, find_kicad_root

    results = find_symbol("AP2112K")
    for r in results:
        print(r["lib_id"], r["footprint"], r["description"])

    exists = check_footprint("Package_TO_SOT_SMD:SOT-23-5")
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


def find_symbol(part_number, kicad_root=None, include_pins=False):
    """Search KiCad symbol libraries for a part number.

    Searches by substring match against symbol names. Returns all matches
    ranked by relevance (exact > startswith > contains).

    For symbols that use `extends`, pin data is resolved from the parent symbol.

    Args:
        part_number: Part number to search (e.g., "AP2112K", "CH340G").
        kicad_root: KiCad data directory. Auto-detected if None.
        include_pins: If True, include pin data in results.

    Returns:
        List of dicts, each with: lib_id, name, footprint, description, pins.
        Empty list if nothing found.
    """
    if kicad_root is None:
        kicad_root = find_kicad_root()
    if kicad_root is None:
        return []

    sym_dir = os.path.join(kicad_root, "symbols")
    if not os.path.isdir(sym_dir):
        return []

    query = part_number.upper()
    exact = []
    prefix = []
    contains = []

    for fname in os.listdir(sym_dir):
        if not fname.endswith(".kicad_sym"):
            continue

        lib_name = fname.replace(".kicad_sym", "")
        fpath = os.path.join(sym_dir, fname)

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

    # Sort each group alphabetically, return exact > prefix > contains
    for group in (exact, prefix, contains):
        group.sort(key=lambda x: x["name"])

    return exact + prefix + contains


# ─── Footprint check ─────────────────────────────────────────────

def check_footprint(footprint_id, kicad_root=None):
    """Verify a KiCad footprint exists in the installed libraries.

    Args:
        footprint_id: Full footprint ID, e.g., "Package_TO_SOT_SMD:SOT-23-5".
        kicad_root: KiCad data directory. Auto-detected if None.

    Returns:
        Dict with: exists (bool), path (str or None), library (str), footprint (str).
    """
    if kicad_root is None:
        kicad_root = find_kicad_root()

    result = {
        "footprint_id": footprint_id,
        "exists": False,
        "path": None,
        "library": "",
        "footprint": "",
    }

    if not kicad_root or ":" not in footprint_id:
        return result

    lib_name, fp_name = footprint_id.split(":", 1)
    result["library"] = lib_name
    result["footprint"] = fp_name

    fp_dir = os.path.join(kicad_root, "footprints")
    fp_path = os.path.join(fp_dir, f"{lib_name}.pretty", f"{fp_name}.kicad_mod")

    if os.path.isfile(fp_path):
        result["exists"] = True
        result["path"] = fp_path

    return result


def search_footprint(query, kicad_root=None, limit=20):
    """Search for footprints by name substring.

    Args:
        query: Search string (e.g., "SOT-23", "QFN-32").
        kicad_root: KiCad data directory. Auto-detected if None.
        limit: Max results to return.

    Returns:
        List of footprint ID strings (e.g., "Package_TO_SOT_SMD:SOT-23-5").
    """
    if kicad_root is None:
        kicad_root = find_kicad_root()
    if kicad_root is None:
        return []

    fp_dir = os.path.join(kicad_root, "footprints")
    if not os.path.isdir(fp_dir):
        return []

    query_upper = query.upper()
    results = []

    for lib_dir in sorted(os.listdir(fp_dir)):
        if not lib_dir.endswith(".pretty"):
            continue
        lib_name = lib_dir.replace(".pretty", "")
        lib_path = os.path.join(fp_dir, lib_dir)

        for fp_file in sorted(os.listdir(lib_path)):
            if not fp_file.endswith(".kicad_mod"):
                continue
            fp_name = fp_file.replace(".kicad_mod", "")
            if query_upper in fp_name.upper():
                results.append(f"{lib_name}:{fp_name}")
                if len(results) >= limit:
                    return results

    return results


# ─── Combined lookup for BOM workflow ────────────────────────────

def lookup_part(part_number, kicad_root=None):
    """Full lookup for a BOM part: find symbol, verify its footprint, get pins.

    This is the main entry point for the skill workflow. Given a part number,
    returns everything needed to place it in a schematic.

    Args:
        part_number: Part number (e.g., "AP2112K-3.3", "CH340G", "BME280").

    Returns:
        Dict with:
            found (bool): Whether a symbol was found.
            lib_id (str): Full symbol ID for place_component().
            footprint (str): Full footprint ID for place_component().
            footprint_exists (bool): Whether the footprint file exists on disk.
            description (str): Part description from KiCad library.
            pins (list): Pin data [{number, name, type}, ...].
            alternatives (list): Other matching symbols if multiple found.
            message (str): Human-readable summary.
    """
    if kicad_root is None:
        kicad_root = find_kicad_root()

    result = {
        "found": False,
        "lib_id": "",
        "footprint": "",
        "footprint_exists": False,
        "description": "",
        "pins": [],
        "alternatives": [],
        "message": "",
    }

    if kicad_root is None:
        result["message"] = "KiCad installation not found"
        return result

    symbols = find_symbol(part_number, kicad_root, include_pins=True)

    if not symbols:
        result["message"] = f"No symbol found for '{part_number}' in KiCad libraries"
        return result

    best = symbols[0]
    result["found"] = True
    result["lib_id"] = best["lib_id"]
    result["footprint"] = best["footprint"]
    result["description"] = best["description"]
    result["pins"] = best.get("pins", [])

    if len(symbols) > 1:
        result["alternatives"] = [
            {"lib_id": s["lib_id"], "footprint": s["footprint"], "description": s["description"]}
            for s in symbols[1:10]  # cap at 10 alternatives
        ]

    # Verify footprint exists
    if best["footprint"]:
        fp_check = check_footprint(best["footprint"], kicad_root)
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
        description="Search KiCad installed libraries for symbols and footprints.",
    )
    parser.add_argument("part", nargs="?", help="Part number to search for")
    parser.add_argument("--footprint", "-f", help="Check/search a specific footprint ID")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--pins", action="store_true", help="Include pin data in results")
    parser.add_argument("--kicad-path", help="Override KiCad installation path")
    parser.add_argument("--lookup", action="store_true",
                        help="Full part lookup (symbol + footprint + pins)")
    args = parser.parse_args()

    kicad_root = find_kicad_root(args.kicad_path)
    if kicad_root is None:
        print("ERROR: Could not find KiCad installation.", file=sys.stderr)
        print("Use --kicad-path to specify the path manually.", file=sys.stderr)
        sys.exit(2)

    # Footprint check/search mode
    if args.footprint:
        if ":" in args.footprint:
            result = check_footprint(args.footprint, kicad_root)
            if args.json:
                print(json_module.dumps(result, indent=2))
            else:
                status = "EXISTS" if result["exists"] else "NOT FOUND"
                print(f"Footprint: {args.footprint} [{status}]")
                if result["path"]:
                    print(f"Path: {result['path']}")
        else:
            results = search_footprint(args.footprint, kicad_root)
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
        result = lookup_part(args.part, kicad_root)
        if args.json:
            print(json_module.dumps(result, indent=2))
        else:
            print(format_lookup_result_text(result, args.part))
        sys.exit(0 if result["found"] else 1)

    # Default: symbol search
    results = find_symbol(args.part, kicad_root, include_pins=args.pins)
    if args.json:
        print(json_module.dumps(results, indent=2))
    else:
        print(format_symbol_results_text(results, args.part))
    sys.exit(0 if results else 1)


if __name__ == "__main__":
    main()
