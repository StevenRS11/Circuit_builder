#!/usr/bin/env python3
"""Tests for check_ledger.py — the bench-truth ledger checker (roadmap W2).

Includes the live integration check: the repo's actual validated_boards.yaml
must verify clean, so a refactor that moves/renames an encoded lesson (gate,
test, doc section) fails the suite instead of silently orphaning the lesson.
"""

import os
import sys

_tests_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_tests_dir, "..", "scripts")))

import yaml
import pytest

from check_ledger import check_ledger, check_ledger_file, DEFAULT_LEDGER, REPO_ROOT


def _entry(**over):
    base = {
        "board": "testboard", "rev": "1.0", "status": "validated",
        "bench_date": "2026-07-01", "summary": "worked",
        "field_reports": [], "lessons": [],
    }
    base.update(over)
    return base


def _run(*boards, repo_root=None, blocks_dir=None):
    return check_ledger({"version": 1, "boards": list(boards)},
                        repo_root=repo_root or REPO_ROOT,
                        blocks_dir=blocks_dir or os.path.join(REPO_ROOT, "no_blocks"))


class TestLiveLedger:
    def test_repo_ledger_verifies_clean(self):
        passed, issues = check_ledger_file(DEFAULT_LEDGER)
        assert passed, [i.message for i in issues if i.severity == "error"]

    def test_repo_ledger_lessons_are_encoded(self):
        with open(DEFAULT_LEDGER, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        lessons = [l for b in data["boards"] for l in (b.get("lessons") or [])]
        assert lessons, "seeded ledger should carry the battery_3s lessons"
        assert all(l.get("encoded_in") for l in lessons)


class TestSchema:
    def test_missing_required_field(self):
        passed, issues = _run(_entry(summary=""))
        assert not passed
        assert any(i.check == "schema" and "'summary'" in i.message for i in issues)

    def test_unknown_status(self):
        passed, issues = _run(_entry(status="probably_fine"))
        assert not passed

    def test_duplicate_board_rev(self):
        passed, issues = _run(_entry(), _entry())
        assert not passed
        assert any(i.check == "duplicate_entry" for i in issues)

    def test_bad_date(self):
        passed, _ = _run(_entry(bench_date="July 1st"))
        assert not passed

    def test_null_date_ok_for_in_bringup(self):
        passed, issues = _run(_entry(status="in_bringup", bench_date=None))
        assert passed and not issues


class TestLessonEncoding:
    def test_lesson_without_encoding_is_error(self):
        lesson = {"defect": "x", "class": "topology", "encoded_in": []}
        passed, issues = _run(_entry(status="validated_with_lessons",
                                     lessons=[lesson]))
        assert not passed
        assert any(i.check == "lesson_not_encoded" for i in issues)

    def test_encoding_file_must_exist(self):
        lesson = {"defect": "x", "class": "topology",
                  "encoded_in": ["no/such/file.py::gate"]}
        passed, issues = _run(_entry(status="validated_with_lessons",
                                     lessons=[lesson]))
        assert not passed

    def test_needle_must_appear_in_file(self):
        good = (".claude/skills/kicad-schematic-gen/scripts/"
                "generate_from_data.py")
        ok = {"defect": "x", "class": "topology",
              "encoded_in": [f"{good}::internal-supply output shorted"]}
        gone = {"defect": "y", "class": "topology",
                "encoded_in": [f"{good}::this needle was refactored away"]}
        passed, _ = _run(_entry(status="validated_with_lessons", lessons=[ok]))
        assert passed
        passed, issues = _run(_entry(status="validated_with_lessons",
                                     lessons=[gone]))
        assert not passed
        assert any("refactored away" in i.message or "not found" in i.message
                   for i in issues)

    def test_validated_with_lessons_requires_a_lesson(self):
        passed, issues = _run(_entry(status="validated_with_lessons"))
        assert not passed
        assert any(i.check == "hygiene" and i.severity == "error" for i in issues)


class TestClaimedArtifacts:
    def test_phantom_block_claim(self):
        passed, issues = _run(_entry(blocks_extracted=["ghost_block"]))
        assert not passed
        assert any(i.check == "phantom_block" for i in issues)

    def test_real_block_claim(self, tmp_path):
        blocks = tmp_path / "blocks"
        (blocks / "nau7802_frontend").mkdir(parents=True)
        (blocks / "nau7802_frontend" / "block.yaml").write_text("name: x")
        passed, issues = _run(_entry(blocks_extracted=["nau7802_frontend"]),
                              blocks_dir=str(blocks))
        assert passed, [i.message for i in issues]

    def test_phantom_field_report(self):
        passed, issues = _run(_entry(field_reports=["reports/none.md"]))
        assert not passed
        assert any(i.check == "phantom_artifact" for i in issues)

    def test_failed_without_lessons_warns(self):
        passed, issues = _run(_entry(status="failed"))
        assert passed  # warning, not error
        assert any(i.severity == "warning" and "wasted board" in i.message
                   for i in issues)
