"""Quality filter (Stufe 2, step 2.6): filtered, ranked combo sets for the deck builder.

Phase 1 stores every combo it finds. A *filter profile* decides what the deck
builder sees:

    minimum        least score (0-100) per axis or scoring ("strength": 70), or least raw value
                   ("outcome.disruptions": 1)
    rank_by        the axis or scoring the members are ordered by; or own `weights` over components
    deck           a deck profile (archetypes, types, attributes) with a least fit `min_fit`
    locks          any | none (no certain restriction) | fitting (only restrictions the deck satisfies)
    min/max_cards  number of required cards
    late           None | True (only combos from later game situations) | False (only from the start)
    drop_dominated hide combos that need the same or more cards than another combo of their family
                   (same result) and are not better on any axis

Profiles and their ranked members are stored in Database 2 (`filter_profiles`,
`filter_results`). The stored members remember the state of Database 2 they
were computed from, so a stale result is never used unnoticed. A profile can
be exported as its own JSON file.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from deepyugioh.analysis.specificity import deck_fit, deck_profile_from_names, lock_fits
from deepyugioh.analysis.strength import percentiles

LOCK_POLICIES = ("any", "none", "fitting")
DOMINANCE_AXES = ("cost", "genericness", "robustness")   # plus the combo's own starting-hand chance


@dataclass(frozen=True)
class FilterProfile:
    name: str
    label: str = ""
    minimum: dict[str, float] = field(default_factory=dict)
    rank_by: str = "strength"
    weights: dict[str, float] = field(default_factory=dict)   # own ranking; overrides rank_by
    archetypes: tuple[str, ...] = ()
    races: tuple[str, ...] = ()
    attributes: tuple[str, ...] = ()
    min_fit: float | None = None
    locks: str = "any"
    min_cards: int | None = None
    max_cards: int | None = None
    late: bool | None = None
    drop_dominated: bool = True
    limit: int | None = None

    def __post_init__(self):
        if not self.name or not self.name.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"filter name {self.name!r}: letters, digits, - and _ only")
        if self.locks not in LOCK_POLICIES:
            raise ValueError(f"locks must be one of {LOCK_POLICIES}")
        if self.locks == "fitting" and not self.has_deck:
            raise ValueError("locks = fitting needs a deck profile")
        if self.min_fit is not None and not self.has_deck:
            raise ValueError("min_fit needs a deck profile (archetypes, races or attributes)")
        if any(w < 0 for w in self.weights.values()) or (self.weights and not any(self.weights.values())):
            raise ValueError("weights must not be negative and not all zero")
        for name in ("archetypes", "races", "attributes"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def has_deck(self) -> bool:
        return bool(self.archetypes or self.races or self.attributes)

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in d.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "FilterProfile":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown filter options: {sorted(unknown)}")
        return cls(**d)


DEFAULT_PROFILES = (
    FilterProfile("stark", "Allgemein stark", {"strength": 70.0}),
    FilterProfile("eroeffnung", "Spielbeginn: mindestens eine Störung", {"opening": 60.0, "outcome.disruptions": 1},
                  rank_by="opening"),
    FilterProfile("durchbrechen", "Als Zweiter: nimmt dem Gegner Karten", {"breaking": 70.0, "outcome.opp_losses": 1},
                  rank_by="breaking"),
)


@dataclass
class FilterResult:
    profile: FilterProfile
    members: list[tuple[str, float, float | None]]   # (signature, ranking value, deck fit), best first
    considered: int
    dropped: dict[str, int]                          # reason -> combos removed


class QualityFilter:
    def __init__(self, repo, consts: dict[str, int] | None = None):
        self.repo = repo
        self.consts = consts
        self._cache: dict = {}

    def _scores(self, axis: str) -> dict[str, float]:
        if ("s", axis) not in self._cache:
            self._cache[("s", axis)] = self.repo.scores(axis)
        return self._cache[("s", axis)]

    def _raw(self, axis: str) -> dict[str, dict]:
        if ("r", axis) not in self._cache:
            self._cache[("r", axis)] = {sig: raw for sig, raw, _ in self.repo.axis_rows(axis)}
        return self._cache[("r", axis)]

    def _component(self, comp: str) -> dict[str, float]:
        """0-100 per combo: the score of an axis, or the percentile of a raw value."""
        axis, _, key = comp.partition(".")
        if not key:
            return self._scores(axis)
        if ("c", comp) not in self._cache:
            vals = {sig: float(raw[key]) for sig, raw in self._raw(axis).items() if key in raw}
            self._cache[("c", comp)] = percentiles(vals, floor_minimum=True) if vals else {}
        return self._cache[("c", comp)]

    def apply(self, profile: FilterProfile) -> FilterResult:
        generic = self._raw("genericness")
        dropped: dict[str, int] = defaultdict(int)
        alive = set(generic)
        considered = len(alive)

        def keep(pred: Callable[[str], bool], reason: str):
            nonlocal alive
            kept = {s for s in alive if pred(s)}
            if len(kept) != len(alive):
                dropped[reason] += len(alive) - len(kept)
            alive = kept

        for comp, least in profile.minimum.items():
            axis, _, key = comp.partition(".")
            if key:
                raws = self._raw(axis)
                keep(lambda s: float(raws.get(s, {}).get(key, float("-inf"))) >= least, f"minimum {comp}")
            else:
                scores = self._scores(axis)
                if not scores:
                    raise ValueError(f"filter {profile.name}: axis {axis!r} has no results in Database 2")
                keep(lambda s: scores.get(s, float("-inf")) >= least, f"minimum {comp}")
        if profile.min_cards is not None:
            keep(lambda s: len(generic[s].get("cards", ())) >= profile.min_cards, "min_cards")
        if profile.max_cards is not None:
            keep(lambda s: len(generic[s].get("cards", ())) <= profile.max_cards, "max_cards")
        if profile.late is not None:
            cost = self._raw("cost")
            keep(lambda s: bool(cost.get(s, {}).get("resources", 0)) == profile.late, "late")
        fits: dict[str, float] = {}
        if profile.has_deck:
            deck = deck_profile_from_names(profile.archetypes, profile.races, profile.attributes, self.consts)
            fits = {s: deck_fit(generic[s], deck) for s in alive}
            if profile.min_fit is not None:
                keep(lambda s: fits[s] >= profile.min_fit, "min_fit")
            if profile.locks == "fitting":
                keep(lambda s: all(lock_fits(lk, deck) for lk in generic[s].get("locks", ()) if lk.get("certain")),
                     "locks")
        if profile.locks == "none":
            keep(lambda s: not any(lk.get("certain") for lk in generic[s].get("locks", ())), "locks")
        if profile.drop_dominated:
            dominated = self._dominated(alive)
            dropped["dominated"] += len(dominated)
            alive -= dominated

        if profile.weights:
            comps = {c: self._component(c) for c, w in profile.weights.items() if w > 0}
            value = {}
            for s in alive:
                used = [(profile.weights[c], v[s]) for c, v in comps.items() if s in v]
                wsum = sum(w for w, _ in used)
                value[s] = sum(w * v for w, v in used) / wsum if wsum else 0.0
        else:
            ranking = self._component(profile.rank_by)
            if not ranking:
                raise ValueError(f"filter {profile.name}: nothing to rank by in {profile.rank_by!r}")
            value = {s: ranking.get(s, 0.0) for s in alive}
        order = sorted(alive, key=lambda s: (-value[s], -fits.get(s, 0.0), s))
        if profile.limit:
            dropped["limit"] += max(0, len(order) - profile.limit)
            order = order[: profile.limit]
        return FilterResult(profile, [(s, round(value[s], 3), fits.get(s)) for s in order], considered,
                            {k: v for k, v in dropped.items() if v})

    def _dominated(self, alive: set[str]) -> set[str]:
        """Combos another combo of their family beats: not more cards, at least as good on every axis, better on one."""
        cons, generic = self._raw("consistency"), self._raw("genericness")
        values = {a: self.repo.values(a) for a in DOMINANCE_AXES}
        families: dict[str, list[str]] = defaultdict(list)
        for s in alive:
            fam = cons.get(s, {}).get("family")
            if fam:
                families[fam].append(s)

        def vector(s):
            return (len(generic[s].get("cards", ())),
                    tuple([cons[s].get("own_first", 0.0)] + [values[a].get(s) for a in DOMINANCE_AXES]))

        out = set()
        for members in families.values():
            if len(members) < 2:
                continue
            vec = {s: vector(s) for s in members}
            for s in members:
                n, v = vec[s]
                for o in members:
                    if o == s:
                        continue
                    m, w = vec[o]
                    if m > n:
                        continue
                    pairs = [(a, b) for a, b in zip(w, v) if a is not None and b is not None]
                    if all(a >= b for a, b in pairs) and (m < n or any(a > b for a, b in pairs)):
                        out.add(s)
                        break
        return out

    # ---- stored profiles ----------------------------------------------------------------------
    def save(self, profile: FilterProfile):
        self.repo.save_filter(profile.name, profile.to_dict())

    def profiles(self) -> dict[str, FilterProfile]:
        return {n: FilterProfile.from_dict(info["spec"]) for n, info in self.repo.filters().items()}

    def ensure_defaults(self):
        existing = self.repo.filters()
        for p in DEFAULT_PROFILES:
            if p.name not in existing:
                self.save(p)

    def run(self, name: str) -> FilterResult:
        """Apply a stored profile and store its ranked members."""
        info = self.repo.filters().get(name)
        if info is None:
            raise KeyError(f"no filter profile {name!r}")
        result = self.apply(FilterProfile.from_dict(info["spec"]))
        self.repo.store_filter_results(name, ((s, i + 1, v, f) for i, (s, v, f) in enumerate(result.members)),
                                       result.considered, result.dropped)
        return result

    def run_all(self) -> dict[str, FilterResult | str]:
        out: dict[str, FilterResult | str] = {}
        for name in self.repo.filters():
            try:
                out[name] = self.run(name)
            except (ValueError, KeyError) as e:   # e.g. an axis that has no results yet
                out[name] = str(e).strip("'\"")
        return out

    def export(self, name: str, path: Path, combos=None, card_name: Callable[[int], str] = str) -> int:
        """Write a profile with its members (and their evaluation) to its own JSON file. Returns the member count.

        `combos`: signature -> ComboRecord lookup (Database 1); without it only the evaluation is exported."""
        info = self.repo.filters().get(name)
        if info is None:
            raise KeyError(f"no filter profile {name!r}")
        members = []
        for sig, rank, value, fit in self.repo.filter_results(name):
            prof = self.repo.profile(sig)
            entry = {"rank": rank, "signature": sig, "value": value, "fit": fit,
                     "axes": {a: {"score": x.score, "value": x.value, "explanation": x.explanation}
                              for a, x in sorted(prof.items())}}
            combo = combos(sig) if combos else None
            if combo is not None:
                entry["cards"] = [{"code": c, "name": card_name(c)} for c in combo.cards]
                entry["requires"] = {z: list(getattr(combo, z)) for z in ("hand", "deck", "extra", "grave", "banished", "field")
                                     if getattr(combo, z, ())}
                entry["line"] = [s.label for s in combo.line]
                entry["setup"] = combo.setup.to_dict()
            members.append(entry)
        data = {"profile": info["spec"], "stale": info["stale"], "considered": info["considered"],
                "dropped": info["dropped"], "state": self.repo.state(), "members": members}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(members)


__all__ = ["DEFAULT_PROFILES", "FilterProfile", "FilterResult", "QualityFilter"]
