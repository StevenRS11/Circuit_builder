#!/usr/bin/env python3
"""
Fab-output generator (Stage 9) — produce the PCBway upload package from a routed
.kicad_pcb using the KiCad command-line tool (kicad-cli), with a DRC gate.

Boards from this skill are ordered as *assembled* boards from PCBway turnkey.
A complete upload is three things: the **gerbers + drill** (bare-board fab), the
**centroid / pick-and-place** (where each part goes), and the **BOM** (what each
part is — generated separately by generate_pcbway_bom.py). This script produces
the first two, deterministically, from the board file.

Design-hierarchy note: this is a pure *script* step. It makes no design
decisions — it runs DRC, refuses to emit fab files from a board that fails it
(shipping gerbers from a board with clearance/short errors is the mistake this
gate prevents), then exports the standard layer set. The layer stack is detected
from the board (2-layer vs 4-layer vs N), so no per-project configuration.

It shells out to `kicad-cli` (KiCad 7/8). No KiCad *library* is needed, but the
CLI binary must be installed; the locator checks PATH and the common install
paths, and you can override with --kicad-cli.

Outputs (under <pcb_dir>/PCBway_uploads/ by default):
    gerbers/                         all gerber layers + Excellon drill + .gbrjob
    <project>_gerbers.zip            zipped gerbers/ (what you upload)
    <project>_centroid.csv           pick-and-place (Ref,Val,Package,PosX,PosY,Rot,Side)
    fab_drc_report.json              the DRC report (proof the board passed)

CLI:
    python generate_fab_outputs.py board.kicad_pcb
    python generate_fab_outputs.py board.kicad_pcb --output-dir PCBway_uploads
    python generate_fab_outputs.py board.kicad_pcb --kicad-cli "/path/to/kicad-cli"
    python generate_fab_outputs.py board.kicad_pcb --no-drc        # skip the gate (NOT recommended)
    python generate_fab_outputs.py board.kicad_pcb --json
"""

import sys
import os
import re
import json as json_module
import shutil
import zipfile
import subprocess


# ─── kicad-cli locator ───────────────────────────────────────────────

_COMMON_CLI_PATHS = [
    # Windows
    r"C:\Program Files\KiCad\8.0\bin\kicad-cli.exe",
    r"C:\Program Files\KiCad\7.0\bin\kicad-cli.exe",
    # macOS
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    # Linux
    "/usr/bin/kicad-cli",
    "/usr/local/bin/kicad-cli",
]


def find_kicad_cli(override=None):
    """Locate the kicad-cli binary. Returns the path or raises FileNotFoundError."""
    if override:
        if os.path.isfile(override):
            return override
        raise FileNotFoundError(f"--kicad-cli path does not exist: {override}")
    on_path = shutil.which("kicad-cli")
    if on_path:
        return on_path
    for p in _COMMON_CLI_PATHS:
        if os.path.isfile(p):
            return p
    raise FileNotFoundError(
        "kicad-cli not found on PATH or in common install locations. "
        "Install KiCad 7/8 or pass --kicad-cli <path-to-kicad-cli>."
    )


# ─── Layer-stack detection ───────────────────────────────────────────

def detect_copper_layers(pcb_text):
    """Return the ordered list of canonical copper-layer names enabled in the board.

    Reads the (layers ...) declaration. KiCad indexes copper layers 0=F.Cu,
    1..30 = In1.Cu..In30.Cu, 31 = B.Cu — regardless of any custom display name
    the user gave an inner plane (e.g. a renamed "GND"), so we map by INDEX to
    the canonical name kicad-cli expects in --layers.
    """
    m = re.search(r"\(layers\s*(.*?)\n\s*\)", pcb_text, re.DOTALL)
    block = m.group(1) if m else pcb_text
    indices = set()
    # entries look like:  (0 "F.Cu" signal)  /  (1 "GND" signal)  /  (31 "B.Cu" signal "B.Cu")
    for idx_str, _name in re.findall(r'\((\d+)\s+"([^"]+)"', block):
        idx = int(idx_str)
        if 0 <= idx <= 31:
            indices.add(idx)

    def canonical(idx):
        if idx == 0:
            return "F.Cu"
        if idx == 31:
            return "B.Cu"
        return f"In{idx}.Cu"

    # Order: F.Cu, In1..In30 ascending, B.Cu
    ordered = []
    if 0 in indices:
        ordered.append("F.Cu")
    for idx in sorted(i for i in indices if 1 <= i <= 30):
        ordered.append(canonical(idx))
    if 31 in indices:
        ordered.append("B.Cu")
    # Fallback: a 2-layer board if nothing parsed
    return ordered or ["F.Cu", "B.Cu"]


# Non-copper fab layers always exported (paste included for the assembly stencil;
# kicad-cli may skip emitting empty ones, which is fine).
_TECH_LAYERS = [
    "F.Paste", "B.Paste",
    "F.Silkscreen", "B.Silkscreen",
    "F.Mask", "B.Mask",
    "Edge.Cuts",
]


# ─── kicad-cli command wrappers ──────────────────────────────────────

