#!/usr/bin/env python3
"""
DC Analysis Engine — validates electrical correctness of circuit designs before
schematic generation by pattern-matching known subcircuit topologies and running
the appropriate formulas.

Operates on a YAML design file (produced from the implementation reference),
NOT on the schematic itself. This means errors are caught before they become wires.

CLI Usage:
    python analyze_dc.py <design.yaml>
    python analyze_dc.py <design.yaml> --json
    python analyze_dc.py <design.yaml> --strict   # treat warnings as errors

Python API Usage:
    from analyze_dc import analyze, load_design

    design = load_design("design.yaml")
    result = analyze(design)
    if not result.passed:
        for issue in result.issues:
            print(issue)

Supported subcircuit patterns:
    - voltage_divider      Vout = Vin * R2/(R1+R2), compare to target
    - ldo_regulator        Dropout, power dissipation, thermal, cap sizing
    - led_circuit          Forward current from Vsource, Vf, R
    - pullup_network       Current draw, logic level thresholds
    - current_budget       Sum loads per rail vs source capacity
    - cap_sizing           Minimum capacitance checks vs datasheet requirements
    - resistor_divider_fb  Feedback dividers for adjustable regulators
"""

import sys
import os
import json as json_module
import math
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Try to import PyYAML; fall back to a minimal subset parser if unavailable.
# The design files are simple enough that the fallback handles them.
# ---------------------------------------------------------------------------
try:
    import yaml as _yaml

    def _load_yaml(text):
        return _yaml.safe_load(text)
except ImportError:
    _yaml = None

    def _load_yaml(text):
        """Minimal YAML-subset loader for flat/nested dicts and lists.

        Handles the specific structure of design YAML files produced by this
        tool.  NOT a general-purpose YAML parser — use PyYAML for that.
        """
        import json as _json
        # Strategy: the design YAML is also valid JSON-ish if we pre-process
        # it a bit.  But safer to just require PyYAML and give a clear error.
        raise ImportError(
            "PyYAML is required for analyze_dc.py. "
            "Install it with: pip install pyyaml"
        )


# ─── Data structures ─────────────────────────────────────────────────────


@dataclass
class AnalysisIssue:
    """A single finding from DC analysis."""
    severity: str          # "error", "warning", "info"
    category: str          # subcircuit pattern name
    message: str           # human-readable description
    details: dict = field(default_factory=dict)  # computed values

    def __str__(self):
        tag = self.severity.upper()
        return f"[{tag}] ({self.category}) {self.message}"


@dataclass
class AnalysisResult:
    """Aggregate result from all DC checks."""
    passed: bool
    issues: list = field(default_factory=list)   # list[AnalysisIssue]
    subcircuits_analyzed: int = 0
    subcircuits_unanalyzed: int = 0

    @property
    def errors(self):
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self):
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def infos(self):
        return [i for i in self.issues if i.severity == "info"]


@dataclass
class Design:
    """Parsed design file — all the subcircuits and rails to analyze."""
    project_name: str = ""
    rails: list = field(default_factory=list)
    voltage_dividers: list = field(default_factory=list)
    ldo_regulators: list = field(default_factory=list)
    led_circuits: list = field(default_factory=list)
    pullup_networks: list = field(default_factory=list)
    cap_checks: list = field(default_factory=list)
    feedback_dividers: list = field(default_factory=list)
    custom_checks: list = field(default_factory=list)


# ─── Design file loader ──────────────────────────────────────────────────


def load_design(filepath):
    """Load a design YAML file into a Design object."""
    with open(filepath, "r", encoding="utf-8") as f:
        raw = _load_yaml(f.read())
    return _parse_design(raw)


def load_design_from_string(text):
    """Load a design from a YAML string."""
    raw = _load_yaml(text)
    return _parse_design(raw)


def _parse_design(raw):
    """Parse raw YAML dict into a Design object."""
    if not isinstance(raw, dict):
        raise ValueError("Design file must be a YAML mapping at the top level")

    d = Design()
    d.project_name = raw.get("project_name", "")
    d.rails = raw.get("rails", [])
    d.voltage_dividers = raw.get("voltage_dividers", [])
    d.ldo_regulators = raw.get("ldo_regulators", [])
    d.led_circuits = raw.get("led_circuits", [])
    d.pullup_networks = raw.get("pullup_networks", [])
    d.cap_checks = raw.get("cap_checks", [])
    d.feedback_dividers = raw.get("feedback_dividers", [])
    d.custom_checks = raw.get("custom_checks", [])
    return d


