#!/usr/bin/env python3
"""
PCBway Assembly Checker - scores a BOM for PCBway turnkey-assembly readiness.

Boards from this skill are ordered as *assembled* boards from PCBway, not
populated in-house. PCBway sources every part for you from authorized
distributors (LCSC, DigiKey, Mouser, Arrow, ...) and assembles the board.

Unlike JLCPCB, PCBway has NO public parts-catalog API, so there is no
"is it in the library" lookup to run. What actually controls whether a BOM
line is buildable is two things:

  1. Is the package assembly-friendly?  (deterministic - this script checks it)
  2. Is the exact part in stock at a distributor PCBway sources from?
     (live data - the skill confirms this with web search, see SKILL.md Stage 3)

This script handles (1) and the bookkeeping for (2): it parses a Stage 3 BOM
markdown table, applies the PCBway-compatibility rubric to every line, and
emits a "sourcing sheet" - the BOM augmented with assembly ratings and the
distributor PN columns you paste into PCBway's quote form. Lines missing the
data PCBway needs to source them are flagged.

It performs NO network access, so it can never silently break. Treat a clean
run as "the BOM is well-formed and assembly-friendly", then confirm live stock
per the SKILL.md web-search step before ordering.

CLI Usage:
    python check_pcbway.py <bom.md>
    python check_pcbway.py <bom.md> --json
    python check_pcbway.py <bom.md> --sourcing-sheet        # emit markdown sheet
    python check_pcbway.py <bom.md> --sourcing-sheet -o sheet.md
    python check_pcbway.py <bom.md> --strict                # cautions also fail

Python API:
    from check_pcbway import check_bom, load_bom_for_pcbway, classify_package

    parts = load_bom_for_pcbway(md_text)
    result = check_bom(parts)
    print(result.passed, result.errors)
"""

import sys
import os
import re
import json as json_module
from dataclasses import dataclass, field


# ─── PCBWay KiCad-plugin field-name contract ─────────────────────────
#
# The official PCBWay plugin (pcbway/PCBWay-Plug-in-for-Kicad) resolves the
# manufacturer part number by scanning symbol/footprint fields for the FIRST key
# that EXISTS (utils.py::get_mpn_keys), in this priority order. First *present*
# key wins even if empty — an empty `mpn` field shadows a populated `MPN` — so a
# baked schematic must carry exactly ONE mpn-family key, non-empty. `LCSC Part #`
# is deliberately NOT in this list (it feeds the JLCPCB toolkit, not PCBWay).
PLUGIN_MPN_KEYS = [
    "mpn", "MPN", "Mpn", "PCBWay_MPN", "part number", "Part Number",
    "Part No.", "Mfr. Part No.", "Mfg Part", "Manufacturer_Part_Number",
]
# Package column keys (plugin get_pack_keys), + upper variants.
PLUGIN_PACK_KEYS = ["pack", "package", "Package", "case", "Case"]

# The canonical keys the generator emits.
CANONICAL_MPN_FIELD = "MPN"
CANONICAL_PACKAGE_FIELD = "Package"
# Trap: the plugin's key is `Mfg Part` (no #); `Mfg Part #` is only the upload-xlsx
# column header. Using it as a symbol field name silently populates nothing.
FORBIDDEN_MPN_FIELD = "Mfg Part #"

_MPN_KEYS_LOWER = {k.strip().lower() for k in PLUGIN_MPN_KEYS}


def is_mpn_family_key(field_name):
    """True if a symbol field name is one the PCBWay plugin reads as the MPN."""
    return (field_name or "").strip().lower() in _MPN_KEYS_LOWER


# ─── Package classification rubric ───────────────────────────────────
#
# Each rule: (regex tested against the footprint string, rating, note).
# Rules are tried in order; the FIRST match wins, so list the most specific /
# most restrictive packages first. Ratings:
#   "block"   - PCBway can't (or won't, by default) assemble this. Fix it.
#   "caution" - assemblable, but adds cost, risk, or hand-work. Confirm intent.
#   "ok"      - standard SMT, no concern.
#
# The footprint string is the full KiCad ID, e.g.
# "Package_TO_SOT_SMD:SOT-23-5" or "Resistor_SMD:R_0805_2012Metric".

