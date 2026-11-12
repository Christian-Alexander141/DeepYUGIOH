"""Step 4 + 5: exhaustive depth-first combo search, executed by the real engine.

Branching currently works by *replay*: to explore an alternative at a decision,
a fresh duel is created and the response prefix is replayed (the engine is
deterministic for a fixed seed). The last alternative reuses the live duel.
This is correct but costs one duel creation per extra branch; faster
branching (fork(2) / core fork) is planned (docs/combo-generator-spec.md, 6).

Everything the search did not explore is recorded in `SearchResult.incomplete`,
so a run is only marked complete if the whole space inside the limits was seen.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field

from deepyugioh.contracts.cards import CardPool
from deepyugioh.contracts.setups import StartSetup
from deepyugioh.engine import constants as K
from deepyugioh.engine.duel import BoardState, Duel, InvalidResponse
from deepyugioh.generator.search.actions import Enumerator, Unsupported
from deepyugioh.generator.search.combo import (
    Benefit, Requirements, activation_tags, benefit, requirements, unused_resources,
)


@dataclass(frozen=True)
class SearchLimits:
    max_cards: int = 5          # combo may require at most this many specific cards
    min_cards: int = 2          # below this it is a single card's effect, not a combo
    max_depth: int = 80         # decisions per line
    max_nodes: int = 50_000     # decision points expanded per search
    time_limit: float = 600.0   # seconds per search
    max_choices: int = 128      # options per card-selection decision
    transpositions: bool = True
    drop_redundant: bool = True # drop combos whose benefit a proper subset of their cards already reaches
    allow_mset: bool = False
    allow_sset: bool = False


@dataclass(frozen=True)
class Step:
    decision: str
    label: str
    response: bytes

    def to_dict(self) -> dict:
        return {"decision": self.decision, "label": self.label, "response": self.response.hex()}


@dataclass
class FoundCombo:
    requirements: Requirements
    benefit: Benefit
    line: list[Step]
    end_board: BoardState
    board_key: tuple      # full board - used to validate a replay
    identity: tuple       # setup independent (see BoardState.identity)
    turn_ended: bool

    @property
    def signature(self) -> tuple:
        r = self.requirements
        if r.resources:
            return (r.hand, r.deck, r.extra, self.identity, r.grave, r.banished, r.field)
        return (r.hand, r.deck, r.extra, self.identity)


@dataclass
class SearchResult:
    setup: StartSetup
    limits: SearchLimits
    combos: list[FoundCombo]
    nodes: int
    replays: int
    elapsed: float
    incomplete: Counter = field(default_factory=Counter)
    pruned: Counter = field(default_factory=Counter)

    @property
    def complete(self) -> bool:
        return not self.incomplete


class _Budget(Exception):
    pass


class ComboSearch:
    def __init__(self, store: CardPool, setup: StartSetup, limits: SearchLimits = SearchLimits()):
        self.store = store
        self.setup = setup
        self.limits = limits
        self.enum = Enumerator(
            names=store.name,
            max_choices=limits.max_choices,
            allow_mset=limits.allow_mset,
            allow_sset=limits.allow_sset,
            branch_zones=setup.branch_zones,
        )
        self.found: dict[tuple, FoundCombo] = {}
        self.seen: set = set()
        self.nodes = 0
        self.replays = 0
        self.incomplete: Counter = Counter()
        self.pruned: Counter = Counter()
        self.start_board: BoardState | None = None

    # ---- public -----------------------------------------------------------
    def run(self) -> SearchResult:
        t0 = time.perf_counter()
        self._deadline = t0 + self.limits.time_limit
        duel = self._new_duel()
        try:
            self._expand(duel, [])
        except _Budget:
            pass
        truncated = self.enum.truncated
        if truncated:
            self.incomplete["choices_truncated"] += truncated
        candidates = list(self.found.values())
        combos = [c for c in candidates if c.requirements.count >= self.limits.min_cards]
        self.pruned["below_min_cards"] += len(candidates) - len(combos)
        if self.limits.drop_redundant:
            kept = [c for c in combos if not _has_redundant_cards(c, candidates)]
            self.pruned["redundant_cards"] += len(combos) - len(kept)
            combos = kept
        combos.sort(key=lambda c: (c.requirements.count, len(c.line)))
        self.pruned = +self.pruned  # drop zero counts
        return SearchResult(
            self.setup, self.limits, combos, self.nodes, self.replays,
            time.perf_counter() - t0, self.incomplete, self.pruned,
        )

    # ---- engine helpers ---------------------------------------------------
    def _new_duel(self) -> Duel:
        return Duel(self.store, self.setup)

    def _opponent(self, decision):
        return self.enum.default_response(decision)

    def _replay(self, line: list[Step]) -> Duel:
        self.replays += 1
        duel = self._new_duel()
        for step in line:
            duel.advance(self._opponent)
            duel.respond(step.response)
        return duel

    # ---- search -----------------------------------------------------------
    def _expand(self, duel: Duel, line: list[Step]) -> None:
        try:
            self._visit(duel, line)
        finally:
            duel.close()

    def _visit(self, duel: Duel, line: list[Step]) -> None:
        if self.nodes >= self.limits.max_nodes:
            self.incomplete["max_nodes"] += 1
            raise _Budget
        if time.perf_counter() > self._deadline:
            self.incomplete["time_limit"] += 1
            raise _Budget
        try:
            decision = duel.advance(self._opponent)
        except InvalidResponse:
            self.incomplete["engine_rejected_response"] += 1
            return
        except Unsupported as e:
            self.incomplete[f"unsupported_opponent:{e}"] += 1
            return
        self.nodes += 1
        board = duel.board()
        if self.start_board is None:
            self.start_board = board
        self.enum.link_on_field = self._link_on_field(board)

        req = requirements(self.setup, board, duel.events)
        if req.count > self.limits.max_cards:
            self.pruned["too_many_cards"] += 1
            return

        if decision is None or duel.turn_over:
            self._record(duel, board, req, line, turn_ended=True)
            return

        if decision.msg == K.MSG_SELECT_IDLECMD and decision.player == 0:
            self._record(duel, board, req, line, turn_ended=False)
            if self.limits.transpositions:
                key = (board.key(), activation_tags(duel.events))
                if key in self.seen:
                    self.pruned["transposition"] += 1
                    return
                self.seen.add(key)

        if len(line) >= self.limits.max_depth:
            self.incomplete["max_depth"] += 1
            return
        try:
            options = self.enum.options(decision)
        except Unsupported as e:
            self.incomplete[f"unsupported:{e}"] += 1
            return

        for i, opt in enumerate(options):
            step = Step(decision.name, opt.label, opt.response)
            child = duel if i == len(options) - 1 else self._replay(line)
            if child is not duel:
                child.advance(self._opponent)
            child.respond(opt.response)
            if child is duel:
                self._visit(duel, line + [step])
            else:
                self._expand(child, line + [step])

    def _link_on_field(self, board: BoardState) -> bool:
        for c in board.players[0].mzone:
            card = self.store.get(c.code)
            if card is not None and card.type & K.TYPE_LINK:
                return True
        return False

    def _record(self, duel: Duel, board: BoardState, req: Requirements, line: list[Step], turn_ended: bool):
        # Candidates below min_cards are kept until the end: they are needed for the redundancy check.
        b = benefit(self.start_board, board)
        if not b.positive:
            return
        identity = board.identity(self.start_board, self._link_on_field(board), unused_resources(self.setup, req))
        combo = FoundCombo(req, b, list(line), board, board.key(), identity, turn_ended)
        old = self.found.get(combo.signature)
        if old is None or len(combo.line) < len(old.line):
            self.found[combo.signature] = combo


def _is_proper_submultiset(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
    ca, cb = Counter(a), Counter(b)
    return ca != cb and not (ca - cb)


def _has_redundant_cards(combo: FoundCombo, candidates: list[FoundCombo]) -> bool:
    """True if a combo using a proper subset of the cards reaches at least the same benefit.

    Example: "Foolish Burial (send anything) + Normal Summon X" gives field +1, but
    "Normal Summon X" alone already does - Foolish Burial does not belong to the combo.
    """
    mine = combo.requirements.cards
    for other in candidates:
        if other is combo or not _is_proper_submultiset(other.requirements.cards, mine):
            continue
        if other.benefit.dominates(combo.benefit):
            return True
    return False