# ─── Parse helpers ────────────────────────────────────────────────────────


def _parse_value(val):
    """Parse a component value string into a float.

    Handles engineering notation:
        "10k" → 10000.0
        "4.7k" → 4700.0
        "100nF" → 1e-7
        "10uF" → 1e-5
        "1M" → 1e6
        "2.2" → 2.2
        "470" → 470.0
    """
    if isinstance(val, (int, float)):
        return float(val)

    s = str(val).strip()

    # Unit suffix multipliers — order matters (longer suffixes first)
    suffixes = [
        ("mF", 1e-3),   ("uF", 1e-6),   ("µF", 1e-6),  ("nF", 1e-9),
        ("pF", 1e-12),  ("mH", 1e-3),   ("uH", 1e-6),  ("µH", 1e-6),
        ("nH", 1e-9),   ("mA", 1e-3),   ("uA", 1e-6),  ("µA", 1e-6),
        ("mV", 1e-3),   ("mW", 1e-3),
        ("meg", 1e6),   ("Meg", 1e6),   ("MEG", 1e6),
        ("M", 1e6),
        ("k", 1e3),     ("K", 1e3),
        ("m", 1e-3),    # lowercase m alone = milli (after mA/mV/mW/mH/mF)
        ("u", 1e-6),    ("µ", 1e-6),
        ("n", 1e-9),    ("p", 1e-12),
        ("F", 1.0),     ("H", 1.0),     ("V", 1.0),
        ("A", 1.0),     ("W", 1.0),     ("R", 1.0),     ("ohm", 1.0),
        ("Ohm", 1.0),   ("OHM", 1.0),   ("Ω", 1.0),
    ]

    for suffix, mult in suffixes:
        if s.endswith(suffix):
            num_part = s[: -len(suffix)].strip()
            if num_part == "":
                continue
            try:
                return float(num_part) * mult
            except ValueError:
                continue

    # No suffix matched — try raw float
    try:
        return float(s)
    except ValueError:
        raise ValueError(f"Cannot parse component value: {val!r}")


# ─── Subcircuit analyzers ────────────────────────────────────────────────

# Each analyzer takes a subcircuit dict and returns a list of AnalysisIssue.


def _analyze_voltage_divider(vd):
    """Analyze a resistive voltage divider.

    Expected keys:
        name:       descriptive name
        vin:        input voltage (V)
        r_top:      top resistor value (connects Vin to tap)
        r_bot:      bottom resistor value (connects tap to GND)
        target_vout: desired output voltage (V)
        tolerance_pct: acceptable error percentage (default 5)
        load_current_ua: load current drawn from tap (default 0)
    """
    issues = []
    name = vd.get("name", "unnamed divider")

    try:
        vin = float(vd["vin"])
        r_top = _parse_value(vd["r_top"])
        r_bot = _parse_value(vd["r_bot"])
        target = float(vd["target_vout"])
        tol = float(vd.get("tolerance_pct", 5))
        i_load = _parse_value(vd.get("load_current_ua", 0)) * 1e-6 if "load_current_ua" in vd else 0
    except (KeyError, ValueError) as e:
        issues.append(AnalysisIssue("error", "voltage_divider",
                                    f"{name}: bad or missing parameter — {e}"))
        return issues

    if r_top <= 0 or r_bot <= 0:
        issues.append(AnalysisIssue("error", "voltage_divider",
                                    f"{name}: resistor values must be positive"))
        return issues

    # Unloaded divider output
    vout_unloaded = vin * r_bot / (r_top + r_bot)

    # Loaded divider: R_bot in parallel with load resistance
    if i_load > 0 and target > 0:
        r_load = target / i_load  # approximate
        r_bot_loaded = (r_bot * r_load) / (r_bot + r_load)
        vout_loaded = vin * r_bot_loaded / (r_top + r_bot_loaded)
    else:
        vout_loaded = vout_unloaded

    # Quiescent current through divider
    i_divider_ma = (vin / (r_top + r_bot)) * 1000

    # Error vs target
    error_pct = abs(vout_loaded - target) / target * 100 if target > 0 else 0

    details = {
        "vin": vin,
        "r_top": r_top,
        "r_bot": r_bot,
        "vout_unloaded": round(vout_unloaded, 4),
        "vout_loaded": round(vout_loaded, 4),
        "target_vout": target,
        "error_pct": round(error_pct, 2),
        "divider_current_mA": round(i_divider_ma, 4),
    }

    if error_pct > tol:
        issues.append(AnalysisIssue(
            "error", "voltage_divider",
            f"{name}: output {vout_loaded:.3f}V is {error_pct:.1f}% off target "
            f"{target}V (tolerance {tol}%). "
            f"R_top={vd['r_top']}, R_bot={vd['r_bot']}",
            details,
        ))
    else:
        issues.append(AnalysisIssue(
            "info", "voltage_divider",
            f"{name}: output {vout_loaded:.3f}V vs target {target}V "
            f"({error_pct:.1f}% error) — OK",
            details,
        ))

    # Warn if quiescent current is high
    if i_divider_ma > 1.0:
        issues.append(AnalysisIssue(
            "warning", "voltage_divider",
            f"{name}: divider draws {i_divider_ma:.2f}mA quiescent — "
            f"consider higher-value resistors if power budget is tight",
            details,
        ))

    # Power dissipation in each resistor
    p_top_mw = (vin - vout_loaded) ** 2 / r_top * 1000
    p_bot_mw = vout_loaded ** 2 / r_bot * 1000
    if p_top_mw > 62.5:  # 1/16W rating for 0805
        issues.append(AnalysisIssue(
            "warning", "voltage_divider",
            f"{name}: R_top dissipates {p_top_mw:.1f}mW — "
            f"check resistor power rating (0805 typ 125mW)",
            details,
        ))
    if p_bot_mw > 62.5:
        issues.append(AnalysisIssue(
            "warning", "voltage_divider",
            f"{name}: R_bot dissipates {p_bot_mw:.1f}mW — "
            f"check resistor power rating (0805 typ 125mW)",
            details,
        ))

    return issues


