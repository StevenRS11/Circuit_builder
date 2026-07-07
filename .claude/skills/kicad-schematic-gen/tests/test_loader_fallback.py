#!/usr/bin/env python3
"""Tests for the loader library-fallback (stale lib_symbols cache).

The defect class: a hand-edited / cross-project-pasted schematic whose placed
symbol's lib_id is missing from the file's embedded lib_symbols cache. KiCad
falls back to the installed libraries; our loader used to drop the component,
cascading false dangling/disconnected/floating errors (1 stale symbol → 14
false errors, observed on pushbuttonDef). The fallback mirrors KiCad: resolve
from registered/explicit libraries, keep connectivity, warn `stale_lib_cache`.
"""

import os
import sys

_tests_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_tests_dir, "..", "scripts")))

import pytest

from validate_kicad_sch import (
    load_kicad_sch, validate, _extract_all_pin_positions,
)


def _errs(result):
    return [i for i in result.issues if i.severity == "error"]


def _warns(result):
    return [i for i in result.issues if i.severity == "warning"]

# A 2-pin part that exists ONLY in the sideloaded library below — never in
# KiCad's built-ins, so resolution can't accidentally come from an install.
TEST_LIB = """(kicad_symbol_lib (version 20231120) (generator kicad_symbol_editor)
  (symbol "FAKEPART_XZ9" (in_bom yes) (on_board yes)
    (property "Reference" "U" (at 0 7.62 0))
    (property "Value" "FAKEPART_XZ9" (at 0 -7.62 0))
    (symbol "FAKEPART_XZ9_0_1"
      (rectangle (start -5.08 5.08) (end 5.08 -5.08))
    )
    (symbol "FAKEPART_XZ9_1_1"
      (pin passive line (at -7.62 2.54 0) (length 2.54)
        (name "A") (number "1"))
      (pin passive line (at -7.62 -2.54 0) (length 2.54)
        (name "B") (number "2"))
    )
  )
)
"""

# The stale-cache schematic: the instance references TestLib:FAKEPART_XZ9 but
# the embedded (lib_symbols) cache is empty.
STALE_SCH = """(kicad_sch (version 20230121) (generator eeschema)
  (uuid "00000000-0000-0000-0000-00000000root")
  (lib_symbols)
{wires}  (symbol (lib_id "TestLib:FAKEPART_XZ9") (at 101.6 101.6 0) (unit 1)
    (property "Reference" "U1" (at 0 0 0))
    (property "Value" "FAKEPART_XZ9" (at 0 0 0))
    (property "Footprint" "Package_SO:SOIC-8" (at 0 0 0))
    (instances
      (project "stale"
        (path "/00000000-0000-0000-0000-00000000root"
          (reference "U1") (unit 1)
        )
      )
    )
  )
)
"""


@pytest.fixture()
def test_lib(tmp_path):
    p = tmp_path / "TestLib.kicad_sym"
    p.write_text(TEST_LIB, encoding="utf-8")
    return [f"TestLib={p}"]


def _write_sch(tmp_path, wires=""):
    p = tmp_path / "stale.kicad_sch"
    p.write_text(STALE_SCH.format(wires=wires), encoding="utf-8")
    return str(p)


class TestFallbackResolution:
    def test_without_fallback_component_has_no_pins(self, tmp_path):
        sch = load_kicad_sch(_write_sch(tmp_path), resolve_from_libraries=False)
        assert "TestLib:FAKEPART_XZ9" not in sch.lib_symbols
        assert _extract_all_pin_positions(sch) == []
        result = validate(sch)
        assert any(i.check_name == "missing_lib_symbol" for i in _errs(result))

    def test_fallback_resolves_pins_and_warns(self, tmp_path, test_lib):
        sch = load_kicad_sch(_write_sch(tmp_path), extra_sym=test_lib)
        assert "TestLib:FAKEPART_XZ9" in sch.lib_symbols
        pins = _extract_all_pin_positions(sch)
        assert sorted(p[1] for p in pins) == ["1", "2"]
        assert sch.stale_lib_cache == ["TestLib:FAKEPART_XZ9"]

        result = validate(sch)
        assert not any(i.check_name == "missing_lib_symbol" for i in _errs(result))
        warns = [i for i in _warns(result) if i.check_name == "stale_lib_cache"]
        assert len(warns) == 1 and "U1" in warns[0].message

    def test_unresolvable_lib_id_still_errors(self, tmp_path):
        # No library anywhere carries this part — fallback must not mask it.
        sch = load_kicad_sch(_write_sch(tmp_path))  # default global resolution
        if "TestLib:FAKEPART_XZ9" in sch.lib_symbols:  # pragma: no cover
            pytest.skip("a registered library unexpectedly carries the nickname")
        assert "TestLib:FAKEPART_XZ9" in sch.unresolved_lib_ids
        result = validate(sch)
        assert any(i.check_name == "missing_lib_symbol" for i in _errs(result))


class TestFalseErrorCascade:
    def test_wired_stale_symbol_cascade_vs_fallback(self, tmp_path, test_lib):
        # Phase A: resolve pins via fallback to learn their true positions.
        probe = load_kicad_sch(_write_sch(tmp_path), extra_sym=test_lib)
        (x1, y1) = probe.get_pin_position("U1", "1")
        (x2, y2) = probe.get_pin_position("U1", "2")

        # Phase B: the same schematic with a wire joining the two pins.
        wire = (f"  (wire (pts (xy {x1} {y1}) (xy {x2} {y2}))\n"
                f"    (stroke (width 0)) (uuid \"w1\")\n  )\n")
        path = _write_sch(tmp_path, wires=wire)

        # Without fallback: the dropped symbol turns a perfectly-connected
        # wire into false errors (the observed cascade class).
        broken = validate(load_kicad_sch(path, resolve_from_libraries=False))
        assert not broken.passed
        false_checks = {i.check_name for i in _errs(broken)}
        assert "dangling_wire" in false_checks
        assert "missing_lib_symbol" in false_checks

        # With fallback: fully clean apart from the stale-cache warning.
        fixed = validate(load_kicad_sch(path, extra_sym=test_lib))
        assert fixed.passed, [i.message for i in _errs(fixed)]
        assert any(i.check_name == "stale_lib_cache" for i in _warns(fixed))