_PACKAGE_RULES = [
    # ── Passives that are too small to assemble reliably ──
    (r"_01005", "block",
     "01005 passive - below PCBway's standard assembly capability; use 0402 or larger"),
    (r"_0201", "caution",
     "0201 passive - assemblable but low yield / extra cost; prefer 0402+ unless space-critical"),

    # ── Fine-pitch / X-ray packages ──
    (r"BGA|_BGA|CSP|WLCSP|FBGA|LFBGA", "caution",
     "BGA/CSP - PCBway can assemble but it needs X-ray inspection and raises cost; confirm pitch >= 0.4mm"),

    # ── Bare die / chip-on-board ──
    (r"DIE|COB|Die_", "block",
     "bare die / chip-on-board - not a standard PCBway turnkey package"),

    # ── Through-hole (THT) ──
    (r"_THT|PinHeader|PinSocket|TO-220|TO-247|TO-92|DIP-|_DIP|_Horizontal|_Vertical|TerminalBlock|Pin_Header|Socket",
     "caution",
     "through-hole / connector - PCBway charges per-pin for THT hand/wave soldering; confirm it's intended"),

    # ── Standard, comfortable SMT packages ──
    (r"_0402|_0603|_0805|_1206|_1210|_2010|_2512", "ok", ""),
    (r"SOT-?23|SOT-?89|SOT-?223|SOT-?353|SOT-?363|SOT-?5|SOT-?6", "ok", ""),
    (r"SOIC|SOP|SO-8|TSSOP|MSOP|SSOP|VSSOP|TSOP|HTSSOP", "ok", ""),
    (r"QFP|LQFP|TQFP|PQFP", "ok", ""),
    (r"QFN|DFN|VQFN|WQFN|UQFN|TDFN|UDFN|VDFN", "ok",
     ""),
    (r"SOD-?123|SOD-?323|SOD-?523|SMA|SMB|SMC|DO-?214|DO-?219|MELF", "ok", ""),
    (r"LGA", "caution",
     "LGA - assemblable; confirm PCBway can place this specific sensor package"),
    (r"Crystal_SMD|Resonator_SMD|Oscillator", "ok", ""),
    (r"LED_SMD|LED_0|Inductor_SMD|Capacitor_SMD|Resistor_SMD|L_0|C_0|R_0", "ok", ""),
]


def classify_package(footprint):
    """Classify a footprint for PCBway assembly friendliness.

    Args:
        footprint: Full KiCad footprint ID (e.g. "Package_TO_SOT_SMD:SOT-23-5").

    Returns:
        Dict with: rating ("ok"|"caution"|"block"|"unknown"), note (str).
        Returns "unknown" when the footprint doesn't match any known package -
        that's a caution, because an unrecognized package should be reviewed.
    """
    if not footprint:
        return {"rating": "block", "note": "no footprint assigned - PCBway cannot place an unfootprinted part"}

    for pattern, rating, note in _PACKAGE_RULES:
        if re.search(pattern, footprint, re.IGNORECASE):
            return {"rating": rating, "note": note}

    return {"rating": "unknown",
            "note": "package not recognized by the rubric - review manually for PCBway assembly fit"}


# ─── Reference-designator → component class ──────────────────────────

# Designator prefixes that are generic passives PCBway can substitute by
# value+package (an exact distributor PN is nice-to-have, not required).
_PASSIVE_PREFIXES = ("R", "C", "L", "FB", "FL")

# Prefixes that need an exact manufacturer/distributor PN to source.
# (Everything not a generic passive: ICs, transistors, diodes, sensors,
#  connectors, crystals, modules, etc.)


def _ref_prefix(reference):
    m = re.match(r"^([A-Za-z]+)", reference.strip())
    return m.group(1).upper() if m else ""


def is_generic_passive(reference, footprint):
    """True if this line is a generic passive PCBway can source by value+package."""
    prefix = _ref_prefix(reference)
    if prefix in _PASSIVE_PREFIXES:
        # A resistor/cap/inductor footprint confirms it; but the designator alone
        # is enough (D is a diode, not generic, and isn't in the passive list).
        return True
    return False


# ─── Notes-field keyword flags ───────────────────────────────────────

_NOTES_FLAGS = [
    (r"\bobsolete\b|\bEOL\b|\bend.of.life\b", "caution",
     "marked obsolete/EOL - PCBway may not be able to source it; pick an active replacement"),
    (r"\bNRND\b|not recommended", "caution",
     "marked NRND - pick an active replacement before ordering"),
    (r"single.source|sole.source", "caution",
     "single-source part - supply risk; consider a second-source alternative"),
    (r"\bMSL[ -]?[3-6]\b|moisture", "caution",
     "moisture-sensitive (MSL 3+) - fine for PCBway but note it; affects shelf life / baking"),
    (r"long.lead|lead.time", "caution",
     "long lead time noted - confirm PCBway can get it within your schedule"),
]