def _analyze_ldo_regulator(ldo):
    """Analyze an LDO regulator block.

    Expected keys:
        name:           descriptive name
        part:           part number
        vin:            input voltage (V)
        vout:           output voltage (V)
        dropout_v:      dropout voltage (V)
        max_current_ma: max rated output current (mA)
        load_current_ma: actual load current (mA)
        cin_uf:         input capacitor value (uF)
        cout_uf:        output capacitor value (uF)
        cin_min_uf:     minimum input cap from datasheet (uF)
        cout_min_uf:    minimum output cap from datasheet (uF)
        package:        package name (for thermal calc)
        theta_ja:       junction-to-ambient thermal resistance (°C/W, optional)
        max_tj:         max junction temp (°C, default 125)
        ambient_c:      ambient temperature (°C, default 25)
        enable_pin:     how EN pin is handled ("tied_high", "external", "internal_pullup")
    """
    issues = []
    name = ldo.get("name", ldo.get("part", "unnamed LDO"))

    try:
        vin = float(ldo["vin"])
        vout = float(ldo["vout"])
        dropout = float(ldo.get("dropout_v", 0.3))
        max_i = float(ldo["max_current_ma"])
        load_i = float(ldo["load_current_ma"])
    except (KeyError, ValueError) as e:
        issues.append(AnalysisIssue("error", "ldo_regulator",
                                    f"{name}: bad or missing parameter — {e}"))
        return issues

    # --- Dropout check ---
    headroom = vin - vout
    if headroom < dropout:
        issues.append(AnalysisIssue(
            "error", "ldo_regulator",
            f"{name}: insufficient headroom. Vin={vin}V - Vout={vout}V = "
            f"{headroom:.2f}V, but dropout is {dropout}V. "
            f"Output will not regulate.",
            {"headroom_v": headroom, "dropout_v": dropout},
        ))
    elif headroom < dropout * 1.5:
        issues.append(AnalysisIssue(
            "warning", "ldo_regulator",
            f"{name}: headroom is tight. Vin-Vout={headroom:.2f}V vs "
            f"dropout {dropout}V — may lose regulation under load or "
            f"at high temperature.",
            {"headroom_v": headroom, "dropout_v": dropout},
        ))

    # --- Current capacity ---
    if load_i > max_i:
        issues.append(AnalysisIssue(
            "error", "ldo_regulator",
            f"{name}: load {load_i}mA exceeds max rating {max_i}mA",
        ))
    elif load_i > max_i * 0.8:
        issues.append(AnalysisIssue(
            "warning", "ldo_regulator",
            f"{name}: load {load_i}mA is >{80}% of max rating {max_i}mA — "
            f"little margin for transients",
        ))

    # --- Power dissipation & thermal ---
    p_diss_w = (vin - vout) * (load_i / 1000.0)
    p_diss_mw = p_diss_w * 1000

    # Typical theta_ja values by package
    default_theta = {
        "SOT-23-5": 250, "SOT-23-6": 230, "SOT-23": 310,
        "SOT-223": 60, "SOIC-8": 120, "DFN": 80, "QFN": 40,
        "TO-252": 30, "TO-263": 20, "TO-220": 25,
    }
    pkg = ldo.get("package", "SOT-23-5")
    theta_ja = float(ldo.get("theta_ja", default_theta.get(pkg, 200)))
    max_tj = float(ldo.get("max_tj", 125))
    ambient = float(ldo.get("ambient_c", 25))

    tj = ambient + p_diss_w * theta_ja
    thermal_details = {
        "power_dissipation_mW": round(p_diss_mw, 1),
        "junction_temp_C": round(tj, 1),
        "max_junction_temp_C": max_tj,
        "theta_ja": theta_ja,
        "package": pkg,
    }

    if tj > max_tj:
        issues.append(AnalysisIssue(
            "error", "ldo_regulator",
            f"{name}: junction temp {tj:.0f}°C exceeds max {max_tj}°C. "
            f"Power dissipation {p_diss_mw:.0f}mW in {pkg} "
            f"(θja={theta_ja}°C/W). Needs a bigger package or heatsink.",
            thermal_details,
        ))
    elif tj > max_tj * 0.8:
        issues.append(AnalysisIssue(
            "warning", "ldo_regulator",
            f"{name}: junction temp {tj:.0f}°C is >{int(max_tj*0.8)}°C "
            f"({p_diss_mw:.0f}mW in {pkg}). Consider thermal margin.",
            thermal_details,
        ))
    else:
        issues.append(AnalysisIssue(
            "info", "ldo_regulator",
            f"{name}: thermal OK — Tj={tj:.0f}°C, "
            f"Pdiss={p_diss_mw:.0f}mW in {pkg}",
            thermal_details,
        ))

    # --- Capacitor checks ---
    cin = _parse_value(ldo.get("cin_uf", 0)) * 1e-6 if "cin_uf" in ldo else None
    cout = _parse_value(ldo.get("cout_uf", 0)) * 1e-6 if "cout_uf" in ldo else None
    cin_min = _parse_value(ldo.get("cin_min_uf", 0)) * 1e-6 if "cin_min_uf" in ldo else None
    cout_min = _parse_value(ldo.get("cout_min_uf", 0)) * 1e-6 if "cout_min_uf" in ldo else None

    # Re-parse as simple floats in uF for comparison
    if "cin_uf" in ldo and "cin_min_uf" in ldo:
        cin_val = float(ldo["cin_uf"])
        cin_min_val = float(ldo["cin_min_uf"])
        if cin_val < cin_min_val:
            issues.append(AnalysisIssue(
                "error", "ldo_regulator",
                f"{name}: input cap {cin_val}uF < datasheet minimum {cin_min_val}uF",
            ))

    if "cout_uf" in ldo and "cout_min_uf" in ldo:
        cout_val = float(ldo["cout_uf"])
        cout_min_val = float(ldo["cout_min_uf"])
        if cout_val < cout_min_val:
            issues.append(AnalysisIssue(
                "error", "ldo_regulator",
                f"{name}: output cap {cout_val}uF < datasheet minimum {cout_min_val}uF",
            ))

    # --- Enable pin ---
    en = ldo.get("enable_pin", None)
    if en is None:
        issues.append(AnalysisIssue(
            "warning", "ldo_regulator",
            f"{name}: enable pin handling not specified — verify it's tied high "
            f"or properly controlled",
        ))

    return issues


