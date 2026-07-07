import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CTX_SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
if CTX_SCRIPTS not in sys.path:
    sys.path.insert(0, CTX_SCRIPTS)

import _paths  # noqa: E402,F401  — puts the shared kicad-schematic-gen scripts on sys.path