def _scan_notes(notes):
    """Return list of (severity-mapped rating, message) for keyword flags in Notes."""
    hits = []
    if not notes:
        return hits
    for pattern, rating, msg in _NOTES_FLAGS:
        if re.search(pattern, notes, re.IGNORECASE):
            hits.append((rating, msg))
    return hits


# ─── Structural line checks (the defect classes that reached PCBway) ──
#
# These are the deterministic, offline catches for the bugs found in the field:
#   - an LCSC code sitting in the Mfg Part # column (PCBway sourced the wrong part)
#   - a description ("56k 5% 0805") where a real MPN belongs
#   - the package field disagreeing with the footprint's size token
#   - a missing Manufacturer (PCBway's form requires one per line)
# They do NOT replace the live web check (a *well-formed* code can still resolve
# to the wrong part — that needs the answer-blind verifier); they're the cheap
# first net.

_IMPERIAL_SIZES = ("01005", "0201", "0402", "0603", "0805",
                   "1206", "1210", "1812", "2010", "2512")


def looks_like_distributor_code(s):
    """True for a clear distributor catalog code (LCSC 'C#####') in the MPN slot.

    Kept narrow on purpose — only the unambiguous LCSC 'C' + digits form — so it
    never false-flags a real MPN that merely starts with a letter+digits.
    """
    return bool(re.match(r"^C\d{3,}$", (s or "").strip()))


def looks_like_description(s):
    """A real MPN has no interior spaces; a description ('56k 5% 0805') does."""
    s = (s or "").strip()
    return bool(s) and " " in s


def footprint_size_token(footprint):
    """Imperial size code (e.g. '0805') from a passive footprint, else None."""
    m = re.search(r"_(\d{4,5})_\d{3,4}Metric", footprint or "")
    return m.group(1) if m else None


def package_size_token(package):
    """The package field if it's a bare imperial size code, else None."""
    pkg = (package or "").strip()
    return pkg if pkg in _IMPERIAL_SIZES else None


def verification_worklist(parts, include_passives=False):
    """Lines that warrant a live (answer-blind) web check.

    Policy (see SKILL.md Stage 9): non-passives always, plus any line the
    structural gate flagged. Generic passives with a clean MPN are skipped unless
    include_passives=True. Run check_bom(parts) first so .flags are populated.
    """
    work = []
    for p in parts:
        passive = is_generic_passive(p.reference, p.footprint)
        if (not passive) or p.flags or include_passives:
            work.append(p)
    return work


# ─── Data model ───────────────────────────────────────────────────────

@dataclass
class PcbwayPart:
    reference: str
    value: str = ""
    manufacturer: str = ""     # part maker (Murata, YAGEO, ...) — PCBway form requires it
    description: str = ""      # human-readable spec (e.g. "CAP CER 47uF 25V X5R 1210")
    part_number: str = ""      # manufacturer part number (MPN)
    package: str = ""
    footprint: str = ""
    supplier: str = ""
    supplier_pn: str = ""      # distributor PN (LCSC/DigiKey/Mouser)
    notes: str = ""
    # Filled in by check_bom():
    rating: str = "ok"         # worst rating across all checks for this part
    flags: list = field(default_factory=list)  # list of human-readable flag strings


@dataclass
class PcbwayIssue:
    severity: str   # "error" (block), "warning" (caution)
    check_name: str
    message: str
    reference: str = ""


@dataclass
class PcbwayResult:
    passed: bool
    issues: list = field(default_factory=list)
    parts: list = field(default_factory=list)   # list of PcbwayPart with ratings

    @property
    def errors(self):
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self):
        return [i for i in self.issues if i.severity == "warning"]


# ─── BOM markdown parser ─────────────────────────────────────────────

# Maps a normalized header cell to the PcbwayPart field it fills.
_COLUMN_ALIASES = {
    "ref": "reference", "reference": "reference",
    "value": "value",
    "manufacturer": "manufacturer", "mfr": "manufacturer", "mfg": "manufacturer", "maker": "manufacturer",
    "part number": "part_number", "part": "part_number", "mpn": "part_number",
    "description": "description", "description / value": "description", "desc": "description",
    "manufacturer part number": "part_number", "part #": "part_number",
    "package": "package", "pkg": "package",
    "supplier": "supplier",
    "supplier pn": "supplier_pn", "supplier #": "supplier_pn",
    "distributor pn": "supplier_pn", "lcsc": "supplier_pn", "lcsc pn": "supplier_pn",
    "notes": "notes", "note": "notes",
    "dnp": "dnp", "dns": "dnp",
}

