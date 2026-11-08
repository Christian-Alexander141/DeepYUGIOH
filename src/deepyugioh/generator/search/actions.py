"""Enumeration of the legal responses to an engine decision.

Every decision the engine asks player 0 is turned into a list of `Option`s.
The search branches over all options except where a documented pruning rule
applies (see `SearchLimits` and docs/combo-generator-spec.md):

  * zone choice (SELECT_PLACE)          - first free zone, unless `branch_zones`:
                                          then main zone vs. each Extra Monster Zone, and
                                          every free monster zone while a Link monster is
                                          on the field (its arrows make zones differ)
  * battle position (SELECT_POSITION)   - face-up attack preferred, no branching
  * repositioning / shuffling hand      - never chosen
  * monster set / spell-trap set        - off by default (flags)
  * identical choices                   - deduplicated (same card code in the
                                          same non-field location)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from itertools import combinations

from deepyugioh.engine import constants as K
from deepyugioh.engine.messages import CardRef, Decision


@dataclass(frozen=True)
class Option:
    label: str
    response: bytes


class Unsupported(Exception):
    """A decision type the search cannot answer yet; the branch is dropped."""


def _i32(v: int) -> bytes:
    return struct.pack("<i", v)


def _cards_response(indices: tuple[int, ...]) -> bytes:
    return struct.pack("<iI", 0, len(indices)) + b"".join(struct.pack("<I", i) for i in indices)


def _card_key(c: CardRef) -> tuple:
    # Copies of one card in hand/deck/GY/banishment are interchangeable; on the field the zone matters.
    if c.loc.location & K.LOCATION_ONFIELD:
        return (c.code, c.loc.controller, c.loc.location, c.loc.sequence)
    return (c.code, c.loc.controller, c.loc.location)


def _subsets(cards: list[CardRef], lo: int, hi: int, limit: int, ok=None):
    """Distinct index subsets with lo <= size <= hi; returns (subsets, truncated)."""
    out, seen = [], set()
    for k in range(lo, hi + 1):
        for idx in combinations(range(len(cards)), k):
            key = tuple(sorted(_card_key(cards[i]) for i in idx))
            if key in seen:
                continue
            if ok is not None and not ok(idx):
                continue
            seen.add(key)
            out.append(idx)
            if len(out) >= limit:
                return out, True
    return out, False


@dataclass
class Enumerator:
    names: callable  # code -> display name
    max_choices: int = 128
    allow_mset: bool = False
    allow_sset: bool = False
    branch_zones: bool = False
    link_on_field: bool = False  # set by the search before each decision
    truncated: int = 0  # number of decisions whose option list was cut at max_choices

    def options(self, d: Decision) -> list[Option]:
        handler = getattr(self, f"_{d.name.lower()}", None)
        if handler is None:
            raise Unsupported(d.name)
        return handler(d)

    def default_response(self, d: Decision) -> bytes:
        """First option - used for the passive opponent."""
        if d.msg == K.MSG_SELECT_CHAIN and not d.data["forced"]:
            return _i32(-1)
        opts = self.options(d)
        if not opts:
            raise Unsupported(f"{d.name} without options")
        return opts[0].response

    def _n(self, c: CardRef) -> str:
        return self.names(c.code)

    # ---- main phase ------------------------------------------------------
    def _msg_select_idlecmd(self, d: Decision) -> list[Option]:
        opts, seen = [], set()

        def add(kind: int, i: int, c: CardRef, verb: str, desc: int = 0):
            key = (kind, desc) + _card_key(c)
            if key in seen:
                return
            seen.add(key)
            opts.append(Option(f"{verb} {self._n(c)}", _i32((i << 16) | kind)))

        for i, ch in enumerate(d.data["activatable"]):
            add(5, i, ch.card, f"activate[{ch.description & 0xFFFFF}]", ch.description)
        for i, c in enumerate(d.data["summonable"]):
            add(0, i, c, "normal summon")
        for i, c in enumerate(d.data["spsummonable"]):
            add(1, i, c, "special summon")
        if self.allow_mset:
            for i, c in enumerate(d.data["msetable"]):
                add(3, i, c, "set monster")
        if self.allow_sset:
            for i, c in enumerate(d.data["ssetable"]):
                add(4, i, c, "set spell/trap")
        if d.data["to_ep"]:
            opts.append(Option("end turn", _i32(7)))
        return opts

    def _msg_select_battlecmd(self, d: Decision) -> list[Option]:
        if d.data["to_ep"]:
            return [Option("end turn", _i32(3))]
        return [Option("main phase 2", _i32(2))]

    # ---- yes/no style ----------------------------------------------------
    def _msg_select_effectyn(self, d: Decision) -> list[Option]:
        n = self._n(d.data["card"])
        return [Option(f"use {n}: yes", _i32(1)), Option(f"use {n}: no", _i32(0))]

    def _msg_select_yesno(self, d: Decision) -> list[Option]:
        return [Option("yes", _i32(1)), Option("no", _i32(0))]

    def _msg_select_option(self, d: Decision) -> list[Option]:
        return [Option(f"option {i}", _i32(i)) for i in range(len(d.data["options"]))]

    def _msg_select_chain(self, d: Decision) -> list[Option]:
        opts, seen = [], set()
        for i, ch in enumerate(d.data["chains"]):
            key = (ch.description,) + _card_key(ch.card)
            if key in seen:
                continue
            seen.add(key)
            opts.append(Option(f"chain {self._n(ch.card)}", _i32(i)))
        if not d.data["forced"]:
            opts.append(Option("no chain", _i32(-1)))
        return opts

    # ---- card selections -------------------------------------------------
    def _msg_select_card(self, d: Decision) -> list[Option]:
        cards = d.data["cards"]
        hi = min(d.data["max"], len(cards))
        lo = min(d.data["min"], hi)
        subsets, cut = _subsets(cards, lo, hi, self.max_choices)
        self.truncated += cut
        return [
            Option("select " + (", ".join(self._n(cards[i]) for i in s) or "nothing"), _cards_response(s))
            for s in subsets
        ]

    def _msg_select_tribute(self, d: Decision) -> list[Option]:
        cards = d.data["cards"]
        need, hi = d.data["min"], min(d.data["max"], len(cards))

        def minimal(idx):
            total = sum(cards[i].extra for i in idx)
            return total >= need and all(total - cards[i].extra < need for i in idx)

        subsets, cut = _subsets(cards, 1 if need else 0, hi, self.max_choices, minimal)
        self.truncated += cut
        return [Option("tribute " + ", ".join(self._n(cards[i]) for i in s), _cards_response(s)) for s in subsets]

    def _msg_select_sum(self, d: Decision) -> list[Option]:
        cards, must, acc = d.data["cards"], d.data["must"], d.data["acc"]
        must_params = [c.extra for c in must]

        if d.data["exact"]:
            def ok(idx):
                return _sum_exact(must_params + [cards[i].extra for i in idx], acc)
            lo, hi = d.data["min"], min(max(d.data["max"], d.data["min"]), len(cards))
        else:
            def ok(idx):
                return _sum_at_least_minimal(must_params + [cards[i].extra for i in idx], acc)
            lo, hi = 0, len(cards)

        subsets, cut = _subsets(cards, lo, hi, self.max_choices, ok)
        self.truncated += cut
        return [Option("materials " + ", ".join(self._n(cards[i]) for i in s), _cards_response(s)) for s in subsets]

    def _msg_select_unselect_card(self, d: Decision) -> list[Option]:
        opts, seen = [], set()
        if d.data["finishable"]:
            opts.append(Option("finish selection", _i32(-1)))
        for i, c in enumerate(d.data["select"]):
            key = _card_key(c)
            if key in seen:
                continue
            seen.add(key)
            opts.append(Option(f"pick {self._n(c)}", struct.pack("<ii", 1, i)))
        if not opts and d.data["cancelable"]:
            opts.append(Option("cancel", _i32(-1)))
        return opts

    # ---- non-branching decisions ----------------------------------------
    def _msg_select_place(self, d: Decision, opponent_first: bool = False) -> list[Option]:
        free = ~d.data["flag"]
        me, opp = d.player, 1 - d.player
        if self.branch_zones and not opponent_first and d.data["count"] == 1 and d.player == 0:
            zones = [s for s in range(7) if free & (1 << s)]
            if not self.link_on_field:
                # without Link arrows the main monster zones are interchangeable:
                # only "a main zone" vs. each Extra Monster Zone differ
                main = [s for s in zones if s < 5][:1]
                zones = main + [s for s in zones if s >= 5]
            if len(zones) > 1:
                return [Option(f"zone M{s + 1}" if s < 5 else f"zone EMZ{s - 4}", bytes((me, K.LOCATION_MZONE, s))) for s in zones]
        order = [(me, K.LOCATION_MZONE, s, 0, s) for s in range(7)]
        order += [(me, K.LOCATION_SZONE, s, 8, s) for s in range(8)]
        opp_order = [(opp, K.LOCATION_MZONE, s, 16, s) for s in range(7)]
        opp_order += [(opp, K.LOCATION_SZONE, s, 24, s) for s in range(8)]
        order = opp_order + order if opponent_first else order + opp_order
        picks = []
        for player, loc, seq, shift, _ in order:
            if free & (1 << (shift + seq)):
                picks.append(bytes((player, loc, seq)))
                if len(picks) == d.data["count"]:
                    break
        if len(picks) < d.data["count"]:
            raise Unsupported("not enough free zones")
        return [Option("place", b"".join(picks))]

    def _msg_select_disfield(self, d: Decision) -> list[Option]:
        return self._msg_select_place(d, opponent_first=True)

    def _msg_select_position(self, d: Decision) -> list[Option]:
        for pos in (K.POS_FACEUP_ATTACK, K.POS_FACEUP_DEFENSE, K.POS_FACEDOWN_DEFENSE, K.POS_FACEDOWN_ATTACK):
            if d.data["positions"] & pos:
                return [Option("position", _i32(pos))]
        raise Unsupported("no position available")

    def _msg_select_counter(self, d: Decision) -> list[Option]:
        left, out = d.data["count"], []
        for c in d.data["cards"]:
            take = min(left, c.extra)
            out.append(take)
            left -= take
        return [Option("remove counters", b"".join(struct.pack("<h", v) for v in out))]

    def _msg_sort_card(self, d: Decision) -> list[Option]:
        return [Option("keep order", struct.pack("<b", -1))]

    _msg_sort_chain = _msg_sort_card

    def _msg_announce_race(self, d: Decision) -> list[Option]:
        return [Option("declare type", struct.pack("<Q", _lowest_bits(d.data["available"], d.data["count"])))]

    def _msg_announce_attrib(self, d: Decision) -> list[Option]:
        return [Option("declare attribute", struct.pack("<I", _lowest_bits(d.data["available"], d.data["count"])))]

    def _msg_announce_number(self, d: Decision) -> list[Option]:
        return [Option(f"declare {v}", _i32(i)) for i, v in enumerate(d.data["options"][: self.max_choices])]

    def _msg_rock_paper_scissors(self, d: Decision) -> list[Option]:
        return [Option("rock", _i32(1))]


def _lowest_bits(mask: int, count: int) -> int:
    out, bit = 0, 0
    while count and mask >> bit:
        if mask & (1 << bit):
            out |= 1 << bit
            count -= 1
        bit += 1
    return out


def _sum_exact(params: list[int], acc: int) -> bool:
    """Port of select_sum_check1 from playerop.cpp."""

    def check(i: int, rest: int) -> bool:
        if rest == 0 or i == len(params):
            return False
        o1, o2 = params[i] & 0xFFFF, params[i] >> 16
        if i == len(params) - 1:
            return rest == o1 or rest == o2
        return (rest > o1 and check(i + 1, rest - o1)) or (o2 > 0 and rest > o2 and check(i + 1, rest - o2))

    return check(0, acc)


def _sum_at_least_minimal(params: list[int], acc: int) -> bool:
    if not params:
        return False
    total = mx = 0
    mn = 0x7FFFFFFF
    for op in params:
        o1, o2 = op & 0xFFFF, op >> 16
        ms = o2 if (o2 and o2 < o1) else o1
        total += ms
        mx += max(o1, o2)
        mn = min(mn, ms)
    return mx >= acc and total - mn < acc
