"""Tier 8: start setups for later game situations (Stufe 2, step 2.9).

The tiers 1-7 start every search from an opening hand. Later in a game the
situation differs: few cards in hand, but resources in the GY, banished or
on the field. This source builds such setups from the Effect Signatures:

    resource card   a card with an effect it can use from the GY (or while banished, or - optional -
                    as a monster already on the field)
    setup           the resource card lies in that zone; the hand holds only its partner from the
                    interaction graph (if any) and the generic helpers it needs - 0 to 3 cards.
                    Deck and extra deck are built exactly like in tiers 1-2.

Per resource card: one setup on its own, then one per NAMED / RESTRICTED
interaction partner, each against every configured opponent board. The order
is fixed (card code, then graph order), so runs can be resumed.

Combos found here go to Database 1 like all others; the cards they use from
those zones are part of their requirements (`grave`, `banished`, `field`).
"""

from __future__ import annotations

from typing import Iterator

from deepyugioh.contracts.cards import CardPool, InteractionGraph
from deepyugioh.contracts.effects import EffectKnowledge
from deepyugioh.contracts.setups import Candidate, FieldCard, StartSetup
from deepyugioh.engine.constants import TYPE_LINK, TYPE_MONSTER

LATE_TIER = 8
ZONE_OF = {"GRAVE": "grave", "BANISHED": "banished", "MZONE": "field"}
MAX_HAND = 3


class LateGameCandidateSource:
    def __init__(self, knowledge: EffectKnowledge, graph: InteractionGraph, pool: CardPool, graph_source,
                 zones: tuple[str, ...] = ("grave", "banished")):
        self.knowledge, self.graph, self.pool, self.graph_source = knowledge, graph, pool, graph_source
        self.zones = tuple(zones)
        unknown = set(self.zones) - set(ZONE_OF.values())
        if unknown:
            raise ValueError(f"unknown late game zones: {sorted(unknown)}")

    def version(self) -> str:
        return f"late:1:{self.knowledge.version()}:{self.graph.version()}:{'+'.join(self.zones)}"

    def tier_size(self, tier: int) -> int | None:
        return None

    def resources(self) -> Iterator[tuple[int, str]]:
        """(card, zone) for every card with a usable effect in one of the configured zones."""
        for code in self.knowledge.codes():
            card = self.pool.get(code)
            if card is None:
                continue
            found = set()
            for sig in self.knowledge.signatures(code):
                if sig.kind not in ("IGNITION", "QUICK", "TRIGGER"):
                    continue
                for loc in sig.location:
                    zone = ZONE_OF.get(loc)
                    if zone not in self.zones or zone in found:
                        continue
                    if zone == "field" and not (card.type & TYPE_MONSTER and not card.is_extra_deck and sig.kind != "TRIGGER"):
                        continue   # on the field: main deck monsters with an effect they can activate there
                    found.add(zone)
            for zone in sorted(found):
                yield code, zone

    def candidates(self, tier: int) -> Iterator[Candidate]:
        if tier != LATE_TIER:
            raise ValueError(f"unknown tier {tier}")
        for code, zone in self.resources():
            partners, seen = [None], set()
            for it in self.graph.neighbors(code):
                other = it.target if it.source == code else it.source
                card = self.pool.get(other)
                if other != code and other not in seen and card is not None and not card.is_extra_deck:
                    seen.add(other)
                    partners.append(other)
            for partner in partners:
                core = (code,) if partner is None else (code, partner)
                built = self.graph_source.build(core)
                if built is None:
                    continue
                hand, deck, extra, truncated, helpers = built
                hand = list(hand)
                if code in hand:
                    hand.remove(code)       # the resource card is already where its effect works
                else:
                    extra = tuple(c for c in extra if c != code) if code in extra else extra
                if len(hand) > MAX_HAND:
                    continue                # no longer a "small hand" situation
                where = {"grave": (code,) if zone == "grave" else (), "banished": (code,) if zone == "banished" else (),
                         "field": (FieldCard(code),) if zone == "field" else ()}
                branch = any((c := self.pool.get(x)) is not None and c.type & TYPE_LINK for x in (*extra, *hand))
                for board in self.graph_source.boards:
                    setup = StartSetup(hand=tuple(hand), deck=deck, extra=extra, opponent=board, branch_zones=branch, **where)
                    yield Candidate(setup, LATE_TIER, {"strategy": "late", "resource": code, "zone": zone,
                                                       "partner": partner, "helpers": helpers, "board": board.name}, truncated)
