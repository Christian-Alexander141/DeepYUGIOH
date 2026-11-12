"""Robustness (Stufe 2, step 2.8): what is left of a combo when the opponent interrupts - decided by the engine.

Phase 1 searches against an opponent that never acts. Here the opponent gets
one handtrap and the search becomes adversarial:

    our decisions        every option is tried, the best result counts          (maximum)
    opponent decisions   wherever the engine lets the opponent use the handtrap, both ways are
                         tried - use it (with every target the engine offers) or not - and the
                         worst result for us counts                               (minimum)

The result per handtrap is the *guaranteed* outcome: the minimum over the
opponent's choices of the maximum over our answers. It is measured with the
same value as the axis `outcome`, at the end of the turn. Nothing is
approximated: a search that hits a limit is stored as incomplete and never
counts as passed.

Cards: the search uses exactly the combo's own cards (hand, deck, extra deck
and, for later game situations, its resources). If the combo's result cannot
be reached with them alone (e.g. an effect needs more cards in the deck), the
full start setup of the combo is used instead and the result says so.

    retention(handtrap) = (guaranteed - floor) / (undisturbed - floor), clamped to 0..1
    robustness          = mean retention, weighted by how common each handtrap is

`floor` is the value of the start board (doing nothing), `undisturbed` the
best result without any handtrap in the same setup.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Callable

from deepyugioh.analysis.enginejobs import run_jobs, worker_pool, worker_state
from deepyugioh.contracts.cards import CardPool
from deepyugioh.contracts.combos import ComboReader, ComboRecord
from deepyugioh.contracts.setups import FieldCard, OpponentBoard, OpponentCard, StartSetup

AXIS = "robustness"
VERSION = "1"
BASELINE = "-"

# key -> (card, cards the opponent needs in the deck for it, default weight = how common it is, 0-1)
DEFAULT_CATALOG: dict[str, dict] = {
    "ash": {"card": "Ash Blossom & Joyous Spring", "weight": 1.0},
    "maxx_c": {"card": 'Maxx "C"', "weight": 0.9},
    "imperm": {"card": "Infinite Impermanence", "weight": 0.8},
    "veiler": {"card": "Effect Veiler", "weight": 0.6},
    "nibiru": {"card": "Nibiru, the Primal Being", "weight": 0.6},
    "droll": {"card": "Droll & Lock Bird", "weight": 0.5},
    "ogre": {"card": "Ghost Ogre & Snow Rabbit", "weight": 0.4},
    "belle": {"card": "Ghost Belle & Haunted Mansion", "weight": 0.3},
    "crow": {"card": "D.D. Crow", "weight": 0.3},
    "gamma": {"card": "PSY-Framegear Gamma", "deck": ["PSY-Frame Driver"], "weight": 0.3},
}


@dataclass(frozen=True)
class RobustnessSettings:
    max_nodes: int = 20_000        # decision points per search (one search per combo and handtrap)
    time_limit: float = 60.0       # seconds per search
    max_depth: int = 150           # decisions per line
    max_choices: int = 128
    allow_mset: bool = False
    allow_sset: bool = False
    opponent_deck: tuple[str, ...] = ("Mystical Elf",) * 15   # so that drawing (Maxx "C") never decks the opponent out
    catalog: dict = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_CATALOG.items()})

    def search_version(self) -> str:
        """Stored job results are only reused if they come from the same search rules."""
        payload = json.dumps([VERSION, self.allow_mset, self.allow_sset, self.max_choices, list(self.opponent_deck),
                              sorted((k, v["card"], v.get("deck", [])) for k, v in self.catalog.items())])
        return f"{VERSION}/" + hashlib.sha256(payload.encode()).hexdigest()[:10]

    def axis_version(self) -> str:
        weights = sorted((k, v.get("weight", 1.0)) for k, v in self.catalog.items())
        return self.search_version() + "/w-" + hashlib.sha256(json.dumps(weights).encode()).hexdigest()[:8]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["opponent_deck"] = list(self.opponent_deck)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RobustnessSettings":
        d = dict(d)
        known = set(cls.__dataclass_fields__)
        unknown = set(d) - known - {"handtraps", "workers"}
        if unknown:
            raise ValueError(f"unknown [analysis.robustness] options: {sorted(unknown)}")
        catalog = {k: dict(v) for k, v in DEFAULT_CATALOG.items()}
        for key, spec in d.pop("handtraps", {}).items():
            if spec is False or (isinstance(spec, dict) and spec.get("enabled") is False):
                catalog.pop(key, None)
                continue
            entry = catalog.setdefault(key, {})
            entry.update({k: v for k, v in spec.items() if k != "enabled"})
            if "card" not in entry:
                raise ValueError(f"[analysis.robustness.handtraps.{key}]: needs `card`")
            if not 0 <= float(entry.get("weight", 1.0)) <= 1:
                raise ValueError(f"[analysis.robustness.handtraps.{key}]: weight must be between 0 and 1")
        d.pop("workers", None)
        d.pop("catalog", None)
        if "opponent_deck" in d:
            d["opponent_deck"] = tuple(d["opponent_deck"])
        out = cls(catalog=catalog, **d)
        if out.max_nodes < 1 or out.time_limit <= 0 or out.max_depth < 1 or not out.catalog:
            raise ValueError("[analysis.robustness]: limits must be positive and the catalog not empty")
        return out


_HIDDEN = {"no chain", "place", "position", "keep order"}


# ---- the adversarial search ----------------------------------------------------------------------
class _Budget(Exception):
    pass


@dataclass
class SearchOutcome:
    value: float | None            # None if the search is incomplete
    line: list[tuple[int, str]]    # principal variation: (player, label)
    nodes: int
    replays: int
    elapsed: float
    incomplete: dict
    end: dict | None = None        # raw outcome facts of the guaranteed end board

    @property
    def complete(self) -> bool:
        return not self.incomplete


class AdversarialSearch:
    """Minimax over one start setup. `acting` are the opponent's cards that may be used (the handtrap)."""

    def __init__(self, pool: CardPool, setup: StartSetup, evaluate: Callable[[dict, dict], tuple[dict, float]],
                 settings: RobustnessSettings = RobustnessSettings(), acting: frozenset[int] = frozenset()):
        from deepyugioh.generator.search.actions import Enumerator

        self.pool, self.setup, self.evaluate, self.settings, self.acting = pool, setup, evaluate, settings, acting
        self.enum = Enumerator(names=pool.name, max_choices=settings.max_choices, allow_mset=settings.allow_mset,
                               allow_sset=settings.allow_sset, branch_zones=setup.branch_zones)
        self.nodes = self.replays = 0
        self.incomplete: Counter = Counter()
        self.memo: dict = {}
        self.start = None
        self.floor: float | None = None
        self.critical: dict | None = None   # the interruption that costs us most (see _visit)

    def run(self) -> SearchOutcome:
        t0 = time.perf_counter()
        self._deadline = t0 + self.settings.time_limit
        value, line, end = None, [], None
        try:
            value, line, end = self._expand(self._duel(), [])
        except _Budget:
            pass
        if self.enum.truncated:
            self.incomplete["choices_truncated"] += self.enum.truncated
        if self.incomplete:
            value = None
        return SearchOutcome(value, line, self.nodes, self.replays, time.perf_counter() - t0, dict(self.incomplete), end)

    # ---- engine ------------------------------------------------------------------------------
    def _duel(self):
        from deepyugioh.engine.duel import Duel

        return Duel(self.pool, self.setup, simple_ai=False)

    def _auto(self, decision):
        """Opponent decisions that are no real choice are answered at once; real ones go to the search."""
        from deepyugioh.engine import constants as K

        if decision.msg == K.MSG_SELECT_CHAIN:
            usable = [ch for ch in decision.data["chains"] if ch.card.code in self.acting]
            if not usable:   # nothing the opponent may use: pass, or take a mandatory effect
                return (0 if decision.data["forced"] else -1).to_bytes(4, "little", signed=True)
            return None
        options = self.enum.options(decision)
        if not options:
            from deepyugioh.generator.search.actions import Unsupported

            raise Unsupported(f"{decision.name} without options")
        return options[0].response if len(options) == 1 else None

    def _replay(self, line):
        self.replays += 1
        duel = self._duel()
        for _, _, response in line:
            duel.advance(self._auto)
            duel.respond(response)
        return duel

    def _options(self, decision):
        from deepyugioh.engine import constants as K
        from deepyugioh.generator.search.actions import Option

        if decision.player == 1 and decision.msg == K.MSG_SELECT_CHAIN:
            out, seen = [], set()
            for i, ch in enumerate(decision.data["chains"]):
                key = (ch.card.code, ch.description)
                if ch.card.code in self.acting and key not in seen:
                    seen.add(key)
                    out.append(Option(f"chain {self.pool.name(ch.card.code)}", i.to_bytes(4, "little", signed=True)))
            if not decision.data["forced"]:
                out.append(Option("no chain", b"\xff\xff\xff\xff"))
            return out
        return self.enum.options(decision)

    # ---- minimax -------------------------------------------------------------------------------
    def _expand(self, duel, line):
        try:
            return self._visit(duel, line)
        finally:
            duel.close()

    def _terminal(self, board):
        from deepyugioh.generator.search.combo import benefit

        b = benefit(self.start, board).to_dict()
        # the handtrap leaving the opponent's hand is its use, not a card we took from the opponent
        left = Counter(c.code for c in self.start.players[1].hand) - Counter(c.code for c in board.players[1].hand)
        b["opp_hand_loss"] -= sum(n for code, n in left.items() if code in self.acting)
        raw, value = self.evaluate(board.to_dict(), b)
        return value, [], raw

    def _visit(self, duel, line):
        from deepyugioh.engine import constants as K
        from deepyugioh.engine.duel import InvalidResponse
        from deepyugioh.generator.search.actions import Unsupported
        from deepyugioh.generator.search.combo import activation_tags

        if self.nodes >= self.settings.max_nodes:
            self.incomplete["max_nodes"] += 1
            raise _Budget
        if time.perf_counter() > self._deadline:
            self.incomplete["time_limit"] += 1
            raise _Budget
        try:
            decision = duel.advance(self._auto)
        except InvalidResponse:
            self.incomplete["engine_rejected_response"] += 1
            raise _Budget from None
        except Unsupported as e:
            self.incomplete[f"unsupported:{e}"] += 1
            raise _Budget from None
        self.nodes += 1
        board = duel.board()
        if self.start is None:
            self.start = board
            self.floor = self._terminal(board)[0]
        self.enum.link_on_field = any(
            (card := self.pool.get(c.code)) is not None and card.type & K.TYPE_LINK for c in board.players[0].mzone)
        if decision is None or duel.turn_over or duel.ended:
            return self._terminal(board)
        ours = decision.player == 0
        key = None
        if ours and decision.msg == K.MSG_SELECT_IDLECMD:
            # hidden state (once per turn, negated effects) follows from what was activated and from what the
            # opponent did - the same assumption as the transposition rule of Phase 1, plus the opponent's choices
            opp = tuple(r for p, label, r in line if p == 1 and label != "no chain")
            key = (board.key(), activation_tags(duel.events), opp)
            if key in self.memo:
                return self.memo[key]
        if len(line) >= self.settings.max_depth:
            self.incomplete["max_depth"] += 1
            raise _Budget
        try:
            options = self._options(decision)
        except Unsupported as e:
            self.incomplete[f"unsupported:{e}"] += 1
            raise _Budget from None
        best = None
        values: dict[str, float] = {}
        for i, opt in enumerate(options):
            step = (decision.player, opt.label, opt.response)
            last = i == len(options) - 1
            child = duel if last else self._replay(line)
            if not last:
                child.advance(self._auto)
            child.respond(opt.response)
            value, rest, end = self._visit(child, line + [step]) if last else self._expand(child, line + [step])
            values[opt.label] = value
            if best is None or (value > best[0] if ours else value < best[0]):
                best = (value, [(decision.player, opt.label)] + rest, end)
        if best is None:
            best = self._terminal(board)
        if not ours and "no chain" in values and len(values) > 1:
            # the critical step: where using the handtrap costs us most compared to not using it there.
            # We may avoid that step in the end (the guaranteed line then never shows the handtrap).
            label, worst = min(((k, v) for k, v in values.items() if k != "no chain"), key=lambda kv: kv[1])
            cost = values["no chain"] - worst
            if cost > 0 and (self.critical is None or cost > self.critical["cost"] + 1e-9):
                own = [lab for p, lab, _ in line if p == 0 and lab not in _HIDDEN]
                self.critical = {"after": own[-1] if own else "Beginn des Zuges", "opponent": label[6:],
                                 "cost": round(cost, 4), "step": len(line)}
        if key is not None:
            self.memo[key] = best
        return best


