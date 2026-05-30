#!/usr/bin/env python3
"""
Fact-card cross-check — verifies the Claude-authored design docs match the
verified per-part fact cards.

A fact card (`{MPN}.facts.yaml`, saved next to the cached datasheet by the Stage 2
research subagent) is the authoritative source for a part's *intrinsic* facts:
`lib_id`, `footprint`, and the pinout (number / name / type). This checker is the
mechanical backstop for the "copy, don't paraphrase" rule — it joins the cards
against the Stage 3 flat BOM and the Stage 6 layout YAML and fails on any drift
between what was verified (the card) and what was authored (the docs).

It is a **pure verifier** — it makes no design decisions. It runs *before* the
Stage 6 build (it needs no `.kicad_sch`), so card drift is caught at the cheapest
point. The join key is `lib_id`, shared by the layout `symbols:` block, the layout
placements, and each card's `kicad.lib_id` — no MPN is needed in the flat BOM.

Checks:
  * pinout_unverified   (error)   — a used card still has pinout_verified: false
  * pin_set_mismatch    (error)   — symbol pin numbers != card pin numbers
  * pin_name_mismatch   (error)   — symbol pin name != card name (same number)
  * pin_type_mismatch   (error)   — symbol pin type != card type (same number)
  * footprint_mismatch  (error)   — BOM footprint for a ref != its card footprint
  * ic_without_card     (warning) — a layout symbols: IC has no matching card
                                     (error under --strict)
  * ambiguous_card      (warning) — two cards claim the same lib_id (pins skipped)
  * card_missing_lib_id (warning) — a card has no kicad.lib_id (can't be joined)
  * unused_card         (info)    — a card's lib_id is never placed

Sourcing fields (distributor PN, stock) are out of scope here — they live on the
full Stage 3 BOM and are covered by check_pcbway.py.

CLI Usage:
    python check_cards.py <layout.yaml> <bom_flat.md> --cards-dir <datasheets_dir>
    python check_cards.py <layout.yaml> <bom_flat.md> --cards-dir <dir> --json
    python check_cards.py <layout.yaml> <bom_flat.md> --cards-dir <dir> --strict

Python API:
    from check_cards import check_cards, load_cards_from_dir
    from generate_from_data import load_layout
    from cross_check_bom import load_bom_from_markdown

    cards = load_cards_from_dir("MyBoard_datasheets")
    layout = load_layout("MyBoard_06_layout.yaml")
    bom = load_bom_from_markdown(open("MyBoard_03_bom_flat.md").read())
    result = check_cards(cards, layout, bom)
"""

import sys
import os
import glob
import json as json_module
from dataclasses import dataclass, field

import yaml

_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from generate_from_data import load_layout, load_layout_from_string, PASSIVE_DISPATCH, _CONN_RE
from cross_check_bom import load_bom_from_markdown


# ─── Data model ───────────────────────────────────────────────────

@dataclass
class FactCard:
    """Intrinsic per-part facts, parsed from a {MPN}.facts.yaml file."""
    mpn: str
    lib_id: str = ""
    footprint: str = ""
    pins: list = field(default_factory=list)   # list of {number, name, type}
    pinout_verified: bool = False
    source_file: str = ""


@dataclass
class CardIssue:
    severity: str       # "error", "warning", "info"
    check_name: str
    message: str
    reference: str = ""  # ref or lib_id the issue concerns


@dataclass
class CardCheckResult:
    passed: bool
    issues: list = field(default_factory=list)

    @property
    def errors(self):
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self):
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def infos(self):
        return [i for i in self.issues if i.severity == "info"]


# ─── Normalization ────────────────────────────────────────────────

def _norm(s):
    """Normalize a pin name/type/footprint for comparison: str, strip, casefold."""
    return str(s).strip().casefold()


def _card_pin_index(card):
    """Return {pin_number(str): {'name':, 'type':}} for a card."""
    out = {}
    for p in card.pins:
        if isinstance(p, dict):
            num = p.get("number")
            out[str(num)] = {"name": p.get("name", ""), "type": p.get("type", "")}
        elif isinstance(p, (list, tuple)) and p:
            # tolerate [number, name, type, ...] form
            num = p[0]
            out[str(num)] = {
                "name": p[1] if len(p) > 1 else "",
                "type": p[2] if len(p) > 2 else "",
            }
    return out


# ─── Card loading ─────────────────────────────────────────────────

