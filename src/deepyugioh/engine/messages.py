"""Parsing of the binary message stream produced by OCG_DuelGetMessage.

Only messages relevant for the combo search are decoded in detail:
  * every decision message (the engine waits for a response)
  * a few event messages used to track which cards took part in a combo
All other messages are kept as `Event(msg, None)` and otherwise ignored.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from deepyugioh.engine import constants as K


class Reader:
    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes, pos: int = 0):
        self.buf = buf
        self.pos = pos

    def _take(self, fmt: str):
        v = struct.unpack_from(fmt, self.buf, self.pos)[0]
        self.pos += struct.calcsize(fmt)
        return v

    def u8(self) -> int:
        return self._take("<B")

    def i8(self) -> int:
        return self._take("<b")

    def u16(self) -> int:
        return self._take("<H")

    def u32(self) -> int:
        return self._take("<I")

    def i32(self) -> int:
        return self._take("<i")

    def u64(self) -> int:
        return self._take("<Q")

    def loc_info(self) -> "Loc":
        return Loc(self.u8(), self.u8(), self.u32(), self.u32())

    def remaining(self) -> int:
        return len(self.buf) - self.pos


@dataclass(frozen=True)
class Loc:
    controller: int
    location: int
    sequence: int
    position: int = 0


@dataclass(frozen=True)
class CardRef:
    """A card offered in a decision: its code and where it currently is."""

    code: int
    loc: Loc
    extra: int = 0  # release_param / sum_param / counter count depending on the message


@dataclass(frozen=True)
class ChainOption:
    card: CardRef
    description: int
    client_mode: int


@dataclass
class Decision:
    msg: int
    player: int
    data: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return _MSG_NAMES.get(self.msg, str(self.msg))


@dataclass(frozen=True)
class Event:
    msg: int
    data: dict | None


_MSG_NAMES = {v: k for k, v in vars(K).items() if k.startswith("MSG_") and isinstance(v, int)}


def _cards_short(r: Reader, n: int, seq32: bool = True, extra8: bool = False) -> list[CardRef]:
    """code u32, con u8, loc u8, seq u32|u8 (+ optional u8 extra)"""
    out = []
    for _ in range(n):
        code, con, loc = r.u32(), r.u8(), r.u8()
        seq = r.u32() if seq32 else r.u8()
        extra = r.u8() if extra8 else 0
        out.append(CardRef(code, Loc(con, loc, seq), extra))
    return out


def _cards_info(r: Reader, n: int, extra32: bool = False) -> list[CardRef]:
    """code u32 + loc_info (+ optional u32 extra)"""
    out = []
    for _ in range(n):
        code, loc = r.u32(), r.loc_info()
        extra = r.u32() if extra32 else 0
        out.append(CardRef(code, loc, extra))
    return out


def _chains(r: Reader, n: int, loc_info: bool) -> list[ChainOption]:
    out = []
    for _ in range(n):
        code = r.u32()
        if loc_info:
            loc = r.loc_info()
        else:
            loc = Loc(r.u8(), r.u8(), r.u32())
        out.append(ChainOption(CardRef(code, loc), r.u64(), r.u8()))
    return out


def parse_decision(msg: int, r: Reader) -> Decision:
    p = r.u8()
    d: dict = {}
    if msg == K.MSG_SELECT_IDLECMD:
        d["summonable"] = _cards_short(r, r.u32())
        d["spsummonable"] = _cards_short(r, r.u32())
        d["repositionable"] = _cards_short(r, r.u32(), seq32=False)
        d["msetable"] = _cards_short(r, r.u32())
        d["ssetable"] = _cards_short(r, r.u32())
        d["activatable"] = _chains(r, r.u32(), loc_info=False)
        d["to_bp"], d["to_ep"], d["can_shuffle"] = r.u8(), r.u8(), r.u8()
    elif msg == K.MSG_SELECT_BATTLECMD:
        d["activatable"] = _chains(r, r.u32(), loc_info=False)
        n = r.u32()
        d["attackable"] = [
            CardRef(r.u32(), Loc(r.u8(), r.u8(), r.u8()), r.u8()) for _ in range(n)
        ]
        d["to_m2"], d["to_ep"] = r.u8(), r.u8()
    elif msg == K.MSG_SELECT_EFFECTYN:
        d["card"] = CardRef(r.u32(), r.loc_info())
        d["description"] = r.u64()
    elif msg == K.MSG_SELECT_YESNO:
        d["description"] = r.u64()
    elif msg in (K.MSG_SELECT_OPTION, K.MSG_ANNOUNCE_CARD, K.MSG_ANNOUNCE_NUMBER):
        d["options"] = [r.u64() for _ in range(r.u8())]
    elif msg == K.MSG_SELECT_CARD:
        d["cancelable"], d["min"], d["max"] = r.u8(), r.u32(), r.u32()
        d["cards"] = _cards_info(r, r.u32())
    elif msg == K.MSG_SELECT_UNSELECT_CARD:
        d["finishable"], d["cancelable"], d["min"], d["max"] = r.u8(), r.u8(), r.u32(), r.u32()
        d["select"] = _cards_info(r, r.u32())
        d["unselect"] = _cards_info(r, r.u32())
    elif msg == K.MSG_SELECT_CHAIN:
        d["spe_count"], d["forced"] = r.u8(), r.u8()
        d["hint_timing"], d["other_timing"] = r.u32(), r.u32()
        d["chains"] = _chains(r, r.u32(), loc_info=True)
    elif msg in (K.MSG_SELECT_PLACE, K.MSG_SELECT_DISFIELD):
        d["count"], d["flag"] = r.u8(), r.u32()
    elif msg == K.MSG_SELECT_POSITION:
        d["code"], d["positions"] = r.u32(), r.u8()
    elif msg == K.MSG_SELECT_TRIBUTE:
        d["cancelable"], d["min"], d["max"] = r.u8(), r.u32(), r.u32()
        d["cards"] = _cards_short(r, r.u32(), extra8=True)
    elif msg == K.MSG_SELECT_COUNTER:
        d["counter_type"], d["count"] = r.u16(), r.u16()
        n = r.u32()
        d["cards"] = [CardRef(r.u32(), Loc(r.u8(), r.u8(), r.u8()), r.u16()) for _ in range(n)]
    elif msg == K.MSG_SELECT_SUM:
        d["exact"] = r.u8() == 0  # 0 = select exactly `acc`, 1 = at least `acc`
        d["acc"], d["min"], d["max"] = r.u32(), r.u32(), r.u32()
        d["must"] = _cards_info(r, r.u32(), extra32=True)
        d["cards"] = _cards_info(r, r.u32(), extra32=True)
    elif msg in (K.MSG_SORT_CARD, K.MSG_SORT_CHAIN):
        n = r.u32()
        d["cards"] = [CardRef(r.u32(), Loc(r.u8(), r.u32(), r.u32())) for _ in range(n)]
    elif msg == K.MSG_ANNOUNCE_RACE:
        d["count"], d["available"] = r.u8(), r.u64()
    elif msg == K.MSG_ANNOUNCE_ATTRIB:
        d["count"], d["available"] = r.u8(), r.u32()
    elif msg == K.MSG_ROCK_PAPER_SCISSORS:
        pass
    return Decision(msg, p, d)


def parse_event(msg: int, r: Reader) -> Event:
    if msg == K.MSG_CHAINING:
        code, loc = r.u32(), r.loc_info()
        return Event(msg, {"code": code, "loc": loc, "controller": r.u8()})
    if msg in (K.MSG_SUMMONING, K.MSG_SPSUMMONING, K.MSG_FLIPSUMMONING):
        return Event(msg, {"code": r.u32(), "loc": r.loc_info()})
    if msg == K.MSG_MOVE:
        code = r.u32()
        return Event(msg, {"code": code, "from": r.loc_info(), "to": r.loc_info(), "reason": r.u32()})
    if msg == K.MSG_WIN:
        return Event(msg, {"player": r.u8(), "reason": r.u8()})
    return Event(msg, None)


def split_messages(buf: bytes) -> list[tuple[int, Reader]]:
    out = []
    pos = 0
    while pos + 4 <= len(buf):
        (size,) = struct.unpack_from("<I", buf, pos)
        body = buf[pos + 4 : pos + 4 + size]
        pos += 4 + size
        if body:
            out.append((body[0], Reader(body, 1)))
    return out
