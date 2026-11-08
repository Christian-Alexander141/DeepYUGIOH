"""Contract: start setups (Startaufbauten).

A start setup is the concrete game state a single search starts from:
which cards player 0 has in hand, deck and extra deck, what the opponent
has, and the rules. The engine can only execute concrete states, so the
global search is split into many searches over many start setups.

Card lists are stored sorted: the order in which cards are given never
changes the setup, its id, or the replay of a line.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

ZONES = ("mzone", "szone", "hand")
POSITIONS = ("faceup_attack", "faceup_defense", "facedown_defense", "facedown")


@dataclass(frozen=True)
class OpponentCard:
    code: int
    zone: str = "mzone"
    position: str = "faceup_attack"

    def __post_init__(self):
        if self.zone not in ZONES:
            raise ValueError(f"zone must be one of {ZONES}")
        if self.position not in POSITIONS:
            raise ValueError(f"position must be one of {POSITIONS}")

    def to_dict(self) -> dict:
        return {"code": self.code, "zone": self.zone, "position": self.position}


@dataclass(frozen=True)
class OpponentBoard:
    """The opponent's cards. They never act (no effects are activated by the opponent)."""

    name: str = "empty"
    cards: tuple[OpponentCard, ...] = ()

    def to_dict(self) -> dict:
        return {"name": self.name, "cards": [c.to_dict() for c in self.cards]}

    @classmethod
    def from_dict(cls, d: dict) -> "OpponentBoard":
        return cls(d["name"], tuple(OpponentCard(**c) for c in d["cards"]))


EMPTY_BOARD = OpponentBoard()


@dataclass(frozen=True)
class StartSetup:
    hand: tuple[int, ...]
    deck: tuple[int, ...] = ()
    extra: tuple[int, ...] = ()
    opponent: OpponentBoard = EMPTY_BOARD
    seed: tuple[int, int, int, int] = (1, 2, 3, 4)
    starting_lp: int = 8000
    master_rule: int = 5
    branch_zones: bool = False  # branch over monster zones (needed for Link arrows)

    def __post_init__(self):
        for name in ("hand", "deck", "extra"):
            object.__setattr__(self, name, tuple(sorted(getattr(self, name))))
        if self.master_rule != 5:
            raise ValueError("only Master Rule 5 is supported")

    @property
    def id(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()[:20]

    def to_dict(self) -> dict:
        return {
            "hand": list(self.hand), "deck": list(self.deck), "extra": list(self.extra),
            "opponent": self.opponent.to_dict(), "seed": list(self.seed),
            "starting_lp": self.starting_lp, "master_rule": self.master_rule,
            "branch_zones": self.branch_zones,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StartSetup":
        return cls(
            hand=tuple(d["hand"]), deck=tuple(d.get("deck", ())), extra=tuple(d.get("extra", ())),
            opponent=OpponentBoard.from_dict(d["opponent"]) if "opponent" in d else EMPTY_BOARD,
            seed=tuple(d.get("seed", (1, 2, 3, 4))), starting_lp=d.get("starting_lp", 8000),
            master_rule=d.get("master_rule", 5), branch_zones=d.get("branch_zones", False),
        )


@dataclass(frozen=True)
class Candidate:
    """A start setup proposed by a candidate source, plus why it was proposed."""

    setup: StartSetup
    tier: int                      # processing priority, lower first
    origin: dict = field(default_factory=dict, compare=False, hash=False)
    truncated: tuple[str, ...] = ()  # neighbourhood parts that were cut (-> run is incomplete)

    @property
    def id(self) -> str:
        return self.setup.id
