#!/usr/bin/env python3
"""import_lib.py — Import a KiCad part bundle (zip) into the local KiCad install.

Given a vendor zip (SnapEDA, Ultra Librarian, Component Search Engine/Samacsys,
or a manufacturer download), this script:

  1. Extracts the zip to a temp dir and classifies its contents
     (.kicad_sym symbols, .kicad_mod footprints, .step/.stp/.wrl 3D models).
  2. Merges each symbol into a single consolidated symbol library (default: the
     user's existing "Custom" library), skipping symbols already present.
  3. Copies footprints into a consolidated .pretty folder.
  4. Copies 3D models into a consolidated .3dshapes folder and rewrites the
     copied footprints' (model ...) paths to point at the installed files.
  5. Registers the symbol/footprint libraries in the global sym-lib-table /
     fp-lib-table (idempotent — never duplicates an existing nickname).

Pure stdlib, no KiCad install required. Restart KiCad (or re-scan libraries)
after running so the new parts appear.

Usage:
  python import_lib.py <bundle.zip> [--name NICK]
                       [--symbol-lib PATH] [--footprint-dir DIR] [--model-dir DIR]
                       [--config-dir DIR] [--dry-run] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field

# --- User defaults (override on the CLI) ------------------------------------
# These match this install's existing convention: a consolidated "Custom"
# symbol library already registered in sym-lib-table, plus sibling folders for
# footprints and 3D models under the KiCad documents tree.
DEFAULTS = {
    "symbol_lib": r"D:\Documents\KiCad\8.0\symbols\Library.kicad_sym",
    "symbol_nick": "Custom",
    "footprint_dir": r"D:\Documents\KiCad\8.0\footprints\Custom.pretty",
    "footprint_nick": "Custom",
    "model_dir": r"D:\Documents\KiCad\8.0\3dmodels\Custom.3dshapes",
}

SYM_HEADER = (
    "(kicad_symbol_lib\n"
    "\t(version 20231120)\n"
    '\t(generator "kicad_symbol_editor")\n'
    '\t(generator_version "8.0")\n'
)

MODEL_EXTS = (".step", ".stp", ".wrl", ".wings")
PREFERRED_MODEL_ORDER = {".step": 0, ".stp": 1, ".wrl": 2, ".wings": 3}


# --- Result bookkeeping ------------------------------------------------------
@dataclass
class Report:
    bundle: str = ""
    symbols_added: list[str] = field(default_factory=list)
    symbols_skipped: list[str] = field(default_factory=list)
    footprints_added: list[str] = field(default_factory=list)
    footprints_skipped: list[str] = field(default_factory=list)
    models_added: list[str] = field(default_factory=list)
    model_paths_rewritten: list[str] = field(default_factory=list)
    tables_updated: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors


# --- S-expression symbol parsing --------------------------------------------
def parse_lib_symbols(text: str):
    """Yield (name, block_text) for each TOP-LEVEL symbol in a kicad_symbol_lib.

    Depth-aware and string-aware so nested unit symbols (depth 3+) and parens
    inside quoted strings don't confuse the scan.
    """
    out = []
    i, n = 0, len(text)
    depth = 0
    in_str = esc = False
    block_start = None
    name_re = re.compile(r'\(\s*symbol\s+"((?:[^"\\]|\\.)*)"')
    while i < n:
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
        elif c == "(":
            depth += 1
            if depth == 2 and re.match(r"\(\s*symbol\b", text[i:]):
                block_start = i
        elif c == ")":
            if depth == 2 and block_start is not None:
                block = text[block_start:i + 1]
                m = name_re.match(block)
                out.append((m.group(1) if m else None, block))
                block_start = None
            depth -= 1
        i += 1
    return out


def merge_symbols(target: str, new_blocks, report: Report, dry: bool):
    if not new_blocks:
        return
    existing_text = ""
    existing_names = set()
    if os.path.isfile(target):
        with open(target, encoding="utf-8") as f:
            existing_text = f.read()
        existing_names = {nm for nm, _ in parse_lib_symbols(existing_text) if nm}

    to_add = []
    for name, block in new_blocks:
        if name and name in existing_names:
            report.symbols_skipped.append(name)
        else:
            to_add.append((name, block))
            if name:
                existing_names.add(name)
            report.symbols_added.append(name or "<unnamed>")

    if not to_add or dry:
        return

    os.makedirs(os.path.dirname(target), exist_ok=True)
    if existing_text.strip():
        idx = existing_text.rstrip().rfind(")")
        head = existing_text[:idx].rstrip("\n") + "\n"
        body = "".join("\t" + b.strip() + "\n" for _, b in to_add)
        new_text = head + body + ")\n"
    else:
        body = "".join("\t" + b.strip() + "\n" for _, b in to_add)
        new_text = SYM_HEADER + body + ")\n"
    _backup(target)
    with open(target, "w", encoding="utf-8") as f:
        f.write(new_text)


# --- Footprint + 3D model handling ------------------------------------------
def install_models(models, model_dir: str, report: Report, dry: bool):
    """Copy 3D models; return {stem_lower: installed_abs_path} (prefer .step)."""
    stem_map = {}
    for src in models:
        stem = os.path.splitext(os.path.basename(src))[0].lower()
        ext = os.path.splitext(src)[1].lower()
        dest = os.path.join(model_dir, os.path.basename(src))
        if not dry:
            os.makedirs(model_dir, exist_ok=True)
            shutil.copy2(src, dest)
        report.models_added.append(os.path.basename(src))
        prev = stem_map.get(stem)
        if prev is None or PREFERRED_MODEL_ORDER.get(ext, 9) < PREFERRED_MODEL_ORDER.get(
            os.path.splitext(prev)[1].lower(), 9
        ):
            stem_map[stem] = os.path.abspath(dest)
    return stem_map


def install_footprints(footprints, fp_dir: str, model_map, report: Report, dry: bool):
    for src in footprints:
        base = os.path.basename(src)
        dest = os.path.join(fp_dir, base)
        if os.path.isfile(dest):
            report.footprints_skipped.append(base)
            continue
        report.footprints_added.append(base)
        if dry:
            continue
        os.makedirs(fp_dir, exist_ok=True)
        with open(src, encoding="utf-8") as f:
            text = f.read()
        text, n = _rewrite_model_paths(text, model_map)
        if n:
            report.model_paths_rewritten.append(base)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(text)


def _rewrite_model_paths(text: str, model_map):
    count = 0

    def repl(m):
        nonlocal count
        ref = m.group(1)
        stem = os.path.splitext(os.path.basename(ref.replace("\\", "/")))[0].lower()
        if stem in model_map:
            count += 1
            return '(model "' + model_map[stem].replace("\\", "/") + '"'
        return m.group(0)

    return re.sub(r'\(model\s+"([^"]*)"', repl, text), count


# --- Library table registration ---------------------------------------------
def register_lib(table_path: str, kind: str, nick: str, uri: str, report: Report, dry: bool):
    """kind is 'sym' or 'fp'. Idempotent on nickname."""
    if not os.path.isfile(table_path):
        report.warnings.append(f"library table not found, skipping registration: {table_path}")
        return
    with open(table_path, encoding="utf-8") as f:
        text = f.read()
    if re.search(r'\(lib\s+\(name\s+"' + re.escape(nick) + r'"', text):
        return  # already registered
    uri_fwd = uri.replace("\\", "/")
    descr = "Imported parts (kicad-import-lib)"
    entry = f'  (lib (name "{nick}")(type "KiCad")(uri "{uri_fwd}")(options "")(descr "{descr}"))\n'
    idx = text.rstrip().rfind(")")
    new_text = text[:idx].rstrip("\n") + "\n" + entry + ")\n"
    report.tables_updated.append(os.path.basename(table_path) + f" (+{nick})")
    if dry:
        return
    _backup(table_path)
    with open(table_path, "w", encoding="utf-8") as f:
        f.write(new_text)


# --- Bundle classification ---------------------------------------------------
def classify(root: str, report: Report):
    symbols, footprints, models, legacy = [], [], [], []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            ext = os.path.splitext(fn)[1].lower()
            if ext == ".kicad_sym":
                symbols.append(p)
            elif ext == ".kicad_mod":
                footprints.append(p)
            elif ext in MODEL_EXTS:
                models.append(p)
            elif ext == ".lib":
                legacy.append(p)
    if legacy and not symbols:
        report.warnings.append(
            "bundle contains only legacy KiCad-5 .lib symbol(s); no .kicad_sym found. "
            "Convert with KiCad's Symbol Editor (File > Save As .kicad_sym) or "
            "`kicad-cli sym upgrade`, then re-run. Legacy files: "
            + ", ".join(os.path.basename(p) for p in legacy)
        )
    return symbols, footprints, models


def _backup(path: str):
    if os.path.isfile(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass


def detect_config_dir() -> str | None:
    candidates = []
    if sys.platform.startswith("win"):
        base = os.path.join(os.environ.get("APPDATA", ""), "kicad")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Preferences/kicad")
    else:
        base = os.path.expanduser("~/.config/kicad")
    if os.path.isdir(base):
        for d in os.listdir(base):
            full = os.path.join(base, d)
            if os.path.isfile(os.path.join(full, "sym-lib-table")):
                candidates.append((d, full))
    if not candidates:
        return None
    # highest version dir wins
    candidates.sort(key=lambda kv: kv[0], reverse=True)
    return candidates[0][1]


# --- Main --------------------------------------------------------------------
def run(args) -> Report:
    report = Report(bundle=os.path.basename(args.zip), dry_run=args.dry_run)

    if not os.path.isfile(args.zip):
        report.errors.append(f"zip not found: {args.zip}")
        return report
    if not zipfile.is_zipfile(args.zip):
        report.errors.append(f"not a valid zip file: {args.zip}")
        return report

    symbol_lib = args.symbol_lib or DEFAULTS["symbol_lib"]
    footprint_dir = args.footprint_dir or DEFAULTS["footprint_dir"]
    model_dir = args.model_dir or DEFAULTS["model_dir"]
    symbol_nick = args.name or DEFAULTS["symbol_nick"]
    footprint_nick = args.name or DEFAULTS["footprint_nick"]
    config_dir = args.config_dir or detect_config_dir()

    with tempfile.TemporaryDirectory(prefix="kicad_import_") as tmp:
        try:
            with zipfile.ZipFile(args.zip) as zf:
                zf.extractall(tmp)
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"failed to extract zip: {e}")
            return report

        symbols, footprints, models = classify(tmp, report)
        if not (symbols or footprints or models):
            report.errors.append("bundle contained no .kicad_sym, .kicad_mod, or 3D model files")
            return report

        # 1) symbols
        new_blocks = []
        for sp in symbols:
            with open(sp, encoding="utf-8", errors="replace") as f:
                new_blocks.extend(parse_lib_symbols(f.read()))
        merge_symbols(symbol_lib, new_blocks, report, args.dry_run)

        # 2) models, then 3) footprints (rewrite model paths against installed models)
        model_map = install_models(models, model_dir, report, args.dry_run)
        install_footprints(footprints, footprint_dir, model_map, report, args.dry_run)

        # 4) register libraries
        if config_dir:
            if report.symbols_added:
                register_lib(
                    os.path.join(config_dir, "sym-lib-table"), "sym",
                    symbol_nick, symbol_lib, report, args.dry_run,
                )
            if report.footprints_added:
                register_lib(
                    os.path.join(config_dir, "fp-lib-table"), "fp",
                    footprint_nick, footprint_dir, report, args.dry_run,
                )
        else:
            report.warnings.append(
                "could not locate KiCad config dir; libraries copied but not registered. "
                "Pass --config-dir or add them manually in KiCad's library manager."
            )

    return report


def format_human(r: Report) -> str:
    lines = []
    tag = " (dry-run)" if r.dry_run else ""
    lines.append(f"Import: {r.bundle}{tag}")
    if r.symbols_added:
        lines.append(f"  Symbols added:    {', '.join(r.symbols_added)}")
    if r.symbols_skipped:
        lines.append(f"  Symbols present:  {', '.join(r.symbols_skipped)}")
    if r.footprints_added:
        lines.append(f"  Footprints added: {', '.join(r.footprints_added)}")
    if r.footprints_skipped:
        lines.append(f"  Footprints present: {', '.join(r.footprints_skipped)}")
    if r.models_added:
        lines.append(f"  3D models added:  {', '.join(r.models_added)}")
    if r.model_paths_rewritten:
        lines.append(f"  Model paths fixed in: {', '.join(r.model_paths_rewritten)}")
    if r.tables_updated:
        lines.append(f"  Lib tables updated: {', '.join(r.tables_updated)}")
    for w in r.warnings:
        lines.append(f"  WARN: {w}")
    for e in r.errors:
        lines.append(f"  ERROR: {e}")
    if r.ok and not r.dry_run:
        lines.append("  Done. Restart KiCad (or re-scan libraries) to see the new parts.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Import a KiCad part bundle (zip) into the local install.")
    ap.add_argument("zip", help="path to the vendor zip bundle")
    ap.add_argument("--name", help="library nickname for symbols+footprints (default: Custom)")
    ap.add_argument("--symbol-lib", help="consolidated .kicad_sym file to merge into")
    ap.add_argument("--footprint-dir", help="consolidated .pretty footprint dir")
    ap.add_argument("--model-dir", help="consolidated .3dshapes model dir")
    ap.add_argument("--config-dir", help="KiCad config dir holding sym-lib-table/fp-lib-table")
    ap.add_argument("--dry-run", action="store_true", help="report actions without writing")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    report = run(args)
    if args.json:
        print(json.dumps(report.__dict__, indent=2))
    else:
        print(format_human(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
