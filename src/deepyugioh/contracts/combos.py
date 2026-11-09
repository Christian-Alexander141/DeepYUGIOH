"""Contract: Combo Generator -> Database 1 (Combo Repository) -> Analyzers.

`ComboRecord` and `RunRecord` are the only data that leave the generator.
They are plain, JSON-serialisable and versioned (`SCHEMA_VERSION`).
Writers implement `ComboSink`, readers (the Analyzer System) use `ComboReader`.

A ComboRecord states *what is possible* - never how good it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Protocol, Sequence, runtime_checkable

from deepyugioh.contracts.setups import StartSetup

SCHEMA_VERSION = 2

BENEFIT_CRITERIA = ("lp_gain", "opp_lp_loss", "hand_delta", "field_delta", "opp_field_loss", "opp_hand_loss")


@dataclass(frozen=True)
class LineStep:
    decision: str   # engine message, e.g. MSG_SELECT_IDLECMD
    label: str      # human readable choice
    response: str   # hex encoded engine response - replays the step

    def to_dict(self) -> dict:
        return {"decision": self.decision, "label": self.label, "response": self.response}


@dataclass(frozen=True)
class ComboRecord:
    signature: str                  # identity: required cards + end board (setup independent)
    hand: tuple[int, ...]           # cards needed in the opening hand
    deck: tuple[int, ...]           # cards taken from the main deck
    extra: tuple[int, ...]          # cards taken from the extra deck
    benefit: dict                   # raw deltas: BENEFIT_CRITERIA + card_advantage + wins
    positive: tuple[str, ...]       # benefit criteria that are > 0
    end_board: dict                 # both players' visible state after the line
    turn_ended: bool                # line includes the end phase
    line: tuple[LineStep, ...]      # replayable action line
    setup: StartSetup               # the setup the line replays in
    validated: bool                 # replayed independently with identical result
    schema_version: int = SCHEMA_VERSION
    id: int | None = field(default=None, compare=False)

    @property
    def cards(self) -> tuple[int, ...]:
        return tuple(sorted(self.hand + self.deck + self.extra))

    @property
    def card_count(self) -> int:
        return len(self.hand) + len(self.deck) + len(self.extra)


@dataclass(frozen=True)
class RunRecord:
    """One search over one start setup - stored for provenance and coverage."""

    setup: StartSetup
    candidate: str | None           # candidate id, None for manual searches
    tier: int | None
    limits: dict
    nodes: int
    replays: int
    elapsed_s: float
    complete: bool                  # whole space inside the limits explored
    incomplete: dict                # reason -> count (also neighbourhood truncation)
    pruned: dict                    # pruning rule -> count
    combos_found: int
    generator_version: str
    engine_commit: str
    scripts_commit: str
    cdb_commit: str
    graph_version: str | None = None


@dataclass(frozen=True)
class StoreResult:
    run_id: int
    new_combos: int
    new_signatures: tuple[str, ...] = ()   # signatures of the combos that were new in Database 1


@runtime_checkable
class ComboSink(Protocol):
    def store_run(self, run: RunRecord, combos: Sequence[ComboRecord]) -> StoreResult:
        """Persist a run and its validated combos atomically. Known combos are linked, not duplicated."""
        ...


@runtime_checkable
class ComboReader(Protocol):
    def get(self, combo_id: int) -> ComboRecord | None: ...

    def iter_combos(
        self,
        *,
        card: int | None = None,
        max_cards: int | None = None,
        positive: str | None = None,
        limit: int | None = None,
    ) -> Iterator[ComboRecord]:
        """Combos ordered by card count, then line length."""
        ...

    def count(self) -> int: ...