# Markers (in a DNP column, or free-form in Notes/Value) that mean the part is not
# fitted at assembly. Kept narrow so an ordinary note isn't misread as DNP.
_DNP_RE = re.compile(r"\b(dnp|dns|do[\s-]*not[\s-]*(populate|fit|place|install)|no[\s-]*stuff|unpopulated)\b",
                     re.IGNORECASE)


def _truthy_dnp(cell):
    return bool(cell) and cell.strip().lower() in ("yes", "y", "true", "1", "dnp", "dns", "x")


def bom_dnp(record):
    """Whether a parsed BOM record marks a Do-Not-Populate / Do-Not-Stuff part.

    Single source for the DNP signal so the schematic `(dnp yes)` attribute and the
    Stage-9 upload sheet agree: a dedicated DNP/DNS column, a DNP marker in Notes,
    or Value == 'DNP'.
    """
    if _truthy_dnp(record.get("dnp", "")):
        return True
    if (record.get("value", "") or "").strip().upper() in ("DNP", "DNS"):
        return True
    if _DNP_RE.search(record.get("notes", "") or ""):
        return True
    return False


def _norm_header(cell):
    c = cell.lower().strip()
    if "footprint" in c:
        return "footprint"
    if "symbol" in c or "lib_id" in c:
        return None  # not needed for sourcing
    return _COLUMN_ALIASES.get(c)


def _split_row(line):
    """Split a markdown table row into cells, preserving interior empties.

    Only the leading/trailing empty cells produced by the outer pipes are
    dropped - interior blanks are kept so column indices stay aligned.
    """
    cells = [c.strip() for c in line.split("|")]
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return cells


def parse_bom_records(md_text):
    """Parse a Stage 3 BOM markdown table into raw field-name→value record dicts.

    This is the single column-parsing routine shared by every BOM consumer
    (PCBway checks, the xlsx generator, and the schematic generator via
    cross_check_bom) so they can never drift on how columns are read. Placeholder
    ({...}) cells are normalized to "" and template/separator rows are skipped.
    Each record always has a non-empty "reference".

    Returns list[dict]. Unknown columns are ignored.
    """
    records = []
    lines = md_text.strip().split("\n")

    header_idx = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("|") and "Ref" in s and "Value" in s:
            header_idx = i
            break
    if header_idx is None:
        return records

    cols = _split_row(lines[header_idx])

    idx_to_field = {}
    for idx, col in enumerate(cols):
        field_name = _norm_header(col)
        if field_name:
            idx_to_field[idx] = field_name

    if "reference" not in idx_to_field.values():
        return records

    for i in range(header_idx + 2, len(lines)):
        line = lines[i].strip()
        if not line.startswith("|"):
            break
        cells = _split_row(line)
        if not cells:
            continue

        raw = {}
        for idx, field_name in idx_to_field.items():
            if idx < len(cells):
                raw[field_name] = cells[idx].strip()

        ref = raw.get("reference", "")
        raw_value = raw.get("value", "")
        # Skip placeholder / separator rows: a stub row has a real designator but
        # {value}-style template fields, or the row is a markdown separator. Test the
        # RAW cells before {…} placeholders are normalized away.
        if not ref or ref.startswith("{") or raw_value.startswith("{") \
                or set(ref) <= set("-: "):
            continue

        record = {k: ("" if v.startswith("{") else v) for k, v in raw.items()}
        records.append(record)

    return records


def load_bom_for_pcbway(md_text):
    """Parse a Stage 3 BOM markdown table into PcbwayPart records.

    Captures every column relevant to PCBway sourcing (ref, value, part number,
    package, footprint, supplier, supplier PN, notes). Unknown columns are
    ignored. Template/placeholder rows ({...}) are skipped.

    Returns list of PcbwayPart.
    """
    parts = []
    for record in parse_bom_records(md_text):
        parts.append(PcbwayPart(
            reference=record.get("reference", ""),
            value=record.get("value", ""),
            manufacturer=record.get("manufacturer", ""),
            description=record.get("description", ""),
            part_number=record.get("part_number", ""),
            package=record.get("package", ""),
            footprint=record.get("footprint", ""),
            supplier=record.get("supplier", ""),
            supplier_pn=record.get("supplier_pn", ""),
            notes=record.get("notes", ""),
        ))
    return parts