def _parse_card(raw, source_file=""):
    """Build a FactCard from a parsed YAML dict."""
    kicad = raw.get("kicad", {}) or {}
    pins = raw.get("pins", []) or []
    return FactCard(
        mpn=str(raw.get("mpn", "") or raw.get("MPN", "")),
        lib_id=str(kicad.get("lib_id", "") or ""),
        footprint=str(kicad.get("footprint", "") or ""),
        pins=list(pins),
        pinout_verified=bool(raw.get("pinout_verified", False)),
        source_file=source_file,
    )


def load_card_from_string(text, source_file=""):
    return _parse_card(yaml.safe_load(text) or {}, source_file)


def load_card(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        return _parse_card(yaml.safe_load(f) or {}, os.path.basename(filepath))


def load_cards_from_dir(dirpath):
    """Load every *.facts.yaml in a directory. Returns list[FactCard]."""
    cards = []
    for path in sorted(glob.glob(os.path.join(dirpath, "*.facts.yaml"))):
        cards.append(load_card(path))
    return cards


# ─── Helpers ──────────────────────────────────────────────────────

def _is_intrinsic_ic(lib_id):
    """True if a lib_id is a true IC/named-pin part (gets a symbols: entry),
    i.e. not an auto-registered passive or generic connector."""
    if lib_id in PASSIVE_DISPATCH:
        return False
    if _CONN_RE.match(lib_id):
        return False
    return True


def _index_cards_by_lib_id(cards, issues):
    """Return {lib_id: FactCard}, recording ambiguity / missing-lib_id issues."""
    by_lib = {}
    seen = {}
    for card in cards:
        if not card.lib_id:
            issues.append(CardIssue(
                severity="warning", check_name="card_missing_lib_id",
                message=f"card {card.source_file or card.mpn} has no kicad.lib_id "
                        f"— cannot be joined to the layout",
                reference=card.mpn,
            ))
            continue
        if card.lib_id in seen:
            issues.append(CardIssue(
                severity="warning", check_name="ambiguous_card",
                message=f"lib_id '{card.lib_id}' is claimed by two cards "
                        f"({seen[card.lib_id]}, {card.mpn}) — pin checks skipped",
                reference=card.lib_id,
            ))
            by_lib.pop(card.lib_id, None)
            continue
        seen[card.lib_id] = card.mpn
        by_lib[card.lib_id] = card
    return by_lib


# ─── Cross-check engine ───────────────────────────────────────────

def check_cards(cards, layout, bom_entries, strict=False):
    """Cross-check fact cards against the authored layout + flat BOM.

    Args:
        cards:       list[FactCard]
        layout:      a generate_from_data.Layout
        bom_entries: list[cross_check_bom.BomEntry]
        strict:      escalate ic_without_card from warning to error.

    Returns CardCheckResult.
    """
    issues = []
    cards_by_lib = _index_cards_by_lib_id(cards, issues)

    bom_by_ref = {e.reference: e for e in bom_entries}
    placed_lib_ids = {pl.lib_id for pl in layout.placements.values()}

    # 1: every layout symbols: entry (a true IC) must have a verified card,
    #    and its pin number/name/type must match the card.
    for lib_id, sym in layout.symbols.items():
        card = cards_by_lib.get(lib_id)
        if card is None:
            sev = "error" if strict else "warning"
            issues.append(CardIssue(
                severity=sev, check_name="ic_without_card",
                message=f"IC '{lib_id}' has a layout symbols: entry but no fact card "
                        f"— its pinout was not verified",
                reference=lib_id,
            ))
            continue

        if not card.pinout_verified:
            issues.append(CardIssue(
                severity="error", check_name="pinout_unverified",
                message=f"card for '{lib_id}' ({card.mpn}) has pinout_verified: false "
                        f"— run the Stage 4 re-derivation before it feeds symbols:",
                reference=lib_id,
            ))

        card_pins = _card_pin_index(card)
        sym_pins = {}
        for p in sym.pins:
            if not p:
                continue
            num = str(p[0])
            sym_pins[num] = {
                "name": p[1] if len(p) > 1 else "",
                "type": p[2] if len(p) > 2 else "",
            }

        # pin-number set
        if set(sym_pins) != set(card_pins):
            missing = sorted(set(card_pins) - set(sym_pins))
            extra = sorted(set(sym_pins) - set(card_pins))
            detail = []
            if missing:
                detail.append(f"missing from symbol: {missing}")
            if extra:
                detail.append(f"extra in symbol: {extra}")
            issues.append(CardIssue(
                severity="error", check_name="pin_set_mismatch",
                message=f"{lib_id}: symbol pin numbers != card pin numbers "
                        f"({'; '.join(detail)})",
                reference=lib_id,
            ))

        # name/type for shared pin numbers
        for num in sorted(set(sym_pins) & set(card_pins)):
            s = sym_pins[num]
            c = card_pins[num]
            if _norm(s["name"]) != _norm(c["name"]):
                issues.append(CardIssue(
                    severity="error", check_name="pin_name_mismatch",
                    message=f"{lib_id} pin {num}: symbol name '{s['name']}' != "
                            f"card name '{c['name']}'",
                    reference=lib_id,
                ))
            if _norm(s["type"]) != _norm(c["type"]):
                issues.append(CardIssue(
                    severity="error", check_name="pin_type_mismatch",
                    message=f"{lib_id} pin {num}: symbol type '{s['type']}' != "
                            f"card type '{c['type']}'",
                    reference=lib_id,
                ))

    # 2: BOM footprint for any ref whose lib_id has a card must match the card.
    for ref, pl in layout.placements.items():
        card = cards_by_lib.get(pl.lib_id)
        if card is None or not card.footprint:
            continue
        bom = bom_by_ref.get(ref)
        if bom is None or not bom.footprint:
            continue
        if _norm(bom.footprint) != _norm(card.footprint):
            issues.append(CardIssue(
                severity="error", check_name="footprint_mismatch",
                message=f"{ref} ({pl.lib_id}): BOM footprint '{bom.footprint}' != "
                        f"card footprint '{card.footprint}'",
                reference=ref,
            ))

    # 3: informational — a card whose lib_id is never placed (stale/unused).
    for lib_id, card in cards_by_lib.items():
        if lib_id not in placed_lib_ids:
            issues.append(CardIssue(
                severity="info", check_name="unused_card",
                message=f"card for '{lib_id}' ({card.mpn}) is not placed in the layout",
                reference=lib_id,
            ))

    has_errors = any(i.severity == "error" for i in issues)
    return CardCheckResult(passed=not has_errors, issues=issues)


# ─── Output formatters ────────────────────────────────────────────

def format_result_text(result, layout_path=None, bom_path=None, cards_dir=None):
    lines = []
    lines.append("=" * 60)
    lines.append("FACT-CARD CROSS-CHECK REPORT")
    lines.append("=" * 60)
    if cards_dir:
        lines.append(f"Cards:  {cards_dir}")
    if layout_path:
        lines.append(f"Layout: {layout_path}")
    if bom_path:
        lines.append(f"BOM:    {bom_path}")
    lines.append("")

    errors = result.errors
    warnings = result.warnings
    infos = result.infos
    if result.passed:
        lines.append(f"RESULT: PASSED ({len(warnings)} warnings, {len(infos)} info)")
    else:
        lines.append(f"RESULT: FAILED ({len(errors)} errors, "
                     f"{len(warnings)} warnings, {len(infos)} info)")
    lines.append("")

    for label, bucket in (("ERRORS", errors), ("WARNINGS", warnings), ("INFO", infos)):
        if bucket:
            lines.append(f"{label}:")
            for i in bucket:
                lines.append(f"  [{i.check_name}] {i.message}")
            lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def format_result_json(result, layout_path=None, bom_path=None, cards_dir=None):
    output = {
        "cards_dir": cards_dir,
        "layout_file": layout_path,
        "bom_file": bom_path,
        "passed": result.passed,
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "info_count": len(result.infos),
        "issues": [
            {
                "severity": i.severity,
                "check": i.check_name,
                "reference": i.reference,
                "message": i.message,
            }
            for i in result.issues
        ],
    }
    return json_module.dumps(output, indent=2)


# ─── CLI ──────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Cross-check per-part fact cards against the layout YAML + flat BOM.",
    )
    parser.add_argument("layout", help="Stage 6 layout YAML")
    parser.add_argument("bom", help="Stage 3 flat BOM markdown")
    parser.add_argument("--cards-dir", required=True,
                        help="directory of {MPN}.facts.yaml cards "
                             "(the {project}_datasheets folder)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--strict", action="store_true",
                        help="treat ic_without_card as an error")
    args = parser.parse_args()

    cards = load_cards_from_dir(args.cards_dir)
    layout = load_layout(args.layout)
    with open(args.bom, "r", encoding="utf-8") as f:
        bom_entries = load_bom_from_markdown(f.read())

    result = check_cards(cards, layout, bom_entries, strict=args.strict)

    if args.json:
        print(format_result_json(result, args.layout, args.bom, args.cards_dir))
    else:
        print(format_result_text(result, args.layout, args.bom, args.cards_dir))

    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
