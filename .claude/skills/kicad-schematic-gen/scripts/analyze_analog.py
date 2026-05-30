#!/usr/bin/env python3
"""
Analog Front-End Completeness Analyzer — Stage 5 (schematic-level).

Validates that sensitive analog signal chains have the support circuitry their
datasheets and good practice require, operating on the *netlist YAML* (design
intent) — BEFORE it becomes wires. The whole point: a missing input filter cap
is a schematic defect, and you cannot add a part during PCB layout. Catch it here.

It works two ways, which converge on the same checks:

  1. Recipe-driven (analog_recipes.yaml): for a recognized part (e.g. NAU7802),
     the recipe knows which pins are differential inputs / supplies / references,
     so the checker catches a bare input even if the nets were never classified.

  2. Class-driven (net `class:` tags in the netlist): for any net classed
     analog / analog_differential / high_impedance, run the same front-end checks
     — works for parts with no recipe yet.

What it catches:
    - a differential ADC input wired straight from connector to pin with NO
      anti-alias / EMI filter (the canonical defect)
    - missing differential filter cap across a differential pair
    - common-mode caps unbalanced or Cdiff < ~10x Ccm (converts CM noise to error)
    - missing supply / reference / bandgap decoupling per recipe
    - a ratiometric-capable part whose reference is NOT tied to the excitation

See references/analog_layout.md for the reasoning behind every check.

CLI Usage:
    python analyze_analog.py <netlist.yaml>
    python analyze_analog.py <netlist.yaml> --json
    python analyze_analog.py <netlist.yaml> --strict     # warnings -> failures
    python analyze_analog.py <netlist.yaml> --recipes <path>

Python API:
    from analyze_analog import analyze_netlist_file, analyze_netlist
    result = analyze_netlist_file("design_05b_netlist.yaml")
    if not result.passed:
        for issue in result.issues:
            print(issue)
"""

import sys
import os
import json as json_module

# Sibling imports
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

# Reuse the DC analyzer's issue/result types, value parser, and formatters so
# output is consistent across the skill's analyzers.
from analyze_dc import AnalysisIssue, AnalysisResult, _parse_value
from verify_netlist import (
    IntendedNetlist, load_intended_netlist, load_intended_netlist_from_string,
)

try:
    import yaml as _yaml
except ImportError:
    _yaml = None


# ─── Recipe loading ────────────────────────────────────────────────────────

def _default_recipes_path():
    # analog_recipes.yaml lives at the skill root (one level above scripts/)
    return os.path.join(os.path.dirname(_script_dir), "analog_recipes.yaml")


def load_recipes(path=None):
    """Load analog_recipes.yaml -> dict of recipe-key -> recipe dict."""
    if _yaml is None:
        raise ImportError("PyYAML is required: pip install pyyaml")
    path = path or _default_recipes_path()
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = _yaml.safe_load(f) or {}
    return raw.get("recipes", {}) or {}


def match_recipe(part, recipes):
    """Find the recipe whose alias is a case-insensitive substring of `part`."""
    if not part:
        return None, None
    p = part.upper()
    for key, recipe in recipes.items():
        aliases = recipe.get("aliases", [key])
        for a in aliases:
            if a.upper() in p:
                return key, recipe
    return None, None


# ─── Netlist index helpers ──────────────────────────────────────────────────

