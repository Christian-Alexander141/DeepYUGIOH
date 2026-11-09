"""Candidate pool of AIDE: every plan (hypothesis) with status and provenance.

Status model (proposal section 9):

    GENERATED           proposed by a planner
    ABSTRACT_REJECTED   a backward seed the rule checks could not complete (reason stored)
    ABSTRACT_VALID      the plan's steps fit together in the abstract state
    SIMULATION_PENDING  handed to the generator as start setup(s), not searched yet
    VALIDATED           the engine found a validated combo that uses every plan card
    INVALID             every search of its setups was complete, no such combo exists there
    INCONCLUSIVE        a search hit a limit before it could tell
    UNSUPPORTED         a search failed (e.g. a script error)

Only the engine moves a hypothesis to VALIDATED, INVALID, INCONCLUSIVE or
UNSUPPORTED - and only validated combos are ever stored in Database 1 (by the
generator, exactly as for every other candidate). The pool lives in its own
database (data/aide.sqlite); Database 1 is not touched. A negative result
holds for that start setup and those limits only.

Plans are also cached per (source version, planner, root): a resumed run
does not have to plan again what it already planned.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Callable, Iterable

from deepyugioh import paths
from deepyugioh.generator.aide.planner import Plan, Step

STATUSES = ("GENERATED", "ABSTRACT_REJECTED", "ABSTRACT_VALID", "SIMULATION_PENDING", "VALIDATED", "INVALID",
            "INCONCLUSIVE", "UNSUPPORTED")
FINAL = {"VALIDATED", "INVALID", "UNSUPPORTED"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS hypotheses (
    id          TEXT NOT NULL,
    version     TEXT NOT NULL,        -- AIDE source version (signatures + graph + planner settings)
    planner     TEXT NOT NULL,        -- forward | backward
    root        INTEGER NOT NULL,     -- start card (forward) or goal card (backward)
    ord         INTEGER NOT NULL,     -- order within the root
    cards       TEXT NOT NULL,        -- JSON [[code, start zone], ...]
    plan        TEXT NOT NULL,        -- JSON Plan
    links       INTEGER NOT NULL,
    status      TEXT NOT NULL,
    reason      TEXT,
    setups      TEXT NOT NULL DEFAULT '[]',   -- JSON setup ids handed to the generator
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (version, id)
);
CREATE INDEX IF NOT EXISTS hypotheses_status ON hypotheses(version, status);
CREATE TABLE IF NOT EXISTS planned_roots (
    version TEXT NOT NULL,
    planner TEXT NOT NULL,
    root    INTEGER NOT NULL,
    plans   INTEGER NOT NULL,
    PRIMARY KEY (version, planner, root)
);
CREATE TABLE IF NOT EXISTS rejected (
    version TEXT NOT NULL,
    planner TEXT NOT NULL,
    root    INTEGER NOT NULL,
    detail  TEXT NOT NULL            -- JSON: goal, producer, reason
);
"""


def plan_from_dict(d: dict) -> Plan:
    steps = tuple(Step(s["code"], s["action"], tuple(tuple(m) for m in s["moved"]), tuple(tuple(x) for x in s["links"]))
                  for s in d["steps"])
    return Plan(d["planner"], d["root"], tuple(tuple(x) for x in d["start"]), steps, d.get("specific_links", 0))


