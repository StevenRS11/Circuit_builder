#!/usr/bin/env python3
"""
PCB Signal-Integrity / Analog-Noise Layout Backstop — Stage 8 (layout-level).

Parses a routed .kicad_pcb and runs the layout-level analog-noise checks that
can only be evaluated once geometry exists. This is the BACKSTOP, not the main
event: the decisive analog defects (missing filter caps) are caught upstream at
Stage 5 by analyze_analog.py. This stage catches how the *routing* treats the
sensitive nets.

Checks (all keyed off which nets are sensitive):
    - diff_pair_symmetry   differential pair routed together: same layers,
                           ~equal length, ~equal via count (asymmetry kills CMRR)
    - reference_layer      sensitive net routed on a layer that references a
                           POWER plane (not GND) — return-path / split-crossing risk
    - aggressor_proximity  sensitive trace runs close to an RF / switching / digital
                           aggressor footprint (e.g. a WiFi module)
    - run_length           very long sensitive analog run (couples more noise)
    - via_in_pad           a via centered in a solderable component pad
    - return_via           a via on a sensitive net with no nearby GND via
    - guarding             advisory, source-impedance aware (low-Z bridge => guards
                           are low priority; high-Z => recommended)

Sensitivity comes from (best first):
    1. --netlist <05b_netlist.yaml>  : authoritative `class:` tags
    2. net-name heuristics           : *SIG*, *_P/_N, *AIN*, diff suffixes, etc.

Severities are deliberately conservative (mostly warning/advisory): per
references/analog_layout.md, do not nag the user into wrecking a tight form
factor for copper that buys nothing on a low-impedance bridge.

CLI Usage:
    python analyze_pcb_si.py <board.kicad_pcb>
    python analyze_pcb_si.py <board.kicad_pcb> --netlist <05b_netlist.yaml>
    python analyze_pcb_si.py <board.kicad_pcb> --json
    python analyze_pcb_si.py <board.kicad_pcb> --strict
"""

import sys
import os
import re
import math
import json as json_module

_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from analyze_dc import AnalysisIssue, AnalysisResult

try:
    import yaml as _yaml
except ImportError:
    _yaml = None

# Thresholds (mm) — tunable knobs, documented in analog_layout.md.
LONG_RUN_MM = 40.0            # sensitive analog run beyond this is flagged
AGGRESSOR_CLEARANCE_MM = 2.0  # sensitive trace closer than this to an aggressor
RETURN_VIA_MM = 1.5           # GND via should be within this of a signal via
LENGTH_MISMATCH_FRAC = 0.15   # diff-pair leg length mismatch fraction
LENGTH_MISMATCH_MIN_MM = 2.0  # ...but ignore mismatches smaller than this


# ─── .kicad_pcb parsing ──────────────────────────────────────────────────────

