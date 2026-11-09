"""Producer / consumer index over effect signatures (AIDE step 3).

    producer   an effect that moves cards matching a condition into a zone
               (Foolish Burial: DECK -> GRAVE, any monster)
    consumer   a card that can use an effect once it is in a zone
               (Sangan in GRAVE: "if sent from the field to the GY ..."; Monster Reborn in HAND)

Producers are indexed by destination zone and by what their condition names
(card codes, archetypes); consumers by zone. Matching a producer to a card is
then a lookup plus a filter check on a short list - never all-against-all.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property
from typing import Iterable

from deepyugioh.contracts.cards import CardInfo
from deepyugioh.contracts.effects import EffectKnowledge, EffectSignature, Move
from deepyugioh.generator.aide import constraints as C

# a card arriving in a zone through an event: which self-trigger events fit
ARRIVAL_EVENTS = {
    "GRAVE": {"TO_GRAVE", "DESTROYED", "RELEASE", "DISCARD", "BE_MATERIAL"},
    "MZONE": {"SPSUMMON"},
    "BANISHED": {"REMOVE"},
    "HAND": {"TO_HAND"},
}
PLAN_ZONES = ("HAND", "GRAVE", "MZONE", "BANISHED")


@dataclass(frozen=True)
class Producer:
    code: int
    sig: EffectSignature
    move: Move


class EffectIndex:
    def __init__(self, knowledge: EffectKnowledge, codes: Iterable[int] | None = None):
        self.k = knowledge
        wanted = set(codes) if codes is not None else None
        self.codes = [c for c in knowledge.codes() if wanted is None or c in wanted]
        self.cards: dict[int, CardInfo] = {c: knowledge.card(c) for c in self.codes}
        self.sigs: dict[int, list[EffectSignature]] = {
            c: [s for s in knowledge.signatures(c) if s.usable] for c in self.codes
        }

    # ---- consumers ----------------------------------------------------------------------
    def uses(self, code: int, zone: str) -> list[EffectSignature]:
        """Effects of `code` that become usable when it arrives in `zone` (in a first turn)."""
        card = self.cards.get(code)
        if card is None:
            return []
        out = []
        for s in self.sigs.get(code, ()):
            if C.blocked(s, card):
                continue
            if s.kind == "TRIGGER":
                t = s.trigger
                if t.self_only and t.event in ARRIVAL_EVENTS.get(zone, ()):
                    out.append(s)
                elif not t.self_only and zone in s.location:
                    out.append(s)  # waits there for another card's event
            elif zone in s.location:
                if s.kind == "ACTIVATE" and zone != "HAND":
                    continue
                out.append(s)
        if zone == "HAND" and C.normal_summonable(card):
            out += [s for s in self.uses(code, "MZONE") if s not in out]
            out += [s for s in self.sigs.get(code, ())
                    if s.kind == "TRIGGER" and s.trigger.self_only and s.trigger.event == "SUMMON" and s not in out
                    and not C.blocked(s, card)]
        return out

    @cached_property
    def consumers(self) -> dict[str, list[int]]:
        """zone -> cards that use an effect there; chain extenders (their effect moves more cards) first."""
        out = {}
        for zone in PLAN_ZONES:
            scored = []
            for c in self.codes:
                uses = self.uses(c, zone)
                if uses:
                    extends = sum(1 for s in uses for m in s.moves if not m.opponent and m.what is not None and m.src)
                    conf = max(s.confidence for s in uses)
                    scored.append((-min(extends, 3), -conf, c))
            out[zone] = [c for *_, c in sorted(scored)]
        return out

    @cached_property
    def _consumer_sets(self) -> dict[str, set[int]]:
        return {z: set(v) for z, v in self.consumers.items()}

    def is_consumer(self, code: int, zone: str) -> bool:
        return code in self._consumer_sets.get(zone, ())

    # ---- producers ----------------------------------------------------------------------
    @cached_property
    def _producers(self):
        by_dst = defaultdict(list)          # dst -> generic producers
        by_code = defaultdict(list)         # (dst, code) -> producers naming the card
        by_set = defaultdict(list)          # (dst, setbase) -> producers naming the archetype
        for c in self.codes:
            card = self.cards[c]
            for s in self.sigs[c]:
                if C.blocked(s, card):
                    continue
                for m in s.all_moves():
                    if m.opponent or m.is_self or m.dst not in PLAN_ZONES or not m.src:
                        continue
                    p = Producer(c, s, m)
                    alts = m.what.any_of
                    if alts and all(f.codes for f in alts):
                        for f in alts:
                            for code in f.codes:
                                by_code[(m.dst, code)].append(p)
                    elif alts and all(f.codes or f.setcodes for f in alts):
                        for f in alts:
                            for sc in f.setcodes:
                                by_set[(m.dst, sc & 0xFFF)].append(p)
                            for code in f.codes:
                                by_code[(m.dst, code)].append(p)
                    else:
                        by_dst[m.dst].append(p)
        for lst in by_dst.values():
            lst.sort(key=lambda p: (-p.sig.confidence, len(self.sigs[p.code]), p.code, p.sig.effect_id))
        return by_dst, by_code, by_set

    def producers_for(self, code: int, zone: str, limit: int) -> list[Producer]:
        """Effects of other cards that can move `code` into `zone`: specific ones first, then generic."""
        card = self.cards.get(code)
        if card is None:
            return []
        by_dst, by_code, by_set = self._producers
        out, seen = [], set()

        def add(p: Producer):
            key = (p.code, p.sig.effect_id)
            if key not in seen and p.code != code and C.can_move(p.move, card, p.code):
                seen.add(key)
                out.append(p)

        for p in by_code.get((zone, code), ()):
            add(p)
        for sc in card.setcodes:
            for p in by_set.get((zone, sc & 0xFFF), ()):
                add(p)
        for p in by_dst.get(zone, ()):
            if len(out) >= limit:
                break
            add(p)
        return out[:limit]

    # ---- relations between cards ----------------------------------------------------------
    @cached_property
    def by_setbase(self) -> dict[int, list[int]]:
        out = defaultdict(list)
        for c in self.codes:
            for sc in self.cards[c].setcodes:
                out[sc & 0xFFF].append(c)
        return out

    @cached_property
    def named_by(self) -> dict[int, set[int]]:
        out = defaultdict(set)
        for c in self.codes:
            for n in self.k.names(c):
                out[n].add(c)
        return out

    def related(self, code: int) -> list[int]:
        """Cards connected by name or archetype to `code` (both directions), sorted."""
        card = self.cards.get(code)
        if card is None:
            return []
        rel = set(self.k.names(code)) | self.named_by.get(code, set())
        for sc in set(card.setcodes) | self.k.archetypes(code):
            rel.update(self.by_setbase.get(sc & 0xFFF, ()))
        rel.discard(code)
        return sorted(c for c in rel if c in self.cards)

    def stats(self) -> dict:
        by_dst, by_code, by_set = self._producers
        return {
            "cards": len(self.codes),
            "usable_effects": sum(len(v) for v in self.sigs.values()),
            "consumers": {z: len(v) for z, v in self.consumers.items()},
            "producers_generic": {z: len(v) for z, v in by_dst.items()},
            "producers_specific": sum(len(v) for v in by_code.values()) + sum(len(v) for v in by_set.values()),
        }
