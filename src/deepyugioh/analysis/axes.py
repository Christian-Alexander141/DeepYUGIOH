"""The first three axes of the Analyzer System (Stufe 2, steps 2.2): outcome, cost, consistency.

All three are computed from Database 1 and the card knowledge only - no
simulation. Each returns raw facts, one scalar (higher = better) and an
explanation. The weights *inside* an axis are part of its version; the
weights *between* axes (overall strength) come later from the settings.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable

from deepyugioh.analysis.probability import any_requirement, requirement
from deepyugioh.contracts.analysis import AxisResult
from deepyugioh.contracts.cards import CardPool
from deepyugioh.contracts.combos import ComboRecord
from deepyugioh.contracts.effects import EffectKnowledge, EffectSignature

POS_FACEDOWN = 0x2 | 0x8
TYPE_MONSTER, TYPE_SPELL, TYPE_TRAP, TYPE_QUICKPLAY = 0x1, 0x2, 0x4, 0x10000
NEGATE = {"CATEGORY_NEGATE", "CATEGORY_DISABLE", "CATEGORY_DISABLE_SUMMON"}
REMOVAL = {"CATEGORY_DESTROY", "CATEGORY_REMOVE", "CATEGORY_TOHAND", "CATEGORY_TODECK", "CATEGORY_CONTROL",
           "CATEGORY_POSITION", "CATEGORY_TOGRAVE", "CATEGORY_RELEASE"}
OPPONENT_EVENTS = {"SUMMON", "SPSUMMON", "FLIP_SUMMON", "CHAINING", "ATTACK", "TO_HAND", "DRAW", "PHASE"}


@dataclass(frozen=True)
class DeckModel:
    deck_size: int = 40
    copies: int = 3
    hand_first: int = 5
    hand_second: int = 6
    family_max_cards: int = 14   # relevant hand cards per family for the exact union probability


# ---- outcome --------------------------------------------------------------------------
class OutcomeAnalyzer:
    """Ergebnis: what the end board does for us and against the opponent."""

    name = "outcome"
    version = "2"   # 2: raw opp_losses (what the opponent loses, for "Durchbrechen")
    global_ = False
    # value = sum(weight * raw[key]) - documented, part of the version
    WEIGHTS = {"wins": 100.0, "negates": 4.0, "disruptions": 3.0, "unclear_disruptions": 1.0, "opp_field_loss": 2.0,
               "opp_hand_loss": 2.0, "opp_lp_loss_k": 1.0, "card_advantage": 1.0, "monsters": 0.5, "atk_total_k": 0.3}

    def __init__(self, pool: CardPool, knowledge: EffectKnowledge | None):
        self.pool = pool
        self.knowledge = knowledge
        if knowledge is not None:  # the disruptions come from the effect signatures
            self.version = f"{type(self).version}/{knowledge.version()}"

    def prepare(self, combos: Iterable[ComboRecord]) -> None:
        pass

    def _sigs(self, code: int) -> list[EffectSignature]:
        return self.knowledge.signatures(code) if self.knowledge is not None else []

    def disruptions(self, own: dict) -> tuple[list[tuple[int, str, str]], int]:
        """(card, zone, kind) of effects usable in the opponent's turn that hit the opponent; unclear count."""
        found, seen_limits, unclear = [], set(), 0
        cards = [(c["code"], "MZONE", c.get("pos", 1)) for c in own["mzone"]] + \
                [(c["code"], "SZONE", c.get("pos", 1)) for c in own["szone"]] + \
                [(c, "GRAVE", 1) for c in own["grave"]] + [(c, "HAND", 1) for c in own["hand"]] + \
                [(c, "BANISHED", 1) for c in own["banished"]]
        for code, zone, pos in cards:
            card = self.pool.get(code)
            if card is None:
                continue
            facedown = bool(pos & POS_FACEDOWN)
            for sig in self._sigs(code):
                if not self._usable_in_opponent_turn(sig, card, zone, facedown):
                    continue
                cats = set(sig.categories)
                hits_opp = any(m.opponent for m in sig.moves)
                if cats & NEGATE:
                    kind = "negate"
                elif hits_opp:
                    kind = "removal"
                elif cats & REMOVAL and sig.unknown:
                    unclear += 1
                    continue
                else:
                    continue
                if sig.count_limit and sig.count_limit.startswith("HOPT"):
                    key = (code, sig.count_limit)   # a hard once-per-turn counts once, however many copies
                    if key in seen_limits:
                        continue
                    seen_limits.add(key)
                found.append((code, zone, kind))
        return found, unclear

    @staticmethod
    def _usable_in_opponent_turn(sig: EffectSignature, card, zone: str, facedown: bool) -> bool:
        if zone == "SZONE" and facedown:
            # set traps and quick-play spells can be activated in the opponent's turn
            return sig.kind == "ACTIVATE" and bool(card.type & TYPE_TRAP or card.type & TYPE_QUICKPLAY)
        if zone == "MZONE" and facedown:
            return False
        if sig.kind == "QUICK":
            return zone in sig.location
        if sig.kind == "TRIGGER" and sig.trigger and not sig.trigger.self_only:
            return zone in sig.location and sig.trigger.event in OPPONENT_EVENTS
        if sig.kind == "ACTIVATE" and zone == "HAND":
            return False  # spells from the hand only in our own turn (quick-plays need to be set first)
        return False

    def analyze(self, combo: ComboRecord) -> AxisResult:
        own = combo.end_board["players"][0]
        b = combo.benefit
        monsters = [c for c in own["mzone"]]
        atk = []
        for c in monsters:
            card = self.pool.get(c["code"])
            if card is not None and not c.get("pos", 1) & POS_FACEDOWN:
                atk.append(max(card.atk, 0))
        found, unclear = self.disruptions(own)
        negates = sum(1 for _, _, k in found if k == "negate")
        raw = {
            "monsters": len(monsters),
            "field_cards": len(own["mzone"]) + len(own["szone"]),
            "atk_total": sum(atk),
            "atk_max": max(atk, default=0),
            "negates": negates,
            "disruptions": len(found),
            "unclear_disruptions": unclear,
            "disruption_cards": [[c, z, k] for c, z, k in found],
            "card_advantage": b.get("card_advantage", 0),
            "hand_after": len(own["hand"]),
            "opp_field_loss": b.get("opp_field_loss", 0),
            "opp_hand_loss": b.get("opp_hand_loss", 0),
            "opp_lp_loss": b.get("opp_lp_loss", 0),
            "wins": bool(b.get("wins", False)),
        }
        raw["opp_losses"] = round(raw["opp_field_loss"] + raw["opp_hand_loss"] + raw["opp_lp_loss"] / 1000
                                  + (10 if raw["wins"] else 0), 3)
        terms = dict(raw, opp_lp_loss_k=raw["opp_lp_loss"] / 1000, atk_total_k=raw["atk_total"] / 1000,
                     disruptions=raw["disruptions"] - negates, wins=1.0 if raw["wins"] else 0.0)
        value = sum(w * float(terms[k]) for k, w in self.WEIGHTS.items())
        parts = []
        if raw["wins"]:
            parts.append("gewinnt das Duell")
        if raw["disruptions"]:
            parts.append(f"{raw['disruptions']} Störung(en) für den gegnerischen Zug, davon {negates} Negate")
        elif unclear:
            parts.append(f"{unclear} mögliche Störung(en), nicht sicher erkannt")
        else:
            parts.append("keine erkannte Störung")
        parts.append(f"{raw['monsters']} Monster ({raw['atk_total']} ATK)")
        if raw["opp_field_loss"] or raw["opp_hand_loss"]:
            parts.append(f"Gegner verliert {raw['opp_field_loss']} Feld- und {raw['opp_hand_loss']} Handkarte(n)")
        if raw["opp_lp_loss"]:
            parts.append(f"{raw['opp_lp_loss']} Schaden")
        parts.append(f"Kartenvorteil {raw['card_advantage']:+d}")
        return AxisResult(raw, round(value, 4), ", ".join(parts))


