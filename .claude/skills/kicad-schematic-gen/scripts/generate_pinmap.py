#!/usr/bin/env python3
"""generate_pinmap.py — firmware pinmap handoff (roadmap item A2).

The boards this skill builds are mostly MCU test fixtures: the schematic is
half the product, the firmware that drives it is the other half — and the seam
between them ("which GPIO is DRDY on?") is where a whole defect class lives
(firmware GPIO ≠ schematic GPIO). The schematic already knows every MCU pin ↔
net binding, so emit it as code instead of having a human re-derive it.

Pure transform of an authored/extracted artifact (fits the design hierarchy:
no judgment, no network). Works on generated *and* existing boards — the
kicad-board-context skill's extracted schematic feeds it identically.

Outputs:
  * ``board_pins.h`` — one ``#define PIN_<NET> <gpio>`` per MCU GPIO that is
    wired to a *named* net, with pin number/name provenance per line. Output
    is byte-deterministic (provenance is source + sha256, no timestamp) so it
    can live in a firmware repo without churn.
  * ``bringup.ino`` (``--sketch``) — a bringup skeleton: serial banner, I²C
    scan on the board's SDA/SCL pins if present, status-LED blink if present.

MCU selection: ``--mcu <ref>`` wins; otherwise auto-detect by part-name match
(ESP32/RP2040/STM32/…), falling back to "the component with the most
GPIO-looking pin names". Ambiguity is an error, never a guess.

GPIO parsing from symbol pin names (first token of a "/"-split name):
  IO4 / GPIO4 / GP4  → numeric Arduino pin 4
  PA5                → symbolic port define (STM32-style cores accept PA5)
Anything else (3V3, EN, GND, XTAL…) is not a GPIO and is skipped, listed in
the header's audit comment so nothing silently vanishes.

CLI:
    python generate_pinmap.py board.kicad_sch -o out_dir [--mcu U1] [--sketch]
    python generate_pinmap.py board.kicad_sch --json
    python generate_pinmap.py board.kicad_sch --project-dir <dir>   # stale-cache libs
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from validate_kicad_sch import load_kicad_sch, extract_netlist


MCU_NAME_PATTERN = re.compile(
    r"ESP32|ESP8266|RP2040|RP2350|STM32|ATMEGA|ATSAMD|SAMD2|SAMD5|NRF52"
    r"|CH32|MSP430|ATTINY|PIC(?:16|18|24|32)", re.IGNORECASE)

_GPIO_NUM = re.compile(r"^(?:GPIO|IO|GP)(\d+)$", re.IGNORECASE)
_GPIO_PORT = re.compile(r"^P([A-H])(\d{1,2})$")


def _gpio_of(pin_name):
    """Parse a symbol pin name into a GPIO value: int, 'PA5'-style str, or None."""
    for token in re.split(r"[/\s]+", (pin_name or "").strip()):
        m = _GPIO_NUM.match(token)
        if m:
            return int(m.group(1))
        m = _GPIO_PORT.match(token)
        if m:
            return token.upper()
    return None


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _macro_name(net_name):
    s = re.sub(r"[^A-Za-z0-9]+", "_", net_name).strip("_").upper()
    if not s:
        s = "NET"
    if s[0].isdigit():
        s = "_" + s
    return "PIN_" + s


def detect_mcu(sch):
    """Return the MCU component, or raise ValueError (ambiguous / none found)."""
    candidates = []
    for comp in sch.components:
        if comp.reference.startswith("#"):
            continue
        lib_sym = sch.lib_symbols.get(comp.lib_id)
        if lib_sym is None or lib_sym.is_power:
            continue
        if MCU_NAME_PATTERN.search(f"{comp.value} {comp.lib_id}"):
            candidates.append(comp)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        refs = sorted(c.reference for c in candidates)
        raise ValueError(f"multiple MCU candidates {refs} — pass --mcu <ref>")

    # Fallback: the part with the most GPIO-looking pin names.
    scored = []
    for comp in sch.components:
        lib_sym = sch.lib_symbols.get(comp.lib_id)
        if lib_sym is None or lib_sym.is_power or comp.reference.startswith("#"):
            continue
        n = sum(1 for p in lib_sym.pins if _gpio_of(p.name) is not None)
        if n >= 4:
            scored.append((n, comp))
    if len(scored) == 1:
        return scored[0][1]
    if len(scored) > 1:
        scored.sort(key=lambda t: -t[0])
        if scored[0][0] > scored[1][0]:
            return scored[0][1]
        refs = sorted(c.reference for _n, c in scored)
        raise ValueError(f"ambiguous MCU candidates {refs} — pass --mcu <ref>")
    raise ValueError("no MCU found (no part matches known MCU names and none "
                     "has ≥4 GPIO-named pins) — pass --mcu <ref>")


def build_pinmap(sch, mcu_ref=None):
    """Map every MCU GPIO to its net. Returns a dict (see keys below)."""
    if mcu_ref:
        mcu = next((c for c in sch.components if c.reference == mcu_ref), None)
        if mcu is None:
            raise ValueError(f"--mcu {mcu_ref}: no such reference in schematic")
    else:
        mcu = detect_mcu(sch)

    lib_sym = sch.lib_symbols.get(mcu.lib_id)
    if lib_sym is None:
        raise ValueError(f"{mcu.reference}: lib symbol '{mcu.lib_id}' unresolved "
                         f"— pass --project-dir / --sym-lib")

    netlist = extract_netlist(sch)
    entries, skipped, used_macros = [], [], {}

    seen_pins = set()
    for pin in lib_sym.pins:
        if pin.number in seen_pins:
            continue
        seen_pins.add(pin.number)
        gpio = _gpio_of(pin.name)
        net = netlist.get_net_for_pin(mcu.reference, pin.number)
        net_entry = netlist.nets.get(net) if net else None

        if gpio is None:
            skipped.append({"pin": pin.number, "name": pin.name,
                            "net": net or "", "reason": "not a GPIO"})
            continue
        # A truly unconnected / NC'd pin still surfaces as a single-pin
        # auto-named net in the extractor — treat both shapes as unconnected.
        if net is None or (net_entry is not None and not net_entry.has_label
                           and len(net_entry.pins) <= 1):
            skipped.append({"pin": pin.number, "name": pin.name,
                            "net": "", "reason": "unconnected"})
            continue
        if net_entry is not None and net_entry.is_power:
            skipped.append({"pin": pin.number, "name": pin.name,
                            "net": net, "reason": "power net"})
            continue
        if net_entry is not None and not net_entry.has_label:
            skipped.append({"pin": pin.number, "name": pin.name,
                            "net": net, "reason": "unlabeled net"})
            continue

        macro = _macro_name(net)
        if macro in used_macros:
            macro = f"{macro}_P{pin.number}"
        used_macros[macro] = True
        entries.append({"macro": macro, "gpio": gpio, "net": net,
                        "pin": pin.number, "pin_name": pin.name})

    entries.sort(key=lambda e: (isinstance(e["gpio"], str), str(e["gpio"]).zfill(4)
                                if isinstance(e["gpio"], int) else e["gpio"]))
    skipped.sort(key=lambda s: (len(s["pin"]), s["pin"]))
    return {"mcu_ref": mcu.reference, "mcu_part": mcu.value,
            "mcu_lib_id": mcu.lib_id, "entries": entries, "skipped": skipped}


# ─── emission ────────────────────────────────────────────────────────

def emit_header(pinmap, sch_path, board_name=None):
    name = board_name or os.path.splitext(os.path.basename(sch_path))[0]
    lines = [
        "// board_pins.h — MCU pin map generated from the schematic. DO NOT EDIT.",
        f"// board:  {name}",
        f"// mcu:    {pinmap['mcu_ref']} ({pinmap['mcu_part']})",
        f"// source: {os.path.basename(sch_path)}",
        f"// sha256: {_sha256(sch_path)}",
        "// tool:   kicad-schematic-gen/scripts/generate_pinmap.py",
        "#pragma once",
        "",
    ]
    if pinmap["entries"]:
        width = max(len(e["macro"]) for e in pinmap["entries"])
        for e in pinmap["entries"]:
            lines.append(
                f"#define {e['macro']:<{width}} {e['gpio']}"
                f"  // {pinmap['mcu_ref']} pin {e['pin']} \"{e['pin_name']}\""
                f" -> net {e['net']}")
    lines.append("")
    lines.append("// Not mapped (audit trail — nothing silently vanishes):")
    for s in pinmap["skipped"]:
        net = f" net {s['net']}" if s["net"] else ""
        lines.append(f"//   pin {s['pin']:>3} \"{s['name']}\"{net} — {s['reason']}")
    lines.append("")
    return "\n".join(lines)


def _find_entry(pinmap, pattern):
    rx = re.compile(pattern, re.IGNORECASE)
    for e in pinmap["entries"]:
        if rx.search(e["net"]):
            return e
    return None


def emit_sketch(pinmap, sch_path, board_name=None):
    name = board_name or os.path.splitext(os.path.basename(sch_path))[0]
    sda = _find_entry(pinmap, r"SDA")
    scl = _find_entry(pinmap, r"SCL")
    led = _find_entry(pinmap, r"LED")

    lines = [
        f"// bringup.ino — first-power-on skeleton for {name}. Generated; edit freely.",
        f"// mcu: {pinmap['mcu_ref']} ({pinmap['mcu_part']})",
        '#include "board_pins.h"',
    ]
    if sda and scl:
        lines.append("#include <Wire.h>")
    lines += ["", "void setup() {", "  Serial.begin(115200);",
              "  delay(500);",
              f'  Serial.println("bringup: {name}");']
    if led:
        lines.append(f"  pinMode({led['macro']}, OUTPUT);")
    if sda and scl:
        lines += [
            "",
            f"  Wire.begin({sda['macro']}, {scl['macro']});",
            "  // I2C scan — compare against the BOM's expected addresses",
            "  for (uint8_t a = 1; a < 127; a++) {",
            "    Wire.beginTransmission(a);",
            "    if (Wire.endTransmission() == 0) {",
            '      Serial.printf("I2C device at 0x%02X\\n", a);',
            "    }",
            "  }",
        ]
    lines += ["}", "", "void loop() {"]
    if led:
        lines += [f"  digitalWrite({led['macro']}, HIGH); delay(500);",
                  f"  digitalWrite({led['macro']}, LOW);  delay(500);"]
    else:
        lines.append("  delay(1000);")
    lines += ["}", ""]
    return "\n".join(lines)


# ─── CLI ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Generate a firmware pin map (board_pins.h) from a .kicad_sch")
    ap.add_argument("schematic", help="path to .kicad_sch")
    ap.add_argument("-o", "--output-dir",
                    help="write board_pins.h (+ bringup.ino) here; default stdout")
    ap.add_argument("--mcu", help="MCU reference (e.g. U1); default auto-detect")
    ap.add_argument("--name", help="board name for the header banner")
    ap.add_argument("--sketch", action="store_true",
                    help="also emit a bringup.ino skeleton")
    ap.add_argument("--json", action="store_true",
                    help="print the pin map as JSON instead of C")
    ap.add_argument("--project-dir", default=None,
                    help="KiCad project dir for stale-cache symbol resolution")
    ap.add_argument("--sym-lib", action="append", default=None,
                    metavar="[NICK=]PATH", help="extra symbol library (repeatable)")
    args = ap.parse_args()

    sch = load_kicad_sch(args.schematic, project_dir=args.project_dir,
                         extra_sym=args.sym_lib)
    try:
        pinmap = build_pinmap(sch, mcu_ref=args.mcu)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(pinmap, indent=2))
        return 0

    header = emit_header(pinmap, args.schematic, args.name)
    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
        hp = os.path.join(args.output_dir, "board_pins.h")
        with open(hp, "w", encoding="utf-8", newline="\n") as f:
            f.write(header)
        written = [hp]
        if args.sketch:
            sp = os.path.join(args.output_dir, "bringup.ino")
            with open(sp, "w", encoding="utf-8", newline="\n") as f:
                f.write(emit_sketch(pinmap, args.schematic, args.name))
            written.append(sp)
        print(f"{pinmap['mcu_ref']} ({pinmap['mcu_part']}): "
              f"{len(pinmap['entries'])} GPIO(s) mapped, "
              f"{len(pinmap['skipped'])} pin(s) skipped", file=sys.stderr)
        for w in written:
            print(f"  wrote {w}", file=sys.stderr)
    else:
        print(header)
        if args.sketch:
            print(emit_sketch(pinmap, args.schematic, args.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
