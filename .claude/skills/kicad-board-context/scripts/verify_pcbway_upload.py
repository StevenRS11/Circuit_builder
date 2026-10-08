#!/usr/bin/env python3
"""verify_pcbway_upload.py — check the files actually uploaded to PCBWay
against the board they claim to come from.

WHY THIS EXISTS
---------------
An order is built from what was *uploaded*, not from the .kicad_pcb. The
PCBWay KiCad plugin writes a zip (gerbers + drill + PCBWay_bom.csv +
PCBWay_positions.csv + PCBWay_netlist.ipc) at one moment; the board keeps
changing afterwards, and the BOM/centroid are often re-uploaded separately
(PCBWay's web form renames them BOM_<board>.xls / Centroid_<board>.xls but
they are still the plugin's CSVs). This tool answers "is what PCBWay has
exactly the board I mean to build?" — deterministically, from the files.

Checks (each one independent of the plugin's own logic):
  gerbers   every layer in the zip is re-plotted from the board with kicad-cli
            using the plugin's options (X2, protel-off, mask subtracted from
            silk iff the upload did) and compared line-for-line (timestamps
            stripped). Identical = the gerbers ARE the current board.
            Also: required layer set present for the board's copper count.
  drill     PTH+NPTH hits (any units) vs a fresh kicad-cli drill export,
            matched by diameter + position (10 µm tolerance).
  centroid  every placeable, non-DNP footprint present once; X/Y/rotation/side
            match the board (KiCad pos convention: Y negated).
  bom       designators == board footprints in BOM; quantity == designator
            count; DNP column == board DNP; MPN present for every fitted line
            and equal to the board footprint's MPN field; value/footprint agree.
  ipc       IPC-D-356 netlist (PCBWay's electrical test) — every pad's net
            equals the board's.
  copies    separately uploaded BOM/centroid files byte-identical to the zip's.

The board file is never modified.

CLI:
    python verify_pcbway_upload.py board.kicad_pcb --zip upload.zip
    python verify_pcbway_upload.py board.kicad_pcb --zip upload.zip \\
        --bom BOM_board.xls --centroid Centroid_board.xls --json
Exit status 0 = no errors (warnings allowed), 1 = errors.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, asdict

import _paths  # noqa: F401
from crosscheck_pcbway_plugin_bom import _balanced_end, MPN_KEYS

_CLI_CANDIDATES = [
    r"C:\Program Files\KiCad\8.0\bin\kicad-cli.exe",
    r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
    r"C:\Program Files\KiCad\7.0\bin\kicad-cli.exe",
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "/usr/bin/kicad-cli",
]

POS_TOL_MM = 0.01
ROT_TOL_DEG = 0.05


@dataclass
class UploadIssue:
    severity: str   # error | warning | info
    check: str
    message: str


def find_kicad_cli(override=None):
    if override:
        return override if os.path.exists(override) else None
    w = shutil.which("kicad-cli")
    if w:
        return w
    return next((p for p in _CLI_CANDIDATES if os.path.exists(p)), None)


# ─── board model ─────────────────────────────────────────────────────

def parse_board_footprints(pcb_text: str) -> dict:
    """ref -> {x, y, rot, side, value, footprint, mpn, dnp, in_bom, in_pos, smd, tht}"""
    out = {}
    for m in re.finditer(r"\n\s*\(footprint \"([^\"]*)\"", pcb_text):
        start = m.start() + pcb_text[m.start():].index("(")
        block = pcb_text[start:_balanced_end(pcb_text, start)]
        # header = everything before the first pad/property child text items
        at = re.search(r"\n\s*\(at ([-\d.]+) ([-\d.]+)(?: ([-\d.]+))?\)", block)
        layer = re.search(r'\n\s*\(layer "([^"]+)"\)', block)
        props = dict(re.findall(r'\(property "([^"]+)" "([^"]*)"', block))
        ref = props.get("Reference")
        if not ref or not at:
            continue
        attr = re.search(r"\n\s*\(attr ([^)]*)\)", block)
        attrs = attr.group(1).split() if attr else []
        mpn = next((props[k] for k in MPN_KEYS if k in props), "")
        out[ref] = {
            "x": float(at.group(1)), "y": float(at.group(2)),
            "rot": float(at.group(3) or 0),
            "side": "bottom" if layer and layer.group(1).startswith("B.") else "top",
            "value": props.get("Value", ""),
            "footprint": m.group(1).split(":")[-1],
            "mpn": mpn,
            "dnp": "dnp" in attrs or bool(re.search(r"\n\s*\(dnp yes\)", block)),
            "in_bom": "exclude_from_bom" not in attrs,
            "in_pos": "exclude_from_pos_files" not in attrs,
            "smd": "smd" in attrs, "tht": "through_hole" in attrs,
        }
    return out


def parse_board_pad_nets(pcb_text: str) -> dict:
    """(ref, pad) -> net name, for pads that carry a net."""
    out = {}
    for m in re.finditer(r"\n\s*\(footprint \"", pcb_text):
        start = m.start() + pcb_text[m.start():].index("(")
        block = pcb_text[start:_balanced_end(pcb_text, start)]
        ref = re.search(r'\(property "Reference" "([^"]+)"', block)
        if not ref:
            continue
        for pm in re.finditer(r'\(pad "([^"]*)"', block):
            pblock = block[pm.start():_balanced_end(block, pm.start())]
            net = re.search(r'\(net (?:\d+ )?"([^"]*)"\)', pblock)
            if pm.group(1) and net and net.group(1):
                out[(ref.group(1), pm.group(1))] = net.group(1)
    return out


def copper_layer_count(pcb_text: str) -> int:
    layers = re.search(r"\(layers\s(.*?)\n\s*\)", pcb_text, re.S)
    body = layers.group(1) if layers else pcb_text
    return len(set(re.findall(r'"((?:F|B|In\d+)\.Cu)"', body)))


# ─── gerbers ─────────────────────────────────────────────────────────

_FUNC_TO_LAYER = {
    ("Soldermask", "Top"): "F.Mask", ("Soldermask", "Bot"): "B.Mask",
    ("Legend", "Top"): "F.SilkS", ("Legend", "Bot"): "B.SilkS",
    ("Paste", "Top"): "F.Paste", ("Paste", "Bot"): "B.Paste",
}


def gerber_layer(text: str, n_cu: int):
    """KiCad layer name from a gerber's %TF.FileFunction X2 attribute."""
    m = re.search(r"%TF\.FileFunction,([^*]*)\*%", text)
    if not m:
        return None
    parts = m.group(1).split(",")
    if parts[0] == "Copper":
        idx = int(parts[1][1:])
        return "F.Cu" if idx == 1 else "B.Cu" if idx == n_cu else f"In{idx - 1}.Cu"
    if parts[0] == "Profile":
        return "Edge.Cuts"
    return _FUNC_TO_LAYER.get((parts[0], parts[1] if len(parts) > 1 else ""))


