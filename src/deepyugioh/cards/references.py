"""Static extraction of what a card script refers to.

Per card (not per effect - scripts share filter functions between effects):

  * named cards      s.listed_names, s.material, IsCode(...), CARD_* constants
  * named archetypes s.listed_series, s.material_setcode, IsSetCard(...), SET_* constants
  * simple filters   IsLevelBelow/Above/IsLevel, IsRace, IsAttribute, IsType(TYPE_TUNER)
  * locations used   LOCATION_* constants used outside SetRange (where effects take cards from)

Everything here is a *hint* for candidate generation and deliberately generous;
the engine decides what actually works.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from deepyugioh import paths

_DEF = re.compile(r"^\s*((?:CARD|SET|RACE|ATTRIBUTE)_[A-Z0-9_]+)\s*=\s*(0x[0-9a-fA-F]+|\d+)", re.M)
_LISTED = re.compile(r"s\.(listed_names|listed_series|material|material_setcode)\s*=\s*\{?([^}\n]*)\}?")
_ISCODE = re.compile(r"IsCode\(([^()]*)\)")
_ISSET = re.compile(r"IsSetCard\(([^()]*)\)")
_LEVEL = re.compile(r"Is(Level|Rank|Link)(Below|Above)?\((\d+)\)")
_RACE = re.compile(r"\bRACE_[A-Z]+\b")
_ATTR = re.compile(r"\bATTRIBUTE_[A-Z]+\b")
_LOC = re.compile(r"\bLOCATION_(DECK|HAND|GRAVE|REMOVED|EXTRA|MZONE|SZONE|ONFIELD)\b")
_TYPEF = re.compile(r"\bIs(Monster|Spell|Trap|SpellTrap)\(|\bTYPE_(MONSTER|SPELL|TRAP|EQUIP|FIELD|CONTINUOUS|QUICKPLAY|RITUAL|NORMAL)\b")
_TOKEN = re.compile(r"\b(0x[0-9a-fA-F]+|\d{5,9}|(?:CARD|SET)_[A-Z0-9_]+)\b")


@cache
def constants() -> dict[str, int]:
    """CARD_*, SET_*, RACE_*, ATTRIBUTE_* values defined by the script library."""
    out: dict[str, int] = {}
    for f in sorted(paths.CARD_SCRIPTS.glob("*.lua")):
        for name, value in _DEF.findall(f.read_text(encoding="utf-8", errors="replace")):
            out.setdefault(name, int(value, 0))
    return out


@dataclass
class ScriptRefs:
    codes: set[int] = field(default_factory=set)
    setcodes: set[int] = field(default_factory=set)
    max_level: int | None = None      # loosest upper bound found (None = unbounded)
    min_level: int | None = None
    races: int = 0                    # bitmask, 0 = any
    attributes: int = 0               # bitmask, 0 = any
    tuner: bool = False
    type_filters: set[str] = field(default_factory=set)  # MONSTER, SPELL, TRAP, EQUIP, ...
    locations: set[str] = field(default_factory=set)

    @property
    def restricted(self) -> bool:
        return bool(self.codes or self.setcodes)


def _values(text: str, own_code: int, consts: dict[str, int], want: str) -> set[int]:
    out = set()
    for tok in _TOKEN.findall(text):
        if tok.startswith(("CARD_", "SET_")):
            if tok.startswith(want) and tok in consts:
                out.add(consts[tok])
        elif want == "SET_" and tok.startswith("0x"):
            out.add(int(tok, 16))
        elif want == "CARD_" and tok.isdigit():
            out.add(int(tok))
    if want == "CARD_" and re.search(r"\bid\b", text):
        out.add(own_code)
    return out


def parse_refs(source: str, own_code: int) -> ScriptRefs:
    consts = constants()
    refs = ScriptRefs()
    code_text = "\n".join(line.split("--", 1)[0] for line in source.splitlines())

    for kind, body in _LISTED.findall(code_text):
        if kind in ("listed_names", "material"):
            refs.codes |= _values(body, own_code, consts, "CARD_")
        else:
            refs.setcodes |= _values(body, own_code, consts, "SET_")
    for body in _ISCODE.findall(code_text):
        refs.codes |= _values(body, own_code, consts, "CARD_")
    for body in _ISSET.findall(code_text):
        refs.setcodes |= _values(body, own_code, consts, "SET_")
    refs.codes.discard(own_code)

    for kind, bound, n in _LEVEL.findall(code_text):
        n = int(n)
        if bound in ("Below", ""):
            refs.max_level = max(refs.max_level or 0, n)
        if bound in ("Above", ""):
            refs.min_level = n if refs.min_level is None else min(refs.min_level, n)
    for name in set(_RACE.findall(code_text)):
        if name != "RACE_ALL":
            refs.races |= consts.get(name, 0)
    for name in set(_ATTR.findall(code_text)):
        if name != "ATTRIBUTE_ALL":
            refs.attributes |= consts.get(name, 0)
    refs.tuner = "TYPE_TUNER" in code_text
    for fn, const in _TYPEF.findall(code_text):
        if fn == "SpellTrap":
            refs.type_filters |= {"SPELL", "TRAP"}
        else:
            refs.type_filters.add((fn or const).upper())

    for line in code_text.splitlines():
        if "SetRange" not in line:
            refs.locations.update(_LOC.findall(line))
    return refs


def setcode_matches(card_setcodes: tuple[int, ...] | list[int], wanted: int) -> bool:
    """Same semantics as the core: base archetype (low 12 bits) + sub-archetype bits."""
    base, sub = wanted & 0xFFF, wanted & 0xF000
    return any((sc & 0xFFF) == base and (sc & 0xF000 & sub) == sub for sc in card_setcodes)


def script_path(rel: str | None) -> Path | None:
    return paths.CARD_SCRIPTS / rel if rel else None