class Pcb:
    def __init__(self, text):
        self.text = text
        self.nets = {}            # id(str) -> name
        self.segments = []        # dict: start,end,width,layer,net
        self.vias = []            # dict: x,y,layers,net
        self.zones = []           # dict: net, layer
        self.footprints = []      # dict: ref, x, y, rot, layer, pads[]
        self.copper_layers = []   # (order_index, name, type, plane_net)
        self._parse()

    def _parse(self):
        t = self.text
        self.nets = {a: b for a, b in re.findall(r'\(net (\d+) "([^"]*)"\)', t)}

        # Copper layer stackup (ordinal + optional plane net for power layers)
        lm = re.search(r'\(layers\s*\n(.*?)\n\s*\)', t, re.S)
        if lm:
            for num, name, typ, pnet in re.findall(
                    r'\((\d+) "([^"]+)" (\w+)(?:\s+"([^"\n]*)")?\)', lm.group(1)):
                if not name.endswith(".Cu"):
                    continue
                order = self._cu_order(int(num), name)
                plane_net = pnet if (typ in ("power", "mixed") and pnet) else None
                self.copper_layers.append([order, name, typ, plane_net])
            self.copper_layers.sort(key=lambda r: r[0])

        # Segments
        for m in re.finditer(
                r'\(segment\s*\(start ([\-\d.]+) ([\-\d.]+)\)\s*\(end ([\-\d.]+) ([\-\d.]+)\)'
                r'\s*\(width ([\d.]+)\)\s*\(layer "([^"]+)"\)[^)]*\(net (\d+)\)', t):
            x1, y1, x2, y2, w, layer, net = m.groups()
            self.segments.append({
                "x1": float(x1), "y1": float(y1), "x2": float(x2), "y2": float(y2),
                "width": float(w), "layer": layer, "net": net,
            })

        # Vias
        for m in re.finditer(
                r'\(via\s*(?:\(type[^)]*\)\s*)?\(at ([\-\d.]+) ([\-\d.]+)\)\s*\(size [\d.]+\)'
                r'\s*\(drill [\d.]+\)\s*\(layers "([^"]+)" "([^"]+)"\)[^Z]*?\(net (\d+)\)', t):
            x, y, l1, l2, net = m.groups()
            self.vias.append({"x": float(x), "y": float(y), "layers": (l1, l2), "net": net})

        # Zones (net + layer) — to confirm plane nets when layer def lacks them
        for zm in re.finditer(r'\(zone\b', t):
            chunk = t[zm.start():zm.start() + 1200]
            nn = re.search(r'\(net_name "([^"]*)"\)', chunk)
            lay = re.search(r'\(layers? "?([^")\n]+)"?\)', chunk)
            if nn and lay:
                self.zones.append({"net": nn.group(1), "layer": lay.group(1)})

        # Footprints with absolute pad positions
        for b in re.split(r'\n\s*\(footprint ', t)[1:]:
            ref = re.search(r'\(property "Reference" "([^"]+)"', b)
            at = re.search(r'\(at ([\-\d.]+) ([\-\d.]+)(?: ([\-\d.]+))?\)', b)
            val = re.search(r'\(property "Value" "([^"]*)"', b)
            lay = re.search(r'\(layer "([^"]+)"\)', b)
            if not (ref and at):
                continue
            fx, fy = float(at.group(1)), float(at.group(2))
            frot = math.radians(float(at.group(3) or 0))
            pads = []
            for pm in re.finditer(
                    r'\(pad "([^"]+)" (\w+) \w+\s*\(at ([\-\d.]+) ([\-\d.]+)(?: ([\-\d.]+))?\)'
                    r'\s*\(size ([\d.]+) ([\d.]+)\)', b):
                px, py = float(pm.group(3)), float(pm.group(4))
                ax = fx + px * math.cos(frot) - py * math.sin(frot)
                ay = fy + px * math.sin(frot) + py * math.cos(frot)
                pads.append({"num": pm.group(1), "type": pm.group(2),
                             "x": ax, "y": ay, "w": float(pm.group(6)), "h": float(pm.group(7))})
            xs = [p["x"] for p in pads] or [fx]
            ys = [p["y"] for p in pads] or [fy]
            self.footprints.append({
                "ref": ref.group(1), "value": val.group(1) if val else "",
                "x": fx, "y": fy, "layer": lay.group(1) if lay else "F.Cu",
                "pads": pads,
                "bbox": (min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1),
            })

    @staticmethod
    def _cu_order(num, name):
        if name == "F.Cu":
            return 0
        if name == "B.Cu":
            return 1000
        return num  # In1.Cu=1, In2.Cu=2, ...

    # ── derived helpers ──
    def reference_plane_of(self, layer_name):
        """Net name of the plane that a signal layer references (nearest copper
        plane neighbor), or None."""
        idx = next((i for i, r in enumerate(self.copper_layers) if r[1] == layer_name), None)
        if idx is None:
            return None
        # plane net from layer def or from a zone on that layer
        def plane_net(rec):
            if rec[3]:
                return rec[3]
            z = next((z["net"] for z in self.zones if z["layer"] == rec[1]), None)
            return z
        # search outward from this layer for the nearest plane
        for dist in range(1, len(self.copper_layers)):
            for j in (idx - dist, idx + dist):
                if 0 <= j < len(self.copper_layers):
                    pn = plane_net(self.copper_layers[j])
                    if pn:
                        return pn
        return None

    def segments_of_net(self, net_name):
        ids = [i for i, n in self.nets.items() if n == net_name]
        return [s for s in self.segments if s["net"] in ids]

    def vias_of_net(self, net_name):
        ids = [i for i, n in self.nets.items() if n == net_name]
        return [v for v in self.vias if v["net"] in ids]

    def gnd_via_positions(self):
        ids = [i for i, n in self.nets.items()
               if n.upper().strip("+-/") in ("GND", "GROUND", "AGND", "DGND", "VSS", "AVSS", "DVSS")]
        return [(v["x"], v["y"]) for v in self.vias if v["net"] in ids]