def _analyze_led_circuit(led):
    """Analyze an LED current-limiting resistor circuit.

    Expected keys:
        name:           descriptive name
        vsource:        supply voltage (V)
        vf:             LED forward voltage (V)
        resistor:       current-limiting resistor value
        target_current_ma: desired LED current (mA, default 10)
        max_current_ma: max LED rating (mA, default 20)
    """
    issues = []
    name = led.get("name", "unnamed LED")

    try:
        vsource = float(led["vsource"])
        vf = float(led["vf"])
        r = _parse_value(led["resistor"])
        target_ma = float(led.get("target_current_ma", 10))
        max_ma = float(led.get("max_current_ma", 20))
    except (KeyError, ValueError) as e:
        issues.append(AnalysisIssue("error", "led_circuit",
                                    f"{name}: bad or missing parameter — {e}"))
        return issues

    if r <= 0:
        issues.append(AnalysisIssue("error", "led_circuit",
                                    f"{name}: resistor must be positive"))
        return issues

    if vsource <= vf:
        issues.append(AnalysisIssue("error", "led_circuit",
                                    f"{name}: Vsource {vsource}V <= Vf {vf}V — "
                                    f"LED will not turn on"))
        return issues

    i_ma = (vsource - vf) / r * 1000
    p_r_mw = (vsource - vf) ** 2 / r * 1000
    p_led_mw = vf * i_ma

    details = {
        "current_mA": round(i_ma, 2),
        "resistor_power_mW": round(p_r_mw, 1),
        "led_power_mW": round(p_led_mw, 1),
    }

    if i_ma > max_ma:
        issues.append(AnalysisIssue(
            "error", "led_circuit",
            f"{name}: current {i_ma:.1f}mA exceeds max LED rating {max_ma}mA. "
            f"Increase R from {led['resistor']} to at least "
            f"{_suggest_resistor(vsource, vf, max_ma)}",
            details,
        ))
    elif i_ma < 0.5:
        issues.append(AnalysisIssue(
            "warning", "led_circuit",
            f"{name}: current {i_ma:.2f}mA is very low — LED may not be visible",
            details,
        ))
    elif abs(i_ma - target_ma) / target_ma > 0.5:
        issues.append(AnalysisIssue(
            "warning", "led_circuit",
            f"{name}: current {i_ma:.1f}mA differs significantly from "
            f"target {target_ma}mA",
            details,
        ))
    else:
        issues.append(AnalysisIssue(
            "info", "led_circuit",
            f"{name}: {i_ma:.1f}mA through LED — OK",
            details,
        ))

    return issues


