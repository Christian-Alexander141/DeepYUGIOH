"""Step 3: Interaction Discovery - a static interaction graph over the card pool.

For every card a *profile* is derived from its effects and script references:

  provides  what its effects do to other cards   SEND_GY, ADD_HAND, SPECIAL_SUMMON, BANISH
  reacts    what makes the card itself useful     TO_GY, TO_HAND, SUMMONED, BANISHED
  uses_gy   its effects take cards from the GY (revive, recycle)

An interaction A -> B of kind X exists when A provides X, B reacts to X and A
can plausibly target B. Interactions are graded by how specific the targeting is:

  NAMED       A names B by card code (listed_names, materials, IsCode)
  RESTRICTED  A's targets are restricted by code/archetype and B matches
  CONSTRAINED A is generic but has level/type/attribute filters and B matches
  GENERIC     A targets anything of the right card type

NAMED and RESTRICTED interactions are materialised in the card database.
CONSTRAINED and GENERIC ones are far more numerous and are enumerated lazily
in a fixed order (`iter_generic`).

The graph only proposes candidates - the engine decides what really works.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Iterator

from deepyugioh import paths
from deepyugioh.cards.references import setcode_matches
from deepyugioh.contracts.cards import EnablerRole, Interaction, InteractionGrade
from deepyugioh.engine.constants import TYPE_EXTRA, TYPE_MONSTER, TYPE_SPELL, TYPE_TOKEN

PROVIDES = {
    "SEND_GY": {"CATEGORY_TOGRAVE", "CATEGORY_DECKDES", "CATEGORY_HANDES", "CATEGORY_RELEASE", "CATEGORY_DESTROY"},
    "ADD_HAND": {"CATEGORY_SEARCH", "CATEGORY_TOHAND"},
    "SPECIAL_SUMMON": {"CATEGORY_SPECIAL_SUMMON"},
    "BANISH": {"CATEGORY_REMOVE"},
}
REACTION = {"SEND_GY": "TO_GY", "ADD_HAND": "TO_HAND", "SPECIAL_SUMMON": "SUMMONED", "BANISH": "BANISHED"}
_SUMMON_EVENTS = {"EVENT_SUMMON_SUCCESS", "EVENT_SPSUMMON_SUCCESS", "EVENT_FLIP_SUMMON_SUCCESS"}
TYPE_NORMAL = 0x10


@dataclass
class Profile:
    code: int
    name: str
    type: int
    level: int
    race: int
    attribute: int
    setcodes: tuple[int, ...]
    has_script: bool
    script_len: int = 0
    categories: set[str] = field(default_factory=set)
    provides: set[str] = field(default_factory=set)
    reacts: set[str] = field(default_factory=set)
    uses_gy: bool = False
    ref_codes: set[int] = field(default_factory=set)
    ref_sets: set[int] = field(default_factory=set)
    max_level: int | None = None
    min_level: int | None = None
    races: int = 0
    attributes: int = 0
    tuner_filter: bool = False
    type_filters: set[str] = field(default_factory=set)
    locations: set[str] = field(default_factory=set)

    @property
    def is_monster(self) -> bool:
        return bool(self.type & TYPE_MONSTER)

    @property
    def is_extra(self) -> bool:
        return self.is_monster and bool(self.type & TYPE_EXTRA)

    @property
    def restricted(self) -> bool:
        return bool(self.ref_codes or self.ref_sets)

    @property
    def constrained(self) -> bool:
        return self.max_level is not None or self.min_level is not None or bool(self.races or self.attributes or self.tuner_filter)

    def accepts(self, b: "Profile") -> bool:
        """Do this card's simple filters allow `b` as target? (generous)"""
        if self.type_filters and not self._type_ok(b):
            return False
        if not b.is_monster:
            return not (self.max_level or self.min_level or self.races or self.attributes or self.tuner_filter)
        if self.max_level is not None and b.level > self.max_level:
            return False
        if self.min_level is not None and b.level < self.min_level:
            return False
        if self.races and not (b.race & self.races):
            return False
        if self.attributes and not (b.attribute & self.attributes):
            return False
        return True

    def _type_ok(self, b: "Profile") -> bool:
        kinds = {"MONSTER": b.is_monster, "SPELL": bool(b.type & TYPE_SPELL), "TRAP": bool(b.type & 0x4)}
        wanted = self.type_filters & kinds.keys()
        return not wanted or any(kinds[k] for k in wanted)

    def names(self, b: "Profile") -> bool:
        return b.code in self.ref_codes or any(setcode_matches(b.setcodes, s) for s in self.ref_sets)


