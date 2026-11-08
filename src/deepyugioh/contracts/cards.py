"""Contract: Card Knowledge System -> other modules.

Everything the Combo Generator needs to know about cards comes through these
two interfaces. The generator never reads the card database directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Protocol, runtime_checkable

TYPE_MONSTER = 0x1
TYPE_EXTRA = 0x40 | 0x2000 | 0x800000 | 0x4000000  # fusion | synchro | xyz | link


@dataclass(frozen=True)
class CardInfo:
    """Engine-relevant card data (as in cards.cdb)."""

    code: int
    alias: int
    name: str
    type: int
    level: int
    lscale: int
    rscale: int
    attribute: int
    race: int
    atk: int
    def_: int
    link_marker: int
    setcodes: tuple[int, ...]
    is_alt_art: bool
    script: str | None

    @property
    def is_extra_deck(self) -> bool:
        return bool(self.type & TYPE_MONSTER) and bool(self.type & TYPE_EXTRA)


@runtime_checkable
class CardPool(Protocol):
    def get(self, code: int) -> CardInfo | None: ...

    def resolve(self, ref: str | int) -> CardInfo:
        """Card code or exact English name; raises KeyError if unknown."""
        ...

    def name(self, code: int) -> str: ...


class InteractionGrade(str, Enum):
    """How specific an interaction is - also the processing priority."""

    NAMED = "NAMED"              # A names B by card code
    RESTRICTED = "RESTRICTED"    # A targets B's archetype/code
    CONSTRAINED = "CONSTRAINED"  # A is generic but filtered (level, type, attribute ...)
    GENERIC = "GENERIC"          # A targets anything of the right card type


class EnablerRole(str, Enum):
    """Generic helper roles that are added to a start setup when its cards need them."""

    SEND_GY_FROM_DECK = "SEND_GY_FROM_DECK"
    SPSUMMON_FROM_GY = "SPSUMMON_FROM_GY"
    BANISH_FROM_GY = "BANISH_FROM_GY"


@dataclass(frozen=True)
class Interaction:
    source: int
    target: int
    kind: str  # NAMED | SEND_GY | ADD_HAND | SPECIAL_SUMMON | BANISH
    grade: InteractionGrade


@runtime_checkable
class InteractionGraph(Protocol):
    def version(self) -> str:
        """Changes whenever the graph changes; candidate order is only stable within one version."""
        ...

    def starters(self) -> list[int]:
        """All cards that may start a combo."""
        ...

    def interactions(self) -> list[Interaction]:
        """All NAMED and RESTRICTED interactions in a fixed order."""
        ...

    def iter_generic(self, grade: InteractionGrade) -> Iterable[Interaction]:
        """CONSTRAINED or GENERIC interactions in a fixed order (lazy: may be millions)."""
        ...

    def neighbors(self, code: int) -> list[Interaction]:
        """NAMED/RESTRICTED interactions touching `code`."""
        ...

    def related_extra(self, code: int) -> list[int]:
        """Extra deck monsters related to `code`, most specific first."""
        ...

    def needs(self, code: int) -> set[EnablerRole]: ...

    @property
    def enablers(self) -> dict[EnablerRole, int]: ...