def _norm_gerber(text: str):
    return [ln for ln in text.splitlines()
            if not ln.startswith(("%TF.CreationDate", "G04 Created by",
                                  "G04 #@! TF.CreationDate"))]


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def check_gerbers(cli, pcb_path, pcb_text, uploaded: dict, issues, work):
    n_cu = copper_layer_count(pcb_text)
    by_layer = {}
    for name, text in uploaded.items():
        lay = gerber_layer(text, n_cu)
        if lay:
            by_layer[lay] = (name, text)
        else:
            issues.append(UploadIssue("warning", "gerbers",
                                      f"{name}: no X2 FileFunction — not compared"))
    need = ["F.Cu", "B.Cu", "F.Mask", "B.Mask", "Edge.Cuts", "F.SilkS"]
    need += [f"In{i}.Cu" for i in range(1, n_cu - 1)]
    for lay in need:
        if lay not in by_layer:
            issues.append(UploadIssue("error", "gerbers",
                                      f"layer {lay} missing from upload ({n_cu}-layer board)"))
    if not cli:
        issues.append(UploadIssue("warning", "gerbers",
                                  "kicad-cli not found — gerbers not re-plotted"))
        return {}
    dates = sorted({d for _, t in by_layer.values()
                    for d in re.findall(r"%TF\.CreationDate,([^*]*)\*%", t)})
    subtract = any("%LPC*%" in t for lay, (_, t) in by_layer.items()
                   if lay.endswith("SilkS"))
    out_dir = os.path.join(work, "gerbers")
    os.makedirs(out_dir)
    cmd = [cli, "pcb", "export", "gerbers", "-l", ",".join(by_layer),
           "-o", out_dir + os.sep]
    if subtract:
        cmd.append("--subtract-soldermask")
    r = _run(cmd + [pcb_path])
    if r.returncode:
        issues.append(UploadIssue("error", "gerbers", f"kicad-cli failed: {r.stderr.strip()}"))
        return {}
    regen = {}
    for f in os.listdir(out_dir):
        if f.endswith(".gbrjob"):
            continue
        with open(os.path.join(out_dir, f), encoding="utf-8", errors="replace") as fh:
            t = fh.read()
        lay = gerber_layer(t, n_cu)
        if lay:
            regen[lay] = t
    result = {}
    for lay, (name, text) in sorted(by_layer.items()):
        a, b = _norm_gerber(text), _norm_gerber(regen.get(lay, ""))
        if a == b:
            result[lay] = "identical"
            continue
        import difflib
        diff = [d for d in difflib.unified_diff(a, b, lineterm="", n=0)
                if d[:1] in "+-" and not d.startswith(("+++", "---"))]
        result[lay] = f"{len(diff)} lines differ"
        issues.append(UploadIssue(
            "error", "gerbers",
            f"{name} ({lay}) differs from the current board: {len(diff)} lines "
            f"(first: {diff[0][:90] if diff else '?'}) - re-export"))
    if all(v == "identical" for v in result.values()) and result:
        issues.append(UploadIssue("info", "gerbers",
                                  f"{len(result)} layers identical to current board"
                                  f" (plotted {', '.join(dates) or '?'})"))
    return result