# ---- one combo -------------------------------------------------------------------------------------
def combo_setup(combo: ComboRecord, full: bool = False) -> StartSetup:
    """The setup the robustness search runs in: the combo's own cards, or its full start setup."""
    s = combo.setup
    if full:
        return s
    fields, need = [], Counter(combo.field)
    for c in s.field:
        if need[c.code] > 0:
            need[c.code] -= 1
            fields.append(FieldCard(c.code, c.zone, c.position))
    return StartSetup(hand=combo.hand, deck=combo.deck, extra=combo.extra, opponent=s.opponent, seed=s.seed,
                      starting_lp=s.starting_lp, master_rule=s.master_rule, branch_zones=s.branch_zones,
                      grave=combo.grave, banished=combo.banished, field=tuple(fields))


def with_handtrap(setup: StartSetup, pool: CardPool, settings: RobustnessSettings, key: str | None) -> tuple[StartSetup, frozenset[int]]:
    """The setup with the opponent holding the handtrap (key None: no handtrap) and a deck to draw from."""
    cards = list(setup.opponent.cards)
    cards += [OpponentCard(pool.resolve(n).code, "deck") for n in settings.opponent_deck]
    acting: set[int] = set()
    if key is not None:
        entry = settings.catalog[key]
        code = pool.resolve(entry["card"]).code
        cards.append(OpponentCard(code, "hand"))
        acting.add(code)
        for name in entry.get("deck", ()):
            extra = pool.resolve(name).code
            cards.append(OpponentCard(extra, "deck"))
            acting.add(extra)
    board = OpponentBoard(f"{setup.opponent.name}+{key or 'none'}", tuple(cards))
    return StartSetup(hand=setup.hand, deck=setup.deck, extra=setup.extra, opponent=board, seed=setup.seed,
                      starting_lp=setup.starting_lp, master_rule=setup.master_rule, branch_zones=setup.branch_zones,
                      grave=setup.grave, banished=setup.banished, field=setup.field), frozenset(acting)