# ─── Rubric engine ────────────────────────────────────────────────────

_RATING_RANK = {"ok": 0, "unknown": 1, "caution": 1, "block": 2}
_RATING_SEVERITY = {"block": "error", "caution": "warning", "unknown": "warning"}


def _bump(current, candidate):
    """Return the worse of two ratings."""
    return candidate if _RATING_RANK[candidate] > _RATING_RANK[current] else current


def check_bom(parts):
    """Apply the PCBway-compatibility rubric to a list of PcbwayPart.

    Checks (deterministic, offline):
      - package assemblability (classify_package)
      - missing footprint (block)
      - missing distributor PN where PCBway needs an exact part (non-passives)
      - Notes-field flags (obsolete/EOL/NRND/MSL/single-source/long-lead)

    Live distributor stock is NOT checked here - confirm that via web search
    per SKILL.md Stage 3 before ordering.

    Returns PcbwayResult. result.passed is False if any "block" (error) exists.
    """
    issues = []

    for part in parts:
        ref = part.reference
        part.rating = "ok"
        part.flags = []

        # ── Package assemblability ──
        pkg = classify_package(part.footprint)
        if pkg["rating"] != "ok":
            part.rating = _bump(part.rating, pkg["rating"])
            part.flags.append(pkg["note"])
            issues.append(PcbwayIssue(
                severity=_RATING_SEVERITY.get(pkg["rating"], "warning"),
                check_name="package",
                message=f"{ref} [{part.footprint or 'no footprint'}]: {pkg['note']}",
                reference=ref,
            ))

        # ── Sourcing data PCBway needs ──
        has_pn = bool(part.supplier_pn or part.part_number)
        if not has_pn:
            if is_generic_passive(ref, part.footprint):
                msg = (f"{ref} ({part.value}): generic passive with no distributor PN - "
                       f"PCBway can substitute an equivalent, but specifying an LCSC PN "
                       f"avoids tolerance/temco surprises")
                part.rating = _bump(part.rating, "caution")
                part.flags.append("no distributor PN (passive - substitutable)")
                issues.append(PcbwayIssue(
                    severity="warning", check_name="sourcing",
                    message=msg, reference=ref,
                ))
            else:
                msg = (f"{ref} ({part.value}): no MPN or distributor PN - PCBway needs an "
                       f"exact part number to source this; add a manufacturer + LCSC/DigiKey PN")
                part.rating = _bump(part.rating, "block")
                part.flags.append("no part number (cannot be sourced)")
                issues.append(PcbwayIssue(
                    severity="error", check_name="sourcing",
                    message=msg, reference=ref,
                ))

        # ── Notes keyword flags ──
        for rating, msg in _scan_notes(part.notes):
            part.rating = _bump(part.rating, rating)
            part.flags.append(msg)
            issues.append(PcbwayIssue(
                severity=_RATING_SEVERITY.get(rating, "warning"),
                check_name="notes_flag",
                message=f"{ref}: {msg}",
                reference=ref,
            ))

        # ── Mfg Part # hygiene — PCBway sources by the manufacturer part number ──
        mpn = part.part_number.strip()
        if looks_like_distributor_code(mpn):
            part.rating = _bump(part.rating, "block")
            part.flags.append("distributor code in Mfg Part # column")
            issues.append(PcbwayIssue(
                severity="error", check_name="distributor_code_as_mpn",
                message=(f"{ref}: Mfg Part # '{mpn}' is a distributor catalog code (LCSC), "
                         f"not a manufacturer part number. PCBway will source the wrong part — "
                         f"put the real MPN here and the LCSC code in the Notes/supplier column."),
                reference=ref))
        elif mpn and looks_like_description(mpn):
            part.rating = _bump(part.rating, "caution")
            part.flags.append("Mfg Part # looks like a description, not an MPN")
            issues.append(PcbwayIssue(
                severity="warning", check_name="mpn_not_real",
                message=(f"{ref}: Mfg Part # '{mpn}' looks like a description, not a manufacturer "
                         f"part number — PCBway wants an exact MPN for every line (passives included)."),
                reference=ref))

        # ── package field ↔ footprint size token (passive internal drift) ──
        fps, pks = footprint_size_token(part.footprint), package_size_token(part.package)
        if fps and pks and fps != pks:
            part.rating = _bump(part.rating, "block")
            part.flags.append(f"package {part.package} != footprint size {fps}")
            issues.append(PcbwayIssue(
                severity="error", check_name="package_mismatch",
                message=(f"{ref}: package field '{part.package}' disagrees with the footprint "
                         f"size '{fps}' ({part.footprint}). Make them agree."),
                reference=ref))

        # ── Manufacturer presence (PCBway form requires it) ──
        if not part.manufacturer.strip():
            part.rating = _bump(part.rating, "caution")
            part.flags.append("no Manufacturer")
            issues.append(PcbwayIssue(
                severity="warning", check_name="missing_manufacturer",
                message=f"{ref} ({part.value}): no Manufacturer — PCBway's BOM form requires one per line.",
                reference=ref))

    has_errors = any(i.severity == "error" for i in issues)
    return PcbwayResult(passed=not has_errors, issues=issues, parts=parts)