class _Index:
    """Connectivity indexes built from an IntendedNetlist for fast lookups."""

    def __init__(self, netlist: IntendedNetlist):
        self.nl = netlist
        self.pin_to_net = {}        # (ref, pin) -> net_name
        self.net_pins = {}          # net_name -> set of (ref, pin)
        self.ref_part = {}          # ref -> part string
        self.ref_pincount = {}      # ref -> number of pins

        for ref, comp in netlist.components.items():
            self.ref_part[ref] = comp.part
            self.ref_pincount[ref] = len(comp.pins)

        for net_name, net in netlist.nets.items():
            s = set()
            for p in net.pins:
                self.pin_to_net[(p.ref, p.pin)] = net_name
                s.add((p.ref, p.pin))
            self.net_pins[net_name] = s

        # GND-ish nets (used for "cap to ground" detection)
        self.gnd_nets = set()
        for net_name, net in netlist.nets.items():
            nm = net_name.upper().strip("+-/")
            if nm in ("GND", "GROUND", "AGND", "DGND", "VSS", "AVSS", "DVSS", "0V"):
                self.gnd_nets.add(net_name)
            elif any(ps.upper().strip("+-/") in ("GND", "GNDA", "AGND", "DGND")
                     for ps in net.power_symbols):
                self.gnd_nets.add(net_name)

    def net_of(self, ref, pin):
        return self.pin_to_net.get((ref, pin))

    def is_cap(self, ref):
        part = (self.ref_part.get(ref) or "")
        return ref[:1].upper() == "C" or part.strip().upper().endswith("F")

    def is_res(self, ref):
        part = (self.ref_part.get(ref) or "")
        u = part.strip().upper()
        return ref[:1].upper() == "R" or u.endswith("R") or u.endswith("OHM") or u.endswith("Ω")

    def two_pin_other_pin_net(self, ref, in_net):
        """For a 2-pin component with a pin on `in_net`, return the net on its
        OTHER pin (or None). Used to see where a cap's far side goes."""
        comp = self.nl.components.get(ref)
        if not comp or len(comp.pins) != 2:
            return None
        on_net = [p for p in comp.pins if self.net_of(ref, p) == in_net]
        if not on_net:
            return None
        for p in comp.pins:
            if self.net_of(ref, p) != in_net:
                return self.net_of(ref, p)
        return in_net  # both pins on same net (unusual)

    def cap_across(self, net_a, net_b):
        """Return ref of a capacitor bridging net_a <-> net_b, else None."""
        for ref in self.nl.components:
            if not self.is_cap(ref):
                continue
            pins = self.nl.components[ref].pins
            if len(pins) != 2:
                continue
            nets = {self.net_of(ref, p) for p in pins}
            if net_a in nets and net_b in nets and net_a != net_b:
                return ref
        return None

    def cap_to_gnd(self, net):
        """Return ref of a capacitor from `net` to any GND net, else None."""
        for g in self.gnd_nets:
            r = self.cap_across(net, g)
            if r:
                return r
        return None

    def caps_to_gnd(self, net):
        """Return refs of ALL capacitors from `net` to a GND net."""
        out = []
        for ref in self.nl.components:
            if not self.is_cap(ref):
                continue
            pins = self.nl.components[ref].pins
            if len(pins) != 2:
                continue
            nets = {self.net_of(ref, p) for p in pins}
            if net in nets and (nets & self.gnd_nets):
                out.append(ref)
        return out

    def best_cap_to_gnd(self, net):
        """Return (ref, value_farads) of the LARGEST cap from `net` to GND.

        A rail is often decoupled by several caps in parallel (1uF bulk +
        100nF HF); the datasheet minimum is met by the bulk, so judge by the
        largest, not whichever happens to be found first."""
        best_ref, best_val = None, -1.0
        for ref in self.caps_to_gnd(net):
            v = self.cap_value_uf(ref)
            if v is None:
                if best_ref is None:
                    best_ref = ref  # unknown value, keep as fallback
                continue
            if v > best_val:
                best_ref, best_val = ref, v
        return best_ref, (best_val if best_val >= 0 else None)

    def series_r_on(self, net):
        """Return ref of a resistor with a pin on `net` (series-R evidence)."""
        for (ref, pin), n in self.pin_to_net.items():
            if n == net and self.is_res(ref):
                return ref
        return None

    def cap_value_uf(self, ref):
        """Best-effort capacitor value in farads, or None."""
        part = self.ref_part.get(ref)
        if not part:
            return None
        try:
            return _parse_value(part)
        except ValueError:
            return None


# ─── Core check: differential front-end ──────────────────────────────────────

