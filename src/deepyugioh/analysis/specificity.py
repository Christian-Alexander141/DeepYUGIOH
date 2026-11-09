"""Deckspezifität (Stufe 2, step 2.3): what a combo demands from the deck it is played in.

Genericness is *measured*, not guessed. For every card:

    need breadth   share of the card pool its effects can work with
                   (Reinforcement of the Army: Warriors of level 4 or lower)
    lock breadth   share of the card pool its restrictions still allow
                   ("you cannot Special Summon from the Extra Deck, except Synchro Monsters")
    genericness    log-scaled product of both: 100 = works with everything, 75 = 10 %, 50 = 1 %,
                   25 = 0.1 %, 0 = 0.01 % or less of the pool

Shares are counted with bit sets over the eligible card pool (one bit per
card, one mask per property), so a filter is a few integer ANDs. A combo's
genericness combines its cards and the restrictions it triggers. Deck fit
then tells how well a combo suits a *deck profile* (archetypes, types,
attributes): a dragon-specific combo has a low genericness but a high fit in
a dragon deck - deck-specific is not bad, it is made visible.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import cached_property
from typing import Iterable

from deepyugioh.contracts.analysis import AxisResult
from deepyugioh.contracts.cards import CardInfo
from deepyugioh.contracts.combos import ComboRecord
from deepyugioh.contracts.effects import KINDS, CardFilter, Condition, EffectKnowledge, EffectSignature, Lock

TYPE_MONSTER = 0x1
TYPE_EXTRA = 0x40 | 0x2000 | 0x800000 | 0x4000000


def pct(share: float) -> str:
    """Readable share: 59 %, 5.1 %, 0.03 %, < 0.01 %."""
    if share <= 0:
        return "0 %"
    if share < 0.0001:
        return "< 0.01 %"
    if share < 0.01:
        return f"{100 * share:.2f} %"
    if share < 0.1:
        return f"{100 * share:.1f} %"
    return f"{100 * share:.0f} %"


def generic_scale(share: float) -> float:
    """100 for the whole pool, -25 per factor 10 less, 0 at 0.01 %."""
    if share <= 0:
        return 0.0
    return round(max(0.0, min(100.0, 100.0 * (1.0 + math.log10(share) / 4.0))), 2)


# ---- the card pool as bit sets ------------------------------------------------------------
class CardUniverse:
    def __init__(self, cards: Iterable[CardInfo]):
        self.cards = [c for c in cards if c is not None]
        self.bit = {c.code: i for i, c in enumerate(self.cards)}
        self.all = (1 << len(self.cards)) - 1
        self._cache: dict = {}

    def _mask(self, key, pred) -> int:
        m = self._cache.get(key)
        if m is None:
            m = 0
            for i, c in enumerate(self.cards):
                if pred(c):
                    m |= 1 << i
            self._cache[key] = m
        return m

    @cached_property
    def monsters(self) -> int:
        return self._mask("monsters", lambda c: c.type & TYPE_MONSTER)

    @cached_property
    def extra(self) -> int:
        return self._mask("extra", lambda c: c.type & TYPE_MONSTER and c.type & TYPE_EXTRA)

    @cached_property
    def main(self) -> int:
        return self.all & ~self.extra

    def count(self, mask: int) -> int:
        return mask.bit_count()

    def share(self, mask: int, population: int) -> float:
        n = population.bit_count()
        return (mask & population).bit_count() / n if n else 0.0

    def filter_mask(self, f: CardFilter, own_code: int | None = None) -> int:
        m = self.all
        if f.kinds:
            km = 0
            for k in f.kinds:
                km |= self._mask(("kind", k), lambda c, t=KINDS[k]: c.type & t)
            m &= km
        if f.type_bits:
            m &= self._mask(("type", f.type_bits), lambda c, t=f.type_bits: c.type & t == t)
        if f.codes:
            cm = 0
            for c in f.codes:
                if c in self.bit:
                    cm |= 1 << self.bit[c]
            cm |= self._mask(("alias", f.codes), lambda c, s=f.codes: c.alias in s)
            m &= cm
        if f.setcodes:
            from deepyugioh.contracts.effects import _setcode_matches

            sm = 0
            for sc in f.setcodes:
                sm |= self._mask(("set", sc), lambda c, w=sc: _setcode_matches(c.setcodes, w))
            m &= sm
        numeric = (f.min_level, f.max_level, f.races, f.attributes, f.max_atk, f.min_atk, f.max_def, f.min_def)
        if any(v for v in numeric if v is not None):
            m &= self.monsters
            if f.min_level is not None or f.max_level is not None:
                lo, hi = f.min_level or 0, f.max_level if f.max_level is not None else 99
                m &= self._mask(("level", lo, hi), lambda c, a=lo, b=hi: a <= c.level <= b)
            if f.races:
                m &= self._mask(("race", f.races), lambda c, r=f.races: c.race & r)
            if f.attributes:
                m &= self._mask(("attr", f.attributes), lambda c, a=f.attributes: c.attribute & a)
            for name, val, cmp in (("max_atk", f.max_atk, lambda c, v: c.atk <= v), ("min_atk", f.min_atk, lambda c, v: c.atk >= v),
                                   ("max_def", f.max_def, lambda c, v: c.def_ <= v), ("min_def", f.min_def, lambda c, v: c.def_ >= v)):
                if val is not None:
                    m &= self._mask((name, val), lambda c, v=val, f2=cmp: f2(c, v))
        if f.exclude_self and own_code in self.bit:
            m &= ~(1 << self.bit[own_code])
        return m

    def condition_mask(self, cond: Condition, own_code: int | None = None) -> int:
        if not cond.any_of:
            return self.all
        m = 0
        for f in cond.any_of:
            m |= self.filter_mask(f, own_code)
        return m


# ---- names for tags -----------------------------------------------------------------------
@dataclass
class Names:
    archetypes: dict[int, str]
    races: dict[int, str]
    attributes: dict[int, str]

    @classmethod
    def from_constants(cls, consts: dict[str, int]) -> "Names":
        def nice(name: str, prefix: str) -> str:
            return name[len(prefix):].replace("_", " ").title()

        arche, races, attrs = {}, {}, {}
        for name, v in sorted(consts.items()):
            if name.startswith("SET_"):
                arche.setdefault(v, nice(name, "SET_"))
            elif name.startswith("RACE_") and name != "RACE_ALL" and v and v & (v - 1) == 0:
                races.setdefault(v, nice(name, "RACE_"))
            elif name.startswith("ATTRIBUTE_") and name != "ATTRIBUTE_ALL" and v and v & (v - 1) == 0:
                attrs.setdefault(v, nice(name, "ATTRIBUTE_"))
        return cls(arche, races, attrs)

    def bits(self, mask: int, table: dict[int, str]) -> list[str]:
        return [n for b, n in sorted(table.items()) if mask & b]

    def archetype(self, sc: int) -> str:
        return self.archetypes.get(sc, self.archetypes.get(sc & 0xFFF, f"0x{sc:x}"))

    def describe(self, cond: Condition | None) -> str:
        if cond is None:
            return "?"
        if not cond.any_of:
            return "alle Karten"
        out = []
        for f in cond.any_of:
            parts = []
            if f.setcodes:
                parts.append("/".join(f'"{self.archetype(s)}"' for s in sorted(f.setcodes)))
            if f.codes:
                parts.append(f"{len(f.codes)} genannte Karte(n)")
            if f.races:
                parts.append("/".join(self.bits(f.races, self.races)))
            if f.attributes:
                parts.append("/".join(self.bits(f.attributes, self.attributes)))
            if f.type_bits:
                parts.append(_type_names(f.type_bits))
            if f.min_level is not None and f.max_level is not None and f.min_level == f.max_level:
                parts.append(f"Level {f.min_level}")
            elif f.max_level is not None:
                parts.append(f"Level ≤ {f.max_level}")
            elif f.min_level is not None:
                parts.append(f"Level ≥ {f.min_level}")
            if f.kinds and not parts:
                parts.append("/".join(sorted(k.title() for k in f.kinds)))
            out.append(" ".join(parts) or "beliebig")
        return " oder ".join(out)


_TYPE_BITS = {0x10: "Normal", 0x20: "Effekt", 0x40: "Fusion", 0x80: "Ritual", 0x1000: "Tuner", 0x2000: "Synchro",
              0x800000: "Xyz", 0x1000000: "Pendel", 0x4000000: "Link"}


def _type_names(bits: int) -> str:
    return "/".join(n for b, n in _TYPE_BITS.items() if bits & b) or f"Typ 0x{bits:x}"


# ---- card profiles --------------------------------------------------------------------------
@dataclass
class CardProfile:
    code: int
    name: str
    archetypes: list[str]
    race: str | None
    attribute: str | None
    monster: bool
    extra: bool
    need_share: float           # share of the pool its effects can work with (1.0 = no specific need)
    needs: list[str]            # readable needs, most specific first
    lock_share: float           # share of the pool its restrictions still allow (1.0 = no restriction)
    locks: list[dict]           # [{kind, scope, allowed, share, whole_turn, unknown}]
    genericness: float
    explanation: str
    setcodes: list[int] = field(default_factory=list)
    race_bits: int = 0
    attribute_bits: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class CardProfiler:
    VERSION = "3"   # 2: every effect counts (geometric mean); 3: readable shares

    def __init__(self, knowledge: EffectKnowledge, universe: CardUniverse | None = None, names: Names | None = None):
        self.k = knowledge
        self.universe = universe or CardUniverse(knowledge.card(c) for c in knowledge.codes())
        if names is None:
            from deepyugioh.cards.references import constants

            names = Names.from_constants(constants())
        self.names = names
        self._cache: dict[int, CardProfile] = {}

    def _population(self, move_dst: str) -> int:
        u = self.universe
        return u.monsters if move_dst == "MZONE" else u.all

    def effect_need(self, sig: EffectSignature) -> tuple[float, list[str]] | None:
        """(share, needs) for an effect that takes other cards; None if it needs none."""
        u = self.universe
        shares, needs = [], []
        for m in sig.all_moves():
            if m.is_self or m.opponent or not m.src or m.what is None or not m.what.any_of:
                continue
            pop = self._population(m.dst)
            if "EXTRA" in m.src and len(m.src) == 1:
                pop &= u.extra
            elif "DECK" in m.src or "HAND" in m.src:
                pop &= u.main if "EXTRA" not in m.src else u.all
            share = u.share(u.condition_mask(m.what, sig.card), pop)
            shares.append(share)
            needs.append(f"{self.names.describe(m.what)} ({pct(share)})")
        if not shares:
            return None
        return min(shares), needs

    def lock_share(self, lock: Lock) -> float | None:
        r = self.lock_shares(lock)
        return None if r is None else r[0]

    def lock_shares(self, lock: Lock) -> tuple[float, float] | None:
        """(weighted share for genericness, share allowed within the restricted summons)."""
        u = self.universe
        if lock.unknown:
            return None
        pop = u.all if lock.kind == "ACTIVATE" else u.monsters
        if lock.scope == "extra":
            pop &= u.extra
        elif lock.scope == "main":
            pop &= u.main
        if lock.allowed is not None:
            inner = u.share(u.condition_mask(lock.allowed), pop)
        elif lock.forbidden is not None:
            inner = 1.0 - u.share(u.condition_mask(lock.forbidden), pop)
        else:
            inner = 0.0
        if lock.scope in ("extra", "main"):
            return 0.5 + 0.5 * inner, inner   # the other half of the summons stays free
        if lock.scope not in ("all", "field"):
            return 0.75 + 0.25 * inner, inner  # only one summon type is restricted
        return inner, inner

    def profile(self, code: int) -> CardProfile:
        if code in self._cache:
            return self._cache[code]
        card = self.k.card(code)
        n = self.names
        sigs = [s for s in self.k.signatures(code) if s.usable or s.locks]
        # every effect that does something counts: one needing a narrow archetype pulls the card down,
        # effects that need nothing (summon itself, draw, hit the opponent) keep it generic
        effect_shares = []
        all_needs = []
        for s in sigs:
            if not s.usable:
                continue
            r = self.effect_need(s)
            if r is None:
                if s.moves or s.draws or s.costs:
                    effect_shares.append(1.0)
            else:
                effect_shares.append(max(r[0], 1e-6))
                all_needs += r[1]
        need_share = 10 ** (sum(math.log10(x) for x in effect_shares) / len(effect_shares)) if effect_shares else 1.0
        locks, lock_shares = [], []
        for s in sigs:
            for lock in s.locks:
                shares = self.lock_shares(lock)
                share, inner = shares if shares is not None else (None, None)
                locks.append({"effect": s.effect_id, "kind": lock.kind, "scope": lock.scope,
                              "allowed": self.names.describe(lock.allowed) if lock.allowed else None,
                              "forbidden": self.names.describe(lock.forbidden) if lock.forbidden else None,
                              "share": None if share is None else round(share, 4),
                              "allowed_share": None if inner is None else round(inner, 4), "whole_turn": lock.whole_turn,
                              "unknown": lock.unknown, "source": lock.source})
                lock_shares.append(0.5 if share is None else share)
        lock_share = min(lock_shares) if lock_shares else 1.0
        g = generic_scale(need_share * lock_share)
        setcodes = sorted(set(card.setcodes)) if card else []
        parts = []
        parts.append("ohne besondere Anforderungen" if need_share >= 0.999 else
                     f"Effekte arbeiten im Mittel mit {pct(need_share)} des Kartenpools; braucht: {'; '.join(sorted(set(all_needs))[:3])}")
        shown = set()
        for lk in locks:
            text = lock_text(lk)
            if text not in shown:
                shown.add(text)
                parts.append(text)
        prof = CardProfile(
            code, card.name if card else str(code), [n.archetype(s) for s in setcodes],
            (n.bits(card.race, n.races) or [None])[0] if card and card.type & TYPE_MONSTER else None,
            (n.bits(card.attribute, n.attributes) or [None])[0] if card and card.type & TYPE_MONSTER else None,
            bool(card and card.type & TYPE_MONSTER), bool(card and card.type & TYPE_MONSTER and card.type & TYPE_EXTRA),
            round(need_share, 6), sorted(set(all_needs)), round(lock_share, 6), locks, g, "; ".join(parts),
            setcodes, card.race if card and card.type & TYPE_MONSTER else 0,
            card.attribute if card and card.type & TYPE_MONSTER else 0,
        )
        self._cache[code] = prof
        return prof


_KIND = {"SPECIAL_SUMMON": "Spezialbeschwörung", "NORMAL_SUMMON": "Normalbeschwörung", "ACTIVATE": "Aktivierung"}
_SCOPE = {"extra": " aus dem Extra Deck", "main": " außer aus dem Extra Deck", "field": ", solange sie auf dem Feld liegt"}


def lock_text(lk: dict, prefix: str = "Sperre") -> str:
    if lk["unknown"]:
        return f"{prefix} ({_KIND.get(lk['kind'], lk['kind'])}): nicht lesbar"
    what = (f"nur {lk['allowed']}" if lk["allowed"] else f"nicht {lk['forbidden']}" if lk["forbidden"] else "gar nicht")
    share = lk.get("allowed_share")
    return (f"{prefix} {_KIND.get(lk['kind'], lk['kind'])}{_SCOPE.get(lk['scope'], '')}: {what}"
            + ("" if share is None or not (lk["allowed"] or lk["forbidden"]) else f" ({share:.0%} davon erlaubt)")
            + (", ganzer Zug" if lk.get("whole_turn") else ""))


# ---- deck profiles and fit -------------------------------------------------------------------
@dataclass(frozen=True)
class DeckProfile:
    """What a deck is built around. Empty = no preference."""

    archetypes: frozenset[int] = frozenset()
    races: int = 0
    attributes: int = 0
    name: str = ""

    def card_fits(self, p: CardProfile) -> bool:
        from deepyugioh.contracts.effects import _setcode_matches

        if self.archetypes and any(_setcode_matches(p.setcodes, a) for a in self.archetypes):
            return True
        if self.races and p.race_bits & self.races:
            return True
        if self.attributes and p.attribute_bits & self.attributes:
            return True
        return False


FOREIGN_ARCHETYPE = 0.5   # a card of an archetype the deck is not built around
OFF_TYPE_MONSTER = 0.6    # a monster of a type / attribute the deck is not built around


def card_fit(c: dict, deck: DeckProfile) -> float:
    """0-1: one card of a combo (entry of raw["cards"]) in a deck with this profile."""
    from deepyugioh.contracts.effects import _setcode_matches

    on_theme = (deck.archetypes and any(_setcode_matches(c["setcodes"], a) for a in deck.archetypes)) \
        or (deck.races and c["race_bits"] & deck.races) or (deck.attributes and c["attribute_bits"] & deck.attributes)
    if on_theme:
        return 1.0
    fit = c["genericness"] / 100.0
    if c["setcodes"]:
        fit *= FOREIGN_ARCHETYPE
    elif c["race_bits"] and (deck.races or deck.attributes):
        fit *= OFF_TYPE_MONSTER
    return fit


def deck_fit(raw: dict, deck: DeckProfile) -> float:
    """0-100: how well a combo (raw of the genericness axis) suits a deck profile.

    Cards that belong to the profile count fully; others by their genericness, less if they belong to a
    foreign archetype or are monsters of another type. Restrictions the combo triggers count fully if the
    deck itself is allowed by them, otherwise by the share they still allow."""
    cards = raw.get("cards", [])
    if not cards:
        return 0.0
    fits = [card_fit(c, deck) for c in cards]
    lock_factor = 1.0
    for lk in raw.get("locks", []):
        if not lk["certain"]:
            continue
        ok = _lock_fits(lk, deck)
        lock_factor *= 1.0 if ok else (lk["share"] if lk["share"] is not None else 0.5)
    return round(100.0 * sum(fits) / len(fits) * lock_factor, 2)


def _lock_fits(lk: dict, deck: DeckProfile) -> bool:
    """Does the deck itself fall inside what the restriction allows?"""
    allowed = lk.get("allowed_filters")
    if not allowed:
        return False
    bases = {a & 0xFFF for a in deck.archetypes}
    for f in allowed:
        if bases and {s & 0xFFF for s in f.get("setcodes", ())} & bases:
            return True
        if deck.races and f.get("races", 0) & deck.races:
            return True
        if deck.attributes and f.get("attributes", 0) & deck.attributes:
            return True
    return False


# ---- the axis -------------------------------------------------------------------------------------
_USED = re.compile(r"^(?:activate\[\d+\]|use|chain|special summon|normal summon|set)\s+(.+?)(?::\s*yes)?$")


class GenericnessAnalyzer:
    """Deckspezifität as an axis: higher value = more generic (fits more decks)."""

    name = "genericness"
    global_ = False

    def __init__(self, knowledge: EffectKnowledge, profiler: CardProfiler | None = None):
        self.k = knowledge
        self.profiler = profiler or CardProfiler(knowledge)
        self.version = f"3/{knowledge.version()}"

    def prepare(self, combos) -> None:
        pass

    def store_extra(self, repo) -> None:
        """Card profiles of every card seen in the analysed combos go to Database 2."""
        if self.profiler._cache:
            repo.write_card_profiles(self.profiler._cache.values(), f"{CardProfiler.VERSION}/{self.k.version()}")

    def used_cards(self, combo: ComboRecord) -> set[str]:
        out = set()
        for s in combo.line:
            m = _USED.match(s.label)
            if m:
                out.add(m.group(1).strip())
        return out

    def combo_locks(self, combo: ComboRecord) -> list[dict]:
        """Restrictions the combo triggers. Certain if the card was used and all its usable effects restrict."""
        used = self.used_cards(combo)
        out = []
        for code in sorted(set(combo.cards)):
            prof = self.profiler.profile(code)
            if not prof.locks or prof.name not in used:
                continue
            sigs = [s for s in self.k.signatures(code) if s.usable]
            locking = {lk["effect"] for lk in prof.locks}
            certain = bool(sigs) and all(s.effect_id in locking or s.kind == "CONTINUOUS" for s in sigs)
            seen = set()
            for lk in prof.locks:
                key = (lk["kind"], lk["scope"], lk["allowed"], lk["forbidden"])
                if key in seen:
                    continue
                seen.add(key)
                sig = next(s for s in self.k.signatures(code) if s.effect_id == lk["effect"])
                lock = next((x for x in sig.locks if (x.kind, x.scope) == (lk["kind"], lk["scope"])), None)
                out.append({**lk, "card": code, "certain": certain,
                            "allowed_filters": lock.allowed.to_dict() if lock is not None and lock.allowed else None})
        return out

    def analyze(self, combo: ComboRecord) -> AxisResult:
        profs = [self.profiler.profile(c) for c in combo.cards]
        if not profs:
            return AxisResult({"cards": []}, 0.0, "keine Karten")
        # geometric mean of the cards' shares, so one very specific card pulls the combo down
        logs = [math.log10(max(p.need_share, 1e-6)) for p in profs]
        need = 10 ** (sum(logs) / len(logs))
        locks = self.combo_locks(combo)
        lock_share = 1.0
        for lk in locks:
            share = lk["share"] if lk["share"] is not None else 0.5
            lock_share *= share if lk["certain"] else math.sqrt(share)   # possible restriction: half weight
        g = generic_scale(need * lock_share)
        arche = Counter(a for p in profs for a in p.archetypes)
        races = Counter(p.race for p in profs if p.race)
        attrs = Counter(p.attribute for p in profs if p.attribute)
        least = sorted(profs, key=lambda p: p.genericness)[:2]
        raw = {
            "genericness": g, "need_share": round(need, 6), "lock_share": round(lock_share, 6),
            "cards": [{"code": p.code, "genericness": p.genericness, "need_share": p.need_share,
                       "setcodes": p.setcodes, "race_bits": p.race_bits, "attribute_bits": p.attribute_bits}
                      for p in profs],
            "locks": locks,
            "tags": {"archetypes": dict(arche.most_common()), "races": dict(races.most_common()),
                     "attributes": dict(attrs.most_common())},
            "least_generic": [p.code for p in least],
        }
        parts = [f"Generizität {g:.0f}"]
        if arche:
            parts.append("Archetypen " + ", ".join(f"{a} ({n})" for a, n in arche.most_common(3)))
        if least and least[0].genericness < 75:
            parts.append("am spezifischsten: " + ", ".join(f"{p.name} ({p.genericness:.0f})" for p in least))
        for lk in locks:
            parts.append(lock_text(lk, "Sperre" if lk["certain"] else "mögliche Sperre"))
        return AxisResult(raw, g, "; ".join(parts), profile={"tags": raw["tags"]})


__all__ = ["CardProfile", "CardProfiler", "CardUniverse", "DeckProfile", "GenericnessAnalyzer", "Names", "deck_fit",
           "generic_scale"]
