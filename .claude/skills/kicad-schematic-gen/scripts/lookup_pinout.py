#!/usr/bin/env python3
"""
Pinout Lookup — queries the verified pinout database for IC pin assignments.

Reduces hallucination risk when building implementation references (Stage 4)
by providing verified pin assignments from the local database.

CLI Usage:
    python lookup_pinout.py AP2112K-3.3
    python lookup_pinout.py AP2112K --json

Python API:
    from lookup_pinout import lookup_pinout, get_ic_pins_for_generator

    pinout = lookup_pinout("AP2112K-3.3")
    if pinout:
        pins = get_ic_pins_for_generator("AP2112K-3.3")
        sch.add_lib_symbol_ic("custom:AP2112K", pins=pins, ...)
"""

import sys
import os
import json as json_module

_script_dir = os.path.dirname(os.path.abspath(__file__))
_default_db_path = os.path.join(_script_dir, "..", "pinouts", "pinout_db.json")

_cached_db = None


def load_pinout_db(db_path=None):
    """Load the pinout database from JSON file.

    Args:
        db_path: Path to pinout_db.json. Defaults to ../pinouts/pinout_db.json
    """
    global _cached_db
    if db_path is None:
        db_path = _default_db_path
    if _cached_db is None or db_path != _default_db_path:
        with open(db_path, "r") as f:
            _cached_db = json_module.load(f)
    return _cached_db


def lookup_pinout(part_number, db=None):
    """Look up a part by exact or fuzzy match.

    Tries: exact match, case-insensitive, prefix match (e.g., "AP2112K" matches "AP2112K-3.3").

    Returns:
        Dict with part data (package, pins, notes, etc.) or None if not found.
        Adds a "matched_part" key with the exact key that matched.
    """
    if db is None:
        db = load_pinout_db()

    # Exact match
    if part_number in db:
        result = dict(db[part_number])
        result["matched_part"] = part_number
        return result

    # Case-insensitive match
    pn_lower = part_number.lower()
    for key in db:
        if key.lower() == pn_lower:
            result = dict(db[key])
            result["matched_part"] = key
            return result

    # Prefix match (e.g., "AP2112K" matches "AP2112K-3.3")
    matches = []
    for key in db:
        if key.lower().startswith(pn_lower) or pn_lower.startswith(key.lower()):
            matches.append(key)

    if len(matches) == 1:
        result = dict(db[matches[0]])
        result["matched_part"] = matches[0]
        return result

    if len(matches) > 1:
        # Return the shortest match (most likely the base part)
        best = min(matches, key=len)
        result = dict(db[best])
        result["matched_part"] = best
        result["other_matches"] = [m for m in matches if m != best]
        return result

    return None


# Pin type mapping: pinout_db types → KiCad add_lib_symbol_ic types
_TYPE_MAP = {
    "power_in": "power_in",
    "power_out": "power_out",
    "input": "input",
    "output": "output",
    "bidirectional": "bidirectional",
    "passive": "passive",
    "no_connect": "passive",  # NC pins still need a type for the symbol
}


def get_ic_pins_for_generator(part_number, db=None):
    """Return pins in the format expected by KicadSchematic.add_lib_symbol_ic().

    Returns:
        List of (pin_number, pin_name, pin_type, side, position_index) tuples.
        Returns empty list if part not found.

    Side assignment heuristic:
        - power_in pins → left
        - power_out pins → right
        - input pins → left
        - output pins → right
        - bidirectional → right
        - passive/no_connect → right
    """
    data = lookup_pinout(part_number, db)
    if data is None:
        return []

    pins_data = data.get("pins", {})

    # Assign sides
    left_pins = []
    right_pins = []

    for pin_num, pin_info in sorted(pins_data.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
        pin_name = pin_info["name"]
        raw_type = pin_info.get("type", "passive")
        pin_type = _TYPE_MAP.get(raw_type, "passive")
        side = "left" if raw_type in ("power_in", "input") else "right"

        if side == "left":
            left_pins.append((pin_num, pin_name, pin_type))
        else:
            right_pins.append((pin_num, pin_name, pin_type))

    # Build output with position indices
    result = []
    for idx, (pnum, pname, ptype) in enumerate(left_pins):
        result.append((pnum, pname, ptype, "left", idx))
    for idx, (pnum, pname, ptype) in enumerate(right_pins):
        result.append((pnum, pname, ptype, "right", idx))

    return result


# ─── CLI ──────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Look up IC pinouts from the verified database.")
    parser.add_argument("part", help="Part number to look up")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--generator", action="store_true",
                        help="Output in add_lib_symbol_ic() format")
    args = parser.parse_args()

    result = lookup_pinout(args.part)
    if result is None:
        print(f"Part '{args.part}' not found in database.", file=sys.stderr)
        sys.exit(1)

    if args.generator:
        pins = get_ic_pins_for_generator(args.part)
        if args.json:
            print(json_module.dumps(pins, indent=2))
        else:
            for pnum, pname, ptype, side, idx in pins:
                print(f"  ({pnum!r}, {pname!r}, {ptype!r}, {side!r}, {idx}),")
    elif args.json:
        print(json_module.dumps(result, indent=2))
    else:
        print(f"Part: {result.get('matched_part', args.part)}")
        print(f"Package: {result.get('package', '?')}")
        print(f"Manufacturer: {result.get('manufacturer', '?')}")
        print(f"Pins:")
        for pnum, pinfo in sorted(result["pins"].items(),
                                   key=lambda x: int(x[0]) if x[0].isdigit() else 0):
            print(f"  {pnum}: {pinfo['name']} ({pinfo['type']})")
        if result.get("notes"):
            print(f"Notes: {result['notes']}")


if __name__ == "__main__":
    main()