def _check_diff_frontend(idx, pair_label, p_net, n_net, adc_ref=None,
                         frontend=None, source_z=None):
    """Check a differential analog pair has an input filter network.

    p_net / n_net are the net names on the + and - legs at the amplifier input.
    Returns a list of AnalysisIssue.
    """
    issues = []
    cat = "analog_frontend"
    fe = frontend or {}
    diff_required = (fe.get("differential_cap", {}) or {}).get("required", True)

    diff_cap = idx.cap_across(p_net, n_net) if (p_net and n_net) else None
    cm_p = idx.cap_to_gnd(p_net) if p_net else None
    cm_n = idx.cap_to_gnd(n_net) if n_net else None
    ser_p = idx.series_r_on(p_net) if p_net else None
    ser_n = idx.series_r_on(n_net) if n_net else None

    any_filter = any([diff_cap, cm_p, cm_n])
    details = {
        "pair": pair_label, "p_net": p_net, "n_net": n_net,
        "differential_cap": diff_cap, "cm_cap_p": cm_p, "cm_cap_n": cm_n,
        "series_r_p": ser_p, "series_r_n": ser_n,
    }

    # --- The headline check: bare input ---
    if not any_filter:
        sev = "error" if diff_required else "warning"
        issues.append(AnalysisIssue(
            sev, cat,
            f"{pair_label}: BARE differential input — no anti-alias/EMI filter "
            f"on nets {p_net}/{n_net}. The signal runs straight to the ADC pins. "
            f"Add a differential cap across the pair (and ideally matched "
            f"common-mode caps + small series R) at the inputs. "
            f"See analog_layout.md §3.1.",
            details,
        ))
        return issues  # nothing else to check on a bare pair

    # --- Differential cap present? ---
    if diff_cap:
        issues.append(AnalysisIssue(
            "info", cat,
            f"{pair_label}: differential filter cap {diff_cap} across "
            f"{p_net}/{n_net} — OK", details))
    elif diff_required:
        issues.append(AnalysisIssue(
            "warning", cat,
            f"{pair_label}: no differential cap across {p_net}/{n_net} "
            f"(common-mode caps present). A differential cap sets the signal "
            f"bandwidth and is the primary anti-alias element — add one.",
            details))

    # --- Common-mode caps balance ---
    if (cm_p and not cm_n) or (cm_n and not cm_p):
        issues.append(AnalysisIssue(
            "warning", cat,
            f"{pair_label}: common-mode cap present on only one leg "
            f"({'P' if cm_p else 'N'}). Unmatched CM caps convert common-mode "
            f"noise into differential error — use a matched pair or none.",
            details))

    # --- Cdiff >= ~10x Ccm (mismatch tolerance) ---
    if diff_cap and (cm_p or cm_n):
        cdiff = idx.cap_value_uf(diff_cap)
        ccm = idx.cap_value_uf(cm_p or cm_n)
        if cdiff and ccm and ccm > 0:
            ratio = cdiff / ccm
            details["cdiff_ccm_ratio"] = round(ratio, 2)
            if ratio < 10:
                issues.append(AnalysisIssue(
                    "warning", cat,
                    f"{pair_label}: Cdiff/Ccm = {ratio:.1f} (< 10). Keep the "
                    f"differential cap >= ~10x the common-mode caps so CM-cap "
                    f"mismatch can't unbalance the pair. See analog_layout.md §3.1.",
                    details))

    return issues


# ─── Recipe-driven analysis of one component ─────────────────────────────────

def _analyze_recipe_component(idx, ref, recipe_key, recipe):
    issues = []
    cat = "analog_frontend"

    # Differential inputs
    for di in recipe.get("differential_inputs", []):
        p_pin, n_pin = str(di.get("p")), str(di.get("n"))
        name = di.get("name", f"{ref} {p_pin}/{n_pin}")
        p_net = idx.net_of(ref, p_pin)
        n_net = idx.net_of(ref, n_pin)
        # Skip channels that are unused (pins not in any net = NC, or tied to GND)
        if p_net is None and n_net is None:
            issues.append(AnalysisIssue(
                "info", cat, f"{ref} {name}: input pins not netted (unused channel) — skipped"))
            continue
        if p_net in idx.gnd_nets and n_net in idx.gnd_nets:
            issues.append(AnalysisIssue(
                "info", cat, f"{ref} {name}: both legs tied to GND (unused channel) — skipped"))
            continue
        issues.extend(_check_diff_frontend(
            idx, f"{ref} {name}", p_net, n_net, adc_ref=ref,
            frontend=recipe.get("input_frontend"),
            source_z=recipe.get("source_z_default"),
        ))

    # Supply / reference / bandgap decoupling
    for dec in recipe.get("decoupling", []):
        pin = str(dec.get("pin"))
        role = dec.get("role", f"pin {pin}")
        min_uf = dec.get("min_uf")
        net = idx.net_of(ref, pin)
        if net is None:
            issues.append(AnalysisIssue(
                "warning", cat,
                f"{ref}: {role} (pin {pin}) is not connected to any net — "
                f"verify it should be."))
            continue
        cap, val = idx.best_cap_to_gnd(net)
        if not cap:
            issues.append(AnalysisIssue(
                "warning", cat,
                f"{ref}: {role} (pin {pin}, net {net}) has no decoupling cap to "
                f"GND. Datasheet expects >= {min_uf}uF. Add one close to the pin."))
        elif min_uf and val is not None and val < float(min_uf) * 1e-6 * 0.999:
            issues.append(AnalysisIssue(
                "warning", cat,
                f"{ref}: {role} largest decoupling cap {cap} ({idx.ref_part.get(cap)}) is "
                f"below datasheet minimum {min_uf}uF."))
        else:
            issues.append(AnalysisIssue(
                "info", cat,
                f"{ref}: {role} (pin {pin}) decoupled by {cap} — OK"))

    # Ratiometric reference
    rat = recipe.get("ratiometric") or {}
    if rat.get("supported"):
        rh = str(rat.get("ref_high_pin")) if rat.get("ref_high_pin") else None
        ref_net = idx.net_of(ref, rh) if rh else None
        if ref_net is None:
            issues.append(AnalysisIssue(
                "warning", cat,
                f"{ref}: ratiometric part but REF+ (pin {rh}) not connected — "
                f"a ratiometric ADC should reference the bridge excitation."))
        else:
            # Is the reference-high net shared with a connector (excitation)?
            shares_connector = any(
                r[0] != ref and (r[0][:1].upper() in ("J", "P") or "CONN" in (idx.ref_part.get(r[0]) or "").upper()
                                 or "CELL" in (idx.ref_part.get(r[0]) or "").upper())
                for r in idx.net_pins.get(ref_net, set())
            )
            if shares_connector:
                issues.append(AnalysisIssue(
                    "info", cat,
                    f"{ref}: ratiometric reference OK — REF+ net {ref_net} is "
                    f"shared with the excitation source."))
            else:
                issues.append(AnalysisIssue(
                    "warning", cat,
                    f"{ref}: REF+ net {ref_net} does not appear to be tied to the "
                    f"bridge excitation. Ratiometric operation cancels excitation "
                    f"noise only if REF is derived from the excitation. "
                    f"See analog_layout.md §3.3."))

    return issues


