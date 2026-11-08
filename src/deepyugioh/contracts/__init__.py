"""Shared contracts between DeepYuGiOh modules.

Modules depend on these types and protocols only - never on each other's
internals. Changing anything here is an interface change: bump
`SCHEMA_VERSION` in combos.py when the stored combo format changes, and
update docs/schnittstellen.md.
"""

from deepyugioh.contracts.cards import (
    CardInfo,
    CardPool,
    EnablerRole,
    Interaction,
    InteractionGrade,
    InteractionGraph,
)
from deepyugioh.contracts.combos import (
    BENEFIT_CRITERIA,
    SCHEMA_VERSION,
    ComboReader,
    ComboRecord,
    ComboSink,
    LineStep,
    RunRecord,
    StoreResult,
)
from deepyugioh.contracts.setups import EMPTY_BOARD, Candidate, OpponentBoard, OpponentCard, StartSetup

__all__ = [
    "BENEFIT_CRITERIA", "EMPTY_BOARD", "SCHEMA_VERSION", "Candidate", "CardInfo", "CardPool",
    "ComboReader", "ComboRecord", "ComboSink", "EnablerRole", "Interaction", "InteractionGrade",
    "InteractionGraph", "LineStep", "OpponentBoard", "OpponentCard", "RunRecord", "StartSetup",
    "StoreResult",
]
