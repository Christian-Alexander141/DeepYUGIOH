"""Start setup generator: turns the interaction graph into start setups.

The engine can only search concrete game states, so global discovery is split
into many small searches. Each one starts from a *start setup*, built around
1-2 core cards:

    hand   core cards (main deck) + generic helpers the core cards need
           (e.g. Foolish Burial for a card that does something in the GY)
           + for extra deck core cards: the main deck cards they name
    deck   a second copy of every core card + the cards the core cards can
           fetch (NAMED/RESTRICTED neighbourhood, capped)
    extra  extra deck monsters related to the core cards (capped at 15)
    opp.   one candidate per configured opponent board

Tiers (processed in this order, every tier in a fixed order):

    1  every starter card on its own              ~13.5k cores
    2  NAMED / RESTRICTED interaction pairs       ~94k cores
    3  CONSTRAINED interaction pairs              ~750k cores
    4  GENERIC interaction pairs                  ~4.9M cores

All tiers together are the complete global search space of this generator;
the tiers only decide what is searched first.
"""

from __future__ import annotations

from typing import Iterator, Protocol

from deepyugioh.contracts.cards import CardPool, InteractionGrade, InteractionGraph
from deepyugioh.contracts.setups import Candidate, OpponentBoard, StartSetup
from deepyugioh.engine.constants import TYPE_LINK
from deepyugioh.generator.config import CandidateConfig


class CandidateSource(Protocol):
    """Anything that proposes start setups. Must enumerate each tier in a fixed order."""

    def version(self) -> str: ...

    def candidates(self, tier: int) -> Iterator[Candidate]: ...

    def tier_size(self, tier: int) -> int | None:
        """Number of candidates of a tier, None if unknown."""
        ...


class GraphCandidateSource:
    def __init__(self, graph: InteractionGraph, pool: CardPool, config: CandidateConfig, boards: dict[str, OpponentBoard]):
        self.graph = graph
        self.pool = pool
        self.config = config
        missing = set(config.opponent_boards) - set(boards)
        if missing:
            raise ValueError(f"unknown opponent boards: {sorted(missing)}")
        self.boards = [boards[name] for name in config.opponent_boards]

    def version(self) -> str:
        return f"graph:{self.graph.version()}"

    # ---- enumeration ------------------------------------------------------
    def _cores(self, tier: int) -> Iterator[tuple[tuple[int, ...], dict]]:
        if tier == 1:
            for code in self.graph.starters():
                yield (code,), {"strategy": "solo", "card": code}
        elif tier == 2:
            for it in self.graph.interactions():
                yield (it.source, it.target), {"strategy": "pair", "kind": it.kind, "grade": it.grade.value}
        elif tier in (3, 4):
            grade = InteractionGrade.CONSTRAINED if tier == 3 else InteractionGrade.GENERIC
            for it in self.graph.iter_generic(grade):
                yield (it.source, it.target), {"strategy": "pair", "kind": it.kind, "grade": grade.value}
        else:
            raise ValueError(f"unknown tier {tier}")

    def candidates(self, tier: int) -> Iterator[Candidate]:
        last_core = None
        for core, origin in self._cores(tier):
            key = tuple(sorted(core))
            if key == last_core:  # several interaction kinds between the same two cards
                continue
            last_core = key
            built = self.build(core)
            if built is None:
                continue
            hand, deck, extra, truncated, helpers = built
            branch_zones = any(self._is_link(c) for c in extra + hand)
            for board in self.boards:
                setup = StartSetup(hand=hand, deck=deck, extra=extra, opponent=board, branch_zones=branch_zones)
                yield Candidate(setup, tier, {**origin, "core": list(core), "helpers": helpers, "board": board.name}, truncated)

    def tier_size(self, tier: int) -> int | None:
        boards = len(self.boards)
        if tier == 1:
            return len(self.graph.starters()) * boards
        if tier == 2:
            return len({tuple(sorted((i.source, i.target))) for i in self.graph.interactions()}) * boards
        meta = getattr(self.graph, "meta", lambda: {})()
        counts = meta.get("interactions", {})
        grade = "CONSTRAINED" if tier == 3 else "GENERIC"
        return counts[grade] * boards if grade in counts else None

    # ---- setup construction ----------------------------------------------
    def _is_extra(self, code: int) -> bool:
        card = self.pool.get(code)
        return bool(card and card.is_extra_deck)

    def _is_link(self, code: int) -> bool:
        card = self.pool.get(code)
        return bool(card and card.type & TYPE_LINK)

    def build(self, core: tuple[int, ...]):
        """Returns (hand, deck, extra, truncated, helpers) or None if the core is unusable."""
        if any(self.pool.get(c) is None for c in core):
            return None
        hand, deck, extra, truncated = [], [], [], []
        for c in core:
            if self._is_extra(c):
                extra.append(c)
                # an extra deck monster needs its named materials in hand
                for it in self.graph.neighbors(c):
                    if it.source == c and it.kind == "NAMED" and not self._is_extra(it.target) and it.target not in hand:
                        hand.append(it.target)
            else:
                hand.append(c)
                deck.append(c)

        # neighbourhood: what the core cards can fetch from the deck
        neighbours = []
        for c in core:
            for it in self.graph.neighbors(c):
                t = it.target
                if it.source == c and t not in core and t not in neighbours and not self._is_extra(t):
                    neighbours.append(t)
        if len(neighbours) > self.config.max_deck_neighbors:
            truncated.append("deck")
            neighbours = neighbours[: self.config.max_deck_neighbors]
        deck += neighbours

        # extra deck: related monsters, most specific first
        related = []
        for c in list(core) + hand:
            for e in self.graph.related_extra(c):
                if e not in extra and e not in related:
                    related.append(e)
        room = self.config.max_extra - len(extra)
        if len(related) > room:
            truncated.append("extra")
            related = related[: max(room, 0)]
        extra += related

        # generic helpers for needs of the hand cards
        helpers = {}
        if self.config.enablers:
            enablers = self.graph.enablers
            for c in list(hand):
                for role in sorted(self.graph.needs(c), key=lambda r: r.value):
                    code = enablers.get(role)
                    if code is not None and code not in hand and role.value not in helpers:
                        helpers[role.value] = code
            hand += list(helpers.values())
        return tuple(hand), tuple(deck), tuple(extra), tuple(truncated), helpers