# ─── drill ───────────────────────────────────────────────────────────

def parse_excellon(text: str):
    """[(diameter_mm, x_mm, y_mm)] for every hit; slots (G85) use the start point."""
    inch = bool(re.search(r"^(INCH|M72)", text, re.M))
    scale = 25.4 if inch else 1.0
    tools, hits, cur = {}, [], None
    for ln in text.splitlines():
        ln = ln.strip()
        m = re.match(r"T(\d+)C([\d.]+)", ln)
        if m:
            tools[m.group(1)] = float(m.group(2)) * scale
            continue
        m = re.match(r"T(\d+)$", ln)
        if m:
            cur = m.group(1)
            continue
        m = re.match(r"X([-\d.]+)Y([-\d.]+)", ln)
        if m and cur in tools:
            if "." not in m.group(1):
                raise ValueError("excellon without decimal points not supported")
            hits.append((tools[cur], float(m.group(1)) * scale, float(m.group(2)) * scale))
    return hits


def _match_hits(a, b, tol=POS_TOL_MM):
    left = list(b)
    unmatched = []
    for d, x, y in a:
        for i, (d2, x2, y2) in enumerate(left):
            if abs(d - d2) < 0.01 and abs(x - x2) <= tol and abs(y - y2) <= tol:
                del left[i]
                break
        else:
            unmatched.append((d, x, y))
    return unmatched, left


def check_drill(cli, pcb_path, uploaded: dict, issues, work):
    up = []
    for name, text in uploaded.items():
        up += parse_excellon(text)
    if not uploaded:
        issues.append(UploadIssue("error", "drill", "no drill file in upload"))
        return
    if not cli:
        return
    out_dir = os.path.join(work, "drill")
    os.makedirs(out_dir)
    r = _run([cli, "pcb", "export", "drill", "--format", "excellon",
              "--drill-origin", "absolute", "--excellon-units", "mm",
              "-o", out_dir + os.sep, pcb_path])
    if r.returncode:
        issues.append(UploadIssue("error", "drill", f"kicad-cli failed: {r.stderr.strip()}"))
        return
    ref = []
    for f in os.listdir(out_dir):
        if f.endswith(".drl"):
            with open(os.path.join(out_dir, f), encoding="utf-8") as fh:
                ref += parse_excellon(fh.read())
    extra, missing = _match_hits(up, ref)
    for d, x, y in missing[:10]:
        issues.append(UploadIssue("error", "drill",
                                  f"board hole Ø{d:.3f} at ({x:.3f},{y:.3f}) missing from upload"))
    for d, x, y in extra[:10]:
        issues.append(UploadIssue("error", "drill",
                                  f"uploaded hole Ø{d:.3f} at ({x:.3f},{y:.3f}) not on board"))
    if not extra and not missing:
        issues.append(UploadIssue("info", "drill", f"{len(up)} holes match the board"))


