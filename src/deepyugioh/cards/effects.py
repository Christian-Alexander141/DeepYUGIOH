"""Step 2 (first version): static analysis of Lua card scripts.

The scripts already encode most of the effect structure through constants:

    e1:SetCategory(CATEGORY_SPECIAL_SUMMON+CATEGORY_TOGRAVE)   -> what it does
    e1:SetType(EFFECT_TYPE_SINGLE+EFFECT_TYPE_TRIGGER_O)        -> how it is used
    e1:SetCode(EVENT_TO_GRAVE)                                  -> when it triggers
    e1:SetRange(LOCATION_MZONE)                                 -> where it works
    e1:SetCountLimit(1,id)                                      -> once per turn

This module extracts these constants per effect variable. It is deliberately
shallow: target filters ("Level 4 or lower DARK") are not interpreted here.
Everything extracted is a hint for candidate generation only - the engine
remains the authority on whether something actually works.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_CREATE = re.compile(r"\b(\w+)\s*=\s*Effect\.CreateEffect\(")
_CLONE = re.compile(r"\b(\w+)\s*=\s*(\w+):Clone\(\)")
_SETTER = re.compile(r"\b(\w+):(SetCategory|SetType|SetCode|SetRange|SetProperty|SetCountLimit)\((.*)\)")
_CONST = re.compile(r"\b(?:CATEGORY|EFFECT_TYPE|EFFECT_FLAG|EVENT|EFFECT|LOCATION)_[A-Z0-9_]+\b")


@dataclass
class ScriptEffect:
    idx: int
    var: str
    types: list[str] = field(default_factory=list)
    event: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    ranges: list[str] = field(default_factory=list)
    properties: list[str] = field(default_factory=list)
    count_limit: str | None = None

    def copy_as(self, idx: int, var: str) -> "ScriptEffect":
        return ScriptEffect(
            idx, var, list(self.types), list(self.event), list(self.categories),
            list(self.ranges), list(self.properties), self.count_limit,
        )


def parse_script(source: str) -> list[ScriptEffect]:
    effects: list[ScriptEffect] = []
    current: dict[str, ScriptEffect] = {}  # latest effect bound to each variable name
    for raw in source.splitlines():
        line = raw.split("--", 1)[0]
        if not line.strip():
            continue
        if m := _CREATE.search(line):
            eff = ScriptEffect(len(effects), m.group(1))
            effects.append(eff)
            current[eff.var] = eff
            continue
        if (m := _CLONE.search(line)) and m.group(2) in current:
            eff = current[m.group(2)].copy_as(len(effects), m.group(1))
            effects.append(eff)
            current[eff.var] = eff
            continue
        for m in _SETTER.finditer(line):
            eff = current.get(m.group(1))
            if eff is None:
                continue
            setter, args = m.group(2), m.group(3)
            consts = _CONST.findall(args)
            if setter == "SetCategory":
                eff.categories = consts
            elif setter == "SetType":
                eff.types = consts
            elif setter == "SetCode":
                eff.event = consts
            elif setter == "SetRange":
                eff.ranges = consts
            elif setter == "SetProperty":
                eff.properties = consts
            elif setter == "SetCountLimit":
                eff.count_limit = args.strip()
    return effects
