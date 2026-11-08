"""What counts as a combo and how its benefit is measured.

Definition (docs/combo-generator-spec.md):

    A combo is a sequence of actions of player 0 during one turn that
      1. requires at most `max_cards` (default 5) specific cards, and
      2. produces at least one positive benefit component.

Required cards = hand cards used + cards taken out of the deck (searched,
sent, summoned - not drawn) + cards taken out of the extra deck. Tokens and
the opponent's cards never count.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass

from deepyugioh.engine import constants as K
from deepyugioh.engine.duel import BoardState
from deepyugioh.engine.messages import Event


@dataclass(frozen=True)
class Benefit:
    lp_gain: int          # own LP gained (LP paid as cost makes this negative)
    opp_lp_loss: int      # damage dealt to the opponent
    hand_delta: int       # own hand size change
    field_delta: int      # own monster + spell/trap zone occupancy change
    opp_field_loss: int   # opponent cards removed from the field
    opp_hand_loss: int    # opponent hand cards removed
    card_advantage: int   # (hand + field) change - reported, not a criterion on its own
    wins: bool            # opponent LP reached 0 / duel won

    CRITERIA = ("lp_gain", "opp_lp_loss", "hand_delta", "field_delta", "opp_field_loss", "opp_hand_loss")

    @property
    def positive(self) -> list[str]:
        out = [k for k in self.CRITERIA if getattr(self, k) > 0]
        if self.wins:
            out.append("wins")
        return out

    def dominates(self, other: "Benefit") -> bool:
        """At least as good in every criterion (and in winning)."""
        return all(getattr(self, k) >= getattr(other, k) for k in self.CRITERIA) and (self.wins or not other.wins)

    def to_dict(self) -> dict:
        return asdict(self)


def benefit(start: BoardState, end: BoardState) -> Benefit:
    s0, s1 = start.players
    e0, e1 = end.players
    return Benefit(
        lp_gain=e0.lp - s0.lp,
        opp_lp_loss=s1.lp - e1.lp,
        hand_delta=len(e0.hand) - len(s0.hand),
        field_delta=e0.field_count - s0.field_count,
        opp_field_loss=s1.field_count - e1.field_count,
        opp_hand_loss=len(s1.hand) - len(e1.hand),
        card_advantage=(len(e0.hand) + e0.field_count) - (len(s0.hand) + s0.field_count),
        wins=end.winner == 0,
    )


@dataclass(frozen=True)
class Requirements:
    hand: tuple[int, ...]
    deck: tuple[int, ...]
    extra: tuple[int, ...]

    @property
    def count(self) -> int:
        return len(self.hand) + len(self.deck) + len(self.extra)

    @property
    def cards(self) -> tuple[int, ...]:
        return tuple(sorted(self.hand + self.deck + self.extra))


def requirements(start_hand: tuple[int, ...], end: BoardState, events: list[Event]) -> Requirements:
    deck, extra, entered_hand = Counter(), Counter(), Counter()
    for ev in events:
        if ev.msg != K.MSG_MOVE:
            continue
        frm, to = ev.data["from"], ev.data["to"]
        if frm.controller != 0 or frm.location & K.LOCATION_OVERLAY:
            continue
        if frm.location == K.LOCATION_DECK:
            deck[ev.data["code"]] += 1
        elif frm.location == K.LOCATION_EXTRA:
            extra[ev.data["code"]] += 1
        if to.location == K.LOCATION_HAND and to.controller == 0 and frm.location != K.LOCATION_HAND:
            entered_hand[ev.data["code"]] += 1
    still_in_hand = Counter(c.code for c in end.players[0].hand) - entered_hand
    used = Counter(start_hand) - still_in_hand
    return Requirements(
        hand=tuple(sorted(used.elements())),
        deck=tuple(sorted(deck.elements())),
        extra=tuple(sorted(extra.elements())),
    )


def activation_tags(events: list[Event]) -> tuple:
    """Order-independent summary of what was used this turn (for transposition pruning).

    Once-per-turn limits and summon restrictions are hidden engine state; they are
    almost always caused by an activation or a summon, so two paths with the same
    visible board *and* the same multiset of activations/summons are treated as
    equivalent.
    """
    tags = Counter()
    for ev in events:
        if ev.msg == K.MSG_CHAINING and ev.data["controller"] == 0:
            tags[("act", ev.data["code"])] += 1
        elif ev.msg in (K.MSG_SUMMONING, K.MSG_SPSUMMONING, K.MSG_FLIPSUMMONING) and ev.data["loc"].controller == 0:
            tags[(f"summon{ev.msg}", ev.data["code"])] += 1
    return tuple(sorted(tags.items()))
