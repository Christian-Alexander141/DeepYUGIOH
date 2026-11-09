"""A single engine duel, prepared for combo search.

Setup used by the combo generator (see contracts.setups.StartSetup):
  * player 0 starts turn 1 in Main Phase 1 with a fixed hand, deck and extra deck
  * player 1 (opponent) gets the cards of the setup's OpponentBoard and never
    acts (DUEL_SIMPLE_AI covers the few decisions it could be asked for)
  * Master Rule 5, no opening draw (the hand is placed directly)
"""

from __future__ import annotations

import ctypes as C
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from deepyugioh import paths
from deepyugioh.contracts.cards import CardPool
from deepyugioh.contracts.setups import StartSetup
from deepyugioh.engine import constants as K
from deepyugioh.engine import ocg
from deepyugioh.engine.messages import Decision, Event, Reader, parse_decision, parse_event, split_messages

DUEL_FLAGS = K.DUEL_MODE_MR5 | K.DUEL_SIMPLE_AI | K.DUEL_PSEUDO_SHUFFLE

_ZONES = {"mzone": K.LOCATION_MZONE, "szone": K.LOCATION_SZONE, "hand": K.LOCATION_HAND}
_POSITIONS = {
    "faceup_attack": K.POS_FACEUP_ATTACK,
    "faceup_defense": K.POS_FACEUP_DEFENSE,
    "facedown_defense": K.POS_FACEDOWN_DEFENSE,
    "facedown": K.POS_FACEDOWN,
}


class EngineError(RuntimeError):
    pass


@dataclass(frozen=True)
class CardState:
    code: int
    location: int
    sequence: int
    position: int
    overlay: tuple[int, ...] = ()


@dataclass(frozen=True)
class PlayerState:
    lp: int
    hand: tuple[CardState, ...]
    mzone: tuple[CardState, ...]
    szone: tuple[CardState, ...]
    grave: tuple[CardState, ...]
    banished: tuple[CardState, ...]
    deck_count: int
    extra_count: int

    @property
    def field_count(self) -> int:
        return len(self.mzone) + len(self.szone)

    def to_dict(self) -> dict:
        def cards(cs):
            return [{"code": c.code, "seq": c.sequence, "pos": c.position, **({"overlay": list(c.overlay)} if c.overlay else {})} for c in cs]
        return {
            "lp": self.lp, "hand": sorted(c.code for c in self.hand),
            "mzone": cards(self.mzone), "szone": cards(self.szone),
            "grave": sorted(c.code for c in self.grave), "banished": sorted(c.code for c in self.banished),
            "deck_count": self.deck_count, "extra_count": self.extra_count,
        }


