"""Tests for the kicad-board-context ingest scripts.

The schematic-side tests run against the battery_3s golden fixture from the
sibling generator skill — a real, full board whose netlist/BOM ground truth is
already maintained there. The PCB-side tests use a minimal inline board (the
repo has no .kicad_pcb fixture yet).
"""
import os

import pytest

import extract_netlist
import extract_bom
import summarize_pcb
import reconcile

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.normpath(os.path.join(
    HERE, "..", "..", "kicad-schematic-gen", "tests", "fixtures", "battery_3s"))
GOLDEN_SCH = os.path.join(FIXTURES, "battery_3s.golden.kicad_sch")
FIXTURE_BOM = os.path.join(FIXTURES, "bom_flat.md")


# ─── extract_netlist ─────────────────────────────────────────────────

class TestExtractNetlist:
    @pytest.fixture(scope="class")
    def extracted(self):
        doc, summary = extract_netlist.extract(GOLDEN_SCH)
        yaml_text = extract_netlist.emit_yaml(doc, GOLDEN_SCH)
        return doc, summary, yaml_text

    def test_roundtrip_verifies_against_source(self, extracted):
        _doc, _summary, yaml_text = extracted
        result = extract_netlist.self_verify(yaml_text, GOLDEN_SCH)
        assert result.passed, [i.message for i in result.errors]

    def test_loadable_by_the_stage5b_loader(self, extracted):
        from verify_netlist import load_intended_netlist_from_string
        _doc, _summary, yaml_text = extracted
        intended = load_intended_netlist_from_string(yaml_text)
        assert len(intended.components) == 58
        assert intended.nets

    def test_every_pin_in_exactly_one_net_or_nc(self, extracted):
        doc, _summary, _yaml = extracted
        assigned = {}
        for name, net in doc["nets"].items():
            for p in net["pins"]:
                key = (p["ref"], p["pin"])
                assert key not in assigned, f"{key} in {name} and {assigned[key]}"
                assigned[key] = name
        nc = {(n["ref"], n["pin"]) for n in doc["no_connects"]}
        assert not (set(assigned) & nc), "pin both netted and no-connected"
        for ref, comp in doc["components"].items():
            for pin in comp["pins"]:
                assert (ref, pin) in assigned or (ref, pin) in nc, \
                    f"{ref}.{pin} unaccounted"

    def test_no_power_symbol_pseudo_components(self, extracted):
        doc, _summary, _yaml = extracted
        assert not [r for r in doc["components"] if r.startswith("#")]
        for net in doc["nets"].values():
            assert not [p for p in net["pins"] if p["ref"].startswith("#")]

    def test_power_nets_flagged(self, extracted):
        doc, summary, _yaml = extracted
        assert doc["nets"]["GND"]["type"] == "power"
        assert "GND" in doc["nets"]["GND"]["power_symbols"]
        assert summary["power_nets"] >= 2

    def test_clean_source_has_no_floating_pins(self, extracted):
        _doc, summary, _yaml = extracted
        assert summary["floating_pins"] == []

    def test_auto_named_nets_are_deterministic(self, extracted):
        doc, _summary, _yaml = extracted
        doc2, _ = extract_netlist.extract(GOLDEN_SCH)
        auto1 = sorted(n for n in doc["nets"] if n.startswith("N$"))
        auto2 = sorted(n for n in doc2["nets"] if n.startswith("N$"))
        assert auto1 == auto2 and auto1  # present and stable across runs


# ─── extract_bom ─────────────────────────────────────────────────────

class TestExtractBom:
    @pytest.fixture(scope="class")
    def extracted(self):
        rows, summary = extract_bom.extract(GOLDEN_SCH)
        md = extract_bom.emit_markdown(rows, GOLDEN_SCH)
        return rows, summary, md

    def test_all_fitted_lines_extracted_with_identity(self, extracted):
        rows, summary, _md = extracted
        assert summary["lines"] == 58
        assert summary["missing_mpn"] == []
        assert summary["missing_manufacturer"] == []
        assert summary["distributor_code_as_mpn"] == []

    def test_output_cross_checks_against_source_schematic(self, extracted):
        from cross_check_bom import cross_check, load_bom_from_markdown
        from validate_kicad_sch import load_kicad_sch
        _rows, _summary, md = extracted
        result = cross_check(load_bom_from_markdown(md),
                             load_kicad_sch(GOLDEN_SCH))
        assert result.passed, [i.message for i in result.issues]

    def test_output_loads_in_pcbway_checker(self, extracted):
        from check_pcbway import load_bom_for_pcbway
        _rows, _summary, md = extracted
        parts = load_bom_for_pcbway(md)
        assert len(parts) == 58


# ─── reconcile ───────────────────────────────────────────────────────