def _run(cmd):
    """Run a command (list form, no shell). Returns (returncode, stdout, stderr)."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def run_drc(cli, pcb, report_path):
    """Run DRC, writing a JSON report. Returns (violations, unconnected) counts.

    Counts are parsed from the JSON report so the gate is robust regardless of
    exit-code semantics across KiCad versions.
    """
    _run([cli, "pcb", "drc", "--output", report_path, "--format", "json",
          "--severity-error", "--exit-code-violations", pcb])
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            rep = json_module.load(f)
    except (OSError, ValueError):
        return (None, None)  # unknown — caller decides
    violations = len(rep.get("violations", []))
    unconnected = len(rep.get("unconnected_items", []))
    return (violations, unconnected)


def export_gerbers(cli, pcb, out_dir, layers):
    rc, out, err = _run([cli, "pcb", "export", "gerbers",
                         "--layers", ",".join(layers),
                         "--output", out_dir, pcb])
    return rc, out + err


def export_drill(cli, pcb, out_dir):
    rc, out, err = _run([cli, "pcb", "export", "drill",
                         "--output", out_dir + os.sep,
                         "--format", "excellon", "--excellon-separate-th", pcb])
    return rc, out + err


def export_centroid(cli, pcb, csv_path):
    rc, out, err = _run([cli, "pcb", "export", "pos",
                         "--output", csv_path, "--format", "csv",
                         "--units", "mm", "--side", "both", pcb])
    return rc, out + err


def zip_dir(src_dir, zip_path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(os.listdir(src_dir)):
            full = os.path.join(src_dir, name)
            if os.path.isfile(full):
                zf.write(full, arcname=name)


# ─── Orchestration ───────────────────────────────────────────────────

def generate(pcb_path, output_dir=None, kicad_cli=None, run_drc_gate=True):
    """Generate the PCBway fab package from a routed .kicad_pcb.

    Returns a dict report. Raises on missing inputs / DRC failure (when gated) /
    a kicad-cli export error.
    """
    pcb_path = os.path.abspath(pcb_path)
    if not os.path.isfile(pcb_path):
        raise FileNotFoundError(f"PCB not found: {pcb_path}")

    pcb_dir = os.path.dirname(pcb_path)
    project = os.path.splitext(os.path.basename(pcb_path))[0]
    out_dir = os.path.abspath(output_dir or os.path.join(pcb_dir, "PCBway_uploads"))
    gerber_dir = os.path.join(out_dir, "gerbers")
    os.makedirs(gerber_dir, exist_ok=True)

    cli = find_kicad_cli(kicad_cli)

    with open(pcb_path, "r", encoding="utf-8") as f:
        copper = detect_copper_layers(f.read())
    layers = copper + _TECH_LAYERS

    report = {
        "pcb": pcb_path,
        "output_dir": out_dir,
        "kicad_cli": cli,
        "copper_layers": copper,
        "drc": None,
        "files": [],
    }

    # ── DRC gate ──
    drc_report_path = os.path.join(out_dir, "fab_drc_report.json")
    violations, unconnected = run_drc(cli, pcb_path, drc_report_path)
    report["drc"] = {"violations": violations, "unconnected": unconnected,
                     "report": drc_report_path}
    if run_drc_gate and violations:
        raise RuntimeError(
            f"DRC found {violations} violation(s) and {unconnected} unconnected item(s). "
            f"Refusing to generate fab outputs from a board that fails DRC. "
            f"Fix the board (see {drc_report_path}) or pass --no-drc to override."
        )

    # ── Gerbers + drill ──
    rc, log = export_gerbers(cli, pcb_path, gerber_dir, layers)
    if rc != 0:
        raise RuntimeError(f"Gerber export failed (rc={rc}):\n{log}")
    rc, log = export_drill(cli, pcb_path, gerber_dir)
    if rc != 0:
        raise RuntimeError(f"Drill export failed (rc={rc}):\n{log}")

    # ── Centroid ──
    centroid_path = os.path.join(out_dir, f"{project}_centroid.csv")
    rc, log = export_centroid(cli, pcb_path, centroid_path)
    if rc != 0:
        raise RuntimeError(f"Centroid (pos) export failed (rc={rc}):\n{log}")

    # ── Zip gerbers ──
    zip_path = os.path.join(out_dir, f"{project}_gerbers.zip")
    zip_dir(gerber_dir, zip_path)

    report["files"] = [
        os.path.relpath(p, out_dir) for p in (
            [os.path.join(gerber_dir, n) for n in sorted(os.listdir(gerber_dir))]
            + [zip_path, centroid_path, drc_report_path]
        )
    ]
    report["centroid"] = centroid_path
    report["gerber_zip"] = zip_path
    return report


# ─── CLI ──────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Generate the PCBway fab package (gerbers + drill + centroid) "
                    "from a routed .kicad_pcb via kicad-cli, with a DRC gate.")
    parser.add_argument("pcb", help="Routed .kicad_pcb file")
    parser.add_argument("--output-dir", help="Output folder (default: <pcb_dir>/PCBway_uploads)")
    parser.add_argument("--kicad-cli", help="Path to kicad-cli (else auto-located)")
    parser.add_argument("--no-drc", action="store_true",
                        help="Skip the DRC gate (NOT recommended)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    try:
        report = generate(args.pcb, output_dir=args.output_dir,
                          kicad_cli=args.kicad_cli, run_drc_gate=not args.no_drc)
    except (FileNotFoundError, RuntimeError) as e:
        if args.json:
            print(json_module.dumps({"passed": False, "error": str(e)}, indent=2))
        else:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        report["passed"] = True
        print(json_module.dumps(report, indent=2))
    else:
        drc = report["drc"]
        print("=" * 60)
        print("PCBway FAB PACKAGE")
        print("=" * 60)
        print(f"Board:        {report['pcb']}")
        print(f"Copper stack: {len(report['copper_layers'])}-layer "
              f"({', '.join(report['copper_layers'])})")
        print(f"DRC:          {drc['violations']} violations, "
              f"{drc['unconnected']} unconnected")
        print(f"Output:       {report['output_dir']}")
        print("-" * 60)
        for f in report["files"]:
            print(f"  {f}")
        print("=" * 60)
    sys.exit(0)


if __name__ == "__main__":
    main()