# ─── Class-driven analysis (no recipe needed) ────────────────────────────────

def _analyze_classified_nets(idx, covered_pairs):
    """Run front-end checks on nets the designer explicitly classified, for
    parts without a recipe. `covered_pairs` is a set of (p_net, n_net) already
    handled by a recipe, to avoid double-reporting."""
    issues = []
    cat = "analog_frontend"

    # Group analog_differential nets by pair name.
    pairs = {}  # pair_name -> {"P": net, "N": net, "source_z": ...}
    singles = []  # (net_name, net) for class analog / high_impedance

    for net_name, net in idx.nl.nets.items():
        cls = (net.net_class or "").lower()
        if cls == "analog_differential" and net.pair:
            slot = pairs.setdefault(net.pair, {"source_z": net.source_z})
            slot[(net.polarity or "").upper() or "?"] = net_name
        elif cls in ("analog", "high_impedance"):
            singles.append((net_name, net))

    for pair_name, slot in pairs.items():
        p_net, n_net = slot.get("P"), slot.get("N")
        if not p_net or not n_net:
            issues.append(AnalysisIssue(
                "warning", cat,
                f"differential pair '{pair_name}' is missing a P or N leg in the "
                f"netlist (P={p_net}, N={n_net})."))
            continue
        if (p_net, n_net) in covered_pairs or (n_net, p_net) in covered_pairs:
            continue
        issues.extend(_check_diff_frontend(
            idx, f"pair {pair_name}", p_net, n_net, source_z=slot.get("source_z")))

    # Single-ended sensitive nets: want a filter cap to GND somewhere.
    for net_name, net in singles:
        if idx.cap_to_gnd(net_name):
            issues.append(AnalysisIssue(
                "info", cat, f"{net_name} (class {net.net_class}): filter cap to GND present — OK"))
        else:
            issues.append(AnalysisIssue(
                "warning", cat,
                f"{net_name} (class {net.net_class}): no filter cap to GND. A "
                f"sensitive single-ended input usually wants an RC low-pass at "
                f"the pin. See analog_layout.md §3.1."))

    return issues


# ─── Main analysis runner ────────────────────────────────────────────────────

