#!/usr/bin/env python3
"""summarize_pcb.py — distill an existing .kicad_pcb into a compact,
reasonable-to-read board summary YAML (pcb_summary.yaml in the context pack).

A .kicad_pcb is megabytes of geometry; Claude should never freehand-read it.
This script (reusing analyze_pcb_si's parser, so both tools see the identical
board) reduces it to the facts reasoning actually needs:

  * copper stackup (layer order, type, plane nets)
  * board outline bounding box from Edge.Cuts
  * every footprint: ref, value, lib_id, position, side, pad count
  * per-net routing stats for routed nets: total length, width range,
    layers used, via count
  * zones (net + layer) and total via count

It deliberately does NOT judge anything — `analyze_pcb_si.py` is the analyzer;
this is the context builder. Point analyze_pcb_si at the same board (with the
extracted netlist for authoritative net classes) for findings.

CLI:
    python summarize_pcb.py board.kicad_pcb -o claude_context/pcb_summary.yaml
    python summarize_pcb.py board.kicad_pcb --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

import _paths  # noqa: F401
from analyze_pcb_si import Pcb, net_length


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _refsort(ref: str):
    m = re.match(r"([A-Za-z_]+)(\d+)", ref)
    return (m.group(1), int(m.group(2))) if m else (ref, 0)


def _edge_cuts_bbox(text: str):
    """Best-effort board outline bbox from Edge.Cuts graphics."""
    xs, ys = [], []
    for m in re.finditer(r"\(gr_(?:line|rect|arc|circle|poly)\b", text):
        nxt = text.find("(gr_", m.start() + 4)
        chunk = text[m.start(): nxt if nxt != -1 else m.start() + 4000]
        if '"Edge.Cuts"' not in chunk:
            continue
        for cm in re.finditer(
                r"\((?:start|end|center|mid|xy) ([\-\d.]+) ([\-\d.]+)\)", chunk):
            xs.append(float(cm.group(1)))
            ys.append(float(cm.group(2)))
    if not xs:
        return None
    return {
        "x_mm": round(min(xs), 2), "y_mm": round(min(ys), 2),
        "width_mm": round(max(xs) - min(xs), 2),
        "height_mm": round(max(ys) - min(ys), 2),
    }


def summarize(pcb_path: str) -> dict:
    with open(pcb_path, "r", encoding="utf-8") as f:
        text = f.read()
    pcb = Pcb(text)

    # lib_id per footprint (the Pcb parser doesn't keep it) — same block split.
    lib_ids = {}
    for b in re.split(r"\n\s*\(footprint ", text)[1:]:
        lid = re.match(r'"([^"]+)"', b)
        ref = re.search(r'\(property "Reference" "([^"]+)"', b)
        if lid and ref:
            lib_ids[ref.group(1)] = lid.group(1)

    components = []
    for fp in sorted(pcb.footprints, key=lambda f: _refsort(f["ref"])):
        components.append({
            "ref": fp["ref"],
            "value": fp["value"],
            "lib_id": lib_ids.get(fp["ref"], ""),
            "x_mm": round(fp["x"], 2),
            "y_mm": round(fp["y"], 2),
            "side": "top" if fp["layer"].startswith("F") else "bottom",
            "pads": len(fp["pads"]),
        })

    nets = []
    for net_name in sorted(set(pcb.nets.values())):
        if not net_name:
            continue
        segs = pcb.segments_of_net(net_name)
        vias = pcb.vias_of_net(net_name)
        if not segs and not vias:
            continue
        widths = sorted({s["width"] for s in segs})
        nets.append({
            "name": net_name,
            "length_mm": round(net_length(segs), 2),
            "segments": len(segs),
            "width_mm": widths[0] if len(widths) == 1 else
                        {"min": widths[0], "max": widths[-1]} if widths else None,
            "layers": sorted({s["layer"] for s in segs}),
            "vias": len(vias),
        })

    return {
        "source": os.path.basename(pcb_path),
        "source_sha256": _sha256(pcb_path),
        "extracted_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "board": {
            "copper_layers": [
                {"name": name, "type": typ, "plane_net": plane}
                for _order, name, typ, plane in pcb.copper_layers
            ],
            "outline": _edge_cuts_bbox(text),
            "total_vias": len(pcb.vias),
            "zones": [{"net": z["net"], "layer": z["layer"]} for z in pcb.zones],
        },
        "components": components,
        "routed_nets": nets,
        "declared_nets": len([n for n in pcb.nets.values() if n]),
    }


def emit_yaml(doc: dict) -> str:
    """Small hand emitter — keeps the summary diff-friendly and stable."""
    def q(s):
        return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = []
    w = lines.append
    w("# PCB summary (extracted) — context document, not an analysis.")
    w("# Run analyze_pcb_si.py --netlist <netlist.yaml> on the same board for findings.")
    w(f"source: {q(doc['source'])}")
    w(f"source_sha256: {q(doc['source_sha256'])}")
    w(f"extracted_utc: {q(doc['extracted_utc'])}")
    w("board:")
    w("  copper_layers:")
    for layer in doc["board"]["copper_layers"]:
        plane = q(layer["plane_net"]) if layer["plane_net"] else "null"
        w(f"    - {{ name: {q(layer['name'])}, type: {layer['type']}, plane_net: {plane} }}")
    outline = doc["board"]["outline"]
    if outline:
        w(f"  outline: {{ x_mm: {outline['x_mm']}, y_mm: {outline['y_mm']}, "
          f"width_mm: {outline['width_mm']}, height_mm: {outline['height_mm']} }}")
    else:
        w("  outline: null  # no Edge.Cuts found")
    w(f"  total_vias: {doc['board']['total_vias']}")
    if doc["board"]["zones"]:
        w("  zones:")
        for z in doc["board"]["zones"]:
            w(f"    - {{ net: {q(z['net'])}, layer: {q(z['layer'])} }}")
    else:
        w("  zones: []")
    w(f"declared_nets: {doc['declared_nets']}")
    w("components:")
    for c in doc["components"]:
        w(f"  - {{ ref: {c['ref']}, value: {q(c['value'])}, lib_id: {q(c['lib_id'])}, "
          f"x_mm: {c['x_mm']}, y_mm: {c['y_mm']}, side: {c['side']}, pads: {c['pads']} }}")
    w("routed_nets:")
    for n in doc["routed_nets"]:
        if isinstance(n["width_mm"], dict):
            width = f"{{ min: {n['width_mm']['min']}, max: {n['width_mm']['max']} }}"
        else:
            width = str(n["width_mm"])
        layers = ", ".join(q(l) for l in n["layers"])
        w(f"  - {{ name: {q(n['name'])}, length_mm: {n['length_mm']}, "
          f"segments: {n['segments']}, width_mm: {width}, "
          f"layers: [{layers}], vias: {n['vias']} }}")
    w("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description="Summarize a .kicad_pcb into a context-pack YAML")
    ap.add_argument("pcb", help="path to .kicad_pcb")
    ap.add_argument("-o", "--output", help="output YAML path (default stdout)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of YAML")
    args = ap.parse_args()

    doc = summarize(args.pcb)
    out = json.dumps(doc, indent=2) if args.json else emit_yaml(doc)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"summarized: {len(doc['components'])} footprints, "
              f"{len(doc['routed_nets'])} routed nets, "
              f"{len(doc['board']['copper_layers'])} copper layers",
              file=sys.stderr)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
