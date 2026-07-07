"""_paths.py — path shim for the kicad-board-context ingest scripts.

The shared engines (S-expression parser, netlist extractor, cross-checkers,
PCB parser) live in the sibling kicad-schematic-gen skill's scripts/ directory,
which is the single home of all shared code (see CLAUDE.md). Importing this
module puts that directory on sys.path so ingest scripts can do e.g.:

    import _paths  # noqa: F401
    from validate_kicad_sch import load_kicad_sch, extract_netlist

Do NOT copy shared modules into this skill — shared improvements (e.g. the
loader library-fallback) land in kicad-schematic-gen/scripts where both skills
get them.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
SHARED_SCRIPTS = os.path.normpath(
    os.path.join(_HERE, "..", "..", "kicad-schematic-gen", "scripts"))

if SHARED_SCRIPTS not in sys.path:
    sys.path.insert(0, SHARED_SCRIPTS)
