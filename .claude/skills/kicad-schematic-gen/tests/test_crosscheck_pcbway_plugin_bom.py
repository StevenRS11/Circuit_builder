"""Unit tests for the PCBWay plugin-BOM cross-check (crosscheck_pcbway_plugin_bom.py).

Synthetic, offline: a few tab-indented symbol instances mirroring the .kicad_sch
instance format. Covers selection (power/in_bom exclusion), MPN aliasing, DNP
detection, plugin-style grouping, anomaly flags, and the qty-by-MPN diff.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from crosscheck_pcbway_plugin_bom import (
    parse_symbols, build_bom, anomalies, diff_against,
)


def _inst(ref, value, fp, extra_props="", dnp="no", in_bom="yes"):
    props = (
        f'\t\t(property "Reference" "{ref}"\n\t\t\t(at 0 0 0)\n\t\t\t(effects (font (size 1.27 1.27)))\n\t\t)\n'
        f'\t\t(property "Value" "{value}"\n\t\t\t(at 0 0 0)\n\t\t\t(effects (font (size 1.27 1.27)))\n\t\t)\n'
        f'\t\t(property "Footprint" "{fp}"\n\t\t\t(at 0 0 0)\n\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n\t\t)\n'
        + extra_props
    )
    return (f'\t(symbol\n\t\t(lib_id "x")\n\t\t(at 0 0 0)\n\t\t(in_bom {in_bom})\n'
            f'\t\t(dnp {dnp})\n{props}\t)\n')


def _prop(name, value):
    return (f'\t\t(property "{name}" "{value}"\n\t\t\t(at 0 0 0)\n'
            f'\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n\t\t)\n')


SCH = (
    '(kicad_sch\n'
    + _inst("R1", "10k", "Resistor_SMD:R_0805_2012Metric", _prop("MPN", "RC0805FR-0710KL") + _prop("Manufacturer", "Yageo") + _prop("LCSC", "C84376"))
    + _inst("R2", "10k", "Resistor_SMD:R_0805_2012Metric", _prop("MPN", "RC0805FR-0710KL") + _prop("LCSC", "C84376"))
    + _inst("C1", "100nF", "Capacitor_SMD:C_0805_2012Metric", _prop("Mfg Part", "CL21B104KBCNNNC"))  # alias
    + _inst("R3", "1k", "Resistor_SMD:R_0805_2012Metric")  # blank MPN (assembled)
    + _inst("U1", "ESP32", "Module:ESP32", _prop("MPN", "ESP32-S3-Zero"), dnp="yes")  # DNP
    + _inst("#PWR01", "GND", "", in_bom="no")  # power symbol -> excluded
    + ')\n'
)


def test_parse_excludes_power_and_in_bom_no():
    refs = {c["ref"] for c in parse_symbols(SCH)}
    assert refs == {"R1", "R2", "C1", "R3", "U1"}  # no #PWR01


def test_mpn_alias_read():
    c1 = next(c for c in parse_symbols(SCH) if c["ref"] == "C1")
    assert c1["mpn"] == "CL21B104KBCNNNC"  # read via the 'Mfg Part' alias


def test_dnp_detected():
    u1 = next(c for c in parse_symbols(SCH) if c["ref"] == "U1")
    assert u1["dnp"] is True


def test_grouping_merges_same_part():
    rows = build_bom(parse_symbols(SCH))
    tenk = [r for r in rows if r["MPN"] == "RC0805FR-0710KL"]
    assert len(tenk) == 1 and tenk[0]["Quantity"] == 2  # R1+R2 merged
    assert "R1" in tenk[0]["Designator"] and "R2" in tenk[0]["Designator"]


def test_dnp_row_present_and_separate():
    rows = build_bom(parse_symbols(SCH))
    dnp_rows = [r for r in rows if r["DNP"] == "Yes"]
    assert len(dnp_rows) == 1 and dnp_rows[0]["Designator"] == "U1"


def test_anomaly_blank_mpn():
    rows = build_bom(parse_symbols(SCH))
    a = anomalies(rows)
    assert "R3" in a["blank_mpn"]  # assembled part with no MPN flagged


def test_anomaly_fragmented_group():
    # two different Value strings sharing one MPN -> fragmented
    sch = ('(kicad_sch\n'
           + _inst("C1", ".1uf", "Capacitor_SMD:C_0805_2012Metric", _prop("MPN", "CL21B104KBCNNNC"))
           + _inst("C2", "100nF", "Capacitor_SMD:C_0805_2012Metric", _prop("MPN", "CL21B104KBCNNNC"))
           + ')\n')
    a = anomalies(build_bom(parse_symbols(sch)))
    assert "CL21B104KBCNNNC" in a["fragmented_groups"]


def test_diff_against_qty_by_mpn():
    rows = build_bom(parse_symbols(SCH))
    # plugin CSV agreeing on qty-by-MPN (fragmented differently, still fine)
    plugin_ok = ("Designator,Quantity,Value,Footprint,Package,MPN\n"
                 "R1,1,10k,R_0805,,RC0805FR-0710KL\n"
                 "R2,1,10k,R_0805,,RC0805FR-0710KL\n"
                 "C1,1,100nF,C_0805,,CL21B104KBCNNNC\n")
    d = diff_against(rows, plugin_ok)
    assert d["plugin_columns_understood"]
    assert not d["qty_mismatch"] and not d["only_in_plugin"]

    # plugin missing a part (propagation failure) -> flagged
    plugin_bad = ("Designator,Quantity,MPN\nR1,2,RC0805FR-0710KL\n")
    d2 = diff_against(rows, plugin_bad)
    assert "CL21B104KBCNNNC" in d2["only_in_schematic"]