@dataclass(frozen=True)
class BoardState:
    players: tuple[PlayerState, PlayerState]
    winner: int | None = None

    def key(self) -> tuple:
        """Canonical, order-independent identity of the visible board.

        Hand, GY and banishment are treated as multisets; field cards keep
        their zone because zones matter (link arrows, column effects).
        Deck and extra deck are left out: what left them is visible elsewhere,
        and the key must not depend on unused cards of the search setup.
        """
        out = []
        for p in self.players:
            out.append((
                p.lp,
                tuple(sorted(c.code for c in p.hand)),
                tuple(sorted((c.sequence, c.code, c.position, c.overlay) for c in p.mzone)),
                tuple(sorted((c.sequence, c.code, c.position) for c in p.szone)),
                tuple(sorted(c.code for c in p.grave)),
                tuple(sorted(c.code for c in p.banished)),
            ))
        return (tuple(out), self.winner)

    def identity(self, start: "BoardState", zones_matter: bool = True) -> tuple:
        """Setup-independent identity of an end board relative to its start board.

        Player 0's side is taken as is; the opponent's side only as the change
        against the start (cards lost per zone, LP lost). A combo that never
        touches the opponent therefore has the same identity against every
        opponent board. With `zones_matter=False` (no Link monster on the
        field) the five main monster zones count as interchangeable.
        """
        own = self.key()[0][0]
        if not zones_matter:
            lp, hand, mz, sz, gy, ban = own
            # main zones become "M", extra monster zones keep their number; the sort key keeps both
            # comparable (main zones first) without changing the identity of boards that sorted before
            mz = tuple(sorted((("M" if seq < 5 else seq, code, pos, ov) for seq, code, pos, ov in mz),
                              key=lambda t: (t[0] != "M", 0 if t[0] == "M" else t[0], t[1:])))
            own = (lp, hand, mz, sz, gy, ban)
        (s_lp, s_hand, s_m, s_s, s_gy, s_ban), (e_lp, e_hand, e_m, e_s, e_gy, e_ban) = start.key()[0][1], self.key()[0][1]

        def lost(a, b):
            return tuple(sorted((Counter(a) - Counter(b)).elements()))

        def gained(a, b):
            return tuple(sorted((Counter(b) - Counter(a)).elements()))

        field_codes = lambda zone: [x[1] for x in zone]
        opp = (
            s_lp - e_lp,
            lost(s_hand, e_hand),
            lost(field_codes(s_m) + field_codes(s_s), field_codes(e_m) + field_codes(e_s)),
            gained(field_codes(s_m) + field_codes(s_s), field_codes(e_m) + field_codes(e_s)),
            gained(s_gy, e_gy),
            gained(s_ban, e_ban),
        )
        return (own, opp, self.winner)

    def to_dict(self) -> dict:
        return {"players": [p.to_dict() for p in self.players], "winner": self.winner}


_SCRIPT_CACHE: dict[str, bytes | None] = {}


def _find_script(name: str) -> bytes | None:
    if name in _SCRIPT_CACHE:
        return _SCRIPT_CACHE[name]
    data = None
    base = Path(name).name
    for folder in paths.SCRIPT_DIRS:
        f = folder / base
        if f.exists():
            data = f.read_bytes()
            break
    _SCRIPT_CACHE[name] = data
    return data


