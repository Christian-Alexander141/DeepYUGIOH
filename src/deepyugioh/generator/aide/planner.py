"""Forward and backward planning over effect signatures (AIDE steps 4-6).

The planner works on an *abstract* first-turn state: for every card of the
plan only its zone is known (HAND, DECK, GRAVE, MZONE, ...), plus the used
count limits and whether the Normal Summon is used. An action is a Normal
Summon or the use of an effect whose card is in the right zone; its costs and
moves change zones and create events (sent to the GY, special summoned ...)
that can start trigger effects.

Forward   start with one card in the hand, apply effects, pull in cards the
          effects can fetch (producer -> consumer), continue - multi-step.
Backward  start with a goal ("card G uses its GY effect"), find producers that
          can bring G there and what those need, then let the forward search
          complete the plan from that seed (both directions share the state).

A plan is emitted whenever an action used a *link*: a card (or a resource)
that another card's effect brought into place. Plans are hypotheses; the
engine searches the resulting start setup and decides what really works.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from deepyugioh.contracts.effects import EffectSignature, Move
from deepyugioh.generator.aide import constraints as C
from deepyugioh.generator.aide.index import EffectIndex

_EVENT = {"GRAVE": "TO_GRAVE", "MZONE": "SPSUMMON", "BANISHED": "REMOVE", "HAND": "TO_HAND", "DECK": "TO_DECK",
          "SZONE": "SET"}
_STAYS_ON_FIELD = 0x20000 | 0x40000 | 0x80000   # continuous / equip / field spells


@dataclass(frozen=True)
class Step:
    code: int
    action: str                                   # "normal_summon" or an effect id
    moved: tuple[tuple[int, str, str], ...]       # (card, from, to)
    links: tuple[tuple[int, int], ...] = ()       # (producer card, consumer card)

    def to_dict(self) -> dict:
        return {"code": self.code, "action": self.action, "moved": [list(m) for m in self.moved],
                "links": [list(x) for x in self.links]}


@dataclass(frozen=True)
class Plan:
    planner: str                                  # forward | backward
    root: int                                     # start card (forward) or goal card (backward)
    start: tuple[tuple[int, str], ...]            # (card, HAND|DECK|EXTRA), sorted
    steps: tuple[Step, ...]
    specific_links: int = 0

    @property
    def links(self) -> int:
        return sum(len(s.links) for s in self.steps)

    @property
    def cards(self) -> tuple[int, ...]:
        return tuple(c for c, _ in self.start)

    def zone_cards(self, zone: str) -> tuple[int, ...]:
        return tuple(c for c, z in self.start if z == zone)

    @property
    def key(self) -> tuple:
        return self.start

    @property
    def score(self) -> tuple:
        return (self.links, self.specific_links, len(self.start), -len(self.steps))

    @property
    def id(self) -> str:
        payload = json.dumps([[list(x) for x in self.start], [s.to_dict() for s in self.steps]], sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:20]

    def to_dict(self) -> dict:
        return {"planner": self.planner, "root": self.root, "start": [list(x) for x in self.start],
                "steps": [s.to_dict() for s in self.steps], "links": self.links, "specific_links": self.specific_links}


@dataclass
class _State:
    zone: dict[int, str] = field(default_factory=dict)
    start: dict[int, str] = field(default_factory=dict)
    used: set[str] = field(default_factory=set)
    normal_summon: bool = False
    steps: list[Step] = field(default_factory=list)
    brought_by: dict[int, int] = field(default_factory=dict)   # card -> card whose effect moved it last
    events: list[tuple[int, str, str]] = field(default_factory=list)  # (card, event, from zone) of the last action
    specific: int = 0

    def copy(self) -> "_State":
        return _State(dict(self.zone), dict(self.start), set(self.used), self.normal_summon, list(self.steps),
                      dict(self.brought_by), [], self.specific)

    def add(self, code: int, zone: str):
        self.zone[code] = zone
        self.start[code] = zone

    def plan(self, planner: str, root: int) -> Plan:
        return Plan(planner, root, tuple(sorted(self.start.items())), tuple(self.steps), self.specific)


class Planner:
    def __init__(self, index: EffectIndex, *, max_cards: int = 5, branching: int = 4, max_steps: int = 8,
                 node_budget: int = 300, plans_per_root: int = 6):
        self.index = index
        self.max_cards = max_cards
        self.branching = branching
        self.max_steps = max_steps
        self.node_budget = node_budget
        self.plans_per_root = plans_per_root

    # ---- public -------------------------------------------------------------------------
    def forward(self, root: int) -> list[Plan]:
        card = self.index.cards.get(root)
        if card is None or C.is_extra(card):
            return []
        state = _State()
        state.add(root, "HAND")
        return self._search([state], "forward", root)

    def backward(self, goal: int) -> tuple[list[Plan], list[dict]]:
        """Plans that end with `goal` using an effect; also the seeds that led nowhere (with reason)."""
        seeds, rejected = self.seeds(goal)
        plans = self._search(seeds, "backward", goal, require=goal) if seeds else []
        if seeds and not plans:
            rejected.append({"goal": goal, "reason": "no producer could be activated in the abstract state",
                             "seeds": [sorted(s.start.items()) for s in seeds]})
        return plans, rejected

    def seeds(self, goal: int) -> tuple[list[_State], list[dict]]:
        """Backward step: which cards can bring `goal` to a zone where it does something, and from where."""
        idx = self.index
        card = idx.cards.get(goal)
        out, rejected = [], []
        if card is None:
            return out, rejected
        for zone in ("GRAVE", "BANISHED", "MZONE", "HAND"):
            if not idx.uses(goal, zone):
                continue
            for p in idx.producers_for(goal, zone, self.branching):
                g_start = C.start_zone(card, p.move.src)
                placement = self._activation_start(p.code, p.sig)
                if g_start is None:
                    # depth 2: the goal must first reach the producer's source zone (e.g. GY) by another effect
                    for src in p.move.src:
                        if src in ("GRAVE", "BANISHED"):
                            for q in idx.producers_for(goal, src, 2):
                                q_place = self._activation_start(q.code, q.sig)
                                g0 = C.start_zone(card, q.move.src)
                                if q_place and placement and g0 and len({goal, p.code, q.code}) == 3:
                                    s = _State()
                                    s.add(q.code, q_place)
                                    s.add(p.code, placement)
                                    s.add(goal, g0)
                                    out.append(s)
                    continue
                if placement is None:
                    rejected.append({"goal": goal, "producer": p.code, "effect": p.sig.effect_id,
                                     "reason": f"producer cannot be activated from the hand or by Normal Summon ({p.sig.location})"})
                    continue
                s = _State()
                s.add(p.code, placement)
                s.add(goal, g_start)
                out.append(s)
        return out, rejected

    # ---- search -------------------------------------------------------------------------
    def _search(self, roots: list[_State], planner: str, root: int, require: int | None = None) -> list[Plan]:
        best: dict[tuple, Plan] = {}
        budget = [self.node_budget]

        def emit(state: _State):
            if len(state.start) < 2:
                return
            if require is not None and not any(require == c for s in state.steps for _, c in s.links):
                return
            plan = state.plan(planner, root)
            old = best.get(plan.key)
            if old is None or plan.score > old.score:
                best[plan.key] = plan

        def dfs(state: _State):
            if budget[0] <= 0 or len(state.steps) >= self.max_steps:
                return
            for step, nxt in self._actions(state):
                budget[0] -= 1
                if step.links:
                    emit(nxt)
                dfs(nxt)
                if budget[0] <= 0:
                    return

        for s in roots:
            dfs(s)
        plans = sorted(best.values(), key=lambda p: (tuple(-x for x in p.score), p.start))
        return plans[: self.plans_per_root]

    def _actions(self, state: _State):
        idx = self.index
        # 1. triggers of the last action's events
        for code, event, from_zone in state.events:
            for owner in sorted(state.zone):
                ozone = state.zone[owner]
                for sig in idx.sigs.get(owner, ()):
                    t = sig.trigger
                    if sig.kind != "TRIGGER" or t is None or t.event != event:
                        continue
                    if t.self_only and owner != code:
                        continue
                    if not t.self_only and (owner == code or ozone not in sig.location):
                        continue
                    if t.self_only and sig.location and ozone not in sig.location:
                        continue
                    if not self._conditions_ok(sig, from_zone):
                        continue
                    yield from self._use(state, owner, sig, trigger_from=code)
        # 2. Normal Summon
        if not state.normal_summon:
            for code in sorted(state.zone):
                card = idx.cards[code]
                if state.zone[code] == "HAND" and C.normal_summonable(card):
                    nxt = state.copy()
                    nxt.normal_summon = True
                    nxt.zone[code] = "MZONE"
                    nxt.events = [(code, "SUMMON", "HAND")]
                    nxt.steps.append(Step(code, "normal_summon", ((code, "HAND", "MZONE"),)))
                    yield nxt.steps[-1], nxt
        # 3. effects of cards where they are
        for code in sorted(state.zone):
            zone = state.zone[code]
            card = idx.cards[code]
            for sig in idx.sigs.get(code, ()):
                if sig.kind == "TRIGGER" or zone not in sig.location:
                    continue
                if sig.kind == "ACTIVATE" and zone != "HAND":
                    continue
                if C.blocked(sig, card):
                    continue
                yield from self._use(state, code, sig)

    def _conditions_ok(self, sig: EffectSignature, from_zone: str) -> bool:
        for cond in sig.conditions:
            if cond.startswith("PREVIOUS:"):
                zones = cond.split(":", 1)[1].split("+")
                if from_zone not in zones:
                    return False
        return True

    def _limit_key(self, code: int, sig: EffectSignature) -> str | None:
        if sig.count_limit is None:
            return None
        if sig.count_limit == "OPT":
            return sig.effect_id
        return f"{code}:{sig.count_limit}"

    def _use(self, state: _State, code: int, sig: EffectSignature, trigger_from: int | None = None):
        key = self._limit_key(code, sig)
        if key is not None and key in state.used:
            return
        card = self.index.cards[code]
        if C.blocked(sig, card):
            return
        base = state.copy()
        if key is not None:
            base.used.add(key)
        links = []
        if code in state.brought_by and state.brought_by[code] != code:
            links.append((state.brought_by[code], code))
        elif trigger_from is not None and trigger_from != code and trigger_from in state.brought_by:
            links.append((state.brought_by[trigger_from], code))
        moves = [m for m in sig.costs] + [m for m in sig.moves]
        for nxt, moved, res_links in self._apply(base, code, moves, 0):
            if sig.kind == "ACTIVATE" and nxt.zone.get(code) == "HAND":
                stays = card.type & _STAYS_ON_FIELD
                nxt.zone[code] = "SZONE" if stays else "GRAVE"
                moved = moved + ((code, "HAND", nxt.zone[code]),)
            all_links = tuple(dict.fromkeys(links + res_links))
            if all_links:
                nxt.specific += sum(1 for p, c in all_links if c in self.index.related(p))
            step = Step(code, sig.effect_id, tuple(moved), all_links)
            nxt.steps.append(step)
            yield step, nxt

    def _apply(self, state: _State, code: int, moves: list[Move], i: int):
        """Apply moves[i:] in all chosen ways; yields (state, moved, resource links)."""
        if i == len(moves):
            yield state, (), []
            return
        m = moves[i]
        if m.opponent or m.count == 0 or m.dst not in ("HAND", "GRAVE", "MZONE", "BANISHED", "SZONE", "DECK"):
            yield from self._apply(state, code, moves, i + 1)  # opponent / random / unknown destination: no state change
            return
        if m.is_self:
            cur = state.zone.get(code)
            if m.src and cur not in m.src:
                return  # e.g. "discard this card" while it is on the field: not possible
            nxt = state.copy()
            nxt.events = list(state.events)
            nxt.zone[code] = m.dst
            nxt.events.append((code, _EVENT.get(m.dst, m.dst), cur))
            for st, moved, links in self._apply(nxt, code, moves, i + 1):
                yield st, ((code, cur, m.dst),) + moved, links
            return
        options = self._targets(state, code, m)
        if not options:
            if m.role == "effect" and (not m.src or (m.what and m.what.unknown)):
                yield from self._apply(state, code, moves, i + 1)  # cannot tell - let the engine decide
            return
        for target, new_zone in options:
            nxt = state.copy()
            nxt.events = list(state.events)
            links = []
            if new_zone is not None:
                nxt.add(target, new_zone)
            cur = nxt.zone[target]
            if target in state.brought_by and state.brought_by[target] != code and m.role == "effect":
                links.append((state.brought_by[target], code))  # uses a card another effect provided
            nxt.zone[target] = m.dst
            nxt.brought_by[target] = code
            nxt.events.append((target, _EVENT.get(m.dst, m.dst), cur))
            for st, moved, more in self._apply(nxt, code, moves, i + 1):
                yield st, ((target, cur, m.dst),) + moved, links + more

    def _targets(self, state: _State, code: int, m: Move) -> list[tuple[int, str | None]]:
        """(card, start zone if it is new to the plan) a move can take: plan cards first, then new cards."""
        idx = self.index
        out = []
        for c in sorted(state.zone):
            if c != code and state.zone[c] in m.src and C.can_move(m, idx.cards[c], code):
                out.append((c, None))
        if len(state.start) < self.max_cards:
            for c in self._suggest(state, code, m):
                z = C.start_zone(idx.cards[c], m.src)
                if z is not None:
                    out.append((c, z))
                if len(out) >= self.branching:
                    break
        return out[: self.branching]

    def _suggest(self, state: _State, code: int, m: Move):
        """New cards for a move: named ones, related ones (name/archetype), then generic consumers."""
        idx = self.index
        seen = set(state.zone)
        want_zone = m.dst

        def ok(c):
            card = idx.cards.get(c)
            return c not in seen and card is not None and C.can_move(m, card, code) \
                and C.start_zone(card, m.src) is not None

        n = 0
        specific = [x for f in (m.what.any_of if m.what else ()) for x in sorted(f.codes)]
        for f in (m.what.any_of if m.what else ()):
            for sc in sorted(f.setcodes):
                specific += idx.by_setbase.get(sc & 0xFFF, [])
        for c in dict.fromkeys(specific):
            if ok(c) and (m.role == "cost" or idx.is_consumer(c, want_zone)):
                seen.add(c)
                n += 1
                yield c
                if n >= self.branching:
                    return
        related = []
        for p in sorted(state.zone):
            related += idx.related(p)
        for c in dict.fromkeys(related):
            if ok(c) and idx.is_consumer(c, want_zone):
                seen.add(c)
                n += 1
                yield c
                if n >= self.branching:
                    return
        scanned = 0
        for c in idx.consumers.get(want_zone, ()):
            scanned += 1
            if scanned > 400:
                break
            if ok(c):
                seen.add(c)
                n += 1
                yield c
                if n >= self.branching:
                    return
        if m.role == "cost":  # a cost may be paid with any matching card, even one that does nothing there
            for c in idx.codes[:2000]:
                if ok(c):
                    yield c
                    return

    def _activation_start(self, code: int, sig: EffectSignature) -> str | None:
        """Start zone for a card so that it can use `sig` (hand effects, spells, or summon + field effect)."""
        card = self.index.cards.get(code)
        if card is None or C.is_extra(card):
            return None
        loc = sig.location
        if "HAND" in loc:
            return "HAND"
        if "MZONE" in loc and C.normal_summonable(card):
            return "HAND"
        if sig.kind == "TRIGGER" and sig.trigger and sig.trigger.event == "SUMMON" and C.normal_summonable(card):
            return "HAND"
        return None
