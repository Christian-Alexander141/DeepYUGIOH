"""Central location of all project paths."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
THIRD_PARTY = ROOT / "third_party"
CARD_SCRIPTS = THIRD_PARTY / "CardScripts"
BABEL_CDB = THIRD_PARTY / "BabelCDB"
DATA = ROOT / "data"

# Script folders searched in order; the first hit wins.
SCRIPT_DIRS = [
    CARD_SCRIPTS,
    CARD_SCRIPTS / "official",
    CARD_SCRIPTS / "pre-release",
]


def ocgcore_library() -> Path:
    env = os.environ.get("DEEPYUGIOH_OCGCORE")
    if env:
        return Path(env)
    return ROOT / "build" / "ocgcore" / "libocgcore.so"


def card_db() -> Path:
    """Local card database created by `deepyugioh import-cards`."""
    return DATA / "cards.sqlite"


def combo_db() -> Path:
    """Database 1 - Combo Repository."""
    return DATA / "combos.sqlite"