def _suggest_resistor(vsource, vf, target_ma):
    """Suggest a standard resistor value for the target current."""
    ideal = (vsource - vf) / (target_ma / 1000)
    # E12 series
    e12 = [100, 120, 150, 180, 220, 270, 330, 390, 470, 560, 680, 820, 1000,
           1200, 1500, 1800, 2200, 2700, 3300, 3900, 4700, 5600, 6800, 8200,
           10000, 12000, 15000, 18000, 22000, 27000, 33000, 39000, 47000]
    # Pick the first E12 value >= ideal (to keep current at or below target)
    for v in e12:
        if v >= ideal:
            if v >= 1000:
                return f"{v/1000:.1f}k".replace(".0k", "k")
            return str(v)
    return f"{ideal:.0f}"


def _analyze_pullup_network(pu):
    """Analyze pull-up or pull-down resistor networks.

    Expected keys:
        name:           descriptive name
        vsource:        pull-up supply voltage (V)
        resistor:       pull-up/pull-down value
        bus_type:       "i2c", "spi_cs", "reset", "generic" (default "generic")
        bus_speed:      for I2C: "standard" (100kHz), "fast" (400kHz),
                        "fast_plus" (1MHz)
        bus_capacitance_pf: estimated bus capacitance (pF, default 50)
        num_devices:    number of devices on the bus (default 1)
    """
    issues = []
    name = pu.get("name", "unnamed pull-up")

    try:
        vsource = float(pu["vsource"])
        r = _parse_value(pu["resistor"])
    except (KeyError, ValueError) as e:
        issues.append(AnalysisIssue("error", "pullup_network",
                                    f"{name}: bad or missing parameter — {e}"))
        return issues

    bus_type = pu.get("bus_type", "generic")
    i_ma = vsource / r * 1000

    details = {
        "current_mA": round(i_ma, 3),
        "bus_type": bus_type,
    }

    # I2C-specific checks
    if bus_type == "i2c":
        speed = pu.get("bus_speed", "fast")  # default 400kHz
        c_bus_pf = float(pu.get("bus_capacitance_pf", 50))

        # I2C spec: rise time must be < threshold for the speed mode
        # t_rise = 0.8473 * R * C  (10% to 70% of VCC)
        t_rise_ns = 0.8473 * r * c_bus_pf * 1e-12 * 1e9

        rise_time_limits = {
            "standard": 1000,    # 1000ns for 100kHz
            "fast": 300,         # 300ns for 400kHz
            "fast_plus": 120,    # 120ns for 1MHz
        }
        limit_ns = rise_time_limits.get(speed, 300)

        details["rise_time_ns"] = round(t_rise_ns, 1)
        details["rise_time_limit_ns"] = limit_ns
        details["bus_capacitance_pf"] = c_bus_pf

        if t_rise_ns > limit_ns:
            issues.append(AnalysisIssue(
                "warning", "pullup_network",
                f"{name}: rise time {t_rise_ns:.0f}ns exceeds {speed} mode "
                f"limit of {limit_ns}ns (Cbus={c_bus_pf}pF). "
                f"Decrease pull-up value or reduce bus capacitance.",
                details,
            ))

        # I2C spec max sink current: 3mA (standard/fast), 20mA (fast-plus)
        max_sink = 20 if speed == "fast_plus" else 3
        if i_ma > max_sink:
            issues.append(AnalysisIssue(
                "error", "pullup_network",
                f"{name}: pull-up current {i_ma:.1f}mA exceeds I2C {speed} "
                f"mode max sink {max_sink}mA",
                details,
            ))

        # Common I2C range check
        if r < 1000:
            issues.append(AnalysisIssue(
                "warning", "pullup_network",
                f"{name}: {pu['resistor']} is unusually low for I2C — "
                f"typical range 1k-10k for 3.3V",
                details,
            ))
        elif r > 10000 and speed != "standard":
            issues.append(AnalysisIssue(
                "warning", "pullup_network",
                f"{name}: {pu['resistor']} may be too high for I2C {speed} "
                f"mode — check rise time with actual bus capacitance",
                details,
            ))
        else:
            issues.append(AnalysisIssue(
                "info", "pullup_network",
                f"{name}: {pu['resistor']} pull-up, {i_ma:.2f}mA, "
                f"rise time {t_rise_ns:.0f}ns — OK for I2C {speed}",
                details,
            ))
    else:
        # Generic pull-up/pull-down — just report current
        issues.append(AnalysisIssue(
            "info", "pullup_network",
            f"{name}: {pu['resistor']} pull-{'up' if vsource > 0 else 'down'}, "
            f"{i_ma:.2f}mA",
            details,
        ))

    return issues