# ─── centroid / BOM / IPC ────────────────────────────────────────────

def _csv_rows(text: str):
    return list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))


def _split_refs(cell: str):
    return [r.strip() for r in re.split(r"[,\s]+", cell) if r.strip()]


def _rot_eq(a, b):
    return abs(((a - b) + 180) % 360 - 180) <= ROT_TOL_DEG


def _truthy(v):
    return str(v).strip().lower() in ("yes", "y", "true", "1", "dnp")


def check_centroid(text, fps, issues):
    rows = _csv_rows(text)
    seen = {}
    for row in rows:
        ref = row.get("designator", "").strip()
        seen[ref] = seen.get(ref, 0) + 1
        fp = fps.get(ref)
        if not fp:
            issues.append(UploadIssue("error", "centroid", f"{ref}: not on the board"))
            continue
        x, y = float(row["pos_x"]), float(row["pos_y"])
        if abs(x - fp["x"]) > POS_TOL_MM or abs(y + fp["y"]) > POS_TOL_MM:
            issues.append(UploadIssue("error", "centroid",
                                      f"{ref}: ({x},{y}) vs board ({fp['x']},{-fp['y']})"))
        if not _rot_eq(float(row["rotation"]), fp["rot"]):
            issues.append(UploadIssue("error", "centroid",
                                      f"{ref}: rotation {row['rotation']} vs board {fp['rot']}"))
        if row.get("side", "top").strip().lower() != fp["side"]:
            issues.append(UploadIssue("error", "centroid",
                                      f"{ref}: side {row.get('side')} vs board {fp['side']}"))
        if fp["dnp"]:
            issues.append(UploadIssue("warning", "centroid", f"{ref}: DNP on board but placed"))
        mpn = row.get("mpn", "").strip()
        if mpn and fp["mpn"] and mpn != fp["mpn"]:
            issues.append(UploadIssue("error", "centroid",
                                      f"{ref}: mpn {mpn} vs board {fp['mpn']}"))
    for ref, n in seen.items():
        if n > 1:
            issues.append(UploadIssue("error", "centroid", f"{ref}: listed {n} times"))
    for ref, fp in sorted(fps.items()):
        if ref not in seen and fp["in_pos"] and not fp["dnp"] and (fp["smd"] or fp["tht"]):
            issues.append(UploadIssue("error", "centroid",
                                      f"{ref}: placeable, fitted part missing from centroid"))
    return {r["designator"].strip() for r in rows}


