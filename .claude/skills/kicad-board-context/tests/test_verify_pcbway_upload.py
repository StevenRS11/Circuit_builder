"""verify_pcbway_upload — the uploaded package must BE the current board.

Added 2026-10-08 when verifying the DualScale_Compact / pushbuttonDef PCBWay
uploads. Every check is mutation-tested here: a verifier that passes a wrong
upload is worse than none.
"""
import os
import shutil
import subprocess
import zipfile

import pytest

import verify_pcbway_upload as v

BOARD = """(kicad_pcb (version 20240108) (generator "pcbnew") (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal) (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen") (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "Net-(R1-Pad2)")
  (footprint "Resistor_SMD:R_0805_2012Metric" (layer "F.Cu")
    (uuid "11111111-1111-1111-1111-111111111111")
    (at 110 105 90)
    (property "Reference" "R1" (at 0 -1.65 90) (layer "F.SilkS")
      (uuid "11111111-1111-1111-1111-111111111112") (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "10k" (at 0 1.65 90) (layer "F.Fab")
      (uuid "11111111-1111-1111-1111-111111111113") (effects (font (size 1 1) (thickness 0.15))))
    (property "MPN" "RC0805FR-0710KL" (at 0 0 90) (layer "F.Fab") hide
      (uuid "11111111-1111-1111-1111-111111111114") (effects (font (size 1 1) (thickness 0.15))))
    (attr smd)
    (pad "1" smd roundrect (at -0.9125 0 90) (size 1.025 1.4) (layers "F.Cu" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "GND") (uuid "11111111-1111-1111-1111-111111111115"))
    (pad "2" smd roundrect (at 0.9125 0 90) (size 1.025 1.4) (layers "F.Cu" "F.Mask")
      (roundrect_rratio 0.25) (net 2 "Net-(R1-Pad2)") (uuid "11111111-1111-1111-1111-111111111116"))
  )
  (gr_rect (start 100 100) (end 120 110) (stroke (width 0.1) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "22222222-2222-2222-2222-222222222222"))
  (segment (start 110 104.0875) (end 115 104.0875) (width 0.25) (layer "F.Cu") (net 2)
    (uuid "33333333-3333-3333-3333-333333333333"))
  (via (at 115 104.0875) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2)
    (uuid "44444444-4444-4444-4444-444444444444"))
)
"""

BOM = ("﻿Designator,Quantity,Value,Footprint,Package,MPN,DNP,Mount_Type\n"
       "R1,1,10k,R_0805_2012Metric,0805,RC0805FR-0710KL,,smt\n")
POS = ("﻿pos_x,pos_y,rotation,side,designator,mpn,pack,footprint,value,mount_type\n"
       "110.0,-105.0,90.0,top,R1,RC0805FR-0710KL,0805,R_0805_2012Metric,10k,smt\n")
IPC = ("P  CODE 00\n"
       "327GND              R1    -1          A01X+043272Y-041339X0404Y0551R090S2\n"
       "327NET-(R1-PAD2)    R1    -2          A01X+043272Y-041411X0404Y0551R090S2\n"
       "999\n")

CLI = v.find_kicad_cli()
needs_cli = pytest.mark.skipif(not CLI, reason="kicad-cli not installed")


def _errors(res, check=None):
    return [i for i in res["issues"] if i["severity"] == "error"
            and (check is None or i["check"] == check)]


@pytest.fixture
def board(tmp_path):
    p = tmp_path / "t.kicad_pcb"
    p.write_text(BOARD, encoding="utf-8")
    return p


def _make_zip(tmp_path, board, bom=BOM, pos=POS, ipc=IPC, mutate=None, drop=()):
    """Plugin-equivalent zip: kicad-cli gerbers + drill + the CSVs."""
    gdir = tmp_path / "g"
    gdir.mkdir(exist_ok=True)
    subprocess.run([CLI, "pcb", "export", "gerbers", "--no-protel-ext", "-l",
                    "F.Cu,B.Cu,F.Mask,B.Mask,F.SilkS,B.SilkS,Edge.Cuts",
                    "-o", str(gdir) + os.sep, str(board)], check=True, capture_output=True)
    subprocess.run([CLI, "pcb", "export", "drill", "--format", "excellon",
                    "--drill-origin", "absolute", "--excellon-units", "in",
                    "-o", str(gdir) + os.sep, str(board)], check=True, capture_output=True)
    zp = tmp_path / "up.zip"
    with zipfile.ZipFile(zp, "w") as z:
        for f in os.listdir(gdir):
            if f.endswith(".gbrjob") or any(d in f for d in drop):
                continue
            data = (gdir / f).read_text(encoding="utf-8")
            if mutate and mutate[0] in f:
                data = mutate[1](data)
            z.writestr(f, data)
        z.writestr("PCBWay_bom.csv", bom)
        z.writestr("PCBWay_positions.csv", pos)
        z.writestr("PCBWay_netlist.ipc", ipc)
    return zp