MINI_SCH = """(kicad_sch (version 20230121) (generator eeschema)
  (uuid "00000000-0000-0000-0000-000000000000")
  (lib_symbols)
  (symbol (lib_id "Device:R") (at 0 0 0)
    (property "Reference" "R1" (at 0 0 0))
    (property "Value" "10k" (at 0 0 0))
    (property "Footprint" "Resistor_SMD:R_0805_2012Metric" (at 0 0 0))
    (property "MPN" "RC0805FR-0710KL" (at 0 0 0))
  )
  (symbol (lib_id "Device:R") (at 10 0 0)
    (property "Reference" "R2" (at 0 0 0))
    (property "Value" "1k" (at 0 0 0))
    (property "Footprint" "Resistor_SMD:R_0805_2012Metric" (at 0 0 0))
    (property "MPN" "RC0805FR-071KL" (at 0 0 0))
  )
  (symbol (lib_id "Device:C") (at 20 0 0)
    (property "Reference" "C1" (at 0 0 0))
    (property "Value" "100nF" (at 0 0 0))
    (property "Footprint" "Capacitor_SMD:C_0805_2012Metric" (at 0 0 0))
    (property "MPN" "CL21B104KBCNNNC" (at 0 0 0))
  )
)
"""

MINI_PCB = """(kicad_pcb (version 20221018) (generator pcbnew)
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (gr_rect (start 0 0) (end 30 20) (layer "Edge.Cuts") (width 0.1))
  (segment (start 5 5) (end 15 5) (width 0.25) (layer "F.Cu") (net 2))
  (via (at 15 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2))
  (footprint "Resistor_SMD:R_0805_2012Metric" (layer "F.Cu")
    (at 5 5)
    (property "Reference" "R1")
    (property "Value" "10k")
    (property "MPN" "RC0805FR-0710KL")
    (pad "1" smd rect (at -0.9 0) (size 1 1.2) (layers "F.Cu") (net 2 "SIG"))
    (pad "2" smd rect (at 0.9 0) (size 1 1.2) (layers "F.Cu") (net 1 "GND"))
  )
  (footprint "Resistor_SMD:R_0805_2012Metric" (layer "F.Cu")
    (at 10 5)
    (property "Reference" "R2")
    (property "Value" "2.2k")
    (pad "1" smd rect (at -0.9 0) (size 1 1.2) (layers "F.Cu") (net 2 "SIG"))
    (pad "2" smd rect (at 0.9 0) (size 1 1.2) (layers "F.Cu") (net 1 "GND"))
  )
  (footprint "TestPoint:TestPoint_Pad_D1.0mm" (layer "F.Cu")
    (at 25 15)
    (property "Reference" "TP1")
    (property "Value" "TP")
    (pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))
  )
)
"""


class TestReconcile:
    def test_golden_sch_vs_fixture_bom_is_clean(self):
        with open(FIXTURE_BOM, encoding="utf-8") as f:
            issues = reconcile.reconcile_sch_bom(GOLDEN_SCH, f.read())
        assert [i for i in issues if i.severity == "error"] == []

    def test_bom_value_drift_detected(self):
        with open(FIXTURE_BOM, encoding="utf-8") as f:
            drifted = f.read().replace("| R1 | 56k |", "| R1 | 47k |")
        issues = reconcile.reconcile_sch_bom(GOLDEN_SCH, drifted)
        assert any(i.check == "value_mismatch" and i.reference == "R1"
                   for i in issues if i.severity == "error")

    def test_sch_pcb_drift_report(self):
        issues = reconcile.reconcile_sch_pcb(MINI_SCH, MINI_PCB)
        by_check = {}
        for i in issues:
            by_check.setdefault(i.check, []).append(i)

        # C1 in schematic, not on board
        assert [i.reference for i in by_check["missing_on_board"]] == ["C1"]
        # R2 value drifted on the board; its MPN never propagated
        assert [i.reference for i in by_check["value_drift"]] == ["R2"]
        assert [i.reference for i in by_check["field_not_propagated"]] == ["R2"]
        # TP1 is board-only but mechanical → info, not error
        (tp,) = by_check["board_only_component"]
        assert tp.reference == "TP1" and tp.severity == "info"
        # R1 matches everywhere → no issues against it
        assert not [i for i in issues if i.reference == "R1"]


# ─── summarize_pcb ───────────────────────────────────────────────────

class TestSummarizePcb:
    @pytest.fixture()
    def summary(self, tmp_path):
        p = tmp_path / "mini.kicad_pcb"
        p.write_text(MINI_PCB, encoding="utf-8")
        return summarize_pcb.summarize(str(p))

    def test_components_and_stackup(self, summary):
        assert [c["ref"] for c in summary["components"]] == ["R1", "R2", "TP1"]
        assert [l["name"] for l in summary["board"]["copper_layers"]] == \
            ["F.Cu", "B.Cu"]
        r1 = summary["components"][0]
        assert r1["lib_id"] == "Resistor_SMD:R_0805_2012Metric"
        assert r1["side"] == "top" and r1["pads"] == 2

    def test_outline_and_routing(self, summary):
        assert summary["board"]["outline"]["width_mm"] == 30.0
        assert summary["board"]["outline"]["height_mm"] == 20.0
        (sig,) = [n for n in summary["routed_nets"] if n["name"] == "SIG"]
        assert sig["length_mm"] == 10.0
        assert sig["width_mm"] == 0.25
        assert sig["vias"] == 1

    def test_yaml_emission_runs(self, summary):
        text = summarize_pcb.emit_yaml(summary)
        assert "routed_nets:" in text and "copper_layers:" in text