def check_bom(text, fps, issues):
    rows = _csv_rows(text)
    seen = {}
    for row in rows:
        refs = _split_refs(row.get("Designator", ""))
        dnp = _truthy(row.get("DNP", ""))
        try:
            qty = int(float(row.get("Quantity", "0") or 0))
        except ValueError:
            qty = -1
        if qty != len(refs):
            issues.append(UploadIssue("error", "bom",
                                      f"{', '.join(refs)}: quantity {qty} != {len(refs)} designators"))
        mpn = row.get("MPN", "").strip()
        if not dnp and not mpn:
            issues.append(UploadIssue("error", "bom", f"{', '.join(refs)}: fitted line has no MPN"))
        for ref in refs:
            seen[ref] = seen.get(ref, 0) + 1
            fp = fps.get(ref)
            if not fp:
                issues.append(UploadIssue("error", "bom", f"{ref}: not on the board"))
                continue
            if fp["dnp"] != dnp:
                issues.append(UploadIssue("error", "bom",
                                          f"{ref}: DNP={'Yes' if dnp else 'No'} in BOM, "
                                          f"board {'DNP' if fp['dnp'] else 'fitted'}"))
            if mpn and fp["mpn"] and mpn != fp["mpn"]:
                issues.append(UploadIssue("error", "bom", f"{ref}: MPN {mpn} vs board {fp['mpn']}"))
            if row.get("Value", "").strip() != fp["value"]:
                issues.append(UploadIssue("error", "bom",
                                          f"{ref}: value {row.get('Value')!r} vs board {fp['value']!r}"))
            if row.get("Footprint", "").strip() != fp["footprint"]:
                issues.append(UploadIssue("error", "bom",
                                          f"{ref}: footprint {row.get('Footprint')} vs board {fp['footprint']}"))
    for ref, n in seen.items():
        if n > 1:
            issues.append(UploadIssue("error", "bom", f"{ref}: on {n} BOM lines"))
    for ref, fp in sorted(fps.items()):
        if ref not in seen and fp["in_bom"]:
            issues.append(UploadIssue("error", "bom", f"{ref}: on board, missing from BOM"))
    # same passive MPN split across lines (".1uf" vs "100nF") - harmless, but
    # PCBWay may raise a query. Connectors legitimately differ by Value.
    by_mpn = {}
    for row in rows:
        m = row.get("MPN", "").strip()
        refs = _split_refs(row.get("Designator", ""))
        if m and refs and re.match(r"(R|C|L|FB)\d", refs[0]):
            by_mpn.setdefault(m, []).append(row.get("Value", ""))
    for m, vals in sorted(by_mpn.items()):
        if len(vals) > 1:
            issues.append(UploadIssue("warning", "bom",
                                      f"MPN {m} split over {len(vals)} lines "
                                      f"(values {', '.join(vals)}) - merge to avoid a query"))
    fitted = [r for r in rows if not _truthy(r.get("DNP", ""))]
    return {"lines": len(rows), "fitted_lines": len(fitted),
            "fitted_parts": sum(len(_split_refs(r["Designator"])) for r in fitted)}


def parse_ipc356(text: str) -> dict:
    """(ref, pin) -> net from IPC-D-356 317/327 records (VIA rows skipped)."""
    out = {}
    for ln in text.splitlines():
        if ln[:3] not in ("317", "327"):
            continue
        net = ln[3:17].strip()
        ref = ln[20:26].strip()
        pin = ln[27:31].strip()
        if ref and ref != "VIA" and pin:
            out[(ref, pin)] = net
    return out


def _ipc_name(board_net: str) -> str:
    """KiCad's IPC-D-356 net field: upper-cased, last 14 characters."""
    return board_net.upper()[-14:]


def check_ipc(text, pad_nets, issues):
    """Per pad: IPC net == board net (in IPC's upper-cased, 14-char form).
    Truncation can make two board nets share an IPC name, so the partition is
    checked too: each IPC net must map to exactly one board net and back."""
    raw = parse_ipc356(text)
    # IPC-D-356 refdes field is 6 chars: "RFID_Conn1" arrives as "RFID_C".
    board_refs = {r for r, _ in pad_nets}
    ipc = {}
    for (ref, pin), net in raw.items():
        if ref not in board_refs:
            cands = [b for b in board_refs if b.startswith(ref)]
            ref = cands[0] if len(cands) == 1 else ref
        ipc[(ref, pin)] = net
    i2b, b2i, bad = {}, {}, []
    for key, net in ipc.items():
        bn = pad_nets.get(key)
        if bn is None and net == "N/C":
            continue          # unnumbered mounting pads etc. — no net either side
        if bn is None:
            bad.append(f"{key[0]}.{key[1]} in IPC netlist ({net}) has no net on the board")
            continue
        if net != _ipc_name(bn):
            bad.append(f"{key[0]}.{key[1]}: IPC net {net} but board net {bn}")
        i2b.setdefault(net, set()).add(bn)
        b2i.setdefault(bn, set()).add(net)
    for net, bns in sorted(i2b.items()):
        if len(bns) > 1:
            bad.append(f"IPC net {net} joins board nets {sorted(bns)}")
    for bn, nets in sorted(b2i.items()):
        if len(nets) > 1:
            bad.append(f"board net {bn} split across IPC nets {sorted(nets)}")
    for key in sorted(set(pad_nets) - set(ipc)):
        bad.append(f"{key[0]}.{key[1]} ({pad_nets[key]}) missing from IPC netlist")
    for msg in bad[:15]:
        issues.append(UploadIssue("error", "ipc", msg))
    if not bad:
        issues.append(UploadIssue("info", "ipc",
                                  f"{len(ipc)} pads, {len(i2b)} nets - same connectivity as board"))