def check_bom_file(bom_path):
    with open(bom_path, "r", encoding="utf-8") as f:
        text = f.read()
    return check_bom(load_bom_for_pcbway(text))


# ─── [CRITICAL] schematic-MPN gate (the "clean schematic, empty BOM" guard) ──
#
# A board can pass every connectivity check yet still yield an empty PCBWay BOM if
# the identity fields aren't baked into the symbols under the exact key the plugin
# reads. This gate asserts, for every fitted non-passive symbol, that exactly one
# non-empty, real MPN sits under a plugin-recognized key — catching the missing /
# empty-shadow / `Mfg Part #`-trap / distributor-code-as-MPN failures before fab.

_CRIT_CHECK = "critical_schematic_mpn_present"


def _mpn_verdict(mpn_props, forbidden_present):
    """Judge a symbol's mpn-family fields. Returns (ok, reason). ``mpn_props`` is a
    list of (key, value); ``forbidden_present`` is True if the `Mfg Part #` trap key
    is used. Encodes the plugin's field-name contract."""
    if forbidden_present:
        return False, (f"uses forbidden field name '{FORBIDDEN_MPN_FIELD}' — the plugin's "
                       f"key is 'Mfg Part' (no #); use '{CANONICAL_MPN_FIELD}'")
    if not mpn_props:
        return False, f"no manufacturer part number field (expected '{CANONICAL_MPN_FIELD}')"
    if len(mpn_props) > 1:
        keys = ", ".join(sorted(k for k, _ in mpn_props))
        return False, (f"multiple mpn-family fields ({keys}); the plugin takes the first "
                       f"present even if empty — emit exactly one")
    key, val = mpn_props[0]
    val = (val or "").strip()
    if not val:
        return False, f"'{key}' is present but empty — it shadows any real MPN to a blank"
    if looks_like_distributor_code(val):
        return False, f"'{key}' = '{val}' is a distributor catalog code, not a manufacturer PN"
    if looks_like_description(val):
        return False, f"'{key}' = '{val}' looks like a description, not a manufacturer PN"
    return True, ""


def check_schematic_mpns(sch):
    """[CRITICAL] gate on the MPN fields baked into a generated schematic.

    ``sch`` is a KicadSchematic (from validate_kicad_sch.load_kicad_sch). Returns a
    PcbwayResult; result.passed is False if any fitted symbol lacks a single clean,
    plugin-readable MPN. Every fitted line needs a real Manufacturer PN — passives
    included — because PCBway's `*Mfg Part #` column is required per line (confirmed
    by their sample BOM); only board-only mechanical parts (in_bom=no) are exempt.
    """
    issues = []
    for comp in sch.components:
        ref = comp.reference
        if ref.startswith("#PWR"):
            continue
        lib_sym = sch.lib_symbols.get(comp.lib_id)
        if lib_sym is not None and getattr(lib_sym, "is_power", False):
            continue
        if not comp.in_bom:
            continue

        props = comp.extra_properties or {}
        forbidden_present = any(k.strip().lower() == FORBIDDEN_MPN_FIELD.lower()
                                for k in props)
        mpn_props = [(k, v) for k, v in props.items() if is_mpn_family_key(k)]
        ok, reason = _mpn_verdict(mpn_props, forbidden_present)
        if not ok:
            issues.append(PcbwayIssue(
                severity="error", check_name=_CRIT_CHECK,
                message=f"{ref} ({comp.value}): {reason}",
                reference=ref))

    return PcbwayResult(passed=not issues, issues=issues, parts=[])


