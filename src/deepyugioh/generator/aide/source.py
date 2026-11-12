"""AIDE as candidate source: tiers 5 (forward), 6 (backward) and the hybrid tier 7.

    5  AIDE forward   for every starter card: multi-card plans from the hand onwards
    6  AIDE backward  for every card that does something in the GY / banished / on the field:
                      plans that bring it there
    7  HYBRID         tiers 1-4 (unchanged order) interleaved with tiers 5 and 6;
                      `aide_share` of the searches go to AIDE plans

A plan becomes a start setup like every other candidate (hand = the plan's
hand cards, deck = the cards it takes from the deck plus a second copy of the
hand cards, extra = related extra deck monsters) and is searched completely
by the existing engine search. Tiers 1-4 are served by the unchanged
GraphCandidateSource with its unchanged version, so resume state and results
of the existing tiers stay exactly as they were.
"""

from __future__ import annotations

import hashlib
import json
from functools import cached_property
from itertools import chain
from typing import Callable, Iterable, Iterator

from deepyugioh.contracts.cards import CardPool, InteractionGraph
from deepyugioh.contracts.effects import EffectKnowledge
from deepyugioh.contracts.setups import Candidate, OpponentBoard, StartSetup
from deepyugioh.engine.constants import TYPE_LINK
from deepyugioh.generator.aide import constraints as C
from deepyugioh.generator.aide.hypotheses import HypothesisStore
from deepyugioh.generator.aide.index import EffectIndex
from deepyugioh.generator.aide.planner import Plan, Planner
from deepyugioh.generator.config import CandidateConfig

AIDE_TIERS = {5: "forward", 6: "backward"}
HYBRID_TIER = 7
LATE_TIER = 8
BASE_TIERS = (1, 2, 3, 4)
GOAL_ZONES = ("GRAVE", "BANISHED", "MZONE")


class AideCandidateSource:
    def __init__(self, knowledge: EffectKnowledge, graph: InteractionGraph, pool: CardPool, config: CandidateConfig,
                 boards: dict[str, OpponentBoard], *, max_cards: int = 5, hypotheses: HypothesisStore | None = None,
                 codes: Iterable[int] | None = None):
        self.knowledge = knowledge
        self.graph = graph
        self.pool = pool
        self.config = config
        self.max_cards = max(2, max_cards)
        missing = set(config.opponent_boards) - set(boards)
        if missing:
            raise ValueError(f"unknown opponent boards: {sorted(missing)}")
        self.boards = [boards[name] for name in config.opponent_boards]
        self.hypotheses = hypotheses
        self.codes = sorted(codes) if codes is not None else None
        self.duplicates = 0

    @cached_property
    def index(self) -> EffectIndex:
        return EffectIndex(self.knowledge, self.codes)

    @cached_property
    def planner(self) -> Planner:
        c = self.config
        return Planner(self.index, max_cards=self.max_cards, branching=c.aide_branching,
                       plans_per_root=c.aide_plans_per_card)

    @cached_property
    def _version(self) -> str:
        c = self.config
        payload = {
            "signatures": self.knowledge.version(), "graph": self.graph.version(), "max_cards": self.max_cards,
            "plans": c.aide_plans_per_card, "branching": c.aide_branching, "neighbors": c.aide_neighbors,
            "helpers": c.aide_helpers, "extra": c.max_extra, "boards": [b.to_dict() for b in self.boards],
            "codes": hashlib.sha256(json.dumps(self.codes).encode()).hexdigest()[:12] if self.codes else None,
        }
        return "aide:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    def version(self) -> str:
        return self._version

    # ---- roots and plans ------------------------------------------------------------
    def roots(self, planner: str) -> list[int]:
        idx = self.index
        if planner == "forward":
            return [c for c in self.graph.starters() if c in idx.cards]
        goals = set()
        for zone in GOAL_ZONES:
            goals.update(idx.consumers.get(zone, ()))
        return sorted(goals)

    def plans(self, planner: str, root: int) -> list[Plan]:
        store, ver = self.hypotheses, self.version()
        if store is not None:
            cached = store.cached(ver, planner, root)
            if cached is not None:
                return cached
        rejected = []
        if planner == "forward":
            plans = self.planner.forward(root)
        else:
            plans, rejected = self.planner.backward(root)
        if store is not None:
            store.store_plans(ver, planner, root, plans, rejected)
        return plans

    def iter_plans(self, planner: str) -> Iterator[Plan]:
        for root in self.roots(planner):
            yield from self.plans(planner, root)

    # ---- CandidateSource --------------------------------------------------------------
    def candidates(self, tier: int) -> Iterator[Candidate]:
        if tier not in AIDE_TIERS:
            raise ValueError(f"unknown AIDE tier {tier}")
        planner = AIDE_TIERS[tier]
        seen: set[tuple] = set()
        for plan in self.iter_plans(planner):
            if plan.key in seen:
                self.duplicates += 1
                continue
            seen.add(plan.key)
            built = self.build(plan)
            if built is None:
                continue
            hand, deck, extra, truncated, helpers = built
            branch_zones = any(self._is_link(c) for c in extra + hand)
            setup_ids = []
            for board in self.boards:
                setup = StartSetup(hand=hand, deck=deck, extra=extra, opponent=board, branch_zones=branch_zones)
                setup_ids.append(setup.id)
                origin = {"strategy": "aide", "planner": planner, "plan": plan.id, "root": plan.root,
                          "core": list(plan.cards), "links": plan.links, "helpers": helpers, "board": board.name}
                yield Candidate(setup, tier, origin, truncated)
            if self.hypotheses is not None:
                self.hypotheses.handed_over(self.version(), plan.id, setup_ids)

    def tier_size(self, tier: int) -> int | None:
        return None  # only known after planning every root

    # ---- setup construction -------------------------------------------------------------
    def _is_link(self, code: int) -> bool:
        card = self.pool.get(code)
        return bool(card and card.type & TYPE_LINK)

    def build(self, plan: Plan):
        """(hand, deck, extra, truncated, helpers) for a plan, or None if a card is unknown to the pool."""
        if any(self.pool.get(c) is None for c in plan.cards):
            return None
        hand = list(plan.zone_cards("HAND"))
        deck = list(plan.zone_cards("DECK"))
        extra = list(plan.zone_cards("EXTRA"))
        truncated = []
        deck += [c for c in hand if not self.pool.get(c).is_extra_deck]   # second copy, as in tiers 1-4
        if self.config.aide_neighbors > 0:
            neighbours = []
            for c in plan.cards:
                for it in self.graph.neighbors(c):
                    t = it.target
                    card = self.pool.get(t)
                    if it.source == c and t not in plan.cards and t not in neighbours and card and not card.is_extra_deck:
                        neighbours.append(t)
            if len(neighbours) > self.config.aide_neighbors:
                truncated.append("deck")
                neighbours = neighbours[: self.config.aide_neighbors]
            deck += neighbours
        related = []
        for c in plan.cards:
            for e in self.graph.related_extra(c):
                if e not in extra and e not in related:
                    related.append(e)
        room = self.config.max_extra - len(extra)
        if len(related) > room:
            truncated.append("extra")
            related = related[: max(room, 0)]
        extra += related
        helpers = {}
        if self.config.aide_helpers:
            enablers = self.graph.enablers
            for c in list(hand):
                for role in sorted(self.graph.needs(c), key=lambda r: r.value):
                    code = enablers.get(role)
                    if code is not None and code not in hand and code not in plan.cards and role.value not in helpers:
                        helpers[role.value] = code
            hand += list(helpers.values())
        return tuple(hand), tuple(deck), tuple(extra), tuple(truncated), helpers