def net_length(segs):
    return sum(math.hypot(s["x2"] - s["x1"], s["y2"] - s["y1"]) for s in segs)


def net_layers(segs):
    return {s["layer"] for s in segs}


def _seg_point_dist(seg, px, py):
    """Min distance from point to a segment."""
    ax, ay, bx, by = seg["x1"], seg["y1"], seg["x2"], seg["y2"]
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _seg_bbox_dist(seg, bbox):
    """Distance from a segment to a rectangle (0 if the segment crosses it).

    Samples along the segment so a long trace passing THROUGH a footprint's
    bounding box is detected, not just one whose endpoints land near it.
    """
    x0, y0, x1, y1 = bbox
    def pt_box(px, py):
        cx = min(max(px, x0), x1)
        cy = min(max(py, y0), y1)
        return math.hypot(px - cx, py - cy)
    length = math.hypot(seg["x2"] - seg["x1"], seg["y2"] - seg["y1"])
    n = max(2, int(length / 0.5) + 1)  # ~0.5mm sampling
    best = float("inf")
    for i in range(n + 1):
        t = i / n
        best = min(best, pt_box(seg["x1"] + t * (seg["x2"] - seg["x1"]),
                                seg["y1"] + t * (seg["y2"] - seg["y1"])))
        if best == 0.0:
            break
    return best


# ─── Sensitivity resolution ──────────────────────────────────────────────────

# Matched as whole words (case-insensitive) against a footprint's value+ref, so
# "RF" flags an RF module but not "SURFACE", while "RFID" is listed explicitly.
AGGRESSOR_VALUE_HINTS = ("ESP32", "ESP", "NRF", "RF", "RFID", "ANTENNA", "ANT",
                         "WIFI", "BLE", "BLUETOOTH", "LORA", "RADIO", "NRF24",
                         "SX127", "SI4", "DCDC", "BUCK", "BOOST", "SMPS")
_AGGRESSOR_RE = re.compile(
    r'(?<![A-Z0-9])(' + "|".join(AGGRESSOR_VALUE_HINTS) + r')(?![A-Z0-9])')


def classify_nets(pcb, netlist=None):
    """Return (sensitive, pairs, aggressor_refs, source_z_by_net).

    sensitive: set of net names that are analog-sensitive.
    pairs: list of (pair_label, p_net, n_net).
    aggressor_refs: set of footprint refs that are noise aggressors.
    source_z_by_net: net -> "low"/"high"/None.
    """
    sensitive = set()
    pairs = []
    source_z = {}
    rf_switching_nets = set()

    if netlist is not None:
        # group by class
        pmap = {}  # pair name -> {P:net, N:net}
        for name, net in netlist.nets.items():
            cls = (net.net_class or "").lower()
            if cls in ("analog", "analog_differential", "high_impedance", "reference", "analog_supply"):
                sensitive.add(name)
                source_z[name] = net.source_z or None
            if cls in ("rf", "switching"):
                rf_switching_nets.add(name)
            if cls == "analog_differential" and net.pair:
                slot = pmap.setdefault(net.pair, {})
                slot[(net.polarity or "?").upper()] = name
                slot.setdefault("z", net.source_z)
        for pname, slot in pmap.items():
            if slot.get("P") and slot.get("N"):
                pairs.append((pname, slot["P"], slot["N"]))

    # Name heuristics (augment / fallback)
    diff_suffix = re.compile(r'^(.*?)[_]?(SIGP|SIGN|P|N|\+|-|POS|NEG)$', re.I)
    bases = {}
    for nid, name in pcb.nets.items():
        if not name or name.startswith("unconnected"):
            continue
        up = name.upper()
        if any(k in up for k in ("SIG", "AIN", "ADC", "SENSE", "LOADCELL", "LC_", "BRIDGE")):
            sensitive.add(name)
        m = diff_suffix.match(name)
        if m and m.group(2):
            base = m.group(1).rstrip("_")
            pol = m.group(2).upper()
            pol = "P" if pol in ("SIGP", "P", "+", "POS") else "N"
            bases.setdefault(base, {})[pol] = name
    for base, slot in bases.items():
        if slot.get("P") and slot.get("N"):
            label = base or "(unnamed)"
            if not any(p[1] == slot["P"] and p[2] == slot["N"] for p in pairs):
                pairs.append((label, slot["P"], slot["N"]))
                sensitive.add(slot["P"]); sensitive.add(slot["N"])

    # Aggressor footprints: by value/ref hint, or touching an rf/switching net
    aggressor_refs = set()
    for fp in pcb.footprints:
        hay = (fp["value"] + " " + fp["ref"]).upper()
        if _AGGRESSOR_RE.search(hay):
            aggressor_refs.add(fp["ref"])

    return sensitive, pairs, aggressor_refs, source_z