def analyze_netlist(netlist: IntendedNetlist, recipes=None, strict=False):
    """Run analog front-end checks on an IntendedNetlist.

    Returns an AnalysisResult (reused from analyze_dc for consistency).
    """
    if recipes is None:
        recipes = load_recipes()

    idx = _Index(netlist)
    all_issues = []
    analyzed = 0
    covered_pairs = set()

    # Recipe-driven, per matching component
    for ref, comp in netlist.components.items():
        key, recipe = match_recipe(comp.part, recipes)
        if not recipe:
            continue
        analyzed += 1
        all_issues.append(AnalysisIssue(
            "info", "analog_frontend",
            f"{ref}: matched recipe '{key}' ({recipe.get('family', '')}) — "
            f"sensor source impedance: {recipe.get('source_z_default', 'unknown')}"))
        for di in recipe.get("differential_inputs", []):
            pn = idx.net_of(ref, str(di.get("p")))
            nn = idx.net_of(ref, str(di.get("n")))
            if pn and nn:
                covered_pairs.add((pn, nn))
        all_issues.extend(_analyze_recipe_component(idx, ref, key, recipe))

    # Class-driven, for anything not covered by a recipe
    class_issues = _analyze_classified_nets(idx, covered_pairs)
    analyzed += sum(1 for i in class_issues if i.severity != "info" or "OK" in i.message)
    all_issues.extend(class_issues)

    if analyzed == 0 and not all_issues:
        all_issues.append(AnalysisIssue(
            "info", "analog_frontend",
            "No recognized analog parts and no analog-classified nets — nothing "
            "to check. (Tag sensitive nets with `class:` or add a recipe.)"))

    has_errors = any(i.severity == "error" for i in all_issues)
    has_warnings = any(i.severity == "warning" for i in all_issues)
    passed = not has_errors and (not strict or not has_warnings)

    return AnalysisResult(
        passed=passed,
        issues=all_issues,
        subcircuits_analyzed=analyzed,
        subcircuits_unanalyzed=0,
    )


def analyze_netlist_file(filepath, recipes_path=None, strict=False):
    netlist = load_intended_netlist(filepath)
    recipes = load_recipes(recipes_path)
    return analyze_netlist(netlist, recipes=recipes, strict=strict)


def analyze_netlist_from_string(text, recipes_path=None, strict=False):
    netlist = load_intended_netlist_from_string(text)
    recipes = load_recipes(recipes_path)
    return analyze_netlist(netlist, recipes=recipes, strict=strict)


# ─── Output formatters ──────────────────────────────────────────────────────

def format_result_text(result, filepath=None):
    lines = []
    lines.append("=" * 60)
    lines.append("ANALOG FRONT-END ANALYSIS")
    if filepath:
        lines.append(f"Netlist: {filepath}")
    lines.append(f"Analog blocks analyzed: {result.subcircuits_analyzed}")
    lines.append("=" * 60)
    lines.append("")
    for issue in result.issues:
        marker = {"error": "✗", "warning": "⚠", "info": "✓"}.get(issue.severity, "?")
        lines.append(f"  {marker} {issue.message}")
    lines.append("")
    lines.append("─" * 60)
    status = "PASS" if result.passed else "FAIL"
    lines.append(f"Result: {status}  |  {len(result.errors)} errors, "
                 f"{len(result.warnings)} warnings, {len(result.infos)} info")
    lines.append("─" * 60)
    return "\n".join(lines)


def format_result_json(result, filepath=None):
    data = {
        "passed": result.passed,
        "analog_blocks_analyzed": result.subcircuits_analyzed,
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "issues": [
            {"severity": i.severity, "category": i.category,
             "message": i.message, "details": i.details}
            for i in result.issues
        ],
    }
    if filepath:
        data["netlist_file"] = filepath
    return json_module.dumps(data, indent=2)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    import argparse
    # Windows consoles default to cp1252 and choke on the ✓/✗/⚠ markers.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(
        description="Analog front-end completeness analyzer (Stage 5). "
                    "Checks sensitive analog chains have anti-alias/EMI filtering, "
                    "decoupling, and ratiometric references before generation.")
    parser.add_argument("netlist_file", help="Path to the Stage 5b netlist YAML")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as failures")
    parser.add_argument("--recipes", default=None, help="Path to analog_recipes.yaml")
    args = parser.parse_args()

    if not os.path.isfile(args.netlist_file):
        print(f"Error: file not found: {args.netlist_file}", file=sys.stderr)
        sys.exit(2)

    try:
        result = analyze_netlist_file(args.netlist_file, recipes_path=args.recipes,
                                      strict=args.strict)
    except Exception as e:
        print(f"Error analyzing netlist: {e}", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(format_result_json(result, args.netlist_file))
    else:
        print(format_result_text(result, args.netlist_file))

    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
