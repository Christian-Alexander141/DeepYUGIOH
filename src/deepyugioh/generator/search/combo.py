"""What counts as a combo and how its benefit is measured.

Definition (docs/combo-generator-spec.md):

    A combo is a sequence of actions of player 0 during one turn that
      1. requires at most `max_cards` (default 5) specific cards, and
      2. produces at least one positive benefit component.

Required cards = hand cards used + cards taken out of the deck (searched,
sent, summoned - not drawn) + cards taken out of the extra deck. A hand card
is used if it leaves the hand or if one of its effects is activated in the
hand (a card that reveals itself stays there, but the combo needs it). Tokens and
the opponent's cards never count. In later game situations (the setup starts
with cards in the GY, banished or on the field) the cards of those zones that
the line uses count as well, with their own origin.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass

from deepyugioh.contracts.setups import StartSetup
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
    grave: tuple[int, ...] = ()
    banished: tuple[int, ...] = ()
    field: tuple[int, ...] = ()

    @property
    def resources(self) -> tuple[int, ...]:
        return tuple(sorted(self.grave + self.banished + self.field))

    @property
    def count(self) -> int:
        return len(self.hand) + len(self.deck) + len(self.extra) + len(self.resources)

    @property
    def cards(self) -> tuple[int, ...]:
        return tuple(sorted(self.hand + self.deck + self.extra + self.resources))


def _zone_use(start: Counter, events: list[Event], location: int) -> Counter:
    """How many of the cards that started in a zone of player 0 the line used: moved out of it or
    activated there. Copies that arrived during the line are used up first (they are not start cards)."""
    arrived, used = Counter(), Counter()
    if not start:
        return used
    for ev in events:
        if ev.msg == K.MSG_MOVE:
            frm, to, code = ev.data["from"], ev.data["to"], ev.data["code"]
            here_from = frm.controller == 0 and bool(frm.location & location) and not frm.location & K.LOCATION_OVERLAY
            here_to = to.controller == 0 and bool(to.location & location) and not to.location & K.LOCATION_OVERLAY
            if here_from and here_to:
                continue
            if here_to:
                arrived[code] += 1
            elif here_from:
                if arrived[code] > 0:
                    arrived[code] -= 1
                elif used[code] < start[code]:
                    used[code] += 1
        elif ev.msg == K.MSG_CHAINING and ev.data["controller"] == 0:
            loc, code = ev.data["loc"], ev.data["code"]
            if loc.controller == 0 and loc.location & location and not arrived[code] and not used[code] and start[code]:
                used[code] = 1
    return used


def requirements(setup: StartSetup, end: BoardState, events: list[Event]) -> Requirements:
    start_hand = setup.hand
    deck, extra, entered_hand = Counter(), Counter(), Counter()
    arrived, activated = Counter(), Counter()   # copies that came to the hand during the line / start copies activated there
    for ev in events:
        if ev.msg == K.MSG_DRAW:
            if ev.data["player"] == 0:   # a drawn card is in the hand without a move event; it is not a start card
                entered_hand.update(ev.data["codes"])
                arrived.update(ev.data["codes"])
            continue
        if ev.msg == K.MSG_CHAINING:
            loc = ev.data["loc"]
            if ev.data["controller"] == 0 and loc.controller == 0 and loc.location == K.LOCATION_HAND \
                    and not arrived[ev.data["code"]]:
                activated[ev.data["code"]] = 1   # used in the hand without leaving it (e.g. revealed)
            continue
        if ev.msg != K.MSG_MOVE:
            continue
        frm, to = ev.data["from"], ev.data["to"]
        if frm.controller == 0 and frm.location == K.LOCATION_HAND and arrived[ev.data["code"]] > 0:
            arrived[ev.data["code"]] -= 1
        if frm.controller != 0 or frm.location & K.LOCATION_OVERLAY:
            continue
        if frm.location == K.LOCATION_DECK:
            deck[ev.data["code"]] += 1
        elif frm.location == K.LOCATION_EXTRA:
            extra[ev.data["code"]] += 1
        if to.location == K.LOCATION_HAND and to.controller == 0 and frm.location != K.LOCATION_HAND:
            entered_hand[ev.data["code"]] += 1
            arrived[ev.data["code"]] += 1
    still_in_hand = Counter(c.code for c in end.players[0].hand) - entered_hand
    used = (Counter(start_hand) - still_in_hand) | (activated & Counter(start_hand))
    if not setup.late:
        return Requirements(tuple(sorted(used.elements())), tuple(sorted(deck.elements())), tuple(sorted(extra.elements())))
    return Requirements(
        hand=tuple(sorted(used.elements())),
        deck=tuple(sorted(deck.elements())),
        extra=tuple(sorted(extra.elements())),
        grave=tuple(sorted(_zone_use(Counter(setup.grave), events, K.LOCATION_GRAVE).elements())),
        banished=tuple(sorted(_zone_use(Counter(setup.banished), events, K.LOCATION_REMOVED).elements())),
        field=tuple(sorted(_zone_use(Counter(c.code for c in setup.field), events, K.LOCATION_ONFIELD).elements())),
    )


def unused_resources(setup: StartSetup, req: Requirements) -> tuple[Counter, Counter, Counter] | None:
    """Start cards in GY / banished / on the field the line did not use - they are not part of the combo
    and must not change its identity. None for setups without such cards."""
    if not setup.late:
        return None
    return (Counter(setup.grave) - Counter(req.grave), Counter(setup.banished) - Counter(req.banished),
            Counter(c.code for c in setup.field) - Counter(req.field))


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