class Duel:
    """Owns one OCG_Duel. Use as a context manager or call close()."""

    def __init__(self, store: CardPool, setup: StartSetup, log=None):
        self.lib = ocg.load_library()
        self.store = store
        self.setup = setup
        self.events: list[Event] = []
        self.logs: list[str] = []
        self.winner: int | None = None
        self.ended = False
        self.turns = 0  # MSG_NEW_TURN count; the 2nd one means player 0's turn is over
        self.turn_over = False
        self._pending: Decision | None = None
        self._log = log
        self._setcode_bufs: dict[int, C.Array] = {}

        self._card_reader = ocg.DataReader(self._read_card)
        self._script_reader = ocg.ScriptReader(self._read_script)
        self._log_handler = ocg.LogHandler(self._on_log)
        self._reader_done = ocg.DataReaderDone(lambda payload, data: None)

        opts = ocg.OCGDuelOptions()
        for i, s in enumerate(setup.seed):
            opts.seed[i] = s
        opts.flags = DUEL_FLAGS
        opts.team1 = ocg.OCGPlayer(setup.starting_lp, 0, 1)
        opts.team2 = ocg.OCGPlayer(setup.starting_lp, 0, 1)
        opts.cardReader = self._card_reader
        opts.scriptReader = self._script_reader
        opts.logHandler = self._log_handler
        opts.cardReaderDone = self._reader_done
        opts.enableUnsafeLibraries = 0

        handle = C.c_void_p()
        status = self.lib.OCG_CreateDuel(C.byref(handle), C.byref(opts))
        if status != 0:
            raise EngineError(f"OCG_CreateDuel failed with status {status}")
        self.handle = handle
        for script in ("constant.lua", "utility.lua"):
            if not self._read_script(None, handle, script.encode()):
                self.close()
                raise EngineError(f"could not load {script} from {paths.CARD_SCRIPTS}")

        self._add_cards(setup.hand, K.LOCATION_HAND)
        self._add_cards(setup.deck, K.LOCATION_DECK)
        self._add_cards(setup.extra, K.LOCATION_EXTRA)
        self._add_opponent(setup)
        self.lib.OCG_StartDuel(handle)

    # ---- engine callbacks -------------------------------------------------
    def _read_card(self, payload, code, data_ptr):
        data = data_ptr.contents
        card = self.store.get(code)
        if card is None:
            data.code = 0
            return
        buf = self._setcode_bufs.get(code)
        if buf is None:
            buf = (C.c_uint16 * (len(card.setcodes) + 1))(*card.setcodes, 0)
            self._setcode_bufs[code] = buf
        data.code = card.code
        data.alias = card.alias
        data.setcodes = C.cast(buf, C.POINTER(C.c_uint16))
        data.type = card.type
        data.level = card.level
        data.attribute = card.attribute
        data.race = card.race
        data.attack = card.atk
        data.defense = card.def_
        data.lscale = card.lscale
        data.rscale = card.rscale
        data.link_marker = card.link_marker

    def _read_script(self, payload, duel, name):
        name = name.decode()
        src = _find_script(name)
        if src is None:
            return 0
        return self.lib.OCG_LoadScript(duel, src, len(src), name.encode())

    def _on_log(self, payload, text, kind):
        msg = text.decode(errors="replace")
        self.logs.append(msg)
        if self._log:
            self._log(kind, msg)

    # ---- setup ------------------------------------------------------------
    def _add_cards(self, codes, location):
        for code in codes:
            info = ocg.OCGNewCardInfo(0, 0, code, 0, location, 0, K.POS_FACEDOWN_DEFENSE)
            self.lib.OCG_DuelNewCard(self.handle, C.byref(info))

    def _add_opponent(self, setup: StartSetup):
        seqs = Counter()
        for card in setup.opponent.cards:
            loc = _ZONES[card.zone]
            pos = _POSITIONS[card.position]
            if loc == K.LOCATION_MZONE and pos == K.POS_FACEDOWN:
                pos = K.POS_FACEDOWN_DEFENSE
            if loc == K.LOCATION_HAND:
                pos = K.POS_FACEDOWN_DEFENSE
            seq = seqs[loc]
            seqs[loc] += 1
            if loc != K.LOCATION_HAND and seq > 4:
                raise EngineError(f"too many opponent cards in {card.zone}")
            info = ocg.OCGNewCardInfo(1, 0, card.code, 1, loc, seq, pos)
            self.lib.OCG_DuelNewCard(self.handle, C.byref(info))

    # ---- processing -------------------------------------------------------
    def advance(self, auto_respond_opponent=None) -> Decision | None:
        """Run the engine until player 0 has to decide. Returns None when the duel ended.

        `auto_respond_opponent(decision) -> bytes` answers decisions of player 1.
        """
        if self._pending is not None:
            return self._pending
        while not self.ended and not self.turn_over:
            status = self.lib.OCG_DuelProcess(self.handle)
            decision = self._drain_messages()
            if self.turns >= 2:
                self.turn_over = True
                return None
            if status == K.DUEL_STATUS_END:
                self.ended = True
                return None
            if decision is None:
                if status == K.DUEL_STATUS_AWAITING:
                    raise EngineError("engine awaits a response but sent no decision message")
                continue
            if decision.player != 0:
                if auto_respond_opponent is None:
                    raise EngineError(f"opponent decision {decision.name} without responder")
                self.lib.OCG_DuelSetResponse(self.handle, *_buf(auto_respond_opponent(decision)))
                continue
            self._pending = decision
            return decision
        return None

    def respond(self, response: bytes) -> None:
        if self._pending is None:
            raise EngineError("respond() without pending decision")
        self._pending = None
        self.lib.OCG_DuelSetResponse(self.handle, *_buf(response))

    def _drain_messages(self) -> Decision | None:
        length = C.c_uint32()
        ptr = self.lib.OCG_DuelGetMessage(self.handle, C.byref(length))
        buf = ocg.read_buffer(ptr, length.value)
        decision = None
        for msg, reader in split_messages(buf):
            if msg in K.DECISION_MESSAGES:
                decision = parse_decision(msg, reader)
            elif msg == K.MSG_RETRY:
                raise InvalidResponse("engine rejected the last response (MSG_RETRY)")
            else:
                ev = parse_event(msg, reader)
                self.events.append(ev)
                if msg == K.MSG_WIN:
                    self.winner = ev.data["player"]
                elif msg == K.MSG_NEW_TURN:
                    self.turns += 1
        return decision

    # ---- state ------------------------------------------------------------
    def board(self) -> BoardState:
        lps = self._query_lps()
        players = []
        for p in (0, 1):
            zones = {
                loc: self._query_location(p, loc)
                for loc in (K.LOCATION_HAND, K.LOCATION_MZONE, K.LOCATION_SZONE, K.LOCATION_GRAVE, K.LOCATION_REMOVED)
            }
            players.append(PlayerState(
                lp=lps[p],
                hand=zones[K.LOCATION_HAND],
                mzone=zones[K.LOCATION_MZONE],
                szone=zones[K.LOCATION_SZONE],
                grave=zones[K.LOCATION_GRAVE],
                banished=zones[K.LOCATION_REMOVED],
                deck_count=self.lib.OCG_DuelQueryCount(self.handle, p, K.LOCATION_DECK),
                extra_count=self.lib.OCG_DuelQueryCount(self.handle, p, K.LOCATION_EXTRA),
            ))
        return BoardState((players[0], players[1]), self.winner)

    def _query_lps(self) -> tuple[int, int]:
        length = C.c_uint32()
        ptr = self.lib.OCG_DuelQueryField(self.handle, C.byref(length))
        r = Reader(ocg.read_buffer(ptr, length.value))
        r.u32()  # duel options
        lps = []
        for _ in range(2):
            lps.append(r.u32())
            for _slot in range(7 + 8):  # 7 monster zones, 8 spell/trap zones
                if r.u8():
                    r.u8()
                    r.u32()
            for _count in range(6):
                r.u32()
        return lps[0], lps[1]

    def _query_location(self, player: int, location: int) -> tuple[CardState, ...]:
        info = ocg.OCGQueryInfo(K.QUERY_CODE | K.QUERY_POSITION | K.QUERY_OVERLAY_CARD, player, location, 0, 0)
        length = C.c_uint32()
        ptr = self.lib.OCG_DuelQueryLocation(self.handle, C.byref(length), C.byref(info))
        r = Reader(ocg.read_buffer(ptr, length.value))
        if r.remaining() < 4:
            return ()
        r.u32()  # total size
        cards = []
        seq = 0
        while r.remaining() >= 2:
            size = r.u16()
            if size == 0:  # empty zone
                seq += 1
                continue
            code = pos = 0
            overlay: tuple[int, ...] = ()
            while True:
                flag = r.u32()
                if flag == K.QUERY_END:
                    break
                body = Reader(r.buf, r.pos)
                if flag == K.QUERY_CODE:
                    code = body.u32()
                elif flag == K.QUERY_POSITION:
                    pos = body.u32()
                elif flag == K.QUERY_OVERLAY_CARD:
                    overlay = tuple(sorted(body.u32() for _ in range(body.u32())))
                r.pos += size - 4
                size = r.u16()
            cards.append(CardState(code, location, seq, pos, overlay))
            seq += 1
        return tuple(cards)

    # ---- lifecycle --------------------------------------------------------
    def close(self):
        if getattr(self, "handle", None):
            self.lib.OCG_DestroyDuel(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        self.close()


class InvalidResponse(EngineError):
    pass


def _buf(data: bytes):
    return data, len(data)