# ─── Checks ──────────────────────────────────────────────────────────────────

def _check_diff_pairs(pcb, pairs):
    issues = []
    cat = "diff_pair_symmetry"
    for label, p_net, n_net in pairs:
        sp, sn = pcb.segments_of_net(p_net), pcb.segments_of_net(n_net)
        if not sp and not sn:
            continue
        lp, ln = net_length(sp), net_length(sn)
        layp, layn = net_layers(sp), net_layers(sn)
        vp, vn = len(pcb.vias_of_net(p_net)), len(pcb.vias_of_net(n_net))
        details = {"pair": label, "len_p_mm": round(lp, 2), "len_n_mm": round(ln, 2),
                   "layers_p": sorted(layp), "layers_n": sorted(layn),
                   "vias_p": vp, "vias_n": vn}

        problems = []
        if layp != layn:
            problems.append(f"different layers (P:{sorted(layp)} vs N:{sorted(layn)})")
        if vp != vn:
            problems.append(f"unequal via count (P:{vp} vs N:{vn})")
        mismatch = abs(lp - ln)
        if mismatch > LENGTH_MISMATCH_MIN_MM and max(lp, ln) > 0 and \
                mismatch / max(lp, ln) > LENGTH_MISMATCH_FRAC:
            problems.append(f"length mismatch {mismatch:.1f}mm "
                            f"({mismatch/max(lp,ln)*100:.0f}%)")
        if problems:
            issues.append(AnalysisIssue(
                "warning", cat,
                f"diff pair {label}: asymmetric routing — {'; '.join(problems)}. "
                f"Route both legs together, same layer, ~equal length/vias, so "
                f"common-mode noise cancels. See analog_layout.md §5.", details))
        else:
            issues.append(AnalysisIssue(
                "info", cat, f"diff pair {label}: symmetric (len≈{lp:.1f}mm, "
                f"layers {sorted(layp)}, vias {vp}) — OK", details))
    return issues


def _check_reference_layer(pcb, sensitive):
    issues = []
    cat = "reference_layer"
    # Determine which signal layers reference GND vs a power plane.
    for net in sorted(sensitive):
        segs = pcb.segments_of_net(net)
        if not segs:
            continue
        bad_layers = []
        for layer in net_layers(segs):
            ref = pcb.reference_plane_of(layer)
            if ref and ref.upper().strip("+-/") not in (
                    "GND", "GROUND", "AGND", "DGND", "VSS", "AVSS", "DVSS"):
                bad_layers.append((layer, ref))
        if bad_layers:
            txt = ", ".join(f"{l}→{r}" for l, r in bad_layers)
            issues.append(AnalysisIssue(
                "warning", cat,
                f"{net}: routed on layer(s) that reference a POWER plane ({txt}), "
                f"not GND. The return path can't follow across a power-plane split. "
                f"Prefer the GND-referenced layer for sensitive analog. "
                f"See analog_layout.md §4.",
                {"net": net, "bad_layers": txt}))
    return issues


def _check_aggressor_proximity(pcb, sensitive, aggressor_refs):
    issues = []
    cat = "aggressor_proximity"
    if not aggressor_refs:
        return issues
    aggs = [fp for fp in pcb.footprints if fp["ref"] in aggressor_refs]
    for net in sorted(sensitive):
        segs = pcb.segments_of_net(net)
        if not segs:
            continue
        for fp in aggs:
            mind = min(_seg_bbox_dist(s, fp["bbox"]) for s in segs)
            if mind < AGGRESSOR_CLEARANCE_MM:
                issues.append(AnalysisIssue(
                    "warning", cat,
                    f"{net}: runs within {mind:.1f}mm of aggressor {fp['ref']} "
                    f"({fp['value']}). Keep sensitive analog away from RF/switching "
                    f"parts, or shield it (targeted GND pour + stitching) and keep "
                    f"the diff pair tight. See analog_layout.md §6–§7.",
                    {"net": net, "aggressor": fp["ref"], "dist_mm": round(mind, 2)}))
    return issues


