"""Constraint matching (AIDE step 7 in the proposal): cheap, deterministic rule checks.

These checks only reject what is *known* to be impossible in a first-turn
combo; anything uncertain (UNKNOWN parts of a signature) is allowed and left
to the engine. They never judge strategic value.
"""

from __future__ import annotations

from deepyugioh.contracts.cards import CardInfo
from deepyugioh.contracts.effects import UNKNOWN, EffectSignature, Move

TYPE_MONSTER, TYPE_SPELL, TYPE_TRAP = 0x1, 0x2, 0x4
TYPE_EXTRA = 0x40 | 0x2000 | 0x800000 | 0x4000000
TYPE_TOKEN = 0x4000

# trigger events that cannot happen during our own first turn before the battle phase
FIRST_TURN_IMPOSSIBLE_EVENTS = {"BATTLE_DESTROYED", "ATTACK", "DRAW", "CHAINING"}


def is_monster(card: CardInfo) -> bool:
    return bool(card.type & TYPE_MONSTER)


def is_extra(card: CardInfo) -> bool:
    return is_monster(card) and bool(card.type & TYPE_EXTRA)


def normal_summonable(card: CardInfo) -> bool:
    """Normal Summon without tribute: main deck monster, level 1-4."""
    return is_monster(card) and not is_extra(card) and 1 <= card.level <= 4


def blocked(sig: EffectSignature, card: CardInfo) -> str | None:
    """Why this effect can certainly not be used in a first-turn combo, or None."""
    if "NOT_TURN_SENT_TO_GY" in sig.conditions and "GRAVE" in sig.location:
        return "not in the turn it was sent to the GY"
    if any(c in ("BATTLE", "REASON:BATTLE") for c in sig.conditions):
        return "needs a battle"
    if sig.trigger and sig.trigger.event in FIRST_TURN_IMPOSSIBLE_EVENTS:
        return f"trigger {sig.trigger.event} does not happen in the first turn"
    if "TRAP_MUST_BE_SET" in sig.restrictions:
        return "trap must be set a turn before"
    if "XYZ_MATERIAL" in sig.restrictions and not is_extra(card):
        return "needs Xyz material"
    if sig.kind == "ACTIVATE" and not (card.type & TYPE_SPELL):
        return "only spells are activated from the hand"
    return None


def can_move(move: Move, card: CardInfo, producer_code: int | None = None) -> bool:
    """Can this move take `card`? (zone-independent part: kind and condition)."""
    if card.type & TYPE_TOKEN:
        return False
    if move.dst == "MZONE" and not is_monster(card):
        return False
    if move.dst in ("HAND", "DECK") and is_extra(card):
        return False  # extra deck monsters never go to the hand or main deck
    if move.what is None:
        return True
    return move.what.matches(card, producer_code)


def start_zone(card: CardInfo, src: tuple[str, ...]) -> str | None:
    """Where a new card must start so that a move from `src` can take it."""
    if is_extra(card):
        return "EXTRA" if "EXTRA" in src else None
    for z in ("DECK", "HAND"):
        if z in src:
            return z
    return None


def known_dst(move: Move) -> bool:
    return move.dst != UNKNOWN
