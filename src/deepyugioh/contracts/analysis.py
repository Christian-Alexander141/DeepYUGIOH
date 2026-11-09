"""Contract: Analyzer System (Stufe 2) -> Database 2 -> Quality Filter / Deck Builder.

An analyzer looks at one combo from Database 1 and returns the facts of one
*axis* (outcome, cost, consistency, ...): raw values, one scalar where higher
is better, and a short explanation. The Analyzer System stores them in
Database 2 and turns the scalar into a 0-100 score (percentile over all
analysed combos), the same way for every axis.

Database 1 is only read. Combos are addressed by their signature, which is
stable across rebuilds of Database 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol, runtime_checkable

from deepyugioh.contracts.combos import ComboRecord

AXES = {
    "outcome": "Ergebnis: Endfeld, Störungen, Kartenvorteil, Verluste des Gegners",
    "cost": "Einsatz: benötigte Karten, Handkarten, Aktionen, LP, Normalbeschwörung",
    "consistency": "Konsistenz: Wahrscheinlichkeit, die nötigen Handkarten zu ziehen (mit Combo-Familie)",
    "genericness": "Deckspezifität: wie allgemein die Combo einsetzbar ist (100 = in jedem Deck, 0 = nur in einem engen Kontext)",
}


@dataclass(frozen=True)
class AxisResult:
    raw: dict                  # measurable facts, JSON-serialisable
    value: float               # one scalar for this axis, higher = better (basis of the 0-100 score)
    explanation: str           # short German text for the deck builder
    profile: dict = field(default_factory=dict)   # extra per-combo facts to keep (e.g. the family)


@runtime_checkable
class Analyzer(Protocol):
    name: str          # axis name, e.g. "outcome"
    version: str       # bump whenever results can change; old results are recomputed
    global_: bool      # True if a combo's result depends on other combos (always recomputed)

    def prepare(self, combos: Iterable[ComboRecord]) -> None:
        """Called once with all combos before analyse (for analyzers that need global context)."""
        ...

    def analyze(self, combo: ComboRecord) -> AxisResult: ...


@dataclass(frozen=True)
class AxisScore:
    axis: str
    version: str
    raw: dict
    value: float
    score: float | None        # 0-100 percentile, None until normalised
    explanation: str


@runtime_checkable
class AnalysisReader(Protocol):
    def profile(self, signature: str) -> dict[str, AxisScore]:
        """All axes of one combo."""
        ...

    def top(self, axis: str, limit: int = 50, min_score: float | None = None) -> list[tuple[str, AxisScore]]:
        """(signature, score) with the best scores on one axis."""
        ...


__all__ = ["AXES", "Analyzer", "AnalysisReader", "AxisResult", "AxisScore"]