def _analyze_current_budget(rails):
    """Analyze current budget for each power rail.

    Each rail in the list has:
        name:           rail name (e.g., "+3V3")
        voltage:        rail voltage (V)
        source_ma:      max current from source (mA)
        loads:          list of {name, current_ma} dicts
    """
    issues = []

    for rail in rails:
        name = rail.get("name", "unnamed rail")

        try:
            source_ma = float(rail["source_ma"])
            loads = rail.get("loads", [])
        except (KeyError, ValueError) as e:
            issues.append(AnalysisIssue("error", "current_budget",
                                        f"{name}: bad or missing parameter — {e}"))
            continue

        total_ma = 0
        load_lines = []
        for load in loads:
            try:
                load_ma = float(load["current_ma"])
            except (KeyError, ValueError):
                issues.append(AnalysisIssue("warning", "current_budget",
                                            f"{name}: cannot parse load {load.get('name', '?')}"))
                continue
            total_ma += load_ma
            load_lines.append(f"  {load.get('name', '?')}: {load_ma}mA")

        details = {
            "rail": name,
            "source_ma": source_ma,
            "total_load_ma": round(total_ma, 2),
            "margin_pct": round((1 - total_ma / source_ma) * 100, 1) if source_ma > 0 else 0,
            "loads": load_lines,
        }

        if total_ma > source_ma:
            issues.append(AnalysisIssue(
                "error", "current_budget",
                f"{name}: total load {total_ma:.0f}mA exceeds source "
                f"capacity {source_ma}mA ({total_ma - source_ma:.0f}mA over)",
                details,
            ))
        elif total_ma > source_ma * 0.8:
            issues.append(AnalysisIssue(
                "warning", "current_budget",
                f"{name}: total load {total_ma:.0f}mA is "
                f"{total_ma/source_ma*100:.0f}% of source capacity {source_ma}mA "
                f"— low margin",
                details,
            ))
        else:
            issues.append(AnalysisIssue(
                "info", "current_budget",
                f"{name}: {total_ma:.0f}mA / {source_ma}mA "
                f"({details['margin_pct']:.0f}% margin) — OK",
                details,
            ))

    return issues