def _used(line: list[tuple[int, str]]) -> bool:
    """Does the opponent use the handtrap in the guaranteed line?"""
    return any(player == 1 and label.startswith("chain ") for player, label in line)


def analyse_combo(pool: CardPool, combo: ComboRecord, evaluate, settings: RobustnessSettings,
                  handtraps: list[str] | None = None, baseline: dict | None = None) -> list[tuple]:
    """Search the baseline (unless given) and the handtraps of one combo.
    Returns rows (handtrap, status, value, data, elapsed) - BASELINE first."""
    rows = []
    own_raw, own_value = evaluate(combo.end_board, combo.benefit)
    if baseline is None:
        elapsed = 0.0
        for full in (False, True):   # first with the combo's own cards, then with its whole start setup
            setup, acting = with_handtrap(combo_setup(combo, full), pool, settings, None)
            search = AdversarialSearch(pool, setup, evaluate, settings, acting)
            res = search.run()
            elapsed += res.elapsed
            data = {"setup": "full" if full else "cards", "nodes": res.nodes, "replays": res.replays,
                    "incomplete": res.incomplete, "floor": search.floor, "combo_value": own_value}
            reached = res.complete and res.value >= own_value - 0.01
            if reached:
                break
        if res.complete and not reached:
            data["incomplete"] = {"combo_not_reproduced": 1}   # even the full setup does not reach the stored result
        status = "complete" if reached else "incomplete"
        rows.append((BASELINE, status, res.value if reached else None, data, elapsed))
        baseline = {"status": status, **data}
    if baseline["status"] != "complete":
        return rows   # without an undisturbed result there is nothing to compare against
    full = baseline["setup"] == "full"
    for key in handtraps if handtraps is not None else sorted(settings.catalog):
        setup, acting = with_handtrap(combo_setup(combo, full), pool, settings, key)
        search = AdversarialSearch(pool, setup, evaluate, settings, acting)
        res = search.run()
        data = {"nodes": res.nodes, "replays": res.replays, "incomplete": res.incomplete,
                "used": res.complete and _used(res.line), "critical": search.critical if res.complete else None,
                "line": [label for p, label in res.line if label not in _HIDDEN][:40] if res.complete else [],
                "end": {k: res.end[k] for k in ("monsters", "disruptions", "negates", "card_advantage",
                                                                   "opp_losses", "wins")} if res.end else None}
        rows.append((key, "complete" if res.complete else "incomplete", res.value, data, res.elapsed))
    return rows


