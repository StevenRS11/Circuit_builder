#!/usr/bin/env python3
"""check_ledger.py — deterministic checker for the bench-truth ledger
(validated_boards.yaml, roadmap W2).

The ledger is only worth having if its claims stay true as the repo evolves,
so this script mechanically verifies them:

  * schema: required fields present, status / lesson-class enums valid,
    (board, rev) unique, dates parse;
  * **lessons are really encoded**: every `encoded_in` entry ("path" or
    "path::needle") must point at an existing repo file, and the needle must
    appear in it — a lesson whose gate/test/doc was refactored away becomes
    an error here instead of silently evaporating (the user's "capabilities
    must never vanish" rule, applied to bench lessons);
  * claimed artifacts exist: `blocks_extracted` entries have a
    blocks/{name}/block.yaml, `eval_anchor` and `field_reports` paths exist;
  * hygiene: a `validated_with_lessons` board must list ≥1 lesson; a
    `validated` board should carry a bench_date and a field report.

Verify-only; makes no judgment about whether a lesson was *worth* encoding —
that's the promotion ritual's job (references/promotion.md).

CLI:
    python check_ledger.py                       # checks the skill's ledger
    python check_ledger.py path/to/ledger.yaml --json --strict
Exit 0 = pass, 1 = errors (or warnings with --strict).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

import yaml

SKILL_DIR = os.path.normpath(os.path.join(_script_dir, ".."))
REPO_ROOT = os.path.normpath(os.path.join(SKILL_DIR, "..", "..", ".."))
DEFAULT_LEDGER = os.path.join(SKILL_DIR, "validated_boards.yaml")
BLOCKS_DIR = os.path.join(SKILL_DIR, "blocks")

STATUSES = {"in_bringup", "validated", "validated_with_lessons", "failed", "retired"}
LESSON_CLASSES = {"pinout", "topology", "sourcing", "layout", "process"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class LedgerIssue:
    def __init__(self, severity, check, message, board=""):
        self.severity = severity  # "error" | "warning"
        self.check = check
        self.message = message
        self.board = board

    def as_dict(self):
        return {"severity": self.severity, "check": self.check,
                "message": self.message, "board": self.board}


def _exists(rel_path, repo_root):
    return os.path.exists(os.path.join(repo_root, rel_path))


def _check_encoded_in(entry, key, issues, repo_root):
    """Verify one 'path' / 'path::needle' encoding reference."""
    path, _, needle = entry.partition("::")
    path = path.strip()
    full = os.path.join(repo_root, path)
    if not os.path.isfile(full):
        issues.append(LedgerIssue(
            "error", "lesson_not_encoded",
            f"{key}: encoded_in file does not exist: '{path}'", key))
        return
    if needle:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            if needle.strip() not in f.read():
                issues.append(LedgerIssue(
                    "error", "lesson_not_encoded",
                    f"{key}: needle '{needle.strip()}' not found in '{path}' — "
                    f"the encoded lesson was moved or refactored away", key))


def check_ledger(data, repo_root=REPO_ROOT, blocks_dir=BLOCKS_DIR):
    """Check a parsed ledger dict. Returns (passed, issues)."""
    issues = []
    boards = data.get("boards")
    if not isinstance(boards, list) or not boards:
        issues.append(LedgerIssue("error", "schema", "no 'boards' list"))
        return False, issues

    seen = set()
    for i, b in enumerate(boards):
        if not isinstance(b, dict):
            issues.append(LedgerIssue("error", "schema", f"boards[{i}] is not a map"))
            continue
        name = str(b.get("board", "")).strip()
        rev = str(b.get("rev", "")).strip()
        key = f"{name} rev {rev}" if name else f"boards[{i}]"

        for field in ("board", "rev", "status", "summary"):
            if not str(b.get(field, "") or "").strip():
                issues.append(LedgerIssue(
                    "error", "schema", f"{key}: missing required field '{field}'", key))

        status = str(b.get("status", "")).strip()
        if status and status not in STATUSES:
            issues.append(LedgerIssue(
                "error", "schema",
                f"{key}: unknown status '{status}' (expected one of "
                f"{sorted(STATUSES)})", key))

        if (name, rev) in seen:
            issues.append(LedgerIssue(
                "error", "duplicate_entry", f"{key}: duplicate (board, rev)", key))
        seen.add((name, rev))

        bench_date = b.get("bench_date")
        if bench_date is not None and str(bench_date).strip():
            if not _DATE.match(str(bench_date).strip()):
                issues.append(LedgerIssue(
                    "error", "schema",
                    f"{key}: bench_date '{bench_date}' is not YYYY-MM-DD", key))
        elif status in ("validated", "validated_with_lessons", "failed"):
            issues.append(LedgerIssue(
                "warning", "hygiene",
                f"{key}: status '{status}' but no bench_date", key))

        # Lessons
        lessons = b.get("lessons") or []
        if status == "validated_with_lessons" and not lessons:
            issues.append(LedgerIssue(
                "error", "hygiene",
                f"{key}: validated_with_lessons but zero lessons listed", key))
        if status == "failed" and not lessons:
            issues.append(LedgerIssue(
                "warning", "hygiene",
                f"{key}: failed at the bench but no lesson recorded — a failure "
                f"that deposits nothing is a wasted board", key))
        for lesson in lessons:
            if not str(lesson.get("defect", "") or "").strip():
                issues.append(LedgerIssue(
                    "error", "schema", f"{key}: lesson missing 'defect'", key))
            lclass = str(lesson.get("class", "")).strip()
            if lclass not in LESSON_CLASSES:
                issues.append(LedgerIssue(
                    "error", "schema",
                    f"{key}: lesson class '{lclass}' not in "
                    f"{sorted(LESSON_CLASSES)}", key))
            encoded = lesson.get("encoded_in") or []
            if not encoded:
                issues.append(LedgerIssue(
                    "error", "lesson_not_encoded",
                    f"{key}: lesson '{str(lesson.get('defect', ''))[:60]}…' has "
                    f"no encoded_in — a lesson that lives only in prose is not "
                    f"a lesson", key))
            for entry in encoded:
                _check_encoded_in(str(entry), key, issues, repo_root)

        # Claimed artifacts
        for block in (b.get("blocks_extracted") or []):
            manifest = os.path.join(blocks_dir, str(block), "block.yaml")
            if not os.path.isfile(manifest):
                issues.append(LedgerIssue(
                    "error", "phantom_block",
                    f"{key}: claims extracted block '{block}' but "
                    f"blocks/{block}/block.yaml does not exist", key))
        anchor = b.get("eval_anchor")
        if anchor and not _exists(str(anchor), repo_root):
            issues.append(LedgerIssue(
                "error", "phantom_artifact",
                f"{key}: eval_anchor '{anchor}' does not exist", key))
        for rp in (b.get("field_reports") or []):
            if not _exists(str(rp), repo_root):
                issues.append(LedgerIssue(
                    "error", "phantom_artifact",
                    f"{key}: field report '{rp}' does not exist", key))
        if status == "validated" and not (b.get("field_reports") or []):
            issues.append(LedgerIssue(
                "warning", "hygiene",
                f"{key}: validated with no field report on record", key))

    passed = not any(i.severity == "error" for i in issues)
    return passed, issues


def check_ledger_file(path=DEFAULT_LEDGER, repo_root=REPO_ROOT,
                      blocks_dir=BLOCKS_DIR):
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return check_ledger(data, repo_root=repo_root, blocks_dir=blocks_dir)


def main():
    ap = argparse.ArgumentParser(description="Check the bench-truth ledger")
    ap.add_argument("ledger", nargs="?", default=DEFAULT_LEDGER)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="warnings also fail")
    args = ap.parse_args()

    passed, issues = check_ledger_file(args.ledger)
    if args.json:
        print(json.dumps({"passed": passed,
                          "issues": [i.as_dict() for i in issues]}, indent=2))
    else:
        n_err = sum(1 for i in issues if i.severity == "error")
        n_warn = len(issues) - n_err
        for i in issues:
            print(f"{i.severity.upper():7s} {i.check}: {i.message}")
        print(f"{'PASS' if passed else 'FAIL'} — {n_err} error(s), {n_warn} warning(s)")
    if args.strict and issues:
        return 1
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