def _check_run_length(pcb, sensitive):
    issues = []
    cat = "run_length"
    for net in sorted(sensitive):
        L = net_length(pcb.segments_of_net(net))
        if L > LONG_RUN_MM:
            issues.append(AnalysisIssue(
                "warning", cat,
                f"{net}: long sensitive run ({L:.0f}mm). Long unamplified analog "
                f"traces couple more noise — place the amp near its connector and "
                f"keep the run short. See analog_layout.md §6.",
                {"net": net, "length_mm": round(L, 1)}))
    return issues


def _check_via_in_pad(pcb):
    issues = []
    cat = "via_in_pad"
    hits = []
    for v in pcb.vias:
        for fp in pcb.footprints:
            for pad in fp["pads"]:
                if pad["type"] != "smd":
                    continue
                dx, dy = v["x"] - pad["x"], v["y"] - pad["y"]
                if abs(dx) <= pad["w"] / 2 and abs(dy) <= pad["h"] / 2:
                    centered = abs(dx) < 0.2 and abs(dy) < 0.2
                    is_mp = pad["num"].upper() in ("MP", "MH")  # mounting tab
                    hits.append((fp["ref"], pad["num"], centered, is_mp,
                                 pcb.nets.get(v["net"], "?")))
    comp_hits = [h for h in hits if not h[3]]
    mp_hits = [h for h in hits if h[3]]
    for ref, pn, centered, _, net in comp_hits:
        sev = "warning" if centered else "info"
        issues.append(AnalysisIssue(
            sev, cat,
            f"{ref}.{pn} (net {net}): via {'centered in' if centered else 'overlapping'} "
            f"a solderable pad. Move it off-pad (or spec filled+capped) to avoid "
            f"solder wicking during assembly.",
            {"ref": ref, "pad": pn, "net": net, "centered": centered}))
    if mp_hits:
        refs = ", ".join(sorted({h[0] for h in mp_hits}))
        issues.append(AnalysisIssue(
            "info", cat,
            f"vias on connector mounting tabs ({refs}) — low risk (mechanical "
            f"solder tabs), but pull them off if convenient."))
    return issues


def _check_return_vias(pcb, sensitive):
    issues = []
    cat = "return_via"
    gnd = pcb.gnd_via_positions()
    for net in sorted(sensitive):
        for v in pcb.vias_of_net(net):
            if not gnd:
                d = float("inf")
            else:
                d = min(math.hypot(v["x"] - gx, v["y"] - gy) for gx, gy in gnd)
            if d > RETURN_VIA_MM:
                issues.append(AnalysisIssue(
                    "warning", cat,
                    f"{net}: layer-change via at ({v['x']:.1f},{v['y']:.1f}) has no "
                    f"GND via within {RETURN_VIA_MM}mm (nearest "
                    f"{('%.1f' % d) if d != float('inf') else 'none'}mm). On a "
                    f"power-vs-GND reference change the return crosses through "
                    f"decoupling — keep the F.Cu escape short and the via near a "
                    f"decoupling cap. See analog_layout.md §4.",
                    {"net": net, "x": round(v["x"], 1), "y": round(v["y"], 1),
                     "nearest_gnd_via_mm": (round(d, 1) if d != float("inf") else None)}))
    return issues


def _guard_advisory(pcb, sensitive, pairs, source_z):
    issues = []
    cat = "guarding"
    if not (sensitive or pairs):
        return issues
    # Determine dominant source impedance among sensitive nets.
    zs = [z for z in source_z.values() if z]
    has_high = any(z == "high" for z in zs)
    has_low = any(z == "low" for z in zs)
    if has_high:
        issues.append(AnalysisIssue(
            "warning", cat,
            "High-impedance sensitive node(s) present: guard ring/trace is "
            "RECOMMENDED — drive it from a low-Z node at the input potential, keep "
            "it continuous, GND both ends, stitched. See analog_layout.md §7."))
    if has_low or (pairs and not has_high):
        issues.append(AnalysisIssue(
            "info", cat,
            "Sensitive nets are low-impedance (e.g. a bridge): guarding is LOW "
            "priority — the solid GND reference plane + diff-pair symmetry do the "
            "work. Do NOT sacrifice form factor for guard pours. A targeted shield "
            "only where a trace passes an aggressor is the one worthwhile case. "
            "See analog_layout.md §7."))
    return issues