# ---- jobs --------------------------------------------------------------------------------------------
def _work(payload):
    combo_id, handtraps, baseline, settings_dict = payload
    state = worker_state()
    if "repo" not in state:
        from deepyugioh.analysis.axes import OutcomeAnalyzer
        from deepyugioh.cards.signatures import SignatureStore
        from deepyugioh.repository.combo_db import ComboRepository

        state["repo"] = ComboRepository()
        state["outcome"] = OutcomeAnalyzer(worker_pool(), SignatureStore())
    skey = json.dumps(settings_dict, sort_keys=True)
    if skey not in state:
        state[skey] = RobustnessSettings(**{**settings_dict, "opponent_deck": tuple(settings_dict["opponent_deck"])})
    settings = state[skey]
    combo = state["repo"].get(combo_id)
    return combo.signature, analyse_combo(worker_pool(), combo, state["outcome"].evaluate, settings, handtraps, baseline)


def run_robustness(reader: ComboReader, repo, pool_factory, settings: RobustnessSettings, *, workers: int = 1,
                   max_combos: int | None = None, time_budget: float | None = None, retry_incomplete: bool = False,
                   should_stop: Callable[[], bool] | None = None, combo_ids: list[int] | None = None,
                   progress: Callable[[dict], None] | None = None) -> dict:
    """Process combos, strongest first, resuming where the last run stopped. Returns a summary."""
    version = settings.search_version()
    stored: dict[str, dict[str, tuple]] = {}
    for sig, h, status, value, data in repo.robustness_rows(version=version):
        stored.setdefault(sig, {})[h] = (status, data)
    strength = repo.scores("strength") or repo.scores("outcome")
    todo = []
    for combo in reader.iter_combos():
        if combo_ids is not None and combo.id not in combo_ids:
            continue
        have = stored.get(combo.signature, {})
        base = have.get(BASELINE)
        if base is not None and base[0] != "complete" and not retry_incomplete:
            continue
        baseline = {"status": "complete", **base[1]} if base is not None and base[0] == "complete" else None
        missing = [k for k in sorted(settings.catalog)
                   if k not in have or (retry_incomplete and have[k][0] != "complete")]
        if baseline is not None and not missing:
            continue
        todo.append((strength.get(combo.signature, 0.0), combo.id, missing, baseline))
    todo.sort(key=lambda t: (-t[0], t[1]))
    if max_combos is not None:
        todo = todo[:max_combos]
    t0 = time.perf_counter()
    deadline = t0 + time_budget if time_budget else None
    stop = lambda: bool((should_stop and should_stop()) or (deadline and time.perf_counter() > deadline))
    summary = {"combos": 0, "pending": len(todo), "complete": 0, "incomplete": 0, "errors": [], "searches": 0}
    payloads = ((cid, missing, baseline, settings.to_dict()) for _, cid, missing, baseline in todo)
    for payload, result, error in run_jobs(_work, payloads, workers=workers, pool_factory=pool_factory, should_stop=stop):
        summary["combos"] += 1
        if error is not None:
            summary["errors"].append(f"combo #{payload[0]}: {error}")
        else:
            sig, rows = result
            repo.write_robustness([(sig, h, version, status, value, data, elapsed) for h, status, value, data, elapsed in rows])
            summary["searches"] += len(rows)
            ok = all(status == "complete" for _, status, *_ in rows)
            summary["complete" if ok else "incomplete"] += 1
        if progress:
            progress({**summary, "elapsed": time.perf_counter() - t0})
    summary["elapsed"] = time.perf_counter() - t0
    summary["stopped"] = stop() and summary["combos"] < summary["pending"]
    return summary