def _analyze_cap_sizing(checks):
    """Analyze capacitor sizing against datasheet requirements.

    Each check has:
        name:           descriptive name (e.g., "U1 output cap")
        reference:      component reference (e.g., "C2")
        value_uf:       actual capacitor value (uF)
        min_uf:         minimum from datasheet (uF)
        max_esr_ohm:    maximum ESR if specified (ohm, optional)
        voltage_rating: capacitor voltage rating (V, optional)
        operating_voltage: voltage across cap in circuit (V, optional)
        type:           "MLCC", "electrolytic", "tantalum" (optional)
    """
    issues = []

    for chk in checks:
        name = chk.get("name", chk.get("reference", "unnamed cap"))

        try:
            val = float(chk["value_uf"])
            min_val = float(chk["min_uf"])
        except (KeyError, ValueError) as e:
            issues.append(AnalysisIssue("error", "cap_sizing",
                                        f"{name}: bad or missing parameter — {e}"))
            continue

        if val < min_val:
            issues.append(AnalysisIssue(
                "error", "cap_sizing",
                f"{name}: {val}uF < datasheet minimum {min_val}uF",
            ))
        else:
            issues.append(AnalysisIssue(
                "info", "cap_sizing",
                f"{name}: {val}uF >= minimum {min_val}uF — OK",
            ))

        # Voltage derating check for MLCC
        cap_type = chk.get("type", "MLCC")
        if "voltage_rating" in chk and "operating_voltage" in chk:
            vr = float(chk["voltage_rating"])
            vo = float(chk["operating_voltage"])

            if cap_type == "MLCC":
                # MLCC loses capacitance under DC bias — recommend 2x derating
                if vr < vo * 2:
                    issues.append(AnalysisIssue(
                        "warning", "cap_sizing",
                        f"{name}: voltage rating {vr}V at {vo}V operating. "
                        f"MLCC capacitance drops under DC bias — recommend "
                        f">= {vo*2:.0f}V rating (2x derating).",
                    ))
                elif vr < vo * 1.2:
                    issues.append(AnalysisIssue(
                        "error", "cap_sizing",
                        f"{name}: voltage rating {vr}V is barely above "
                        f"operating voltage {vo}V — risk of failure",
                    ))
            else:
                # Electrolytic/tantalum: 1.5x derating
                if vr < vo * 1.5:
                    issues.append(AnalysisIssue(
                        "warning", "cap_sizing",
                        f"{name}: voltage rating {vr}V at {vo}V operating. "
                        f"Recommend >= {vo*1.5:.0f}V for {cap_type}.",
                    ))

    return issues


def _analyze_feedback_divider(fb):
    """Analyze a feedback voltage divider for adjustable regulators.

    Expected keys:
        name:           descriptive name
        vref:           regulator internal reference voltage (V)
        r_top:          top resistor (Vout to FB pin)
        r_bot:          bottom resistor (FB pin to GND)
        target_vout:    desired output voltage (V)
        tolerance_pct:  acceptable error (default 2)
        ifb_ua:         FB pin bias current (uA, default 0)
    """
    issues = []
    name = fb.get("name", "unnamed feedback divider")

    try:
        vref = float(fb["vref"])
        r_top = _parse_value(fb["r_top"])
        r_bot = _parse_value(fb["r_bot"])
        target = float(fb["target_vout"])
        tol = float(fb.get("tolerance_pct", 2))
        ifb = float(fb.get("ifb_ua", 0)) * 1e-6
    except (KeyError, ValueError) as e:
        issues.append(AnalysisIssue("error", "feedback_divider",
                                    f"{name}: bad or missing parameter — {e}"))
        return issues

    # For adjustable regulators: Vout = Vref * (1 + R_top/R_bot) + Ifb * R_top
    vout_calc = vref * (1 + r_top / r_bot) + ifb * r_top
    error_pct = abs(vout_calc - target) / target * 100 if target > 0 else 0

    details = {
        "vref": vref,
        "r_top": r_top,
        "r_bot": r_bot,
        "vout_calculated": round(vout_calc, 4),
        "target_vout": target,
        "error_pct": round(error_pct, 2),
    }

    if error_pct > tol:
        issues.append(AnalysisIssue(
            "error", "feedback_divider",
            f"{name}: calculated Vout={vout_calc:.3f}V is {error_pct:.1f}% "
            f"off target {target}V (Vref={vref}V, R_top={fb['r_top']}, "
            f"R_bot={fb['r_bot']})",
            details,
        ))
    else:
        issues.append(AnalysisIssue(
            "info", "feedback_divider",
            f"{name}: Vout={vout_calc:.3f}V vs target {target}V "
            f"({error_pct:.1f}% error) — OK",
            details,
        ))

    return issues


# ─── Main analysis runner ────────────────────────────────────────────────