def _eligible(p: Profile) -> bool:
    """Cards that can take part in combos: no tokens; effect cards need a script."""
    if p.type & TYPE_TOKEN:
        return False
    return p.has_script or bool(p.type & TYPE_NORMAL)


class CardGraph:
    """InteractionGraph implementation backed by the card database."""

    def __init__(self, path: Path | None = None):
        self.path = path or paths.card_db()
        self.db = sqlite3.connect(self.path)

    # ---- profiles ---------------------------------------------------------
    @cached_property
    def profiles(self) -> dict[int, Profile]:
        out: dict[int, Profile] = {}
        for code, name, ctype, level, race, attr, setcodes, script in self.db.execute(
            "SELECT code, name, type, level, race, attribute, setcodes, script FROM cards WHERE is_alt_art = 0"
        ):
            out[code] = Profile(code, name, ctype, level, race, attr, tuple(json.loads(setcodes)), script is not None)
        for code, cats, events, ranges in self.db.execute("SELECT code, categories, event, ranges FROM effects"):
            p = out.get(code)
            if p is None:
                continue
            cats, events, ranges = set(json.loads(cats)), set(json.loads(events)), set(json.loads(ranges))
            p.categories |= cats
            for kind, wanted in PROVIDES.items():
                if cats & wanted:
                    p.provides.add(kind)
            if "EVENT_TO_GRAVE" in events or "LOCATION_GRAVE" in ranges:
                p.reacts.add("TO_GY")
            if "EVENT_REMOVE" in events or "LOCATION_REMOVED" in ranges:
                p.reacts.add("BANISHED")
            if events & _SUMMON_EVENTS or "LOCATION_MZONE" in ranges:
                p.reacts.add("SUMMONED")
        for code, codes, sets, mx, mn, races, attrs, tuner, tfilters, locs, slen in self.db.execute(
            "SELECT code, codes, setcodes, max_level, min_level, races, attributes, tuner, type_filters, locations,"
            " script_len FROM card_refs"
        ):
            p = out.get(code)
            if p is None:
                continue
            p.ref_codes, p.ref_sets = set(json.loads(codes)), set(json.loads(sets))
            p.max_level, p.min_level, p.races, p.attributes = mx, mn, races, attrs
            p.tuner_filter, p.locations, p.script_len = bool(tuner), set(json.loads(locs)), slen
            p.type_filters = set(json.loads(tfilters))
        for p in out.values():
            if not p.is_extra:
                p.reacts.add("TO_HAND")  # every main-deck card can be played from the hand
            p.uses_gy = "GRAVE" in p.locations and bool(p.provides & {"SPECIAL_SUMMON", "ADD_HAND"})
        return {c: p for c, p in out.items() if _eligible(p)}

    @cached_property
    def _by_setbase(self) -> dict[int, list[Profile]]:
        idx = defaultdict(list)
        for p in self.profiles.values():
            for sc in p.setcodes:
                idx[sc & 0xFFF].append(p)
        return idx

    def _named_targets(self, a: Profile) -> list[Profile]:
        out = {c: self.profiles[c] for c in a.ref_codes if c in self.profiles}
        for s in a.ref_sets:
            for b in self._by_setbase.get(s & 0xFFF, ()):
                if setcode_matches(b.setcodes, s):
                    out[b.code] = b
        out.pop(a.code, None)
        return [out[c] for c in sorted(out)]

    # ---- interactions -----------------------------------------------------
    def _specific(self) -> Iterator[Interaction]:
        """NAMED + RESTRICTED interactions, deterministic order."""
        profiles = self.profiles
        for code in sorted(profiles):
            a = profiles[code]
            for b in self._named_targets(a):
                if b.code in a.ref_codes:
                    yield Interaction(a.code, b.code, "NAMED", InteractionGrade.NAMED)
                for kind in sorted(a.provides):
                    if REACTION[kind] in b.reacts and not b.is_extra:
                        yield Interaction(a.code, b.code, kind, InteractionGrade.RESTRICTED)
        # B names A: "if sent to the GY by the effect of a 'Junk' card ..."
        for code in sorted(profiles):
            b = profiles[code]
            for a in self._named_targets(b):
                if a.restricted:
                    continue  # already covered from A's side if A names B
                for kind in sorted(a.provides):
                    if REACTION[kind] in b.reacts and not b.is_extra:
                        yield Interaction(a.code, b.code, kind, InteractionGrade.RESTRICTED)

    def iter_generic(self, grade: InteractionGrade) -> Iterator[Interaction]:
        """CONSTRAINED or GENERIC interactions, enumerated lazily in a fixed order."""
        profiles = self.profiles
        reactive = defaultdict(list)
        for code in sorted(profiles):
            b = profiles[code]
            if not b.is_extra:
                for r in b.reacts:
                    reactive[r].append(b)
        for code in sorted(profiles):
            a = profiles[code]
            if a.restricted or not a.provides:
                continue
            if (grade is InteractionGrade.CONSTRAINED) != a.constrained:
                continue
            for kind in sorted(a.provides):
                if kind == "ADD_HAND":
                    continue  # unrestricted searchers add "any card": covered by enablers, not pairs
                for b in reactive[REACTION[kind]]:
                    if b.code != a.code and a.accepts(b) and (kind != "SPECIAL_SUMMON" or b.is_monster):
                        yield Interaction(a.code, b.code, kind, grade)

    # ---- enablers ---------------------------------------------------------
    @cached_property
    def enablers(self) -> dict[EnablerRole, int]:
        """Generic, unconstrained spell cards that fill a common need of other cards.

        The representative of each role is the eligible card with the shortest
        script (fewest extra conditions); ties are broken by the lower code.
        """
        # role -> (provided kind, required category, forbidden categories, source location)
        wants = {
            EnablerRole.SEND_GY_FROM_DECK: ("SEND_GY", "CATEGORY_TOGRAVE", {"CATEGORY_DECKDES"}, "DECK"),
            EnablerRole.SPSUMMON_FROM_GY: ("SPECIAL_SUMMON", "CATEGORY_SPECIAL_SUMMON", set(), "GRAVE"),
            EnablerRole.BANISH_FROM_GY: ("BANISH", "CATEGORY_REMOVE", set(), "GRAVE"),
        }
        out = {}
        for role, (kind, category, forbidden, loc) in wants.items():
            pool = [
                p for p in self.profiles.values()
                if p.type == TYPE_SPELL and not p.restricted and not p.constrained
                and p.provides == {kind} and category in p.categories and not (p.categories & forbidden)
                and loc in p.locations
                and (role is not EnablerRole.SEND_GY_FROM_DECK or "MONSTER" in p.type_filters)
                and not (p.type_filters & {"EQUIP", "SPELL", "TRAP", "FIELD", "CONTINUOUS", "QUICKPLAY", "RITUAL"})
            ]
            if pool:
                out[role] = min(pool, key=lambda p: (p.script_len, p.code)).code
        return out

    def needs(self, code: int) -> set[EnablerRole]:
        p = self.profiles.get(code)
        if p is None:
            return set()
        out = set()
        if "TO_GY" in p.reacts or p.uses_gy:
            out.add(EnablerRole.SEND_GY_FROM_DECK)
        if "SUMMONED" in p.reacts and p.is_monster and not p.is_extra and "TO_GY" in p.reacts:
            out.add(EnablerRole.SPSUMMON_FROM_GY)
        if "BANISHED" in p.reacts:
            out.add(EnablerRole.BANISH_FROM_GY)
        return out

    # ---- neighbourhood ----------------------------------------------------
    @cached_property
    def _specific_by_card(self) -> dict[int, list[Interaction]]:
        out = defaultdict(list)
        for it in self.interactions():
            out[it.source].append(it)
            out[it.target].append(it)
        return out

    def interactions(self) -> list[Interaction]:
        rows = self.db.execute("SELECT source, target, kind, grade FROM interactions ORDER BY rowid").fetchall()
        return [Interaction(s, t, k, InteractionGrade(g)) for s, t, k, g in rows]

    def neighbors(self, code: int) -> list[Interaction]:
        return self._specific_by_card.get(code, [])

    def related_extra(self, code: int) -> list[int]:
        """Extra deck monsters that name this card or one of its archetypes (named first)."""
        p = self.profiles.get(code)
        if p is None:
            return []
        named, archetype = [], []
        for e in self._extra_profiles:
            if code in e.ref_codes or e.code in p.ref_codes:
                named.append(e.code)
            elif any(setcode_matches(p.setcodes, s) for s in e.ref_sets):
                archetype.append(e.code)
        return named + archetype

    @cached_property
    def _extra_profiles(self) -> list[Profile]:
        return [p for _, p in sorted(self.profiles.items()) if p.is_extra and (p.ref_codes or p.ref_sets)]

    def starters(self) -> list[int]:
        """Every eligible card with an effect is a starting point (normal monsters are only targets)."""
        return [c for c, p in sorted(self.profiles.items()) if p.has_script and not (p.type & TYPE_NORMAL and not p.provides)]

    # ---- build ------------------------------------------------------------
    def build(self) -> dict:
        """Materialise NAMED/RESTRICTED interactions and statistics into the card DB."""
        self.db.executescript(
            """
            DROP TABLE IF EXISTS interactions;
            CREATE TABLE interactions (
                source INTEGER NOT NULL,
                target INTEGER NOT NULL,
                kind   TEXT NOT NULL,     -- NAMED | SEND_GY | ADD_HAND | SPECIAL_SUMMON | BANISH
                grade  TEXT NOT NULL,     -- NAMED | RESTRICTED
                UNIQUE (source, target, kind)
            );
            CREATE INDEX interactions_target ON interactions(target);
            DROP TABLE IF EXISTS graph_meta;
            CREATE TABLE graph_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        counts = defaultdict(int)
        for it in self._specific():
            cur = self.db.execute("INSERT OR IGNORE INTO interactions VALUES (?,?,?,?)", (it.source, it.target, it.kind, it.grade.value))
            counts[it.grade.value] += cur.rowcount
        for grade in (InteractionGrade.CONSTRAINED, InteractionGrade.GENERIC):
            counts[grade.value] = sum(1 for _ in self.iter_generic(grade))
        stats = {
            "cards": len(self.profiles),
            "starters": len(self.starters()),
            "interactions": dict(counts),
            "enablers": {r.value: c for r, c in self.enablers.items()},
        }
        stats["version"] = hashlib.sha256(json.dumps(stats, sort_keys=True).encode()).hexdigest()[:16]
        self.db.executemany("INSERT INTO graph_meta VALUES (?,?)", [(k, json.dumps(v)) for k, v in stats.items()])
        self.db.commit()
        self.__dict__.pop("_specific_by_card", None)
        return stats

    def meta(self) -> dict:
        try:
            return {k: json.loads(v) for k, v in self.db.execute("SELECT key, value FROM graph_meta")}
        except sqlite3.OperationalError:
            return {}

    def version(self) -> str:
        return self.meta().get("version", "unbuilt")
