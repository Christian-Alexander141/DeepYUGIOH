"""Line facts (Stufe 2, step 2.5): what a combo's line really does, replayed by the engine.

Database 1 stores the line and the end board, not the events in between. For
compatibility between combos it matters *which* cards a line summons and
activates - a restriction "you can only Special Summon Dragons" conflicts
with a combo exactly if that combo Special Summons something else. The engine
answers that; nothing is guessed from the line labels.

Stored as the axis `line` in Database 2 (raw facts; the value is the number
of own summons and only orders the list).
"""

from __future__ import annotations

from typing import Callable

from deepyugioh.analysis.enginejobs import run_jobs, worker_pool
from deepyugioh.contracts.cards import CardPool
from deepyugioh.contracts.combos import ComboReader, ComboRecord
from deepyugioh.engine import constants as K

AXIS = "line"
VERSION = "1"
_FROM = {K.LOCATION_DECK: "deck", K.LOCATION_HAND: "hand", K.LOCATION_GRAVE: "grave", K.LOCATION_REMOVED: "banished",
         K.LOCATION_EXTRA: "extra", K.LOCATION_MZONE: "field", K.LOCATION_SZONE: "field"}


def line_facts(pool: CardPool, combo: ComboRecord) -> dict:
    """Replay the line and collect player 0's summons and activations. Raises if the line no longer replays."""
    from deepyugioh.engine.duel import Duel
    from deepyugioh.generator.search.actions import Enumerator

    enum = Enumerator(names=pool.name)
    with Duel(pool, combo.setup) as duel:
        for step in combo.line:
            if duel.advance(enum.default_response) is None:
                raise RuntimeError("line ends early in the replay")
            duel.respond(bytes.fromhex(step.response))
        duel.advance(enum.default_response)
        events = list(duel.events)
    origin: dict[tuple, str] = {}     # (code, location, sequence) of a card on the field -> where it came from
    normal, special, activated = [], [], []
    for ev in events:
        if ev.msg == K.MSG_MOVE:
            frm, to = ev.data["from"], ev.data["to"]
            if to.controller == 0 and to.location & K.LOCATION_ONFIELD and not frm.location & K.LOCATION_ONFIELD:
                origin[(ev.data["code"], to.location, to.sequence)] = _FROM.get(frm.location & 0x7F, "other")
        elif ev.msg == K.MSG_SUMMONING and ev.data["loc"].controller == 0:
            normal.append(ev.data["code"])
        elif ev.msg == K.MSG_SPSUMMONING and ev.data["loc"].controller == 0:
            loc = ev.data["loc"]
            special.append([ev.data["code"], origin.get((ev.data["code"], loc.location, loc.sequence), "other")])
        elif ev.msg == K.MSG_CHAINING and ev.data["controller"] == 0:
            activated.append(ev.data["code"])
    return {"normal_summons": sorted(normal), "special_summons": sorted(special), "activations": sorted(activated)}


def _work(combo_id: int):
    from deepyugioh.repository.combo_db import ComboRepository

    repo = ComboRepository()
    try:
        combo = repo.get(combo_id)
        return combo.signature, line_facts(worker_pool(), combo)
    finally:
        repo.close()


def explain(raw: dict, name: Callable[[int], str]) -> str:
    sp = raw["special_summons"]
    parts = [f"{len(sp)} Spezialbeschwörung(en)" + (": " + ", ".join(f"{name(c)} ({o})" for c, o in sp[:6]) if sp else ""),
             f"{len(raw['normal_summons'])} Normalbeschwörung(en)", f"{len(raw['activations'])} Aktivierung(en)"]
    return "; ".join(parts)


def run_line_facts(reader: ComboReader, repo, pool_factory, *, workers: int = 1, full: bool = False,
                   progress: Callable[[int, int], None] | None = None, name: Callable[[int], str] = str) -> dict:
    """Replay every combo without current line facts. Returns {written, errors, skipped}."""
    stored = {} if full else repo.versions(AXIS)
    todo = [c.id for c in reader.iter_combos() if stored.get(c.signature) != VERSION]
    out = {"written": 0, "errors": [], "skipped": reader.count() - len(todo)}
    if not todo:
        return out
    analysis_id = repo.start({AXIS: VERSION}, {"workers": workers}, "line facts", len(todo))
    rows, done = [], 0
    for cid, result, error in run_jobs(_work, todo, workers=workers, pool_factory=pool_factory):
        done += 1
        if error is not None:
            out["errors"].append(f"combo #{cid}: {error}")
        else:
            sig, raw = result
            rows.append((sig, AXIS, VERSION, raw, float(len(raw["special_summons"]) + len(raw["normal_summons"])),
                         explain(raw, name)))
            out["written"] += 1
        if len(rows) >= 2000:
            repo.write(rows, analysis_id)
            rows = []
        if progress and done % 1000 == 0:
            progress(done, len(todo))
    repo.write(rows, analysis_id)
    repo.normalise(AXIS)
    repo.finish(analysis_id, out["written"])
    return out