def analyze(design, strict=False):
    """Run all DC analysis checks on a Design object.

    Args:
        design: Design object with subcircuits to analyze
        strict: if True, treat warnings as errors for pass/fail

    Returns:
        AnalysisResult
    """
    all_issues = []
    analyzed = 0
    unanalyzed = 0

    # Voltage dividers
    for vd in design.voltage_dividers:
        all_issues.extend(_analyze_voltage_divider(vd))
        analyzed += 1

    # LDO regulators
    for ldo in design.ldo_regulators:
        all_issues.extend(_analyze_ldo_regulator(ldo))
        analyzed += 1

    # LED circuits
    for led in design.led_circuits:
        all_issues.extend(_analyze_led_circuit(led))
        analyzed += 1

    # Pull-up networks
    for pu in design.pullup_networks:
        all_issues.extend(_analyze_pullup_network(pu))
        analyzed += 1

    # Current budgets (rails)
    if design.rails:
        all_issues.extend(_analyze_current_budget(design.rails))
        analyzed += len(design.rails)

    # Capacitor sizing
    if design.cap_checks:
        all_issues.extend(_analyze_cap_sizing(design.cap_checks))
        analyzed += len(design.cap_checks)

    # Feedback dividers
    for fb in design.feedback_dividers:
        all_issues.extend(_analyze_feedback_divider(fb))
        analyzed += 1

    # Custom checks (unanalyzed — just pass through)
    for cc in design.custom_checks:
        unanalyzed += 1
        all_issues.append(AnalysisIssue(
            "info", "custom",
            f"Unanalyzed subcircuit: {cc.get('name', '?')} — "
            f"{cc.get('notes', 'manual review required')}",
        ))

    # Determine pass/fail
    has_errors = any(i.severity == "error" for i in all_issues)
    has_warnings = any(i.severity == "warning" for i in all_issues)
    passed = not has_errors and (not strict or not has_warnings)

    return AnalysisResult(
        passed=passed,
        issues=all_issues,
        subcircuits_analyzed=analyzed,
        subcircuits_unanalyzed=unanalyzed,
    )


# ─── Output formatters ──────────────────────────────────────────────────


def format_result_text(result, filepath=None):
    """Format analysis result as human-readable text."""
    lines = []
    lines.append("=" * 60)
    lines.append("DC ANALYSIS REPORT")
    if filepath:
        lines.append(f"Design file: {filepath}")
    lines.append(f"Subcircuits analyzed: {result.subcircuits_analyzed}")
    if result.subcircuits_unanalyzed > 0:
        lines.append(f"Subcircuits unanalyzed: {result.subcircuits_unanalyzed}")
    lines.append("=" * 60)
    lines.append("")

    if not result.issues:
        lines.append("No subcircuits to analyze.")
        return "\n".join(lines)

    # Group by category
    categories = {}
    for issue in result.issues:
        categories.setdefault(issue.category, []).append(issue)

    for cat, cat_issues in categories.items():
        lines.append(f"── {cat.upper().replace('_', ' ')} {'─' * (40 - len(cat))}")
        for issue in cat_issues:
            marker = {"error": "✗", "warning": "⚠", "info": "✓"}.get(issue.severity, "?")
            lines.append(f"  {marker} {issue.message}")
        lines.append("")

    # Summary
    lines.append("─" * 60)
    n_err = len(result.errors)
    n_warn = len(result.warnings)
    n_info = len(result.infos)
    status = "PASS" if result.passed else "FAIL"
    lines.append(f"Result: {status}  |  {n_err} errors, {n_warn} warnings, "
                 f"{n_info} info")
    lines.append("─" * 60)

    return "\n".join(lines)


def format_result_json(result, filepath=None):
    """Format analysis result as JSON."""
    data = {
        "passed": result.passed,
        "subcircuits_analyzed": result.subcircuits_analyzed,
        "subcircuits_unanalyzed": result.subcircuits_unanalyzed,
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "issues": [
            {
                "severity": i.severity,
                "category": i.category,
                "message": i.message,
                "details": i.details,
            }
            for i in result.issues
        ],
    }
    if filepath:
        data["design_file"] = filepath
    return json_module.dumps(data, indent=2)


# ─── CLI entry point ─────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="DC Analysis Engine — validates electrical correctness "
                    "of circuit designs before schematic generation"
    )
    parser.add_argument("design_file", help="Path to design YAML file")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--strict", action="store_true",
                        help="Treat warnings as errors")
    args = parser.parse_args()

    if not os.path.isfile(args.design_file):
        print(f"Error: file not found: {args.design_file}", file=sys.stderr)
        sys.exit(2)

    try:
        design = load_design(args.design_file)
    except Exception as e:
        print(f"Error loading design file: {e}", file=sys.stderr)
        sys.exit(2)

    result = analyze(design, strict=args.strict)

    if args.json:
        print(format_result_json(result, args.design_file))
    else:
        print(format_result_text(result, args.design_file))

    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
