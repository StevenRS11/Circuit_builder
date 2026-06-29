"""Unit tests for lib-table-driven library resolution (check_kicad_library.py).

Synthetic and fully offline: a fake KiCad install root, a fake global config dir
with a sym-lib-table/fp-lib-table, and a fake project dir with its own tables and
a custom symbol. Covers URI-variable resolution, nickname precedence, explicit
--sym-lib/--fp-lib, footprint resolution, and that a user-library hit is flagged.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from check_kicad_library import (
    build_library_set, parse_lib_table, _resolve_uri, _parse_extra,
    _kicad_uri_subs, find_symbol, check_footprint, lookup_part,
)


# ─── A minimal but valid .kicad_sym body ─────────────────────────────
def _sym(name, footprint="Package_SO:SOIC-8"):
    """One top-level symbol with two pins, tab-indented like KiCad emits."""
    return (
        f'\t(symbol "{name}"\n'
        f'\t\t(property "Footprint" "{footprint}")\n'
        f'\t\t(property "Description" "test part {name}")\n'
        f'\t\t(symbol "{name}_1_1"\n'
        f'\t\t\t(pin power_in line\n'
        f'\t\t\t\t(at 0 0 0)\n'
        f'\t\t\t\t(length 2.54)\n'
        f'\t\t\t\t(name "VDD")\n'
        f'\t\t\t\t(number "1"))\n'
        f'\t\t\t(pin output line\n'
        f'\t\t\t\t(at 0 0 0)\n'
        f'\t\t\t\t(length 2.54)\n'
        f'\t\t\t\t(name "OUT")\n'
        f'\t\t\t\t(number "2"))\n'
        f'\t\t)\n'
        f'\t)\n'
    )


def _lib(*names):
    body = "".join(_sym(n) for n in names)
    return (
        '(kicad_symbol_lib\n\t(version 20231120)\n'
        '\t(generator "test")\n' + body + ')\n'
    )


@pytest.fixture
def install(tmp_path):
    """A fake environment: KiCad root + global config dir + a project dir."""
    root = tmp_path / "kicad_root"
    sym_dir = root / "symbols"
    fp_dir = root / "footprints"
    sym_dir.mkdir(parents=True)
    fp_dir.mkdir(parents=True)
    # a built-in library + a built-in footprint
    (sym_dir / "Device.kicad_sym").write_text(_lib("R", "C", "SHARED"), encoding="utf-8")
    pretty = fp_dir / "Package_SO.pretty"
    pretty.mkdir()
    (pretty / "SOIC-8.kicad_mod").write_text("(footprint)", encoding="utf-8")

    # global config dir with a Custom lib registered by absolute path, plus the
    # built-in Device lib referenced through ${KICAD8_SYMBOL_DIR}
    cfg = tmp_path / "config"
    cfg.mkdir()
    custom_sym = tmp_path / "Custom.kicad_sym"
    custom_sym.write_text(_lib("NAU7802", "SHARED"), encoding="utf-8")
    custom_pretty = tmp_path / "Custom.pretty"
    custom_pretty.mkdir()
    (custom_pretty / "MYFP.kicad_mod").write_text("(footprint)", encoding="utf-8")
    (cfg / "sym-lib-table").write_text(
        '(sym_lib_table\n'
        '  (lib (name "Device")(type "KiCad")(uri "${KICAD8_SYMBOL_DIR}/Device.kicad_sym")(options "")(descr ""))\n'
        f'  (lib (name "Custom")(type "KiCad")(uri "{custom_sym.as_posix()}")(options "")(descr ""))\n'
        '  (lib (name "Legacy")(type "Legacy")(uri "whatever.lib")(options "")(descr ""))\n'
        ')\n', encoding="utf-8")
    (cfg / "fp-lib-table").write_text(
        '(fp_lib_table\n'
        f'  (lib (name "Custom")(type "KiCad")(uri "{custom_pretty.as_posix()}")(options "")(descr ""))\n'
        ')\n', encoding="utf-8")

    # project dir whose table overrides "SHARED"'s home via ${KIPRJMOD}
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "ProjLib.kicad_sym").write_text(_lib("SHARED", "PROJPART"), encoding="utf-8")
    (proj / "sym-lib-table").write_text(
        '(sym_lib_table\n'
        '  (lib (name "ProjLib")(type "KiCad")(uri "${KIPRJMOD}/ProjLib.kicad_sym")(options "")(descr ""))\n'
        ')\n', encoding="utf-8")

    return {
        "root": str(root), "cfg": str(cfg), "proj": str(proj),
        "custom_sym": str(custom_sym),
    }


# ─── URI variable resolution ─────────────────────────────────────────
def test_resolve_uri_kicad_var(install):
    subs = _kicad_uri_subs(install["root"], install["proj"])
    out = _resolve_uri("${KICAD8_SYMBOL_DIR}/Device.kicad_sym", subs)
    assert out == os.path.normpath(os.path.join(install["root"], "symbols", "Device.kicad_sym"))


def test_resolve_uri_kiprjmod(install):
    subs = _kicad_uri_subs(install["root"], install["proj"])
    out = _resolve_uri("${KIPRJMOD}/ProjLib.kicad_sym", subs)
    assert out == os.path.normpath(os.path.join(install["proj"], "ProjLib.kicad_sym"))


def test_resolve_uri_unknown_var_left_intact():
    assert "${NOPE}" in _resolve_uri("${NOPE}/x.kicad_sym", {})


# ─── lib-table parsing ───────────────────────────────────────────────
def test_parse_lib_table_skips_non_kicad(install):
    subs = _kicad_uri_subs(install["root"], None)
    libs, skipped = parse_lib_table(os.path.join(install["cfg"], "sym-lib-table"), subs)
    nicks = {n for n, _ in libs}
    assert "Custom" in nicks and "Device" in nicks
    assert "Legacy" not in nicks
    assert any("Legacy" in s for s in skipped)


def test_parse_extra_nick_and_bare():
    nick_path = _parse_extra(["Foo=/abs/Foo.kicad_sym"])[0]
    assert nick_path[0] == "Foo"
    bare = _parse_extra(["/abs/Bar.kicad_sym"])[0]
    assert bare[0] == "Bar"  # stem, extension stripped
    pretty = _parse_extra(["/abs/MyFps.pretty"])[0]
    assert pretty[0] == "MyFps"


# ─── build_library_set discovery + precedence ────────────────────────
def test_build_set_discovers_global_and_builtin(install):
    ls = build_library_set(install["root"], config_dir=install["cfg"])
    assert "Custom" in ls.sym_libs
    assert "Device" in ls.sym_libs
    assert "Custom" in ls.fp_libs


def test_build_set_includes_project(install):
    ls = build_library_set(install["root"], project_dir=install["proj"],
                           config_dir=install["cfg"])
    assert "ProjLib" in ls.sym_libs


def test_no_builtin_excludes_install_scan(install):
    ls = build_library_set(install["root"], config_dir=install["cfg"],
                           include_builtin=False)
    # Device still arrives via the global table (it references KICAD8_SYMBOL_DIR),
    # but a built-in-only library not in any table would be absent. Here we just
    # assert the explicit flag does not crash and global entries survive.
    assert "Custom" in ls.sym_libs


def test_find_symbol_user_lib_lib_id(install):
    ls = build_library_set(install["root"], config_dir=install["cfg"])
    res = find_symbol("NAU7802", libraries=ls, include_pins=True)
    assert res and res[0]["lib_id"] == "Custom:NAU7802"
    assert len(res[0]["pins"]) == 2


def test_precedence_explicit_wins_tie(install):
    # "SHARED" exists in Device (built-in/global) and an explicit lib; explicit wins.
    ls = build_library_set(
        install["root"], config_dir=install["cfg"],
        extra_sym=[f"Explicit={install['custom_sym']}"],
    )
    res = find_symbol("SHARED", libraries=ls)
    assert res[0]["lib_id"] == "Explicit:SHARED"


def test_precedence_project_over_global(install):
    ls = build_library_set(install["root"], project_dir=install["proj"],
                           config_dir=install["cfg"])
    res = find_symbol("SHARED", libraries=ls)
    # ProjLib (project) outranks Device (global) and Custom (global) on the tie.
    assert res[0]["lib_id"] == "ProjLib:SHARED"


# ─── footprint resolution ────────────────────────────────────────────
def test_check_footprint_builtin(install):
    ls = build_library_set(install["root"], config_dir=install["cfg"])
    r = check_footprint("Package_SO:SOIC-8", install["root"], libraries=ls)
    assert r["exists"]


def test_check_footprint_user_lib(install):
    ls = build_library_set(install["root"], config_dir=install["cfg"])
    r = check_footprint("Custom:MYFP", install["root"], libraries=ls)
    assert r["exists"]


def test_check_footprint_missing(install):
    ls = build_library_set(install["root"], config_dir=install["cfg"])
    r = check_footprint("Custom:DOES_NOT_EXIST", install["root"], libraries=ls)
    assert not r["exists"]


# ─── lookup_part end to end ──────────────────────────────────────────
def test_lookup_part_flags_user_library(install):
    ls = build_library_set(install["root"], config_dir=install["cfg"])
    r = lookup_part("NAU7802", install["root"], libraries=ls)
    assert r["found"]
    assert r["lib_id"] == "Custom:NAU7802"
    assert r["from_user_library"] is True
    assert r["footprint_exists"]  # Package_SO:SOIC-8 from the built-in pretty


def test_lookup_part_builtin_not_flagged(install):
    ls = build_library_set(install["root"], config_dir=install["cfg"])
    r = lookup_part("R", install["root"], libraries=ls)
    assert r["found"]
    assert r["lib_id"] == "Device:R"
    assert r["from_user_library"] is False
