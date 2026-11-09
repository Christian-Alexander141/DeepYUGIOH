"""Contract: structured effect descriptions (Effect Signatures) for algorithmic discovery.

An EffectSignature describes one effect of one card in terms a planner can
reason about: where the card must be, what triggers it, which cards it moves
from where to where (as cost or as effect), and how often it can be used.

Everything here is extracted statically (Lua scripts) and therefore a
*hypothesis*: parts that could not be interpreted are listed in `unknown`
and lower `confidence`. Matching is generous - an unknown predicate never
rules a card out. The engine stays the only authority on what works.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Protocol, runtime_checkable

from deepyugioh.contracts.cards import CardInfo

# Abstract zones of player 0 (the planner never needs the opponent's zones in detail)
ZONES = ("HAND", "DECK", "EXTRA", "GRAVE", "BANISHED", "MZONE", "SZONE")
UNKNOWN = "UNKNOWN"

TYPE_MONSTER, TYPE_SPELL, TYPE_TRAP = 0x1, 0x2, 0x4
KINDS = {"MONSTER": TYPE_MONSTER, "SPELL": TYPE_SPELL, "TRAP": TYPE_TRAP}


def _setcode_matches(card_setcodes, wanted: int) -> bool:
    """Same semantics as the core: base archetype (low 12 bits) + sub-archetype bits."""
    base, sub = wanted & 0xFFF, wanted & 0xF000
    return any((sc & 0xFFF) == base and (sc & 0xF000 & sub) == sub for sc in card_setcodes)


@dataclass(frozen=True)
class CardFilter:
    """A conjunction (AND) of simple card properties. Empty fields mean "any".

    `unknown` lists predicates that were not understood; they never exclude a card.
    """

    kinds: frozenset[str] = frozenset()     # MONSTER / SPELL / TRAP - any of them
    type_bits: int = 0                       # TYPE_* bits that must all be set (TUNER, NORMAL, ...)
    codes: frozenset[int] = frozenset()      # one of these card codes
    setcodes: frozenset[int] = frozenset()   # one of these archetypes
    min_level: int | None = None
    max_level: int | None = None
    races: int = 0                           # bitmask, any of them
    attributes: int = 0                      # bitmask, any of them
    max_atk: int | None = None
    min_atk: int | None = None
    max_def: int | None = None
    min_def: int | None = None
    exclude_self: bool = False               # "except this card's name"
    unknown: tuple[str, ...] = ()

    @property
    def specific(self) -> bool:
        """Names cards or archetypes (as opposed to generic property filters)."""
        return bool(self.codes or self.setcodes)

    @property
    def is_any(self) -> bool:
        return self == CardFilter(unknown=self.unknown)

    def matches(self, card: CardInfo, own_code: int | None = None) -> bool:
        if self.exclude_self and own_code is not None and card.code == own_code:
            return False
        if self.kinds and not any(card.type & KINDS[k] for k in self.kinds):
            return False
        if self.type_bits and (card.type & self.type_bits) != self.type_bits:
            return False
        if self.codes and card.code not in self.codes and card.alias not in self.codes:
            return False
        if self.setcodes and not any(_setcode_matches(card.setcodes, s) for s in self.setcodes):
            return False
        numeric = (self.min_level, self.max_level, self.races, self.attributes,
                   self.max_atk, self.min_atk, self.max_def, self.min_def)
        if any(v for v in numeric if v is not None):
            if not card.type & TYPE_MONSTER:
                return False  # level / race / attribute / ATK filters only accept monsters
            if self.min_level is not None and card.level < self.min_level:
                return False
            if self.max_level is not None and card.level > self.max_level:
                return False
            if self.races and not card.race & self.races:
                return False
            if self.attributes and not card.attribute & self.attributes:
                return False
            if self.max_atk is not None and card.atk > self.max_atk:
                return False
            if self.min_atk is not None and card.atk < self.min_atk:
                return False
            if self.max_def is not None and card.def_ > self.max_def:
                return False
            if self.min_def is not None and card.def_ < self.min_def:
                return False
        return True

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if v not in (None, 0, False, (), frozenset())}
        for k in ("kinds", "codes", "setcodes"):
            if k in d:
                d[k] = sorted(d[k])
        if "unknown" in d:
            d["unknown"] = list(d["unknown"])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CardFilter":
        d = dict(d)
        for k in ("kinds", "codes", "setcodes"):
            if k in d:
                d[k] = frozenset(d[k])
        if "unknown" in d:
            d["unknown"] = tuple(d["unknown"])
        return cls(**d)


@dataclass(frozen=True)
class Condition:
    """Disjunction (OR) of filters: a card qualifies if it matches any alternative.

    `any_of == ()` means "any card" (no filter). Alternatives are kept, so a
    combo never has to satisfy all ways at once.
    """

    any_of: tuple[CardFilter, ...] = ()

    def matches(self, card: CardInfo, own_code: int | None = None) -> bool:
        return not self.any_of or any(f.matches(card, own_code) for f in self.any_of)

    @property
    def specific(self) -> bool:
        return bool(self.any_of) and all(f.specific for f in self.any_of)

    @property
    def unknown(self) -> tuple[str, ...]:
        return tuple(u for f in self.any_of for u in f.unknown)

    def to_dict(self) -> list:
        return [f.to_dict() for f in self.any_of]

    @classmethod
    def from_dict(cls, d: list) -> "Condition":
        return cls(tuple(CardFilter.from_dict(x) for x in d))


@dataclass(frozen=True)
class Move:
    """Cards go from one of `src` to `dst`.

    `what is None` means the card itself. `role` is "cost" or "effect".
    `dst` may be UNKNOWN; `src` may be empty if it could not be determined.
    """

    what: Condition | None
    src: tuple[str, ...]
    dst: str
    role: str = "effect"
    count: int = 1
    opponent: bool = False      # moves the opponent's cards (src/dst are the opponent's zones)
    targets: bool = False       # chosen with SelectTarget (targeting)

    @property
    def is_self(self) -> bool:
        return self.what is None

    def to_dict(self) -> dict:
        d = {"src": list(self.src), "dst": self.dst, "role": self.role}
        if self.what is not None:
            d["what"] = self.what.to_dict()
        if self.count != 1:
            d["count"] = self.count
        if self.opponent:
            d["opponent"] = True
        if self.targets:
            d["targets"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Move":
        what = Condition.from_dict(d["what"]) if "what" in d else None
        return cls(what, tuple(d["src"]), d["dst"], d.get("role", "effect"), d.get("count", 1),
                   d.get("opponent", False), d.get("targets", False))


@dataclass(frozen=True)
class Trigger:
    """What starts a trigger effect. `self_only`: the event must happen to this card."""

    event: str          # TO_GRAVE | SUMMON | SPSUMMON | FLIP | REMOVE | TO_HAND | BE_MATERIAL | DESTROYED | other EVENT_*
    self_only: bool
    mandatory: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Lock:
    """A restriction an effect puts on its player, e.g. "you cannot Special Summon, except Dragon monsters".

    kind      SPECIAL_SUMMON | NORMAL_SUMMON | ACTIVATE
    scope     all | extra (only summons from the Extra Deck) | main (only summons not from the Extra Deck)
              | <summon type> (e.g. PENDULUM) | field (while the card is face-up on the field)
    allowed   cards that are still allowed (None if not known that way)
    forbidden cards that are forbidden (None if not known that way); both None and not unknown = everything
    whole_turn  the restriction also covers the turn *before* the activation (activity counter)
    """

    kind: str
    scope: str = "all"
    allowed: Condition | None = None
    forbidden: Condition | None = None
    whole_turn: bool = False
    unknown: bool = False
    source: str = ""            # Lua text of the restriction, for explanations

    @property
    def forbids_all(self) -> bool:
        return self.allowed is None and self.forbidden is None and not self.unknown

    def allows(self, card: CardInfo, own_code: int | None = None) -> bool | None:
        """True / False, or None if the restriction could not be read."""
        if self.unknown:
            return None
        if self.allowed is not None:
            return self.allowed.matches(card, own_code)
        if self.forbidden is not None:
            return not self.forbidden.matches(card, own_code)
        return False

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "scope": self.scope}
        if self.allowed is not None:
            d["allowed"] = self.allowed.to_dict()
        if self.forbidden is not None:
            d["forbidden"] = self.forbidden.to_dict()
        if self.whole_turn:
            d["whole_turn"] = True
        if self.unknown:
            d["unknown"] = True
        if self.source:
            d["source"] = self.source
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Lock":
        return cls(d["kind"], d.get("scope", "all"),
                   Condition.from_dict(d["allowed"]) if "allowed" in d else None,
                   Condition.from_dict(d["forbidden"]) if "forbidden" in d else None,
                   d.get("whole_turn", False), d.get("unknown", False), d.get("source", ""))


@dataclass(frozen=True)
class EffectSignature:
    effect_id: str                      # "<code>:<idx>"
    card: int
    kind: str                           # ACTIVATE | IGNITION | QUICK | TRIGGER | SUMMON_PROC | CONTINUOUS | OTHER
    location: tuple[str, ...]           # where the card must be to use the effect
    trigger: Trigger | None = None
    conditions: tuple[str, ...] = ()    # recognised preconditions, e.g. "PREVIOUS:ONFIELD"; unrecognised -> UNKNOWN:<fn>
    costs: tuple[Move, ...] = ()
    moves: tuple[Move, ...] = ()
    draws: int = 0
    lp_cost: int = 0
    count_limit: str | None = None      # OPT | HOPT | SHARED:<key> | None
    categories: tuple[str, ...] = ()
    restrictions: tuple[str, ...] = ()  # e.g. NEGATES_SUMMONED, SUMMON_LOCK
    unknown: tuple[str, ...] = ()       # parts that were not interpreted
    source: str = "lua-static"
    confidence: float = 1.0
    locks: tuple[Lock, ...] = ()        # restrictions this effect puts on its player

    @property
    def usable(self) -> bool:
        """Something a planner can activate (not a continuous / passive effect)."""
        return self.kind in ("ACTIVATE", "IGNITION", "QUICK", "TRIGGER", "SUMMON_PROC")

    def all_moves(self) -> tuple[Move, ...]:
        return self.costs + self.moves

    def to_dict(self) -> dict:
        return {
            "effect_id": self.effect_id, "card": self.card, "kind": self.kind, "location": list(self.location),
            "trigger": self.trigger.to_dict() if self.trigger else None, "conditions": list(self.conditions),
            "costs": [m.to_dict() for m in self.costs], "moves": [m.to_dict() for m in self.moves],
            "draws": self.draws, "lp_cost": self.lp_cost, "count_limit": self.count_limit,
            "categories": list(self.categories), "restrictions": list(self.restrictions),
            "unknown": list(self.unknown), "source": self.source, "confidence": self.confidence,
            **({"locks": [x.to_dict() for x in self.locks]} if self.locks else {}),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EffectSignature":
        return cls(
            d["effect_id"], d["card"], d["kind"], tuple(d["location"]),
            Trigger(**d["trigger"]) if d.get("trigger") else None, tuple(d.get("conditions", ())),
            tuple(Move.from_dict(m) for m in d.get("costs", ())), tuple(Move.from_dict(m) for m in d.get("moves", ())),
            d.get("draws", 0), d.get("lp_cost", 0), d.get("count_limit"), tuple(d.get("categories", ())),
            tuple(d.get("restrictions", ())), tuple(d.get("unknown", ())), d.get("source", "lua-static"),
            d.get("confidence", 1.0), tuple(Lock.from_dict(x) for x in d.get("locks", ())),
        )


@runtime_checkable
class EffectKnowledge(Protocol):
    """Card Knowledge System -> algorithmic discovery."""

    def version(self) -> str:
        """Changes whenever signatures change; plan order is only stable within one version."""
        ...

    def codes(self) -> list[int]:
        """Cards that can take part in combos (eligible), sorted."""
        ...

    def card(self, code: int) -> CardInfo | None: ...

    def signatures(self, code: int) -> list[EffectSignature]: ...

    def names(self, code: int) -> set[int]:
        """Cards this card's script names (listed names, IsCode)."""
        ...

    def archetypes(self, code: int) -> set[int]:
        """Archetypes this card's script refers to."""
        ...


def summarize(signatures: Iterable[EffectSignature]) -> dict:
    """Coverage numbers: how much of the effects could be interpreted."""
    sigs = list(signatures)
    usable = [s for s in sigs if s.usable]
    with_unknown = [s for s in usable if s.unknown]
    moves_unknown = [s for s in usable if any(not u.startswith("condition") for u in s.unknown)]
    return {
        "effects": len(sigs),
        "usable": len(usable),
        "with_moves": sum(1 for s in usable if s.moves or s.draws),
        "with_unknown": len(with_unknown),
        "unknown_share": round(len(with_unknown) / len(usable), 4) if usable else 0.0,
        "unknown_share_without_conditions": round(len(moves_unknown) / len(usable), 4) if usable else 0.0,
        "mean_confidence": round(sum(s.confidence for s in usable) / len(usable), 4) if usable else 0.0,
    }


__all__ = [
    "KINDS", "UNKNOWN", "ZONES", "CardFilter", "Condition", "EffectKnowledge", "EffectSignature", "Lock", "Move", "Trigger",
    "summarize",
]