class HypothesisStore:
    def __init__(self, path: Path | None = None):
        self.path = path or paths.DATA / "aide.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)

    # ---- plan cache ---------------------------------------------------------------------
    def cached(self, version: str, planner: str, root: int) -> list[Plan] | None:
        row = self.db.execute("SELECT plans FROM planned_roots WHERE version=? AND planner=? AND root=?",
                              (version, planner, root)).fetchone()
        if row is None:
            return None
        rows = self.db.execute("SELECT plan FROM hypotheses WHERE version=? AND planner=? AND root=? ORDER BY ord",
                               (version, planner, root)).fetchall()
        return [plan_from_dict(json.loads(p)) for (p,) in rows]

    def store_plans(self, version: str, planner: str, root: int, plans: list[Plan], rejected: Iterable[dict] = ()):
        now = time.time()
        with self.db:
            for i, p in enumerate(plans):
                self.db.execute(
                    "INSERT OR IGNORE INTO hypotheses (id, version, planner, root, ord, cards, plan, links, status,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (p.id, version, planner, root, i, json.dumps([list(x) for x in p.start]), json.dumps(p.to_dict()),
                     p.links, "ABSTRACT_VALID", now, now),
                )
            for r in rejected:
                self.db.execute("INSERT INTO rejected VALUES (?,?,?,?)", (version, planner, root, json.dumps(r)))
            self.db.execute("INSERT OR REPLACE INTO planned_roots VALUES (?,?,?,?)", (version, planner, root, len(plans)))

    def handed_over(self, version: str, plan_id: str, setup_ids: list[str]):
        """The plan became start setups for the generator."""
        with self.db:
            row = self.db.execute("SELECT status, setups FROM hypotheses WHERE version=? AND id=?", (version, plan_id)).fetchone()
            if row is None:
                return
            status, setups = row
            known = json.loads(setups)
            merged = known + [s for s in setup_ids if s not in known]
            if status == "ABSTRACT_VALID" or merged != known:
                self.db.execute("UPDATE hypotheses SET setups=?, status=CASE WHEN status='ABSTRACT_VALID'"
                                " THEN 'SIMULATION_PENDING' ELSE status END, updated_at=? WHERE version=? AND id=?",
                                (json.dumps(merged), time.time(), version, plan_id))

    # ---- results ------------------------------------------------------------------------
    def update_status(self, jobs: Callable[[list[str]], dict[str, tuple[str, int | None]]],
                      combos: Callable[[list[int]], dict[int, list[tuple[int, ...]]]], version: str | None = None) -> Counter:
        """Derive the engine status of every handed-over hypothesis from the generator's results.

        jobs(setup_ids)  -> {setup id: (job status, run id)}   (JobStore.jobs_for)
        combos(run_ids)  -> {run id: [combo card tuples]}       (ComboRepository.cards_for_runs)
        """
        where = "WHERE status NOT IN ('ABSTRACT_REJECTED', 'GENERATED', 'ABSTRACT_VALID')"
        params: tuple = ()
        if version is not None:
            where += " AND version=?"
            params = (version,)
        rows = self.db.execute(f"SELECT version, id, cards, setups, status FROM hypotheses {where}", params).fetchall()
        updates = []
        for ver, hid, cards, setups, old in rows:
            setup_ids = json.loads(setups)
            done = jobs(setup_ids)
            if not done:
                continue
            need = Counter(c for c, _ in json.loads(cards))
            run_ids = [r for _, r in done.values() if r is not None]
            found = combos(run_ids) if run_ids else {}
            validated = any(not (need - Counter(cs)) for lst in found.values() for cs in lst)
            statuses = [s for s, _ in done.values()]
            if validated:
                new, reason = "VALIDATED", None
            elif "error" in statuses:
                new, reason = "UNSUPPORTED", "a search of its setup failed"
            elif len(done) < len(setup_ids):
                new, reason = "SIMULATION_PENDING", None
            elif all(s == "complete" for s in statuses):
                new, reason = "INVALID", "complete search, no combo with all plan cards"
            else:
                new, reason = "INCONCLUSIVE", "a search hit a limit"
            if new != old:
                updates.append((new, reason, time.time(), ver, hid))
        with self.db:
            self.db.executemany("UPDATE hypotheses SET status=?, reason=?, updated_at=? WHERE version=? AND id=?", updates)
        return self.counts(version)

    def counts(self, version: str | None = None) -> Counter:
        q = "SELECT status, COUNT(*) FROM hypotheses" + (" WHERE version=?" if version else "") + " GROUP BY status"
        out = Counter(dict(self.db.execute(q, (version,) if version else ())))
        rq = "SELECT COUNT(*) FROM rejected" + (" WHERE version=?" if version else "")
        out["ABSTRACT_REJECTED"] += self.db.execute(rq, (version,) if version else ()).fetchone()[0]
        return out

    def list(self, status: str | None = None, limit: int = 50, version: str | None = None) -> list[dict]:
        where, params = [], []
        if status:
            where.append("status=?")
            params.append(status)
        if version:
            where.append("version=?")
            params.append(version)
        sql = "SELECT id, planner, root, cards, links, status, reason, setups FROM hypotheses"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, id LIMIT ?"
        rows = self.db.execute(sql, (*params, limit)).fetchall()
        return [{"id": r[0], "planner": r[1], "root": r[2], "cards": json.loads(r[3]), "links": r[4], "status": r[5],
                 "reason": r[6], "setups": json.loads(r[7])} for r in rows]

    def close(self):
        self.db.close()
