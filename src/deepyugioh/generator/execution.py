"""Executes one search over one start setup and turns the result into contract records.

This is the unit of work a worker process runs; it has no side effects
(nothing is stored here).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from deepyugioh import __version__
from deepyugioh.contracts.cards import CardPool
from deepyugioh.contracts.combos import ComboRecord, LineStep, RunRecord
from deepyugioh.contracts.setups import Candidate, StartSetup
from deepyugioh.generator.search.dfs import ComboSearch, FoundCombo, SearchLimits
from deepyugioh.generator.search.validate import validate


@dataclass(frozen=True)
class Versions:
    engine_commit: str = "unknown"
    scripts_commit: str = "unknown"
    cdb_commit: str = "unknown"
    graph_version: str | None = None


def combo_signature(combo: FoundCombo) -> str:
    r = combo.requirements
    payload = json.dumps([list(r.hand), list(r.deck), list(r.extra), combo.identity], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def to_record(combo: FoundCombo, setup: StartSetup, validated: bool) -> ComboRecord:
    r = combo.requirements
    return ComboRecord(
        signature=combo_signature(combo),
        hand=r.hand,
        deck=r.deck,
        extra=r.extra,
        benefit=combo.benefit.to_dict(),
        positive=tuple(combo.benefit.positive),
        end_board=combo.end_board.to_dict(),
        turn_ended=combo.turn_ended,
        line=tuple(LineStep(s.decision, s.label, s.response.hex()) for s in combo.line),
        setup=setup,
        validated=validated,
    )


def execute(
    pool: CardPool,
    setup: StartSetup,
    limits: SearchLimits,
    *,
    candidate: Candidate | None = None,
    check: bool = True,
    versions: Versions = Versions(),
) -> tuple[RunRecord, list[ComboRecord]]:
    """Search, validate by replay, and return (run, validated combos)."""
    result = ComboSearch(pool, setup, limits).run()
    records = []
    for combo in result.combos:
        ok = validate(pool, setup, [s.response for s in combo.line], combo.board_key) if check else False
        if ok or not check:
            records.append(to_record(combo, setup, ok))
    incomplete = dict(result.incomplete)
    if candidate is not None:
        for part in candidate.truncated:
            incomplete[f"neighbourhood_truncated:{part}"] = 1
    unvalidated = len(result.combos) - sum(r.validated for r in records)
    if check and unvalidated:
        incomplete["validation_failed"] = unvalidated
    run = RunRecord(
        setup=setup,
        candidate=candidate.id if candidate else None,
        tier=candidate.tier if candidate else None,
        limits=asdict(limits),
        nodes=result.nodes,
        replays=result.replays,
        elapsed_s=result.elapsed,
        complete=not incomplete,
        incomplete=incomplete,
        pruned=dict(result.pruned),
        combos_found=len(result.combos),
        generator_version=__version__,
        engine_commit=versions.engine_commit,
        scripts_commit=versions.scripts_commit,
        cdb_commit=versions.cdb_commit,
        graph_version=versions.graph_version,
    )
    return run, records