# ─── Runner ──────────────────────────────────────────────────────────────────

def analyze_pcb(pcb, netlist=None, strict=False):
    sensitive, pairs, aggressor_refs, source_z = classify_nets(pcb, netlist)

    issues = []
    if not sensitive and not pairs:
        issues.append(AnalysisIssue(
            "info", "signal_integrity",
            "No analog-sensitive nets identified (no netlist classes and no "
            "name matches). Pass --netlist with `class:` tags for a real check."))
    issues += _check_diff_pairs(pcb, pairs)
    issues += _check_reference_layer(pcb, sensitive)
    issues += _check_aggressor_proximity(pcb, sensitive, aggressor_refs)
    issues += _check_run_length(pcb, sensitive)
    issues += _check_via_in_pad(pcb)            # not gated on sensitivity
    issues += _check_return_vias(pcb, sensitive)
    issues += _guard_advisory(pcb, sensitive, pairs, source_z)

    has_err = any(i.severity == "error" for i in issues)
    has_warn = any(i.severity == "warning" for i in issues)
    passed = not has_err and (not strict or not has_warn)
    return AnalysisResult(passed=passed, issues=issues,
                          subcircuits_analyzed=len(sensitive) + len(pairs))


def analyze_pcb_file(pcb_path, netlist_path=None, strict=False):
    with open(pcb_path, "r", encoding="utf-8") as f:
        pcb = Pcb(f.read())
    netlist = None
    if netlist_path:
        from verify_netlist import load_intended_netlist
        netlist = load_intended_netlist(netlist_path)
    return analyze_pcb(pcb, netlist=netlist, strict=strict)


# ─── Formatters ──────────────────────────────────────────────────────────────

def format_result_text(result, filepath=None):
    lines = ["=" * 60, "PCB SIGNAL-INTEGRITY / ANALOG-NOISE ANALYSIS"]
    if filepath:
        lines.append(f"Board: {filepath}")
    lines.append(f"Sensitive nets/pairs analyzed: {result.subcircuits_analyzed}")
    lines.append("=" * 60)
    lines.append("")
    cats = {}
    for i in result.issues:
        cats.setdefault(i.category, []).append(i)
    for cat, items in cats.items():
        lines.append(f"── {cat.upper().replace('_', ' ')}")
        for i in items:
            marker = {"error": "✗", "warning": "⚠", "info": "✓"}.get(i.severity, "?")
            lines.append(f"  {marker} {i.message}")
        lines.append("")
    lines.append("─" * 60)
    status = "PASS" if result.passed else "REVIEW"
    lines.append(f"Result: {status}  |  {len(result.errors)} errors, "
                 f"{len(result.warnings)} warnings, {len(result.infos)} info")
    lines.append("─" * 60)
    return "\n".join(lines)


def format_result_json(result, filepath=None):
    data = {
        "passed": result.passed,
        "analyzed": result.subcircuits_analyzed,
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "issues": [{"severity": i.severity, "category": i.category,
                    "message": i.message, "details": i.details} for i in result.issues],
    }
    if filepath:
        data["board_file"] = filepath
    return json_module.dumps(data, indent=2)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    import argparse
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(
        description="PCB signal-integrity / analog-noise layout backstop (Stage 8).")
    parser.add_argument("pcb_file", help="Path to .kicad_pcb")
    parser.add_argument("--netlist", default=None,
                        help="Stage 5b netlist YAML (for authoritative net classes)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as failures")
    args = parser.parse_args()

    if not os.path.isfile(args.pcb_file):
        print(f"Error: file not found: {args.pcb_file}", file=sys.stderr)
        sys.exit(2)
    try:
        result = analyze_pcb_file(args.pcb_file, netlist_path=args.netlist, strict=args.strict)
    except Exception as e:
        print(f"Error analyzing board: {e}", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(format_result_json(result, args.pcb_file))
    else:
        print(format_result_text(result, args.pcb_file))
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
