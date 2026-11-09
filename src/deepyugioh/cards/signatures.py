"""Step 2b: Effect Signature extraction - what each effect of a card does, structured.

Builds on the effect skeleton of `effects.py` (category, type, event, range,
count limit) and reads the functions an effect points to:

    e1:SetCost(s.cost)          -> costs       (discard, banish itself, pay LP ...)
    e1:SetTarget(s.target)      -> candidates  (IsExistingMatchingCard(s.filter, tp, LOCATION_DECK, ...))
    e1:SetOperation(s.activate) -> moves       (SelectMatchingCard + SendtoHand -> DECK => HAND)
    e1:SetCondition(s.con)      -> conditions  (IsPreviousLocation(LOCATION_ONFIELD), aux.exccon ...)

Filters (`c:IsLevelBelow(4) and c:IsRace(RACE_WARRIOR)`) become CardFilter /
Condition objects; `or` keeps alternatives. Everything not understood is
listed in `unknown` (never guessed) and lowers the confidence. The result is
a hypothesis for planning only - the engine decides what really works.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import re
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path

from deepyugioh import paths
from deepyugioh.cards import lua
from deepyugioh.cards.lua import Node, Tok
from deepyugioh.cards.references import constants
from deepyugioh.contracts.cards import CardInfo
from deepyugioh.contracts.effects import UNKNOWN, CardFilter, Condition, EffectSignature, Move, Trigger, summarize

EXTRACTOR_VERSION = "1"
_TYPE_DEF = re.compile(r"^\s*(TYPE_[A-Z0-9_]+)\s*=\s*(0x[0-9a-fA-F]+|\d+)", re.M)


@cache
def _constants() -> dict[str, int]:
    """CARD_/SET_/RACE_/ATTRIBUTE_ (as the interaction graph) plus TYPE_* from constant.lua."""
    out = dict(constants())
    lib = paths.CARD_SCRIPTS / "constant.lua"
    if lib.exists():
        for name, value in _TYPE_DEF.findall(lib.read_text(encoding="utf-8", errors="replace")):
            out.setdefault(name, int(value, 0))
    return out

_LOCATIONS = {
    "LOCATION_DECK": ("DECK",), "LOCATION_HAND": ("HAND",), "LOCATION_GRAVE": ("GRAVE",),
    "LOCATION_REMOVED": ("BANISHED",), "LOCATION_EXTRA": ("EXTRA",), "LOCATION_MZONE": ("MZONE",),
    "LOCATION_SZONE": ("SZONE",), "LOCATION_ONFIELD": ("MZONE", "SZONE"), "LOCATION_FZONE": ("SZONE",),
    "LOCATION_PZONE": ("SZONE",), "LOCATION_STZONE": ("SZONE",), "LOCATION_EMZONE": ("MZONE",),
    "LOCATION_MMZONE": ("MZONE",),
}
_EVENTS = {
    "EVENT_TO_GRAVE": "TO_GRAVE", "EVENT_SUMMON_SUCCESS": "SUMMON", "EVENT_SPSUMMON_SUCCESS": "SPSUMMON",
    "EVENT_FLIP_SUMMON_SUCCESS": "FLIP_SUMMON", "EVENT_FLIP": "FLIP", "EVENT_REMOVE": "REMOVE",
    "EVENT_TO_HAND": "TO_HAND", "EVENT_BE_MATERIAL": "BE_MATERIAL", "EVENT_DESTROYED": "DESTROYED",
    "EVENT_RELEASE": "RELEASE", "EVENT_DRAW": "DRAW", "EVENT_CHAINING": "CHAINING", "EVENT_DISCARD": "DISCARD",
    "EVENT_TO_DECK": "TO_DECK", "EVENT_LEAVE_FIELD": "LEAVE_FIELD", "EVENT_BATTLE_DESTROYED": "BATTLE_DESTROYED",
    "EVENT_PHASE": "PHASE", "EVENT_ATTACK_ANNOUNCE": "ATTACK", "EVENT_CHAIN_SOLVED": "CHAIN_SOLVED",
}
# where a card is after the event happened to it (for SINGLE triggers without SetRange)
_EVENT_ZONE = {
    "TO_GRAVE": ("GRAVE",), "SUMMON": ("MZONE",), "SPSUMMON": ("MZONE",), "FLIP_SUMMON": ("MZONE",),
    "FLIP": ("MZONE",), "REMOVE": ("BANISHED",), "TO_HAND": ("HAND",), "DESTROYED": ("GRAVE",),
    "RELEASE": ("GRAVE",), "DISCARD": ("GRAVE",), "BATTLE_DESTROYED": ("GRAVE",), "BE_MATERIAL": ("GRAVE",),
    "DRAW": ("HAND",), "TO_DECK": ("DECK",),
}
_ACTIONS = {  # Duel call -> destination zone
    "Duel.SendtoGrave": "GRAVE", "Duel.SendtoHand": "HAND", "Duel.SpecialSummon": "MZONE",
    "Duel.SpecialSummonStep": "MZONE", "Duel.Remove": "BANISHED", "Duel.Destroy": "GRAVE",
    "Duel.Release": "GRAVE", "Duel.SendtoDeck": "DECK", "Duel.Summon": "MZONE", "Duel.SSet": "SZONE",
    "Duel.MoveToField": "SZONE", "Duel.SendtoExtraP": "EXTRA", "Duel.Overlay": "OVERLAY",
}
# selection calls: name -> (filter arg, player arg, self locations, opponent locations)
_SELECT = {
    "Duel.IsExistingMatchingCard": (0, 1, 2, 3), "Duel.GetMatchingGroup": (0, 1, 2, 3),
    "Duel.GetMatchingGroupCount": (0, 1, 2, 3), "Duel.IsExistingTarget": (0, 1, 2, 3),
    "Duel.GetFirstMatchingCard": (0, 1, 2, 3),
    "Duel.SelectMatchingCard": (1, 2, 3, 4), "Duel.SelectTarget": (1, 2, 3, 4),
}
_TARGET_SELECT = {"Duel.SelectTarget", "Duel.IsExistingTarget"}
_TARGET_GETTERS = ("Duel.GetFirstTarget", "Duel.GetTargetCards", "Duel.GetChainInfo", "Duel.GetTargetsRelateToChain")
_CATEGORY_DST = {
    "CATEGORY_TOHAND": "HAND", "CATEGORY_SEARCH": "HAND", "CATEGORY_TOGRAVE": "GRAVE",
    "CATEGORY_SPECIAL_SUMMON": "MZONE", "CATEGORY_REMOVE": "BANISHED", "CATEGORY_DESTROY": "GRAVE",
    "CATEGORY_RELEASE": "GRAVE", "CATEGORY_TODECK": "DECK", "CATEGORY_DECKDES": "GRAVE",
    "CATEGORY_HANDES": "GRAVE", "CATEGORY_SUMMON": "MZONE",
}
_ABLE_DST = {
    "IsAbleToGrave": "GRAVE", "IsAbleToGraveAsCost": "GRAVE", "IsAbleToHand": "HAND", "IsAbleToRemove": "BANISHED",
    "IsAbleToRemoveAsCost": "BANISHED", "IsCanBeSpecialSummoned": "MZONE", "IsAbleToDeck": "DECK",
    "IsAbleToDeckAsCost": "DECK", "IsDiscardable": "GRAVE", "IsReleasable": "GRAVE", "IsReleasableByEffect": "GRAVE",
    "IsSSetable": "SZONE", "IsAbleToExtra": "EXTRA", "IsAbleToHandAsCost": "HAND", "IsSummonable": "MZONE",
}
# predicates about state, not identity: they never restrict which card it is
_NEUTRAL = set(_ABLE_DST) | {
    "IsFaceup", "IsFacedown", "IsLocation", "IsControler", "IsPreviousLocation", "IsRelateToEffect", "IsReason",
    "IsCanBeEffectTarget", "IsPreviousControler", "IsPosition", "IsAttackPos", "IsDefensePos", "IsNegatable",
    "IsHasEffect", "IsCanBeXyzMaterial", "IsCanBeSynchroMaterial", "IsCanBeLinkMaterial", "IsCanBeFusionMaterial",
    "IsCanAddCounter", "IsCanChangePosition", "IsCanTurnSet", "IsPublic", "IsForbidden", "IsOnField",
    "IsCanOverlay", "IsMainMonster", "IsInMainMZone", "IsInExtraMZone", "IsStatus", "IsDisabled",
    "IsSummonType", "IsSummonLocation", "IsPreviousPosition", "IsAbleToChangeControler", "CheckActivateEffect",
}
_COST_SELF = {  # Cost.* helpers that move the card itself
    "Cost.SelfBanish": "BANISHED", "Cost.SelfToGrave": "GRAVE", "Cost.SelfDiscard": "GRAVE",
    "Cost.SelfDiscardToGrave": "GRAVE", "Cost.SelfTribute": "GRAVE", "Cost.SelfRelease": "GRAVE",
    "Cost.SelfToDeck": "DECK", "Cost.SelfToHand": "HAND", "Cost.SelfToExtra": "EXTRA", "aux.bfgcost": "BANISHED",
}
_COST_NOOP = {"Cost.SelfReveal", "Cost.Reveal", "Cost.HintSelectedEffect", "Cost.SoftOncePerChain",
              "Cost.HardOncePerChain", "aux.RemainFieldCost", "Cost.SelfChangePosition"}
_TYPE_KINDS = {"TYPE_MONSTER": "MONSTER", "TYPE_SPELL": "SPELL", "TYPE_TRAP": "TRAP"}
_MAX_ALTERNATIVES = 24


# ---- filter expressions -> Condition ------------------------------------------------
@dataclass
class _Conj:
    """Accumulator for one AND-conjunction."""

    kinds: frozenset | None = None
    type_bits: int = 0
    codes: frozenset | None = None
    setcodes: frozenset | None = None
    min_level: int | None = None
    max_level: int | None = None
    races: int | None = None
    attributes: int | None = None
    max_atk: int | None = None
    min_atk: int | None = None
    max_def: int | None = None
    min_def: int | None = None
    exclude_self: bool = False
    unknown: list = field(default_factory=list)
    hints: set = field(default_factory=set)       # destinations suggested by IsAbleToX
    impossible: bool = False

    def merge(self, o: "_Conj") -> "_Conj":
        r = _Conj()
        r.kinds = _inter(self.kinds, o.kinds)
        r.codes = _inter(self.codes, o.codes)
        if self.setcodes is not None and o.setcodes is not None:
            r.setcodes = self.setcodes
            r.unknown.append("AND of two archetype conditions")
        else:
            r.setcodes = self.setcodes if self.setcodes is not None else o.setcodes
        r.type_bits = self.type_bits | o.type_bits
        r.min_level = _maxn(self.min_level, o.min_level)
        r.max_level = _minn(self.max_level, o.max_level)
        r.races = _mask(self.races, o.races)
        r.attributes = _mask(self.attributes, o.attributes)
        r.max_atk, r.min_atk = _minn(self.max_atk, o.max_atk), _maxn(self.min_atk, o.min_atk)
        r.max_def, r.min_def = _minn(self.max_def, o.max_def), _maxn(self.min_def, o.min_def)
        r.exclude_self = self.exclude_self or o.exclude_self
        r.unknown += self.unknown + o.unknown
        r.hints = self.hints | o.hints
        r.impossible = (self.impossible or o.impossible or r.kinds == frozenset() or r.codes == frozenset()
                        or r.races == 0 and (self.races or o.races) and self.races is not None and o.races is not None
                        or r.attributes == 0 and self.attributes is not None and o.attributes is not None
                        or (r.min_level is not None and r.max_level is not None and r.min_level > r.max_level))
        return r

    def to_filter(self) -> CardFilter:
        return CardFilter(
            kinds=self.kinds or frozenset(), type_bits=self.type_bits, codes=self.codes or frozenset(),
            setcodes=self.setcodes or frozenset(), min_level=self.min_level, max_level=self.max_level,
            races=self.races or 0, attributes=self.attributes or 0, max_atk=self.max_atk, min_atk=self.min_atk,
            max_def=self.max_def, min_def=self.min_def, exclude_self=self.exclude_self,
            unknown=tuple(dict.fromkeys(self.unknown)),
        )


def _inter(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a & b


def _maxn(a, b):
    return b if a is None else a if b is None else max(a, b)


def _minn(a, b):
    return b if a is None else a if b is None else min(a, b)


def _mask(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a & b


@dataclass
class _Ctx:
    funcs: dict[str, lua.Function]
    consts: dict[str, int]
    own_code: int


def _const_values(node: Node, consts: dict[str, int]) -> list[int] | None:
    """`RACE_A|RACE_B` / `RACE_A+RACE_B` -> [a, b]; numbers; None if not constant."""
    if node.op == "num":
        return [int(node.value, 0)] if "." not in node.value else None
    if node.op == "name":
        if node.value in consts:
            return [consts[node.value]]
        if node.value == "id":
            return None
        return None
    if node.op == "arith" and node.value in ("|", "+"):
        a, b = _const_values(node.args[0], consts), _const_values(node.args[1], consts)
        if a is None or b is None:
            return None
        return a + b
    return None


def _bitmask(node: Node, consts) -> int | None:
    vals = _const_values(node, consts)
    if vals is None:
        return None
    out = 0
    for v in vals:
        out |= v
    return out


def _code_values(args: list[Node], ctx: _Ctx) -> tuple[set[int], bool]:
    """Card codes in IsCode(...) args; second value: refers to the card itself (id)."""
    codes, own = set(), False
    for a in args:
        if a.op == "name" and a.value == "id":
            own = True
        elif a.op == "arith" and a.args[0].op == "name" and a.args[0].value == "id":
            continue  # id+1 (tokens) - not a real card
        else:
            vals = _const_values(a, ctx.consts)
            if vals:
                codes.update(vals)
    return codes, own


def _atom(call: Node, subject: str, ctx: _Ctx, depth: int) -> list[_Conj]:
    """One predicate call -> DNF (list of alternatives)."""
    name = call.value
    receiver, _, method = name.rpartition(":")
    conj = _Conj()
    if name in ("aux.SpElimFilter", "aux.nzatk", "aux.nzdef", "aux.disfilter1", "aux.FaceupFilter"):
        return [conj]  # location / state helpers, they do not restrict which card
    if receiver != subject:
        # s.otherfilter(c, ...) : inline another filter of the same script
        if name in ctx.funcs and call.args and call.args[0].op == "name" and call.args[0].value == subject and depth < 4:
            return _filter_function(ctx.funcs[name], ctx, depth + 1)
        conj.unknown.append(_short(name))
        return [conj]
    args = call.args
    if method in _NEUTRAL:
        if method in _ABLE_DST:
            conj.hints.add(_ABLE_DST[method])
        return [conj]
    if method in ("IsLevelBelow", "IsRankBelow", "IsLinkBelow") and args:
        v = _const_values(args[0], ctx.consts)
        if v:
            conj.max_level = v[0]
            return [conj]
    if method in ("IsLevelAbove", "IsRankAbove", "IsLinkAbove") and args:
        v = _const_values(args[0], ctx.consts)
        if v:
            conj.min_level = v[0]
            return [conj]
    if method in ("IsLevel", "IsRank", "IsLink") and args:
        levels = [x for a in args for x in (_const_values(a, ctx.consts) or [])]
        if levels and len(levels) == len(args):
            return [_Conj(min_level=n, max_level=n) for n in levels]
    if method in ("IsRace", "IsOriginalRace") and args:
        m = _bitmask(args[0], ctx.consts)
        if m:
            conj.races = m
            return [conj]
    if method in ("IsAttribute", "IsOriginalAttribute") and args:
        m = _bitmask(args[0], ctx.consts)
        if m:
            conj.attributes = m
            return [conj]
    if method in ("IsMonster",):
        conj.kinds = frozenset({"MONSTER"})
        return [conj]
    if method in ("IsSpell", "IsNormalSpell", "IsQuickPlaySpell", "IsContinuousSpell", "IsFieldSpell",
                  "IsEquipSpell", "IsRitualSpell"):
        conj.kinds = frozenset({"SPELL"})
        return [conj]
    if method in ("IsTrap", "IsNormalTrap", "IsContinuousTrap", "IsCounterTrap"):
        conj.kinds = frozenset({"TRAP"})
        return [conj]
    if method == "IsSpellTrap":
        conj.kinds = frozenset({"SPELL", "TRAP"})
        return [conj]
    if method in ("IsType", "IsOriginalType") and args:
        vals = _const_values(args[0], ctx.consts)
        names = _const_names(args[0])
        if vals and names:
            alts = []
            for n, v in zip(names, vals):
                c = _Conj()
                if n in _TYPE_KINDS:
                    c.kinds = frozenset({_TYPE_KINDS[n]})
                else:
                    c.type_bits = v
                    c.kinds = frozenset({"MONSTER"}) if n not in ("TYPE_QUICKPLAY", "TYPE_CONTINUOUS", "TYPE_EQUIP",
                                                                   "TYPE_FIELD", "TYPE_COUNTER") else None
                alts.append(c)
            return alts
    if method in ("IsSetCard", "IsOriginalSetCard", "IsPreviousSetCard") and args:
        sets = [x for a in args for x in (_const_values(a, ctx.consts) or [])]
        if sets:
            conj.setcodes = frozenset(sets)
            return [conj]
    if method in ("IsCode", "IsOriginalCode", "IsOriginalCodeRule", "IsSummonCode") and args:
        codes, own = _code_values(args, ctx)
        if own and not codes:
            conj.codes = frozenset({ctx.own_code})
            return [conj]
        if codes:
            conj.codes = frozenset(codes | ({ctx.own_code} if own else set()))
            return [conj]
    if method in ("IsAttackBelow", "IsAttackAbove", "IsAttack", "IsDefenseBelow", "IsDefenseAbove", "IsDefense") and args:
        v = _const_values(args[0], ctx.consts)
        if v:
            stat = "atk" if "Attack" in method else "def"
            if method.endswith("Below") or method in ("IsAttack", "IsDefense"):
                setattr(conj, f"max_{stat}", v[0])
            if method.endswith("Above") or method in ("IsAttack", "IsDefense"):
                setattr(conj, f"min_{stat}", v[0])
            return [conj]
    conj.unknown.append(_short(name))
    return [conj]


def _const_names(node: Node) -> list[str] | None:
    if node.op == "name":
        return [node.value]
    if node.op == "arith" and node.value in ("|", "+"):
        a, b = _const_names(node.args[0]), _const_names(node.args[1])
        return a + b if a is not None and b is not None else None
    return None


def _short(name: str) -> str:
    return name.split(":")[-1] if ":" in name else name


def _cmp(node: Node, subject: str, ctx: _Ctx) -> list[_Conj]:
    left, right = node.args
    op = node.value
    if right.op == "call" and left.op != "call":
        left, right = right, left
        op = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}.get(op, op)
    getters = {f"{subject}:GetLevel": "level", f"{subject}:GetOriginalLevel": "level", f"{subject}:GetRank": "level",
               f"{subject}:GetLink": "level", f"{subject}:GetAttack": "atk", f"{subject}:GetBaseAttack": "atk",
               f"{subject}:GetTextAttack": "atk", f"{subject}:GetDefense": "def", f"{subject}:GetBaseDefense": "def",
               f"{subject}:GetTextDefense": "def"}
    if left.op == "call" and left.value in getters and not left.args:
        v = _const_values(right, ctx.consts)
        if v and len(v) == 1:
            stat, n = getters[left.value], v[0]
            c = _Conj()
            lo_name, hi_name = ("min_level", "max_level") if stat == "level" else (f"min_{stat}", f"max_{stat}")
            if op == "==":
                setattr(c, lo_name, n)
                setattr(c, hi_name, n)
            elif op in ("<=", "<"):
                setattr(c, hi_name, n if op == "<=" else n - 1)
            elif op in (">=", ">"):
                setattr(c, lo_name, n if op == ">=" else n + 1)
            else:
                c.unknown.append(f"{_short(left.value)}{op}")
            return [c]
    c = _Conj()
    c.unknown.append("comparison")
    return [c]


def _dnf(node: Node, subject: str, ctx: _Ctx, depth: int) -> list[_Conj]:
    if node.op == "or":
        out = []
        for a in node.args:
            out += _dnf(a, subject, ctx, depth)
        return out[:_MAX_ALTERNATIVES]
    if node.op == "and":
        acc = [_Conj()]
        for a in node.args:
            part = _dnf(a, subject, ctx, depth)
            acc = [x.merge(y) for x in acc for y in part][:_MAX_ALTERNATIVES]
        return [c for c in acc if not c.impossible] or [_impossible()]
    if node.op == "not":
        inner = node.args[0]
        if inner.op == "call" and inner.value in (f"{subject}:IsCode", f"{subject}:IsOriginalCode") and inner.args:
            codes, own = _code_values(inner.args, ctx)
            if own and not codes:
                return [_Conj(exclude_self=True)]
        if inner.op == "call" and inner.value.rpartition(":")[2] in _NEUTRAL:
            return [_Conj()]
        c = _Conj()
        c.unknown.append("not " + (_short(inner.value) if inner.op == "call" else inner.op))
        return [c]
    if node.op == "call":
        return _atom(node, subject, ctx, depth)
    if node.op == "cmp":
        return _cmp(node, subject, ctx)
    if node.op == "name":
        if node.value == "true":
            return [_Conj()]
        if node.value in ("false", "nil"):
            return [_impossible()]
    c = _Conj()
    c.unknown.append(node.value or node.op)
    return [c]


def _impossible() -> _Conj:
    c = _Conj()
    c.impossible = True
    return c


def _filter_function(fn: lua.Function, ctx: _Ctx, depth: int = 0) -> list[_Conj]:
    subject = fn.params[0] if fn.params else "c"
    rets = [r for r in lua.returns(fn.body) if r]
    if not rets:
        c = _Conj()
        c.unknown.append(f"{fn.name}: no return")
        return [c]
    out = []
    for r in rets:
        out += _dnf(lua.parse_expr(r), subject, ctx, depth)
    out = [c for c in out if not c.impossible]
    if len(rets) > 1:
        for c in out:
            c.unknown.append(f"{fn.name}: several return paths")
    return out or [_impossible()]


def filter_condition(arg: list[Tok], ctx: _Ctx) -> tuple[Condition | None, set[str]]:
    """A filter argument -> (Condition, destination hints). None = unsatisfiable."""
    t = lua.text(arg)
    if not arg or t in ("nil", "aux.TRUE"):
        return Condition(), set()
    if arg[0].text == "function":
        anon = lua.anonymous_function(arg, 0)
        if anon:
            return _to_condition(_filter_function(anon[0], ctx))
    node = lua.parse_expr(arg)
    if node.op == "name" and node.value in ctx.funcs:
        return _to_condition(_filter_function(ctx.funcs[node.value], ctx))
    if node.op == "name" and node.value.startswith("Card."):
        method = node.value.split(".", 1)[1]
        return _to_condition(_atom(Node("call", [], f"c:{method}"), "c", ctx, 0))
    if node.op == "call" and node.value in ("aux.FilterBoolFunction", "aux.FilterBoolFunctionEx") and node.args:
        ref = node.args[0]
        if ref.op == "name" and ref.value.startswith("Card."):
            method = ref.value.split(".", 1)[1]
            return _to_condition(_atom(Node("call", node.args[1:], f"c:{method}"), "c", ctx, 0))
    if node.op == "call" and node.value in ("aux.NecroValleyFilter", "aux.nvfilter") and node.args:
        inner = node.args[0]
        if inner.op == "name" and inner.value in ctx.funcs:
            return _to_condition(_filter_function(ctx.funcs[inner.value], ctx))
    if node.op == "call" and node.value == "aux.FaceupFilter" and node.args:
        ref = node.args[0]
        if ref.op == "name" and ref.value.startswith("Card."):
            return _to_condition(_atom(Node("call", node.args[1:], f"c:{ref.value.split('.', 1)[1]}"), "c", ctx, 0))
    c = _Conj()
    c.unknown.append(f"filter {t[:40]}")
    return _to_condition([c])


def _to_condition(conjs: list[_Conj]) -> tuple[Condition | None, set[str]]:
    alive = [c for c in conjs if not c.impossible]
    if not alive:
        return None, set()
    hints = set().union(*(c.hints for c in alive))
    filters = tuple(dict.fromkeys(c.to_filter() for c in alive))
    if any(f == CardFilter() for f in filters):
        return Condition(), hints  # one alternative accepts any card
    return Condition(filters), hints


def _zones(node_toks: list[Tok], consts) -> tuple[str, ...] | None:
    """`LOCATION_HAND|LOCATION_DECK` -> ("HAND", "DECK"); "0" -> (); None if not understood."""
    t = lua.text(node_toks)
    if t in ("0", "nil", ""):
        return ()
    out = []
    for tok in node_toks:
        if tok.kind == "name":
            if tok.text in _LOCATIONS:
                out.extend(_LOCATIONS[tok.text])
            else:
                return None
    return tuple(dict.fromkeys(out)) if out else None


# ---- function bodies -> moves -----------------------------------------------------------
@dataclass
class _Selection:
    condition: Condition | None
    hints: set[str]
    src: tuple[str, ...]
    opp: tuple[str, ...]
    targets: bool
    index: int
    var: str | None
    unknown: list[str]


def _selections(fn: lua.Function, ctx: _Ctx) -> list[_Selection]:
    out = []
    for call in lua.calls(fn.body, ("Duel.",)):
        if call.name not in _SELECT:
            continue
        fi, pi, si, oi = _SELECT[call.name]
        if len(call.args) <= oi:
            continue
        cond, hints = filter_condition(call.args[fi], ctx)
        unknown = []
        src = _zones(call.args[si], ctx.consts)
        opp = _zones(call.args[oi], ctx.consts)
        if lua.text(call.args[pi]) in ("1-tp", "1-p"):
            src, opp = opp, src
        if src is None:
            unknown.append("source zone")
            src = ()
        out.append(_Selection(cond, hints, src, opp or (), call.name in _TARGET_SELECT, call.index,
                              call.assigned_to, unknown))
    return out


def _aliases(fn: lua.Function) -> list[tuple[int, str, str]]:
    """(index, var, rhs) for `var = rhs` assignments (rhs as text), in order."""
    out = []
    b = fn.body
    for i in range(1, len(b) - 1):
        if b[i].text == "=" and b[i - 1].kind == "name":
            j = i + 1
            run = []
            depth = 0
            while j < len(b):
                t = b[j]
                if depth == 0 and t.kind == "name" and t.text in ("local", "if", "for", "while", "return", "end",
                                                                    "then", "else", "do") and run:
                    break
                if depth == 0 and t.kind == "name" and run and run[-1].kind in ("name", "num") and \
                        run[-1].text not in ("and", "or", "not") and t.text not in ("and", "or", "not"):
                    break  # next statement starts (two names in a row)
                if t.text in ("(", "{", "["):
                    depth += 1
                elif t.text in (")", "}", "]"):
                    if depth == 0:
                        break
                    depth -= 1
                    run.append(t)
                    j += 1
                    if depth == 0 and j < len(b) and b[j].text not in (":", ".", "(", "[", "and", "or", "+", "-", "|"):
                        break
                    continue
                run.append(t)
                j += 1
            out.append((i, b[i - 1].text, lua.text(run)))
    return out


def _resolve(expr: str, index: int, aliases, seen=None) -> str:
    """Follow `tc = g:GetFirst()` style aliases back to a selection variable or a known source."""
    seen = seen or set()
    base = expr.split(":")[0].split("(")[0].split(".")[0] if not expr.startswith(("e:GetHandler", "Duel.")) else expr
    if expr.startswith("e:GetHandler"):
        return "SELF"
    if expr.startswith(_TARGET_GETTERS):
        return "TARGETS"
    if base in ("eg",):
        return "EVENT"
    if base in seen:
        return base
    seen.add(base)
    for i, var, rhs in reversed(aliases):
        if var == base and i < index:
            if rhs.startswith("e:GetHandler"):
                return "SELF"
            if rhs.startswith(_TARGET_GETTERS):
                return "TARGETS"
            if rhs.startswith("Duel.Select") or rhs.startswith("Duel.GetMatchingGroup") or rhs.startswith("Duel.GetFirstMatching"):
                return f"SEL:{i}"
            if rhs.startswith("Duel.CreateToken"):
                return "TOKEN"
            if rhs.startswith("aux.SelectUnselectGroup(") or ":Select" in rhs or ":FilterSelect" in rhs \
                    or ":GetFirst" in rhs or ":Filter(" in rhs or ":RandomSelect" in rhs or ":GetNext" in rhs:
                inner = rhs.split("(")[1].split(",")[0] if rhs.startswith("aux.") else rhs.split(":")[0]
                return _resolve(inner, i, aliases, seen)
            return _resolve(rhs, i, aliases, seen) if rhs and rhs != expr else base
    return base


@dataclass
class _Analysis:
    moves: list[Move] = field(default_factory=list)
    draws: int = 0
    lp: int = 0
    unknown: list[str] = field(default_factory=list)
    restrictions: list[str] = field(default_factory=list)
    target_selections: list[_Selection] = field(default_factory=list)
    selections: list[_Selection] = field(default_factory=list)


def _analyse(fn: lua.Function, ctx: _Ctx, role: str, self_zone: tuple[str, ...], targets: list[_Selection],
             depth: int = 0) -> _Analysis:
    res = _Analysis()
    sels = _selections(fn, ctx)
    res.selections = sels
    res.target_selections = [s for s in sels if s.targets]
    aliases = _aliases(fn)
    sel_at = {}
    for s in sels:
        for i, var, rhs in aliases:
            if s.var == var and i < s.index and s.index - i <= 3:
                sel_at[f"SEL:{i}"] = s
    for call in lua.calls(fn.body, ("Duel.", "s.", "aux.", "Cost.")):
        name = call.name
        if name in ctx.funcs and depth < 2 and name not in ("s.initial_effect",):
            sub = _analyse(ctx.funcs[name], ctx, role, self_zone, targets + res.target_selections, depth + 1)
            res.moves += sub.moves
            res.draws += sub.draws
            res.lp += sub.lp
            res.unknown += sub.unknown
            res.restrictions += sub.restrictions
            continue
        if name == "Duel.Draw":
            n = _const_values(lua.parse_expr(call.args[1]), ctx.consts) if len(call.args) > 1 else None
            res.draws += n[0] if n else 1
            continue
        if name == "Duel.PayLPCost":
            n = _const_values(lua.parse_expr(call.args[1]), ctx.consts) if len(call.args) > 1 else None
            res.lp += n[0] if n else 0
            if not n:
                res.unknown.append("LP amount")
            continue
        if name == "Duel.DiscardHand" and len(call.args) > 1:
            cond, _ = filter_condition(call.args[1], ctx)
            if cond is not None:
                res.moves.append(Move(cond, ("HAND",), "GRAVE", _role(call, role)))
            continue
        if name in ("Duel.SelectReleaseGroupCost", "Duel.SelectReleaseGroup") and len(call.args) > 1:
            cond, _ = filter_condition(call.args[1], ctx)
            if cond is not None:
                res.moves.append(Move(cond, ("MZONE",), "GRAVE", "cost" if "Cost" in name else role))
            continue
        if name == "Duel.DiscardDeck":
            res.moves.append(Move(Condition(), ("DECK",), "GRAVE", _role(call, role), count=0))
            res.unknown.append("random cards from the top of the deck")
            continue
        if name == "Duel.CreateToken":
            res.restrictions.append("TOKEN")
            continue
        if name not in _ACTIONS or not call.args:
            continue
        dst = _ACTIONS[name]
        if name == "Duel.SendtoHand" and len(call.args) > 1 and lua.text(call.args[1]) in ("1-tp",):
            dst = UNKNOWN
        ref = _resolve(lua.text(call.args[0]), call.index, aliases)
        if ref == "TOKEN":
            continue
        if ref in ("SELF", "c"):
            res.moves.append(Move(None, self_zone, dst, _role(call, role)))
            continue
        sel = sel_at.get(ref)
        if sel is None and ref == "TARGETS":
            sel = (targets + res.target_selections or [None])[-1]
        if sel is None and ref == "EVENT":
            res.moves.append(Move(Condition(), (), dst, _role(call, role)))
            res.unknown.append("cards from the triggering event")
            continue
        if sel is None:
            prior = [s for s in sels if s.index < call.index] or targets or sels
            sel = prior[-1] if prior else None
        if sel is None:
            res.moves.append(Move(Condition(), (), dst, _role(call, role)))
            res.unknown.append(f"{name.split('.')[1]}: which card")
            continue
        if sel.condition is None:
            continue
        res.unknown += sel.unknown
        if sel.src:
            res.moves.append(Move(sel.condition, sel.src, dst, _role(call, role), targets=sel.targets))
        if sel.opp:
            res.moves.append(Move(sel.condition, sel.opp, dst, _role(call, role), opponent=True, targets=sel.targets))
    return res


def _role(call: lua.Call, default: str) -> str:
    return "cost" if any("REASON_COST" in lua.text(a) for a in call.args) else default


# ---- effects --------------------------------------------------------------------------
@dataclass
class _Raw:
    idx: int
    var: str
    setters: dict = field(default_factory=dict)   # setter name -> arg token runs

    def clone(self, idx, var):
        return _Raw(idx, var, {k: list(v) for k, v in self.setters.items()})


def _raw_effects(toks: list[Tok], funcs: dict[str, lua.Function]) -> list[_Raw]:
    init = funcs.get("s.initial_effect")
    if init is None:
        return []
    effects, current = [], {}
    for call in lua.calls(init.body, ("",)):
        name = call.name
        if name == "Effect.CreateEffect" and call.assigned_to:
            e = _Raw(len(effects), call.assigned_to)
            effects.append(e)
            current[e.var] = e
            continue
        var, _, method = name.partition(":")
        if method == "Clone" and var in current and call.assigned_to:
            e = current[var].clone(len(effects), call.assigned_to)
            effects.append(e)
            current[e.var] = e
            continue
        if var in current and method.startswith("Set"):
            current[var].setters[method] = call.args
    return effects


def _names(args) -> list[str]:
    return [t.text for a in args for t in a if t.kind == "name"]


def extract(source: str, card: CardInfo) -> list[EffectSignature]:
    """All effect signatures of one card script."""
    toks = lua.tokenize(source)
    funcs = lua.functions(toks)
    ctx = _Ctx(funcs, _constants(), card.code)
    out = []
    for raw in _raw_effects(toks, funcs):
        try:
            out.append(_signature(raw, card, ctx))
        except Exception as e:  # never let one odd script break the whole extraction
            out.append(EffectSignature(f"{card.code}:{raw.idx}", card.code, "OTHER", (), unknown=(f"error {type(e).__name__}",),
                                       confidence=0.0))
    return out


def _signature(raw: _Raw, card: CardInfo, ctx: _Ctx) -> EffectSignature:
    s = raw.setters
    types = _names(s.get("SetType", []))
    codes = _names(s.get("SetCode", []))
    cats = tuple(n for n in _names(s.get("SetCategory", [])) if n.startswith("CATEGORY_"))
    ranges = []
    for n in _names(s.get("SetRange", [])):
        ranges.extend(_LOCATIONS.get(n, ()))
    unknown: list[str] = []
    restrictions: list[str] = []
    is_spell, is_trap = bool(card.type & 0x2), bool(card.type & 0x4)

    trigger = None
    event = next((_EVENTS.get(c, c) for c in codes if c.startswith("EVENT_")), None)
    if "EFFECT_TYPE_ACTIVATE" in types:
        kind = "ACTIVATE"
    elif "EFFECT_TYPE_IGNITION" in types:
        kind = "IGNITION"
    elif "EFFECT_TYPE_QUICK_O" in types or "EFFECT_TYPE_QUICK_F" in types:
        kind = "QUICK"
    elif "EFFECT_TYPE_TRIGGER_O" in types or "EFFECT_TYPE_TRIGGER_F" in types:
        kind = "TRIGGER"
        trigger = Trigger(event or UNKNOWN, "EFFECT_TYPE_SINGLE" in types, "EFFECT_TYPE_TRIGGER_F" in types)
    elif "EFFECT_SPSUMMON_PROC" in codes:
        kind = "SUMMON_PROC"
    elif types:
        kind = "CONTINUOUS"
    else:
        kind = "OTHER"

    if ranges:
        location = tuple(dict.fromkeys(ranges))
    elif kind == "ACTIVATE":
        location = ("SZONE",) if is_trap else ("HAND", "SZONE")
        if is_trap:
            restrictions.append("TRAP_MUST_BE_SET")
    elif kind == "TRIGGER" and trigger and trigger.self_only:
        location = _EVENT_ZONE.get(trigger.event, ())
        if not location:
            unknown.append(f"zone after {trigger.event}")
    elif kind in ("IGNITION", "QUICK"):
        location = ("MZONE",) if card.type & 0x1 else ("SZONE",)
    else:
        location = ()
    if kind == "SUMMON_PROC":
        location = location or ("HAND",)

    # count limit
    count_limit = None
    if "SetCountLimit" in s:
        args = [lua.text(a) for a in s["SetCountLimit"]]
        if len(args) == 1:
            count_limit = "OPT"
        else:
            key = args[1].strip("{}")
            count_limit = f"HOPT:{key}"

    # conditions
    conditions = []
    if "SetCondition" in s:
        conditions += _conditions(s["SetCondition"][0] if s["SetCondition"] else [], ctx, unknown)

    # costs
    costs: list[Move] = []
    lp = 0
    if "SetCost" in s and s["SetCost"]:
        c_moves, c_lp = _costs(s["SetCost"][0], ctx, location, unknown, restrictions)
        costs += c_moves
        lp += c_lp

    # target + operation
    target_sels: list[_Selection] = []
    moves: list[Move] = []
    draws = 0
    target_fn = _fn_arg(s.get("SetTarget"), ctx)
    op_fn = _fn_arg(s.get("SetOperation"), ctx)
    tgt = None
    if target_fn is not None:
        tgt = _analyse(target_fn, ctx, "effect", location, [])
        target_sels = tgt.target_selections
        costs += [m for m in tgt.moves if m.role == "cost"]  # some scripts pay costs in the target function
    if op_fn is not None:
        op = _analyse(op_fn, ctx, "cost" if kind == "SUMMON_PROC" else "effect", location, target_sels)
        moves += [m for m in op.moves if m.role == "effect"]
        costs += [m for m in op.moves if m.role == "cost"]
        draws += op.draws
        lp += op.lp
        unknown += op.unknown
        restrictions += op.restrictions
    elif "SetOperation" in s:
        unknown.append("operation " + lua.text(s["SetOperation"][0])[:30])

    if kind == "SUMMON_PROC":
        moves.insert(0, Move(None, location, "MZONE", "effect"))
        # the summoning cost is often selected in the condition / target function and paid in the operation
        sels = []
        for fn in (_fn_arg(s.get("SetCondition"), ctx), target_fn):
            if fn is not None:
                sels += [x for x in _selections(fn, ctx) if x.condition is not None and x.src]
        if sels:
            fixed = []
            for m in costs:
                if not m.src:
                    fixed.append(Move(sels[0].condition, sels[0].src, m.dst, "cost"))
                    unknown = [u for u in unknown if not u.endswith(": which card")]
                else:
                    fixed.append(m)
            if not fixed:
                fixed = [Move(sels[0].condition, sels[0].src, UNKNOWN, "cost")]
            costs = fixed

    # nothing found in the functions: fall back on categories + SetOperationInfo / selections
    if not moves and kind in ("ACTIVATE", "IGNITION", "QUICK", "TRIGGER") and "TOKEN" not in restrictions:
        moves, fallback_unknown = _fallback_moves(cats, target_fn, op_fn, ctx, location)
        unknown += fallback_unknown
    if "CATEGORY_DRAW" in cats and not draws:
        draws = 1
    if any(m.dst == UNKNOWN for m in moves + costs):
        unknown.append("destination")

    unknown = list(dict.fromkeys(unknown + [u for m in moves + costs if m.what for u in m.what.unknown]))
    restrictions = list(dict.fromkeys(restrictions))
    usable = kind in ("ACTIVATE", "IGNITION", "QUICK", "TRIGGER", "SUMMON_PROC")
    confidence = 1.0 if not unknown else max(0.2, round(1.0 - 0.15 * len(unknown), 2))
    if usable and not moves and not draws and not cats:
        confidence = min(confidence, 0.5)
    return EffectSignature(
        f"{card.code}:{raw.idx}", card.code, kind, location, trigger, tuple(conditions), tuple(costs), tuple(moves),
        draws, lp, count_limit, cats, tuple(restrictions), tuple(unknown), "lua-static", confidence,
    )


def _fn_arg(args, ctx: _Ctx) -> lua.Function | None:
    if not args or not args[0]:
        return None
    a = args[0]
    if a[0].text == "function":
        anon = lua.anonymous_function(a, 0)
        return anon[0] if anon else None
    name = lua.text(a)
    return ctx.funcs.get(name)


def _conditions(arg: list[Tok], ctx: _Ctx, unknown: list[str]) -> list[str]:
    name = lua.text(arg)
    if name == "aux.exccon":
        return ["NOT_TURN_SENT_TO_GY"]
    if name in ("aux.bdocon", "aux.bdcon", "aux.bdgcon"):
        return ["BATTLE"]
    fn = _fn_arg([arg], ctx)
    if fn is None:
        unknown.append(f"condition {name[:30]}")
        return [f"{UNKNOWN}:{name[:30]}"]
    out = []
    understood = True
    for r in lua.returns(fn.body):
        txt = lua.text(r)
        if "IsPreviousLocation(" in txt:
            for loc in _LOCATIONS:
                if f"IsPreviousLocation({loc}" in txt:
                    out.append(f"PREVIOUS:{'+'.join(_LOCATIONS[loc])}")
        elif "IsReason(REASON_" in txt:
            for part in txt.split("IsReason(")[1:]:
                out.append("REASON:" + part.split(")")[0].replace("REASON_", ""))
        elif "aux.exccon" in txt:
            out.append("NOT_TURN_SENT_TO_GY")
        elif txt in ("true",):
            continue
        else:
            understood = False
    if not understood or not out:
        out.append(f"{UNKNOWN}:{fn.name}")
        unknown.append(f"condition {fn.name}")
    return out


def _costs(arg: list[Tok], ctx: _Ctx, location, unknown, restrictions) -> tuple[list[Move], int]:
    name = lua.text(arg)
    node = lua.parse_expr(arg)
    base = node.value if node.op in ("name", "call") else name
    if base in _COST_SELF:
        src = ("HAND",) if base in ("Cost.SelfDiscard", "Cost.SelfDiscardToGrave") else \
              ("MZONE",) if base in ("Cost.SelfTribute", "Cost.SelfRelease") else location
        return [Move(None, src, _COST_SELF[base], "cost")], 0
    if base in _COST_NOOP:
        return [], 0
    if base == "Cost.PayLP":
        v = _const_values(node.args[0], ctx.consts) if node.op == "call" and node.args else None
        if not v:
            unknown.append("LP amount")
        return [], v[0] if v else 0
    if base == "Cost.Discard":
        cond = Condition()
        if node.op == "call" and node.args and node.args[0].op == "name" and node.args[0].value in ctx.funcs:
            cond = _to_condition(_filter_function(ctx.funcs[node.args[0].value], ctx))[0] or Condition()
        return [Move(cond, ("HAND",), "GRAVE", "cost")], 0
    if base == "Cost.DetachFromSelf":
        restrictions.append("XYZ_MATERIAL")
        return [], 0
    if base == "Cost.AND" and node.op == "call":
        moves, lp = [], 0
        for a in node.args:
            m, l2 = _costs(_node_tokens(a), ctx, location, unknown, restrictions)
            moves += m
            lp += l2
        return moves, lp
    fn = _fn_arg([arg], ctx)
    if fn is None:
        unknown.append(f"cost {name[:30]}")
        return [], 0
    res = _analyse(fn, ctx, "cost", location, [])
    unknown += res.unknown
    return [replace(m, role="cost") for m in res.moves], res.lp


def _node_tokens(node: Node) -> list[Tok]:
    return lua.tokenize(_render_node(node))


def _render_node(node: Node) -> str:
    if node.op in ("name", "num", "other"):
        return node.value
    if node.op == "call":
        return f"{node.value}({','.join(_render_node(a) for a in node.args)})"
    if node.op == "arith":
        return f"{_render_node(node.args[0])}{node.value}{_render_node(node.args[1])}"
    return node.value


def _fallback_moves(cats, target_fn, op_fn, ctx, location) -> tuple[list[Move], list[str]]:
    dsts = [d for c, d in _CATEGORY_DST.items() if c in cats]
    if not dsts:
        return [], []
    sels = []
    for fn in (op_fn, target_fn):
        if fn is not None:
            sels += [s for s in _selections(fn, ctx) if s.condition is not None and s.src]
    if sels and len(set(dsts)) == 1:
        s = sels[0]
        return [Move(s.condition, s.src, dsts[0], "effect", targets=s.targets)], ["move from category"]
    return [Move(Condition(), (), d, "effect") for d in dict.fromkeys(dsts)], ["move from category, card unknown"]


# ---- store ------------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS effect_signatures (
    code   INTEGER NOT NULL,
    idx    INTEGER NOT NULL,
    data   TEXT NOT NULL,      -- JSON EffectSignature
    PRIMARY KEY (code, idx)
);
CREATE TABLE IF NOT EXISTS signature_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class SignatureStore:
    """EffectKnowledge implementation backed by the card database (table effect_signatures)."""

    def __init__(self, path: Path | None = None, *, build_if_missing: bool = True):
        self.path = path or paths.card_db()
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)
        if build_if_missing and self.version() == "unbuilt":
            self.build()
        self._cards: dict[int, CardInfo] | None = None
        self._sigs: dict[int, list[EffectSignature]] | None = None
        self._refs: dict[int, tuple[set[int], set[int]]] | None = None

    # ---- build ------------------------------------------------------------------
    def build(self) -> dict:
        from deepyugioh.cards.store import CardStore

        store = CardStore(self.path)
        rows = self.db.execute("SELECT code, script FROM cards WHERE is_alt_art = 0 AND script IS NOT NULL ORDER BY code").fetchall()
        sigs = []
        with self.db:
            self.db.execute("DELETE FROM effect_signatures")
            self.db.execute("DELETE FROM signature_meta")
            for code, script in rows:
                path = paths.CARD_SCRIPTS / script
                card = store.get(code)
                if card is None or not path.exists():
                    continue
                for sig in extract(path.read_text(encoding="utf-8", errors="replace"), card):
                    sigs.append(sig)
                    self.db.execute("INSERT INTO effect_signatures VALUES (?,?,?)",
                                    (code, int(sig.effect_id.split(":")[1]), json.dumps(sig.to_dict(), sort_keys=True)))
            stats = summarize(sigs)
            digest = hashlib.sha256()
            for (data,) in self.db.execute("SELECT data FROM effect_signatures ORDER BY code, idx"):
                digest.update(data.encode())
            stats["version"] = f"{EXTRACTOR_VERSION}-{digest.hexdigest()[:12]}"
            self.db.executemany("INSERT INTO signature_meta VALUES (?,?)", [(k, json.dumps(v)) for k, v in stats.items()])
        self._sigs = None
        return stats

    def meta(self) -> dict:
        return {k: json.loads(v) for k, v in self.db.execute("SELECT key, value FROM signature_meta")}

    # ---- EffectKnowledge ------------------------------------------------------------
    def version(self) -> str:
        return self.meta().get("version", "unbuilt")

    def _load(self):
        if self._cards is not None:
            return
        from deepyugioh.cards.graph import CardGraph
        from deepyugioh.cards.store import CardStore

        store = CardStore(self.path)
        graph = CardGraph(self.path)
        eligible = graph.profiles  # same eligibility as the interaction graph
        self._cards = {c: store.get(c) for c in sorted(eligible)}
        self._refs = {c: (set(p.ref_codes), set(p.ref_sets)) for c, p in eligible.items()}
        self._sigs = {}
        for code, data in self.db.execute("SELECT code, data FROM effect_signatures ORDER BY code, idx"):
            if code in self._cards:
                self._sigs.setdefault(code, []).append(EffectSignature.from_dict(json.loads(data)))

    def codes(self) -> list[int]:
        self._load()
        return list(self._cards)

    def card(self, code: int) -> CardInfo | None:
        self._load()
        return self._cards.get(code)

    def signatures(self, code: int) -> list[EffectSignature]:
        self._load()
        return self._sigs.get(code, [])

    def names(self, code: int) -> set[int]:
        self._load()
        return self._refs.get(code, (set(), set()))[0]

    def archetypes(self, code: int) -> set[int]:
        self._load()
        return self._refs.get(code, (set(), set()))[1]