def check_schematic_mpns_file(sch_path):
    """Load a .kicad_sch and run the [CRITICAL] schematic-MPN gate."""
    from validate_kicad_sch import load_kicad_sch
    return check_schematic_mpns(load_kicad_sch(sch_path))


def _is_mechanical_ref(reference):
    """Board-only mechanical parts (test points, fiducials, mounting holes) that are
    legitimately not sourced — excluded from the MPN requirement."""
    m = re.match(r"^([A-Za-z]+)", (reference or "").strip())
    return bool(m) and m.group(1).upper() in ("TP", "FID", "FD", "MH", "MK", "MP", "H")


def check_bom_mpn_ready(parts):
    """BOM-level mirror of the schematic-MPN gate, for the pre-generation flat BOM.

    Asserts every fitted line — passives included — carries a real MPN in its Part
    Number column, the same condition that otherwise produces an unresolvable symbol
    MPN. Emits `critical_schematic_mpn_present` errors so it gates identically to the
    schematic check. Only DNP/DNS and board-only mechanical parts are exempt. Makes
    no other assessment (that's check_bom's job).
    """
    issues = []
    for p in parts:
        if _is_mechanical_ref(p.reference):
            continue
        if bom_dnp({"value": p.value, "notes": p.notes}):
            continue  # DNP parts are not sourced
        pn = (p.part_number or "").strip()
        if not pn:
            reason = "no Part Number (manufacturer PN) — PCBway cannot source it and the symbol MPN will be blank"
        elif looks_like_distributor_code(pn):
            reason = f"Part Number '{pn}' is a distributor catalog code, not a manufacturer PN"
        elif looks_like_description(pn):
            reason = f"Part Number '{pn}' looks like a description, not a manufacturer PN"
        else:
            continue
        issues.append(PcbwayIssue(
            severity="error", check_name=_CRIT_CHECK,
            message=f"{p.reference} ({p.value}): {reason}", reference=p.reference))
    return PcbwayResult(passed=not issues, issues=issues, parts=parts)


# ─── Sourcing-sheet emitter ──────────────────────────────────────────

def build_sourcing_sheet(result, project_name=""):
    """Render the BOM as a PCBway sourcing sheet (markdown).

    This is the table to hand to PCBway: every line with its MPN, package,
    distributor PN, assembly rating, and any flags. Blank distributor-PN cells
    are the ones still needing a web-confirmed stock lookup.
    """
    title = f"PCBway Sourcing Sheet - {project_name}" if project_name else "PCBway Sourcing Sheet"
    lines = [f"# {title}", ""]
    lines.append("> Hand this to PCBway turnkey assembly. Fill any blank **Distributor PN** "
                 "cells with a web-confirmed, in-stock part before ordering.")
    lines.append("")
    lines.append("| Ref | Value | MPN | Package | Distributor | Distributor PN | Assembly | Flags |")
    lines.append("|-----|-------|-----|---------|-------------|----------------|----------|-------|")

    badge = {"ok": "OK", "caution": "CAUTION", "unknown": "REVIEW", "block": "BLOCK"}
    for p in result.parts:
        flags = "; ".join(p.flags) if p.flags else ""
        lines.append(
            f"| {p.reference} | {p.value} | {p.part_number} | {p.package} | "
            f"{p.supplier} | {p.supplier_pn} | {badge.get(p.rating, p.rating)} | {flags} |"
        )

    lines.append("")
    n_block = sum(1 for p in result.parts if p.rating == "block")
    n_caution = sum(1 for p in result.parts if p.rating in ("caution", "unknown"))
    n_ok = sum(1 for p in result.parts if p.rating == "ok")
    lines.append(f"**Lines:** {len(result.parts)}  |  "
                 f"OK: {n_ok}  |  CAUTION: {n_caution}  |  BLOCK: {n_block}")
    lines.append("")
    lines.append("**Next:** confirm live stock for each line at the distributors PCBway sources "
                 "from (LCSC > DigiKey > Mouser), then submit to PCBway for a turnkey quote.")
    return "\n".join(lines)


# ─── Output formatters ───────────────────────────────────────────────