# ---- the axis ---------------------------------------------------------------------------------------
def build_axis(repo, settings: RobustnessSettings, name: Callable[[str], str] = lambda key: key) -> dict:
    """Turn the stored job results into the axis `robustness`. Only combos whose baseline and every handtrap
    of the catalog are complete get a value - a combo that was only partly searched has no robustness yet."""
    version = settings.search_version()
    per: dict[str, dict] = {}
    for sig, h, status, value, data in repo.robustness_rows(version=version):
        per.setdefault(sig, {})[h] = (status, value, data)
    weights = {k: float(v.get("weight", 1.0)) for k, v in settings.catalog.items()}
    rows, partial = [], 0
    for sig, jobs in per.items():
        base = jobs.get(BASELINE)
        if base is None or base[0] != "complete" or any(jobs.get(k, ("",))[0] != "complete" for k in weights):
            partial += 1
            continue
        undisturbed, floor = base[1], base[2]["floor"]
        span = undisturbed - floor
        traps, wsum, total = {}, 0.0, 0.0
        for k, w in weights.items():
            _, value, data = jobs[k]
            retention = (1.0 if value >= undisturbed else 0.0) if span <= 1e-9 else max(0.0, min(1.0, (value - floor) / span))
            traps[k] = {"guaranteed": value, "retention": round(retention, 4), "used": data["used"],
                        "critical": data["critical"], "end": data["end"], "weight": w}
            wsum += w
            total += w * retention
        robustness = total / wsum if wsum else 0.0
        worst = min(traps, key=lambda k: (traps[k]["retention"], -weights[k]))
        raw = {"robustness": round(robustness, 4), "undisturbed": undisturbed, "floor": floor,
               "combo_value": base[2]["combo_value"], "setup": base[2]["setup"], "handtraps": traps, "worst": worst,
               "stops": sorted(k for k, t in traps.items() if t["retention"] <= 0.0)}
        hit = [k for k in sorted(traps, key=lambda k: traps[k]["retention"]) if traps[k]["retention"] < 0.999]
        text = f"behält im Mittel {robustness:.0%} des Ergebnisses gegen {len(traps)} Handtraps"
        if hit:
            text += "; " + ", ".join(f"{name(k)} {traps[k]['retention']:.0%}" for k in hit[:4])
            crit = traps[worst]["critical"]
            if crit:
                text += f"; am empfindlichsten nach „{crit['after']}“ ({name(worst)})"
        else:
            text += "; keine davon kostet etwas"
        if base[2]["setup"] == "full":
            text += " (mit dem ganzen Startaufbau gesucht)"
        rows.append((sig, AXIS, settings.axis_version(), raw, round(robustness, 6), text))
    analysis_id = repo.start({AXIS: settings.axis_version()}, {"robustness": settings.to_dict()}, "robustness", len(per))
    repo.replace_axis(AXIS, rows, analysis_id)
    repo.normalise(AXIS)
    repo.finish(analysis_id, len(rows))
    return {"combos": len(rows), "partial": partial}


__all__ = ["AXIS", "BASELINE", "DEFAULT_CATALOG", "AdversarialSearch", "RobustnessSettings", "analyse_combo", "build_axis",
           "combo_setup", "run_robustness", "with_handtrap"]