class HybridCandidateSource:
    """Tiers 1-4 from the graph source (unchanged), 5/6 from AIDE, 7 interleaves both."""

    def __init__(self, graph_source, aide_factory: Callable[[], AideCandidateSource], share: float = 0.5,
                 late_factory: Callable[[], object] | None = None):
        self.graph_source = graph_source
        self._aide_factory = aide_factory
        self._late_factory = late_factory
        self.share = share
        self.duplicates = 0

    @cached_property
    def late(self):
        """Tier 8 (later game situations), built on first use."""
        if self._late_factory is None:
            raise ValueError("tier 8 needs a late game candidate source")
        return self._late_factory()

    @cached_property
    def aide(self) -> AideCandidateSource:
        return self._aide_factory()

    def version(self) -> str:
        return self.graph_source.version()  # tiers 1-4 keep their cursors

    def tier_version(self, tier: int) -> str:
        if tier == LATE_TIER:
            return self.late.version()
        if tier in AIDE_TIERS:
            return self.aide.version()
        if tier == HYBRID_TIER:
            payload = f"{self.graph_source.version()}|{self.aide.version()}|{self.share}"
            return "hybrid:" + hashlib.sha256(payload.encode()).hexdigest()[:16]
        return self.graph_source.version()

    def candidates(self, tier: int) -> Iterator[Candidate]:
        if tier == LATE_TIER:
            return self.late.candidates(tier)
        if tier in AIDE_TIERS:
            return self.aide.candidates(tier)
        if tier == HYBRID_TIER:
            return self._hybrid()
        return self.graph_source.candidates(tier)

    def tier_size(self, tier: int) -> int | None:
        if tier in AIDE_TIERS or tier in (HYBRID_TIER, LATE_TIER):
            return None
        return self.graph_source.tier_size(tier)

    def _hybrid(self) -> Iterator[Candidate]:
        graph = chain.from_iterable(self.graph_source.candidates(t) for t in BASE_TIERS)
        aide = _round_robin(self.aide.candidates(5), self.aide.candidates(6))
        seen: set[str] = set()
        acc = 0.0
        streams = {"graph": graph, "aide": aide}
        alive = {"graph": True, "aide": True}
        while any(alive.values()):
            acc += self.share
            pick = "aide" if acc >= 1.0 else "graph"
            if pick == "aide":
                acc -= 1.0
            if not alive[pick]:
                pick = "graph" if pick == "aide" else "aide"
            try:
                c = next(streams[pick])
            except StopIteration:
                alive[pick] = False
                continue
            if c.id in seen:
                self.duplicates += 1
                continue
            seen.add(c.id)
            yield Candidate(c.setup, HYBRID_TIER, {**c.origin, "via": pick, "from_tier": c.tier}, c.truncated)


def _round_robin(*iterators: Iterator) -> Iterator:
    its = list(iterators)
    while its:
        for it in list(its):
            try:
                yield next(it)
            except StopIteration:
                its.remove(it)


def plan_is_consistent(plan: Plan, index: EffectIndex) -> list[str]:
    """Rule check of a finished plan (used in tests and reports): returns violations."""
    problems = []
    if len(plan.start) > 60:
        problems.append("too many cards")
    for code, zone in plan.start:
        card = index.cards.get(code)
        if card is None:
            problems.append(f"{code}: unknown card")
        elif zone == "HAND" and C.is_extra(card):
            problems.append(f"{code}: extra deck monster in the hand")
    summons = sum(1 for s in plan.steps if s.action == "normal_summon")
    if summons > 1:
        problems.append("more than one Normal Summon")
    return problems