def format_result_text(result, bom_path=None):
    lines = ["=" * 60, "PCBWAY ASSEMBLY CHECK", "=" * 60]
    if bom_path:
        lines.append(f"BOM: {bom_path}")
    lines.append("")

    errors, warnings = result.errors, result.warnings
    if result.passed:
        lines.append(f"RESULT: PASSED ({len(warnings)} cautions)")
    else:
        lines.append(f"RESULT: FAILED ({len(errors)} blocking, {len(warnings)} cautions)")
    lines.append(f"Lines checked: {len(result.parts)}")
    lines.append("")

    if errors:
        lines.append("BLOCKING (must fix before ordering):")
        for i in errors:
            lines.append(f"  [{i.check_name}] {i.message}")
        lines.append("")
    if warnings:
        lines.append("CAUTIONS (review, confirm intended):")
        for i in warnings:
            lines.append(f"  [{i.check_name}] {i.message}")
        lines.append("")

    lines.append("Note: live distributor stock is NOT checked here. Confirm each line is "
                 "in stock (LCSC/DigiKey/Mouser) via web search before submitting to PCBway.")
    lines.append("=" * 60)
    return "\n".join(lines)


def format_result_json(result, bom_path=None):
    return json_module.dumps({
        "bom_file": bom_path,
        "passed": result.passed,
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "parts": [
            {
                "reference": p.reference,
                "value": p.value,
                "manufacturer": p.manufacturer,
                "description": p.description,
                "part_number": p.part_number,
                "package": p.package,
                "footprint": p.footprint,
                "supplier": p.supplier,
                "supplier_pn": p.supplier_pn,
                "rating": p.rating,
                "flags": p.flags,
            }
            for p in result.parts
        ],
        "issues": [
            {
                "severity": i.severity,
                "check": i.check_name,
                "reference": i.reference,
                "message": i.message,
            }
            for i in result.issues
        ],
    }, indent=2)


# ─── CLI ────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Score a Stage 3 BOM for PCBway turnkey-assembly readiness.",
    )
    parser.add_argument("bom", nargs="?", help="BOM markdown file (Stage 3)")
    parser.add_argument("--schematic", metavar="FILE",
                        help="Run the [CRITICAL] schematic-MPN gate on a generated "
                             ".kicad_sch (checks baked symbol MPN fields) instead of a BOM")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--sourcing-sheet", action="store_true",
                        help="Emit the PCBway sourcing-sheet markdown instead of the report")
    parser.add_argument("-o", "--output", help="Write output to this file instead of stdout")
    parser.add_argument("--strict", action="store_true",
                        help="Treat cautions as failures too (exit 1 on any caution)")
    args = parser.parse_args()

    # ── [CRITICAL] schematic-MPN gate mode ──
    if args.schematic:
        result = check_schematic_mpns_file(args.schematic)
        if args.json:
            out = json_module.dumps({
                "schematic_file": args.schematic,
                "check": _CRIT_CHECK,
                "passed": result.passed,
                "error_count": len(result.errors),
                "issues": [{"severity": i.severity, "check": i.check_name,
                            "reference": i.reference, "message": i.message}
                           for i in result.issues],
            }, indent=2)
        else:
            lines = ["=" * 60, "PCBWAY SCHEMATIC-MPN GATE [CRITICAL]", "=" * 60,
                     f"Schematic: {args.schematic}", ""]
            lines.append("RESULT: PASSED" if result.passed
                         else f"RESULT: FAILED ({len(result.errors)} unresolvable MPNs)")
            for i in result.errors:
                lines.append(f"  [{i.check_name}] {i.message}")
            out = "\n".join(lines)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(out + "\n")
            print(f"Wrote {args.output}")
        else:
            print(out)
        sys.exit(0 if result.passed else 1)

    if not args.bom:
        parser.error("a BOM file is required (or use --schematic FILE)")

    with open(args.bom, "r", encoding="utf-8") as f:
        text = f.read()
    parts = load_bom_for_pcbway(text)
    result = check_bom(parts)

    if args.sourcing_sheet:
        # Derive a project name from a leading "# Bill of Materials - NAME" header
        # (the BOM template uses an em-dash; - matches it, "-" matches a hyphen).
        m = re.search(r"#\s*Bill of Materials\s*[—-]\s*(.+)", text)
        project = m.group(1).strip() if m else ""
        out = build_sourcing_sheet(result, project)
    elif args.json:
        out = format_result_json(result, args.bom)
    else:
        out = format_result_text(result, args.bom)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out + "\n")
        print(f"Wrote {args.output}")
    else:
        print(out)

    failed = (not result.passed) or (args.strict and result.warnings)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