# ─── end to end (needs kicad-cli) ────────────────────────────────────

@needs_cli
def test_matching_upload_passes(tmp_path, board):
    res = v.verify(str(board), str(_make_zip(tmp_path, board)))
    assert res["passed"], _errors(res)
    assert set(res["layers"].values()) == {"identical"}


@needs_cli
def test_stale_gerber_is_caught(tmp_path, board):
    zp = _make_zip(tmp_path, board, mutate=("F_Cu", lambda t: t.replace(
        "X115000000Y-104087500D03*", "X116000000Y-104087500D03*")))
    assert _errors(v.verify(str(board), str(zp)), "gerbers")


@needs_cli
def test_missing_layer_is_caught(tmp_path, board):
    zp = _make_zip(tmp_path, board, drop=("Edge_Cuts",))
    assert any("Edge.Cuts" in e["message"]
               for e in _errors(v.verify(str(board), str(zp)), "gerbers"))


@needs_cli
def test_moved_hole_is_caught(tmp_path, board):
    zp = _make_zip(tmp_path, board, mutate=(".drl", lambda t: t.replace("X4.5276", "X4.5376")))
    assert _errors(v.verify(str(board), str(zp)), "drill")


@needs_cli
def test_separate_copy_that_differs_is_checked_instead(tmp_path, board):
    zp = _make_zip(tmp_path, board)
    other = tmp_path / "Centroid_t.xls"
    other.write_text(POS.replace("90.0,top", "0.0,top"), encoding="utf-8")
    res = v.verify(str(board), str(zp), centroid_path=str(other))
    assert _errors(res, "copies") and _errors(res, "centroid")


# ─── pure checks (no kicad-cli) ──────────────────────────────────────

def _fps():
    return v.parse_board_footprints(BOARD)


def _run(fn, text, *extra):
    issues = []
    fn(text, *extra, issues)
    return [i for i in issues if i.severity == "error"]


def test_board_parse():
    fp = _fps()["R1"]
    assert (fp["x"], fp["y"], fp["rot"], fp["side"]) == (110, 105, 90, "top")
    assert fp["mpn"] == "RC0805FR-0710KL" and fp["smd"] and not fp["dnp"]


def test_centroid_rotation_position_and_missing():
    assert not _run(v.check_centroid, POS, _fps())
    assert _run(v.check_centroid, POS.replace(",90.0,", ",270.0,"), _fps())
    assert _run(v.check_centroid, POS.replace("-105.0", "105.0"), _fps())
    assert _run(v.check_centroid, POS.splitlines()[0] + "\n", _fps())   # R1 missing


def test_bom_quantity_mpn_dnp_value():
    assert not _run(v.check_bom, BOM, _fps())
    assert _run(v.check_bom, BOM.replace("R1,1,", "R1,2,"), _fps())
    assert _run(v.check_bom, BOM.replace("RC0805FR-0710KL", ""), _fps())
    assert _run(v.check_bom, BOM.replace(",,smt", ",Yes,smt"), _fps())
    assert _run(v.check_bom, BOM.replace(",10k,", ",1k,"), _fps())
    assert _run(v.check_bom, BOM.replace("RC0805FR-0710KL", "RC0805FR-071KL"), _fps())


def test_ipc_partition_not_names():
    pads = v.parse_board_pad_nets(BOARD)
    assert not _run(v.check_ipc, IPC, pads)
    swapped = IPC.replace("327GND              R1    -1", "327GND              R1    -2") \
                 .replace("327NET-(R1-PAD2)    R1    -2", "327NET-(R1-PAD2)    R1    -1")
    assert _run(v.check_ipc, swapped, pads)


def test_ipc_truncated_refdes_resolves():
    pads = {("RFID_Conn1", "1"): "GND", ("RFID_Conn1", "2"): "5V"}
    ipc = ("327GND              RFID_C-1          A01X+0Y+0\n"
           "3275V               RFID_C-2          A01X+0Y+0\n"
           "327N/C              RFID_C-MP         A01X+0Y+0\n")
    assert not _run(v.check_ipc, ipc, pads)


def test_excellon_inch_and_mm_agree():
    inch = "M48\nINCH\nT1C0.0118\n%\nT1\nX4.5276Y-4.1373\nM30\n"
    mm = "M48\nMETRIC\nT1C0.300\n%\nT1\nX115.0Y-105.087\nM30\n"
    a, b = v.parse_excellon(inch), v.parse_excellon(mm)
    assert v._match_hits(a, b) == ([], [])


def test_gerber_layer_from_x2():
    assert v.gerber_layer("%TF.FileFunction,Copper,L2,Inr*%", 4) == "In1.Cu"
    assert v.gerber_layer("%TF.FileFunction,Copper,L4,Bot*%", 4) == "B.Cu"
    assert v.gerber_layer("%TF.FileFunction,Legend,Top*%", 4) == "F.SilkS"
    assert v.gerber_layer("%TF.FileFunction,Profile,NP*%", 4) == "Edge.Cuts"