# ─── driver ──────────────────────────────────────────────────────────

def verify(pcb_path, zip_path, bom_path=None, centroid_path=None, kicad_cli=None):
    issues = []
    with open(pcb_path, encoding="utf-8") as fh:
        pcb_text = fh.read()
    fps = parse_board_footprints(pcb_text)
    gerbers, drills, bom_txt, pos_txt, ipc_txt = {}, {}, None, None, None
    raw = {}
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            base = os.path.basename(info.filename)
            data = z.read(info)
            raw[base] = data
            low = base.lower()
            text = data.decode("utf-8", errors="replace")
            if low.endswith((".drl", ".xln", ".exc")):
                drills[base] = text
            elif low == "pcbway_bom.csv":
                bom_txt = text
            elif low == "pcbway_positions.csv":
                pos_txt = text
            elif low.endswith(".ipc"):
                ipc_txt = text
            elif "%FS" in text[:2000]:
                gerbers[base] = text
    for label, path, inzip in (("bom", bom_path, "PCBWay_bom.csv"),
                               ("centroid", centroid_path, "PCBWay_positions.csv")):
        if not path:
            continue
        with open(path, "rb") as fh:
            data = fh.read()
        if inzip not in raw:
            text = data.decode("utf-8-sig", errors="replace")
            if label == "bom":
                bom_txt = text
            else:
                pos_txt = text
        elif data != raw[inzip]:
            issues.append(UploadIssue("error", "copies",
                                      f"{os.path.basename(path)} differs from {inzip} in the zip - "
                                      "checking the separately uploaded file"))
            text = data.decode("utf-8-sig", errors="replace")
            if label == "bom":
                bom_txt = text
            else:
                pos_txt = text
        else:
            issues.append(UploadIssue("info", "copies",
                                      f"{os.path.basename(path)} == {inzip} (byte-identical)"))

    cli = find_kicad_cli(kicad_cli)
    work = tempfile.mkdtemp(prefix="pcbway_verify_")
    try:
        layers = check_gerbers(cli, pcb_path, pcb_text, gerbers, issues, work)
        check_drill(cli, pcb_path, drills, issues, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    bom_stats = None
    if pos_txt is not None:
        check_centroid(pos_txt, fps, issues)
    else:
        issues.append(UploadIssue("warning", "centroid", "no centroid supplied"))
    if bom_txt is not None:
        bom_stats = check_bom(bom_txt, fps, issues)
    else:
        issues.append(UploadIssue("warning", "bom", "no BOM supplied"))
    if ipc_txt is not None:
        check_ipc(ipc_txt, parse_board_pad_nets(pcb_text), issues)
    return {"board": pcb_path, "zip": zip_path, "layers": layers,
            "bom": bom_stats,
            "passed": not any(i.severity == "error" for i in issues),
            "issues": [asdict(i) for i in issues]}


def format_text(res):
    lines = [f"PCBWay upload check: {os.path.basename(res['board'])}  <-  "
             f"{os.path.basename(res['zip'])}"]
    for sev in ("error", "warning", "info"):
        for i in res["issues"]:
            if i["severity"] == sev:
                lines.append(f"  [{sev.upper():7}] {i['check']:9} {i['message']}")
    if res["bom"]:
        b = res["bom"]
        lines.append(f"  BOM: {b['lines']} lines, {b['fitted_lines']} fitted "
                     f"({b['fitted_parts']} parts)")
    lines.append("RESULT: " + ("PASS" if res["passed"] else "FAIL"))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("pcb")
    ap.add_argument("--zip", required=True, help="the gerber zip as uploaded")
    ap.add_argument("--bom", help="BOM file uploaded separately (optional)")
    ap.add_argument("--centroid", help="centroid file uploaded separately (optional)")
    ap.add_argument("--kicad-cli")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = verify(a.pcb, a.zip, a.bom, a.centroid, a.kicad_cli)
    print(json.dumps(res, indent=2) if a.json else format_text(res))
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
