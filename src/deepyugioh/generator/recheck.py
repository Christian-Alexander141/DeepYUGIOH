"""Recheck Database 1: replay every stored combo and determine its requirements again.

Needed when the rule for required cards is corrected (see docs/stufe2-konzept.md, "Befund aus
Phase 1"): a stored combo keeps its line and end board, but which cards it needs - and with that
its identity - may change. The engine decides: every combo is replayed, nothing is derived from
the stored text.

    unchanged   requirements and signature are reproduced exactly
    changed     requirements differ: with `apply` the record is corrected (or merged into the combo
                that already has the corrected identity)
    drift       same requirements, other signature - reported, never changed
    error       the line no longer replays (card scripts changed) - reported, never changed
"""

from __future__ import annotations

from typing import Callable

from deepyugioh.analysis.enginejobs import run_jobs, worker_pool, worker_state
from deepyugioh.contracts.combos import ComboReader

ZONES = ("hand", "deck", "extra", "grave", "banished", "field")


def _work(combo_id: int):
    from deepyugioh.generator.execution import recompute
    from deepyugioh.repository.combo_db import ComboRepository

    state = worker_state()
    if "repo" not in state:
        state["repo"] = ComboRepository()
    combo = state["repo"].get(combo_id)
    req, signature = recompute(worker_pool(), combo)
    old = {z: list(getattr(combo, z)) for z in ZONES}
    new = {z: list(getattr(req, z)) for z in ZONES}
    return combo.signature, signature, old, new


def recheck(reader: ComboReader, pool_factory, *, workers: int = 1, apply: bool = False, repo=None,
            progress: Callable[[int, int], None] | None = None) -> dict:
    """Returns {checked, unchanged, changed: [...], drift: [...], errors: [...], updated, merged}."""
    ids = [c.id for c in reader.iter_combos()]
    out = {"checked": 0, "unchanged": 0, "changed": [], "drift": [], "errors": [], "updated": 0, "merged": 0}
    todo = []
    for cid, result, error in run_jobs(_work, ids, workers=workers, pool_factory=pool_factory):
        out["checked"] += 1
        if error is not None:
            out["errors"].append(f"combo #{cid}: {error}")
        else:
            old_sig, new_sig, old, new = result
            if old == new and old_sig == new_sig:
                out["unchanged"] += 1
            elif old == new:
                out["drift"].append(cid)
            else:
                out["changed"].append({"id": cid, "old": old, "new": new, "signature": new_sig})
                todo.append(cid)
        if progress and out["checked"] % 2000 == 0:
            progress(out["checked"], len(ids))
    if apply and todo:
        if repo is None:
            raise ValueError("apply needs the repository to write to")
        from deepyugioh.generator.search.combo import Requirements

        for change in sorted(out["changed"], key=lambda c: c["id"]):
            req = Requirements(**{z: tuple(change["new"][z]) for z in ZONES})
            result = repo.correct_requirements(change["id"], req, change["signature"])
            change["result"] = result
            out[result] += 1
    return out
