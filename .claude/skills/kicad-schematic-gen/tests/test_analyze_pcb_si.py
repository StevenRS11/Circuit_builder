#!/usr/bin/env python3
"""Tests for the PCB signal-integrity / analog-noise layout backstop (Stage 8)."""

import sys
import os
import pytest

_script_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from analyze_pcb_si import (
    Pcb, analyze_pcb, classify_nets, _seg_bbox_dist, net_length,
    format_result_text, format_result_json,
)


# Minimal hand-built .kicad_pcb covering the features the checks need:
#  - 4-layer stack: F.Cu / In1(power 5V) / In2(power GND) / B.Cu
#  - a sensitive diff pair (SIG names) with one leg on F.Cu, the other split to B.Cu
#  - a via on each leg, far from any GND via
#  - an ESP32 footprint (aggressor) the F.Cu leg runs under
#  - a via centered in a capacitor pad (via-in-pad)
PCB = r"""
(kicad_pcb
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" power "5V")
    (2 "In2.Cu" power "GND")
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "LC_A_SIGP")
  (net 3 "LC_A_SIGN")
  (footprint "Module:ESP32-S3"
    (layer "F.Cu")
    (at 50 50)
    (property "Reference" "U1")
    (property "Value" "ESP32-S3-ZERO")
    (pad "1" smd rect (at -5 -5) (size 1 1) (layers "F.Cu"))
    (pad "2" smd rect (at 5 5) (size 1 1) (layers "F.Cu"))
  )
  (footprint "Capacitor_SMD:C_0805"
    (layer "F.Cu")
    (at 80 80)
    (property "Reference" "C1")
    (property "Value" "100nF")
    (pad "1" smd rect (at 0 0) (size 1.0 1.45) (layers "F.Cu"))
    (pad "2" smd rect (at 1.0 0) (size 1.0 1.45) (layers "F.Cu"))
  )
  (segment (start 50 30) (end 50 70) (width 0.25) (layer "F.Cu") (net 2))
  (segment (start 50 70) (end 60 90) (width 0.25) (layer "B.Cu") (net 2))
  (segment (start 52 30) (end 52 70) (width 0.25) (layer "B.Cu") (net 3))
  (via (at 50 70) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2))
  (via (at 52 70) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 3))
  (via (at 80 80) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (gr_line (start 0 0) (end 100 0) (layer "Edge.Cuts"))
)
"""


@pytest.fixture
def pcb():
    return Pcb(PCB)


class TestParsing:
    def test_nets(self, pcb):
        assert pcb.nets["2"] == "LC_A_SIGP"

    def test_segments(self, pcb):
        assert len(pcb.segments) == 3

    def test_vias(self, pcb):
        assert len(pcb.vias) == 3

    def test_footprints(self, pcb):
        refs = {f["ref"] for f in pcb.footprints}
        assert {"U1", "C1"} <= refs

    def test_copper_layer_order(self, pcb):
        names = [r[1] for r in pcb.copper_layers]
        assert names == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]

    def test_reference_plane(self, pcb):
        # F.Cu references In1 (5V power); B.Cu references In2 (GND)
        assert pcb.reference_plane_of("F.Cu") == "5V"
        assert pcb.reference_plane_of("B.Cu") == "GND"


class TestClassification:
    def test_sensitive_by_name(self, pcb):
        sensitive, pairs, aggressors, _ = classify_nets(pcb)
        assert "LC_A_SIGP" in sensitive and "LC_A_SIGN" in sensitive

    def test_pair_detected(self, pcb):
        _, pairs, _, _ = classify_nets(pcb)
        assert any(p[1] == "LC_A_SIGP" and p[2] == "LC_A_SIGN" for p in pairs)

    def test_aggressor_detected(self, pcb):
        _, _, aggressors, _ = classify_nets(pcb)
        assert "U1" in aggressors


class TestChecks:
    def test_diff_pair_asymmetry_flagged(self, pcb):
        result = analyze_pcb(pcb)
        msgs = [i for i in result.issues if i.category == "diff_pair_symmetry"]
        # P leg is F.Cu+B.Cu, N leg is B.Cu only -> different layers
        assert any("asymmetric" in i.message for i in msgs)

    def test_reference_layer_flagged(self, pcb):
        result = analyze_pcb(pcb)
        msgs = [i for i in result.issues if i.category == "reference_layer"]
        assert any("POWER plane" in i.message for i in msgs)

    def test_aggressor_proximity_flagged(self, pcb):
        result = analyze_pcb(pcb)
        msgs = [i for i in result.issues if i.category == "aggressor_proximity"]
        assert any("U1" in i.message for i in msgs)

    def test_via_in_pad_flagged(self, pcb):
        result = analyze_pcb(pcb)
        msgs = [i for i in result.issues if i.category == "via_in_pad"]
        assert any("C1" in i.message and i.severity == "warning" for i in msgs)

    def test_return_via_flagged(self, pcb):
        result = analyze_pcb(pcb)
        msgs = [i for i in result.issues if i.category == "return_via"]
        assert len(msgs) >= 1  # signal vias far from GND via

    def test_guard_advisory_present(self, pcb):
        result = analyze_pcb(pcb)
        assert any(i.category == "guarding" for i in result.issues)


class TestGeometry:
    def test_seg_through_box_is_zero(self):
        # A segment passing straight through a box -> distance 0
        seg = {"x1": 50, "y1": 30, "x2": 50, "y2": 70}
        assert _seg_bbox_dist(seg, (48, 45, 52, 55)) == 0.0

    def test_net_length(self):
        segs = [{"x1": 0, "y1": 0, "x2": 3, "y2": 4}]
        assert net_length(segs) == 5.0


class TestSourceImpedanceGuarding:
    def test_low_z_guard_is_advisory_not_warning(self, pcb):
        # Provide a netlist marking the pair low-Z -> guarding info, not warning
        from verify_netlist import load_intended_netlist_from_string
        nl = load_intended_netlist_from_string("""
project: t
components:
  U2: { part: ADC, pins: ["1","2"] }
nets:
  LC_A_SIGP: { class: analog_differential, pair: A, polarity: P, source_z: low, pins: [ { ref: U2, pin: "1" } ] }
  LC_A_SIGN: { class: analog_differential, pair: A, polarity: N, source_z: low, pins: [ { ref: U2, pin: "2" } ] }
""")
        result = analyze_pcb(pcb, netlist=nl)
        guard = [i for i in result.issues if i.category == "guarding"]
        assert guard and all(i.severity == "info" for i in guard)

    def test_high_z_guard_is_recommended(self, pcb):
        from verify_netlist import load_intended_netlist_from_string
        nl = load_intended_netlist_from_string("""
project: t
components:
  U2: { part: ADC, pins: ["1"] }
nets:
  SENSE: { class: high_impedance, source_z: high, pins: [ { ref: U2, pin: "1" } ] }
""")
        result = analyze_pcb(pcb, netlist=nl)
        guard = [i for i in result.issues if i.category == "guarding"]
        assert any(i.severity == "warning" for i in guard)


class TestFormatters:
    def test_text(self, pcb):
        result = analyze_pcb(pcb)
        txt = format_result_text(result, "b.kicad_pcb")
        assert "PCB SIGNAL-INTEGRITY" in txt

    def test_json(self, pcb):
        import json
        result = analyze_pcb(pcb)
        data = json.loads(format_result_json(result, "b.kicad_pcb"))
        assert "issues" in data and isinstance(data["issues"], list)