# ---- cost -----------------------------------------------------------------------------
class CostAnalyzer:
    """Einsatz: what the combo needs. Higher value = cheaper."""

    name = "cost"
    version = "1"
    global_ = False
    WEIGHTS = {"hand": 2.0, "deck": 1.0, "extra": 0.5, "actions": 0.05, "lp_paid_k": 1.0, "normal_summon": 0.5}

    def prepare(self, combos: Iterable[ComboRecord]) -> None:
        pass

    def analyze(self, combo: ComboRecord) -> AxisResult:
        actions = sum(1 for s in combo.line if s.decision == "MSG_SELECT_IDLECMD" and not s.label.startswith("end")) \
            + sum(1 for s in combo.line if s.decision == "MSG_SELECT_CHAIN" and s.label.startswith("chain")) \
            + sum(1 for s in combo.line if s.decision == "MSG_SELECT_EFFECTYN" and s.label.endswith("yes"))
        normal_summon = any(s.decision == "MSG_SELECT_IDLECMD" and s.label.startswith("normal summon") for s in combo.line)
        lp_paid = max(0, -int(combo.benefit.get("lp_gain", 0)))
        raw = {
            "cards": combo.card_count, "hand": len(combo.hand), "deck": len(combo.deck), "extra": len(combo.extra),
            "actions": actions, "lp_paid": lp_paid, "normal_summon": normal_summon,
        }
        terms = dict(raw, lp_paid_k=lp_paid / 1000, normal_summon=1.0 if normal_summon else 0.0)
        value = -sum(w * float(terms[k]) for k, w in self.WEIGHTS.items())
        text = (f"{raw['cards']} Karten ({raw['hand']} Hand, {raw['deck']} Deck, {raw['extra']} Extra Deck), "
                f"{actions} Aktionen" + (f", {lp_paid} LP" if lp_paid else "")
                + (", verbraucht die Normalbeschwörung" if normal_summon else ""))
        return AxisResult(raw, round(value, 4), text)


