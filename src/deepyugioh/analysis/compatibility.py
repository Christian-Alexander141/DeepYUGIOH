"""Compatibility between combos (Stufe 2, step 2.5): which combos can share a deck, which exclude each other.

Three relations (docs/stufe2-konzept.md, section 4):

    shared_cards   both combos use the same cards (saves deck slots, often synergy). Strength = weighted
                   overlap; a card almost every combo uses (Foolish Burial) weighs next to nothing.
    archetype      both combos are built from the same archetype without necessarily sharing a card.
    lock_conflict  directed: a restriction combo A triggers forbids something combo B does
                   (a Special Summon, a Normal Summon, an activation). What B does comes from the
                   engine replay of its line (axis `line`); without it only the Extra Deck summons count.

Database 1 has hundreds of millions of related pairs, so not every pair is stored. Database 2 keeps, per
combo, its strongest partners (`compatibility` table) with the conflicts among them, and the totals over
*all* combos as the axis `compatibility` (share of the other combos it can be played with in one turn).
`CompatibilityIndex.relation(a, b)` answers any pair exactly, stored or not.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable

from deepyugioh.contracts.cards import CardInfo
from deepyugioh.contracts.effects import Condition, Lock

AXIS = "compatibility"
VERSION = "1"
KINDS = {"shared_cards": "gemeinsame Karten", "archetype": "gemeinsamer Archetyp", "lock_conflict": "Sperrkonflikt"}
TYPE_EXTRA = 0x40 | 0x2000 | 0x800000 | 0x4000000


@dataclass(frozen=True)
class CompatibilitySettings:
    max_shared: int = 20          # stored shared-card partners per combo (strongest first)
    max_archetype: int = 10       # stored archetype partners per combo
    min_shared: float = 0.15      # weakest shared-card overlap that is stored
    min_archetype: float = 0.3    # a combo belongs to an archetype if this share of its cards does
    ubiquitous_share: float = 0.1 # cards more than this share of all combos use never link two combos on their own
    order_factor: float = 0.5     # a restriction for the rest of the turn only (playable in the right order)


@dataclass
class ComboInterface:
    """What compatibility needs to know about one combo."""

    signature: str
    cards: tuple[int, ...]                      # distinct required cards
    archetypes: dict[str, float]                # archetype -> share of the combo's cards
    locks: list[dict] = field(default_factory=list)       # certain, readable restrictions it triggers
    special: tuple[tuple[int, str], ...] = ()   # (card, origin) of its Special Summons
    normal: tuple[int, ...] = ()
    activated: tuple[int, ...] = ()
    proxy: bool = False                         # no line facts: only Extra Deck summons are known
    score: float = 0.0                          # outcome score, orders partners of equal strength


def interface(signature: str, generic_raw: dict, line_raw: dict | None, card: Callable[[int], CardInfo | None],
              score: float = 0.0) -> ComboInterface:
    cards = generic_raw.get("cards", [])
    codes = tuple(sorted({c["code"] for c in cards}))
    n = max(len(cards), 1)
    arch = {a: k / n for a, k in generic_raw.get("tags", {}).get("archetypes", {}).items()}
    locks = [lk for lk in generic_raw.get("locks", []) if lk.get("certain") and not lk.get("unknown")]
    if line_raw is not None:
        return ComboInterface(signature, codes, arch, locks, tuple((c, o) for c, o in line_raw["special_summons"]),
                              tuple(line_raw["normal_summons"]), tuple(line_raw["activations"]), False, score)
    extra = tuple((c, "extra") for c in codes if (info := card(c)) is not None and info.type & 1 and info.type & TYPE_EXTRA)
    return ComboInterface(signature, codes, arch, locks, extra, (), (), True, score)


def lock_of(lk: dict) -> Lock:
    af, ff = lk.get("allowed_filters"), lk.get("forbidden_filters")
    return Lock(lk["kind"], lk["scope"], None if af is None else Condition.from_dict(af),
                None if ff is None else Condition.from_dict(ff), bool(lk.get("whole_turn")), bool(lk.get("unknown")))


def lock_key(lk: dict) -> str:
    return json.dumps([lk["kind"], lk["scope"], lk.get("allowed_filters"), lk.get("forbidden_filters"),
                       bool(lk.get("whole_turn")), lk.get("card") if lk["scope"] == "field" else None], sort_keys=True)


def restricted_cards(lk: dict, other: ComboInterface) -> list[int] | None:
    """Cards of `other` the restriction applies to; None if that cannot be told (summon-type scopes)."""
    kind, scope = lk["kind"], lk["scope"]
    if kind == "NORMAL_SUMMON":
        return list(other.normal)
    if kind == "ACTIVATE":
        return list(other.activated)
    if scope in ("all", "field"):
        return [c for c, _ in other.special]
    if scope == "extra":
        return [c for c, o in other.special if o == "extra"]
    if scope == "main":
        return [c for c, o in other.special if o != "extra"]
    return None


class CompatibilityIndex:
    def __init__(self, interfaces: Iterable[ComboInterface], card: Callable[[int], CardInfo | None],
                 settings: CompatibilitySettings = CompatibilitySettings()):
        self.items = list(interfaces)
        self.card = card
        self.settings = settings
        self.pos = {it.signature: i for i, it in enumerate(self.items)}
        n = max(len(self.items), 2)
        df: dict[int, int] = defaultdict(int)
        for it in self.items:
            for c in it.cards:
                df[c] += 1
        self.df = dict(df)
        # inverse document frequency, 0..1: a card in every combo weighs 0, a card in one combo 1
        self.weight = {c: math.log(n / k) / math.log(n) for c, k in df.items()}
        self.total = [sum(self.weight[c] for c in it.cards) for it in self.items]
        self._allows: dict[tuple[str, int], bool | None] = {}

    # ---- pairs ------------------------------------------------------------------------------
    def shared(self, a: ComboInterface, b: ComboInterface) -> tuple[list[int], float]:
        common = sorted(set(a.cards) & set(b.cards))
        s = sum(self.weight.get(c, 1.0) for c in common)
        union = sum(self.weight.get(c, 1.0) for c in set(a.cards) | set(b.cards))
        return common, (round(s / union, 4) if union > 0 else (1.0 if common else 0.0))

    @staticmethod
    def archetype(a: ComboInterface, b: ComboInterface) -> tuple[list[str], float]:
        common = sorted(set(a.archetypes) & set(b.archetypes))
        return common, round(max((min(a.archetypes[x], b.archetypes[x]) for x in common), default=0.0), 4)

    def _lock_allows(self, key: str, lock: Lock, code: int, own: int | None) -> bool | None:
        k = (key, code)
        if k not in self._allows:
            info = self.card(code)
            self._allows[k] = None if info is None else lock.allows(info, own)
        return self._allows[k]

    def conflicts(self, a: ComboInterface, b: ComboInterface) -> list[dict]:
        """Restrictions of `a` that forbid something `b` does."""
        out = []
        for lk in a.locks:
            cards = restricted_cards(lk, b)
            if not cards:
                continue
            lock, key = lock_of(lk), lock_key(lk)
            forbidden = sorted({c for c in cards if self._lock_allows(key, lock, c, lk.get("card")) is False})
            if not forbidden:
                continue
            share = sum(1 for c in cards if c in forbidden) / len(cards)
            factor = 1.0 if lk.get("whole_turn") or lk["scope"] == "field" else self.settings.order_factor
            out.append({"lock_card": lk.get("card"), "kind": lk["kind"], "scope": lk["scope"],
                        "allowed": lk.get("allowed"), "forbidden": lk.get("forbidden"),
                        "whole_turn": bool(lk.get("whole_turn")), "cards": forbidden,
                        "strength": round(share * factor, 4), "proxy": b.proxy})
        return out

    def relation(self, sig_a: str, sig_b: str) -> dict:
        a, b = self.items[self.pos[sig_a]], self.items[self.pos[sig_b]]
        cards, s = self.shared(a, b)
        arch, s_arch = self.archetype(a, b)
        return {"shared_cards": cards, "shared_strength": s, "archetypes": arch, "archetype_strength": s_arch,
                "a_forbids_b": self.conflicts(a, b), "b_forbids_a": self.conflicts(b, a)}

    # ---- all combos -------------------------------------------------------------------------
    def _postings(self) -> dict[int, list[int]]:
        post: dict[int, list[int]] = defaultdict(list)
        for i, it in enumerate(self.items):
            for c in it.cards:
                post[c].append(i)
        return post

    def shared_partners(self) -> Iterable[tuple[int, list[tuple[float, int]]]]:
        """Per combo: its strongest shared-card partners as (strength, index).

        Computed per *card set*: many combos use exactly the same cards and differ only in their result,
        so they share their partners. Cards that more than `ubiquitous_share` of all combos use (generic
        helpers like Foolish Burial) never create an edge on their own; they still count towards the
        strength of a pair that shares another card."""
        st, n = self.settings, len(self.items)
        groups: dict[tuple[int, ...], list[int]] = defaultdict(list)
        for i, it in enumerate(self.items):
            groups[it.cards].append(i)
        keys = list(groups)
        for members in groups.values():
            members.sort(key=lambda i: -self.items[i].score)
        common = {c for c, k in self.df.items() if k > st.ubiquitous_share * n}
        post: dict[int, list[int]] = defaultdict(list)
        for g, key in enumerate(keys):
            for c in key:
                if c not in common:
                    post[c].append(g)
        wsum = [sum(self.weight[c] for c in key) for key in keys]
        helpers = [frozenset(key) & common for key in keys]
        for g, key in enumerate(keys):
            acc: dict[int, float] = defaultdict(float)
            for c in key:
                if c in common:
                    continue
                w = self.weight[c]
                for h in post[c]:
                    acc[h] += w
            acc.pop(g, None)
            cand = []
            for h, s in acc.items():
                s += sum(self.weight[c] for c in helpers[g] & helpers[h])
                union = wsum[g] + wsum[h] - s
                strength = s / union if union > 0 else 1.0
                if strength >= st.min_shared:
                    cand.append((round(strength, 4), h))
            cand = heapq.nlargest(st.max_shared, cand)
            members = groups[key]
            for i in members:
                best = [(1.0, j) for j in members[: st.max_shared + 1] if j != i][: st.max_shared]   # same cards, other result
                for s, h in cand:
                    if len(best) >= st.max_shared:
                        break
                    best += [(s, j) for j in groups[keys[h]][: st.max_shared - len(best)]]
                yield i, best

    def archetype_partners(self) -> dict[int, list[tuple[float, int, str]]]:
        st = self.settings
        members: dict[str, list[tuple[float, float, int]]] = defaultdict(list)
        for i, it in enumerate(self.items):
            for a, share in it.archetypes.items():
                if share >= st.min_archetype:
                    members[a].append((share, it.score, i))
        out: dict[int, list[tuple[float, int, str]]] = defaultdict(list)
        for a, lst in members.items():
            lst.sort(reverse=True)
            for share, _, i in lst:
                taken = 0
                for share_j, _, j in lst:
                    if j == i:
                        continue
                    out[i].append((round(min(share, share_j), 4), j, a))
                    taken += 1
                    if taken >= st.max_archetype:
                        break
        return {i: heapq.nlargest(st.max_archetype, v) for i, v in out.items()}

    def conflict_sets(self) -> tuple[list[int], list[int]]:
        """Per combo, as bit sets over the combo indices: combos its restrictions forbid, combos that forbid it."""
        n = len(self.items)
        sp_all: dict[int, int] = defaultdict(int)
        sp_extra: dict[int, int] = defaultdict(int)
        sp_main: dict[int, int] = defaultdict(int)
        normal: dict[int, int] = defaultdict(int)
        act: dict[int, int] = defaultdict(int)
        for i, it in enumerate(self.items):
            bit = 1 << i
            for c, o in it.special:
                sp_all[c] |= bit
                (sp_extra if o == "extra" else sp_main)[c] |= bit
            for c in it.normal:
                normal[c] |= bit
            for c in it.activated:
                act[c] |= bit
        holders: dict[str, int] = defaultdict(int)
        sample: dict[str, dict] = {}
        for i, it in enumerate(self.items):
            for lk in it.locks:
                key = lock_key(lk)
                holders[key] |= 1 << i
                sample.setdefault(key, lk)
        violators: dict[str, int] = {}
        for key, lk in sample.items():
            if lk["kind"] == "NORMAL_SUMMON":
                table = normal
            elif lk["kind"] == "ACTIVATE":
                table = act
            else:
                table = {"all": sp_all, "field": sp_all, "extra": sp_extra, "main": sp_main}.get(lk["scope"])
            if table is None:
                violators[key] = 0
                continue
            lock, v = lock_of(lk), 0
            for code, bits in table.items():
                if self._lock_allows(key, lock, code, lk.get("card")) is False:
                    v |= bits
            violators[key] = v
        out = [0] * n
        for i, it in enumerate(self.items):
            for lk in it.locks:
                out[i] |= violators[lock_key(lk)]
            out[i] &= ~(1 << i)
        # incoming: combos with the same set of violated restrictions share the same set of forbidding combos
        violated: dict[int, list[str]] = defaultdict(list)
        for key, v in violators.items():
            for m in re.finditer("1", bin(v)[:1:-1]):   # set bits, lowest first
                violated[m.start()].append(key)
        cache: dict[tuple, int] = {}
        inc = [0] * n
        for i, keys in violated.items():
            t = tuple(sorted(keys))
            if t not in cache:
                h = 0
                for key in t:
                    h |= holders[key]
                cache[t] = h
            inc[i] = cache[t] & ~(1 << i)
        return out, inc

    def build(self) -> tuple[list[tuple], dict[str, dict]]:
        """(edges, summaries). Edge: (a, b, kind, strength, data); summary per signature: raw of the axis."""
        items, n = self.items, len(self.items)
        edges: dict[tuple[str, str, str], tuple[float, dict]] = {}
        pairs: set[tuple[int, int]] = set()
        partner_count = [0] * n
        for i, best in self.shared_partners():
            partner_count[i] = len(best)
            for s, j in best:
                a, b = (i, j) if items[i].signature < items[j].signature else (j, i)
                common, _ = self.shared(items[a], items[b])
                edges[(items[a].signature, items[b].signature, "shared_cards")] = (s, {"cards": common})
                pairs.add((a, b))
        arch_count = [0] * n
        for i, best in self.archetype_partners().items():
            arch_count[i] = len(best)
            for s, j, name in best:
                a, b = (i, j) if items[i].signature < items[j].signature else (j, i)
                edges[(items[a].signature, items[b].signature, "archetype")] = (s, {"archetype": name})
                pairs.add((a, b))
        for a, b in pairs:   # conflicts among the stored partners, both directions
            for x, y in ((a, b), (b, a)):
                found = self.conflicts(items[x], items[y])
                if found:
                    edges[(items[x].signature, items[y].signature, "lock_conflict")] = (
                        max(f["strength"] for f in found), {"conflicts": found})
        out, inc = self.conflict_sets()
        summaries = {}
        for i, it in enumerate(items):
            either = (out[i] | inc[i]).bit_count()
            share = 1.0 - either / (n - 1) if n > 1 else 1.0
            summaries[it.signature] = {
                "compatible_share": round(share, 6), "forbids": out[i].bit_count(), "forbidden_by": inc[i].bit_count(),
                "combos": n, "shared_partners": partner_count[i], "archetype_partners": arch_count[i],
                "locks": len(it.locks), "facts": "proxy" if it.proxy else "engine",
            }
        rows = [(a, b, kind, s, data) for (a, b, kind), (s, data) in edges.items()]
        return rows, summaries


def conflict_text(f: dict, name: Callable[[int], str]) -> str:
    """One conflicting restriction in words: which card restricts what, and which cards of the other combo it hits."""
    what = f"nur {f['allowed']}" if f.get("allowed") else f"nicht {f['forbidden']}" if f.get("forbidden") else "gar nichts"
    kind = {"SPECIAL_SUMMON": "Spezialbeschwörung", "NORMAL_SUMMON": "Normalbeschwörung", "ACTIVATE": "Aktivierung"}.get(
        f["kind"], f["kind"])
    scope = {"extra": " aus dem Extra Deck", "main": " außer aus dem Extra Deck"}.get(f["scope"], "")
    when = "ganzer Zug" if f["whole_turn"] else "für den Rest des Zuges, also in der richtigen Reihenfolge spielbar"
    return (f"{name(f['lock_card'])} erlaubt als {kind}{scope} {what}; betroffen: "
            f"{', '.join(name(c) for c in f['cards'])} ({when})")


def explain(raw: dict) -> str:
    text = (f"in einem Zug spielbar mit {raw['compatible_share']:.0%} der anderen Combos; sperrt {raw['forbids']}, "
            f"wird gesperrt von {raw['forbidden_by']}; {raw['shared_partners']} gespeicherte Partner mit gemeinsamen "
            f"Karten, {raw['archetype_partners']} im selben Archetyp")
    if raw["facts"] == "proxy":
        text += " (ohne Linienfakten: nur Beschwörungen aus dem Extra Deck berücksichtigt)"
    return text


def load_index(repo, card: Callable[[int], CardInfo | None],
               settings: CompatibilitySettings = CompatibilitySettings()) -> CompatibilityIndex:
    """Build the index from Database 2 (axes genericness, line, outcome)."""
    line = {sig: raw for sig, raw, _ in repo.axis_rows("line")}
    outcome = {sig: score or 0.0 for sig, _, score in repo.axis_rows("outcome")}
    return CompatibilityIndex(
        (interface(sig, raw, line.get(sig), card, outcome.get(sig, 0.0)) for sig, raw, _ in repo.axis_rows("genericness")),
        card, settings)


def fingerprint(repo, settings: CompatibilitySettings) -> str:
    """Changes whenever an input of the compatibility changes (combos, their restrictions, their line facts)."""
    h = hashlib.sha256(json.dumps([VERSION, sorted(settings.__dict__.items())]).encode())
    for axis in ("genericness", "line"):
        for sig, version in sorted(repo.versions(axis).items()):
            h.update(f"{axis}|{sig}|{version}\n".encode())
    return h.hexdigest()[:20]


def run_compatibility(repo, card: Callable[[int], CardInfo | None],
                      settings: CompatibilitySettings = CompatibilitySettings(), *, full: bool = False) -> dict:
    """Recompute edges and the axis if an input changed. Returns counts."""
    fp = fingerprint(repo, settings)
    if not full and repo.meta("compatibility_fingerprint") == fp:
        return {"skipped": True, **repo.edge_stats()}
    index = load_index(repo, card, settings)
    rows, summaries = index.build()
    analysis_id = repo.start({AXIS: VERSION}, {"compatibility": dict(settings.__dict__)}, "compatibility", len(index.items))
    repo.replace_compatibility(rows)
    repo.replace_axis(AXIS, [(sig, AXIS, VERSION, raw, raw["compatible_share"], explain(raw))
                             for sig, raw in summaries.items()], analysis_id)
    repo.normalise(AXIS)
    repo.finish(analysis_id, len(summaries))
    repo.set_meta("compatibility_fingerprint", fp)
    return {"skipped": False, **repo.edge_stats()}


__all__ = ["AXIS", "KINDS", "CompatibilityIndex", "CompatibilitySettings", "ComboInterface", "interface", "load_index",
           "run_compatibility"]