# ---- consistency ------------------------------------------------------------------------
def hand_requirement(combo: ComboRecord) -> tuple[Counter, bool]:
    """Cards the opening hand must contain. If Phase 1 recorded none although the setup had hand cards
    (a card used without leaving the hand, e.g. revealed), the setup's hand is taken - a safe upper bound.
    Second value: True if that fallback was used."""
    if combo.hand or not combo.setup.hand:
        return Counter(combo.hand), False
    return Counter(combo.setup.hand), True


def gained_hand(combo: ComboRecord) -> list[int]:
    """Hand after the combo without the setup's hand cards the combo did not use."""
    own = combo.end_board["players"][0]
    unused = Counter(combo.setup.hand) - Counter(combo.hand)
    return sorted((Counter(own["hand"]) - unused).elements())


def family_key(combo: ComboRecord) -> str:
    """Combos with the same result form a family: own field, cards gained in the hand, what the opponent loses.
    Members differ in their starting cards - more members, more ways to reach the same result."""
    own = combo.end_board["players"][0]
    field_cards = sorted([[c["code"], bool(c.get("pos", 1) & POS_FACEDOWN)] for c in own["mzone"] + own["szone"]])
    b = combo.benefit
    payload = [field_cards, gained_hand(combo), b.get("opp_field_loss", 0), b.get("opp_hand_loss", 0),
               b.get("opp_lp_loss", 0), bool(b.get("wins", False))]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


class ConsistencyAnalyzer:
    """Konsistenz: chance to open the hand cards of the combo - or of any combo of its family."""

    name = "consistency"
    version = "3"
    global_ = True   # depends on the other members of the family

    def __init__(self, deck: DeckModel = DeckModel()):
        self.deck = deck
        self.families: dict[str, list[Counter]] = {}
        self._family_p: dict[tuple[str, int], tuple[float, bool, int]] = {}

    def prepare(self, combos: Iterable[ComboRecord]) -> None:
        fam = defaultdict(set)
        for c in combos:
            fam[family_key(c)].add(tuple(sorted(hand_requirement(c)[0].elements())))
        self.families = {k: [Counter(h) for h in sorted(v)] for k, v in fam.items()}
        self._family_p = {}

    def _p(self, req: Counter, hand: int) -> float:
        d = self.deck
        return requirement(req, deck_size=d.deck_size, copies=d.copies, hand_size=hand)

    def family_probability(self, key: str, hand: int) -> tuple[float, bool, int]:
        """(probability, truncated, members used)."""
        if (key, hand) in self._family_p:
            return self._family_p[(key, hand)]
        d = self.deck
        members = sorted(self.families.get(key, []), key=lambda r: (-self._p(r, hand), sorted(r.items())))
        chosen, codes, truncated = [], set(), False
        for r in members:
            new = codes | set(r)
            if len(new) > d.family_max_cards:
                truncated = True
                continue
            chosen.append(r)
            codes = new
        p = any_requirement(chosen, deck_size=d.deck_size, copies=d.copies, hand_size=hand) if chosen else 0.0
        self._family_p[(key, hand)] = (p, truncated, len(chosen))
        return self._family_p[(key, hand)]

    def analyze(self, combo: ComboRecord) -> AxisResult:
        d = self.deck
        req, fallback = hand_requirement(combo)
        key = family_key(combo)
        own_first, own_second = self._p(req, d.hand_first), self._p(req, d.hand_second)
        fam_first, truncated, used = self.family_probability(key, d.hand_first) if key in self.families else (own_first, False, 1)
        fam_second, _, _ = self.family_probability(key, d.hand_second) if key in self.families else (own_second, False, 1)
        fam_first, fam_second = max(fam_first, own_first), max(fam_second, own_second)
        size = len(self.families.get(key, [req]))
        raw = {
            "hand_cards": sum(req.values()), "own_first": round(own_first, 6), "own_second": round(own_second, 6),
            "family": key, "family_size": size, "family_first": round(fam_first, 6),
            "family_second": round(fam_second, 6), "family_truncated": truncated, "family_members_used": used,
            "hand_from_setup": fallback,
            "deck_model": {"deck_size": d.deck_size, "copies": d.copies, "hand_first": d.hand_first, "hand_second": d.hand_second},
        }
        text = (f"Starthand als Erster: {own_first:.1%} allein, {fam_first:.1%} mit der Familie "
                f"({size} verschiedene Starthand/-hände zum selben Ergebnis); als Zweiter {fam_second:.1%}"
                + (" (Familie gekürzt, untere Grenze)" if truncated else "")
                + (" (Handkarten aus dem Startaufbau übernommen, Phase 1 hat keine erfasst)" if fallback else ""))
        return AxisResult(raw, round(fam_first, 6), text, profile={"family": key})


def default_analyzers(pool: CardPool, knowledge: EffectKnowledge | None, deck: DeckModel = DeckModel()) -> list:
    out = [OutcomeAnalyzer(pool, knowledge), CostAnalyzer(), ConsistencyAnalyzer(deck)]
    if knowledge is not None:
        from deepyugioh.analysis.specificity import GenericnessAnalyzer

        out.append(GenericnessAnalyzer(knowledge))
    return out
