"""Database 2 - Analyzer Repository (data/analysis.sqlite). Only evaluations, never combos.

Tables of this version (the concept, docs/stufe2-konzept.md section 8, adds
more in later steps - every table is created with IF NOT EXISTS, and the
schema version only changes when an existing table changes):

    analyses      one run of the Analyzer System: analyzers + versions, settings, source, counts
    combo_axes    per combo (signature) and axis: raw facts, value, score 0-100, explanation, version
    combo_profiles  per combo: facts several analyzers share (family, card count, hand)
    card_profiles   per card: traits, needs, restrictions, genericness (step 2.3)
    compatibility   edges between combos: shared cards, archetype, lock conflict (step 2.5)
    filter_profiles / filter_results   stored quality filters and their ranked members (step 2.6)
    robustness_jobs   progress of the Robustness Analyzer: one row per combo and handtrap (step 2.8)
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from deepyugioh import paths
from deepyugioh.contracts.analysis import AxisScore

SCHEMA_VERSION = 1   # new tables are added with IF NOT EXISTS; the version changes only for changed tables

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS analyses (
    id          INTEGER PRIMARY KEY,
    started_at  REAL NOT NULL,
    finished_at REAL,
    analyzers   TEXT NOT NULL,     -- JSON {axis: version}
    settings    TEXT NOT NULL,     -- JSON
    source      TEXT NOT NULL,     -- Database 1 path
    combos      INTEGER NOT NULL DEFAULT 0,   -- combos in Database 1
    analysed    INTEGER NOT NULL DEFAULT 0    -- axis results written
);
CREATE TABLE IF NOT EXISTS combo_axes (
    signature   TEXT NOT NULL,
    axis        TEXT NOT NULL,
    version     TEXT NOT NULL,
    raw         TEXT NOT NULL,
    value       REAL NOT NULL,
    score       REAL,              -- percentile 0-100 within (axis, version)
    explanation TEXT NOT NULL,
    analysis_id INTEGER NOT NULL,
    PRIMARY KEY (signature, axis)
);
CREATE INDEX IF NOT EXISTS combo_axes_score ON combo_axes(axis, score);
CREATE TABLE IF NOT EXISTS combo_profiles (
    signature   TEXT PRIMARY KEY,
    card_count  INTEGER NOT NULL,
    hand        TEXT NOT NULL,     -- JSON
    family      TEXT,
    data        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS combo_profiles_family ON combo_profiles(family);
CREATE TABLE IF NOT EXISTS card_profiles (
    code        INTEGER PRIMARY KEY,
    version     TEXT NOT NULL,
    genericness REAL NOT NULL,
    data        TEXT NOT NULL      -- JSON CardProfile
);
CREATE TABLE IF NOT EXISTS compatibility (
    a        TEXT NOT NULL,        -- combo signature; for lock_conflict: the combo whose restriction forbids
    b        TEXT NOT NULL,        -- combo signature; for the undirected kinds a < b
    kind     TEXT NOT NULL,        -- shared_cards | archetype | lock_conflict
    strength REAL NOT NULL,        -- 0-1
    data     TEXT NOT NULL,        -- JSON: shared cards / archetype / the conflicting restrictions
    PRIMARY KEY (a, b, kind)
);
CREATE INDEX IF NOT EXISTS compatibility_b ON compatibility(b, kind);
CREATE TABLE IF NOT EXISTS filter_profiles (
    name       TEXT PRIMARY KEY,
    spec       TEXT NOT NULL,      -- JSON FilterProfile
    updated_at REAL NOT NULL,
    applied_at REAL,
    state      TEXT,               -- state of Database 2 the stored results were computed from
    members    INTEGER NOT NULL DEFAULT 0,
    considered INTEGER NOT NULL DEFAULT 0,
    dropped    TEXT NOT NULL DEFAULT '{}'   -- JSON {reason: combos removed}
);
CREATE TABLE IF NOT EXISTS filter_results (
    profile   TEXT NOT NULL,
    signature TEXT NOT NULL,
    rank      INTEGER NOT NULL,    -- 1 = best
    value     REAL NOT NULL,       -- what the profile ranks by
    fit       REAL,                -- deck fit 0-100 if the profile has a deck profile
    PRIMARY KEY (profile, signature)
);
CREATE INDEX IF NOT EXISTS filter_results_rank ON filter_results(profile, rank);
CREATE TABLE IF NOT EXISTS robustness_jobs (
    signature   TEXT NOT NULL,
    handtrap    TEXT NOT NULL,     -- catalog key, or '-' for the undisturbed baseline
    version     TEXT NOT NULL,
    status      TEXT NOT NULL,     -- complete | incomplete | error
    value       REAL,              -- guaranteed outcome value (minimum over the opponent, maximum over us)
    data        TEXT NOT NULL,     -- JSON: guaranteed end board facts, critical step, nodes, reasons
    elapsed_s   REAL NOT NULL,
    finished_at REAL NOT NULL,
    PRIMARY KEY (signature, handtrap)
);
"""


class SchemaMismatch(RuntimeError):
    pass


class AnalysisRepository:
    def __init__(self, path: Path | None = None):
        self.path = path or paths.DATA / "analysis.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            with self.db:
                self.db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        elif int(row[0]) != SCHEMA_VERSION:
            raise SchemaMismatch(f"{self.path} has schema {row[0]}, expected {SCHEMA_VERSION}")

    # ---- meta -----------------------------------------------------------------------
    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def state(self) -> str:
        """Changes with every finished analysis, rescore or robustness result - stored filter results
        computed from another state are stale."""
        a = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM analyses WHERE finished_at IS NOT NULL").fetchone()[0]
        r = self.db.execute("SELECT COUNT(*), COALESCE(MAX(finished_at), 0) FROM robustness_jobs").fetchone()
        return f"{a}/{r[0]}/{r[1]:.3f}"

    # ---- writing --------------------------------------------------------------------
    def start(self, analyzers: dict[str, str], settings: dict, source: str, combos: int) -> int:
        with self.db:
            cur = self.db.execute("INSERT INTO analyses (started_at, analyzers, settings, source, combos) VALUES (?,?,?,?,?)",
                                  (time.time(), json.dumps(analyzers), json.dumps(settings), source, combos))
        return cur.lastrowid

    def finish(self, analysis_id: int, analysed: int):
        with self.db:
            self.db.execute("UPDATE analyses SET finished_at=?, analysed=? WHERE id=?", (time.time(), analysed, analysis_id))

    def versions(self, axis: str) -> dict[str, str]:
        """signature -> stored version of one axis."""
        return dict(self.db.execute("SELECT signature, version FROM combo_axes WHERE axis=?", (axis,)))

    def write(self, rows: Iterable[tuple], analysis_id: int):
        """rows: (signature, axis, version, raw, value, explanation)."""
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO combo_axes (signature, axis, version, raw, value, score, explanation, analysis_id)"
                " VALUES (?,?,?,?,?,NULL,?,?)",
                [(s, a, v, json.dumps(r, sort_keys=True), val, e, analysis_id) for s, a, v, r, val, e in rows],
            )

    def replace_axis(self, axis: str, rows: Iterable[tuple], analysis_id: int):
        """Replace every result of one (derived) axis; rows as for write()."""
        with self.db:
            self.db.execute("DELETE FROM combo_axes WHERE axis=?", (axis,))
        self.write(rows, analysis_id)

    def write_profiles(self, rows: Iterable[tuple]):
        """rows: (signature, card_count, hand, family, data)."""
        with self.db:
            self.db.executemany(
                "INSERT INTO combo_profiles (signature, card_count, hand, family, data) VALUES (?,?,?,?,?)"
                " ON CONFLICT(signature) DO UPDATE SET card_count=excluded.card_count, hand=excluded.hand,"
                " family=COALESCE(excluded.family, combo_profiles.family), data=excluded.data",
                [(s, n, json.dumps(h), f, json.dumps(d, sort_keys=True)) for s, n, h, f, d in rows],
            )

    def write_card_profiles(self, profiles: Iterable, version: str):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO card_profiles VALUES (?,?,?,?)",
                                [(p.code, version, p.genericness, json.dumps(p.to_dict(), ensure_ascii=False)) for p in profiles])

    def card_profile(self, code: int) -> dict | None:
        row = self.db.execute("SELECT data FROM card_profiles WHERE code=?", (code,)).fetchone()
        return json.loads(row[0]) if row else None

    # ---- compatibility (step 2.5) -----------------------------------------------------
    def replace_compatibility(self, rows: Iterable[tuple]):
        """rows: (a, b, kind, strength, data)."""
        with self.db:
            self.db.execute("DELETE FROM compatibility")
            self.db.executemany("INSERT INTO compatibility VALUES (?,?,?,?,?)",
                                ((a, b, k, s, json.dumps(d, sort_keys=True)) for a, b, k, s, d in rows))

    def edges(self, signature: str, kind: str | None = None, limit: int = 50) -> list[dict]:
        """Stored edges of one combo, strongest first. `other` is the partner; `direction` matters for
        lock_conflict: "out" = this combo forbids the other, "in" = the other forbids this combo."""
        q = ("SELECT a, b, kind, strength, data FROM compatibility WHERE (a=? OR b=?)"
             + (" AND kind=?" if kind else "") + " ORDER BY strength DESC, kind, a, b LIMIT ?")
        args = [signature, signature, *([kind] if kind else []), limit]
        return [{"other": b if a == signature else a, "kind": k, "strength": s, "data": json.loads(d),
                 "direction": "out" if a == signature else "in"} for a, b, k, s, d in self.db.execute(q, args)]

    def edge_stats(self) -> dict:
        return {"edges": dict(self.db.execute("SELECT kind, COUNT(*) FROM compatibility GROUP BY kind"))}

    # ---- filter profiles (step 2.6) ----------------------------------------------------
    def save_filter(self, name: str, spec: dict):
        with self.db:
            self.db.execute("INSERT INTO filter_profiles (name, spec, updated_at) VALUES (?,?,?)"
                            " ON CONFLICT(name) DO UPDATE SET spec=excluded.spec, updated_at=excluded.updated_at,"
                            " state=NULL", (name, json.dumps(spec, sort_keys=True, ensure_ascii=False), time.time()))

    def delete_filter(self, name: str):
        with self.db:
            self.db.execute("DELETE FROM filter_results WHERE profile=?", (name,))
            self.db.execute("DELETE FROM filter_profiles WHERE name=?", (name,))

    def filters(self) -> dict[str, dict]:
        """{name: {spec, members, considered, dropped, applied_at, stale}}."""
        now = self.state()
        return {n: {"spec": json.loads(spec), "members": m, "considered": c, "dropped": json.loads(d),
                    "applied_at": at, "stale": st != now}
                for n, spec, at, st, m, c, d in self.db.execute(
                    "SELECT name, spec, applied_at, state, members, considered, dropped FROM filter_profiles ORDER BY name")}

    def store_filter_results(self, name: str, rows: Iterable[tuple], considered: int, dropped: dict):
        """rows: (signature, rank, value, fit). Replaces the stored members of the profile."""
        rows = list(rows)
        with self.db:
            self.db.execute("DELETE FROM filter_results WHERE profile=?", (name,))
            self.db.executemany("INSERT INTO filter_results VALUES (?,?,?,?,?)", ((name, *r) for r in rows))
            self.db.execute("UPDATE filter_profiles SET applied_at=?, state=?, members=?, considered=?, dropped=?"
                            " WHERE name=?", (time.time(), self.state(), len(rows), considered, json.dumps(dropped), name))

    def filter_results(self, name: str, limit: int | None = None) -> list[tuple[str, int, float, float | None]]:
        q = "SELECT signature, rank, value, fit FROM filter_results WHERE profile=? ORDER BY rank"
        return self.db.execute(q + (" LIMIT ?" if limit else ""), (name, *([limit] if limit else []))).fetchall()

    # ---- robustness jobs (step 2.8) ------------------------------------------------------
    def robustness_done(self, version: str) -> dict[str, set[str]]:
        """signature -> handtraps with a stored result of this version."""
        out: dict[str, set[str]] = {}
        for sig, h in self.db.execute("SELECT signature, handtrap FROM robustness_jobs WHERE version=?", (version,)):
            out.setdefault(sig, set()).add(h)
        return out

    def write_robustness(self, rows: Iterable[tuple]):
        """rows: (signature, handtrap, version, status, value, data, elapsed_s)."""
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO robustness_jobs VALUES (?,?,?,?,?,?,?,?)",
                                [(s, h, v, st, val, json.dumps(d, sort_keys=True), el, time.time())
                                 for s, h, v, st, val, d, el in rows])

    def robustness_rows(self, signature: str | None = None, version: str | None = None):
        """(signature, handtrap, status, value, data)."""
        where, args = [], []
        if signature is not None:
            where.append("signature=?")
            args.append(signature)
        if version is not None:
            where.append("version=?")
            args.append(version)
        q = "SELECT signature, handtrap, status, value, data FROM robustness_jobs"
        for sig, h, st, val, d in self.db.execute(q + (" WHERE " + " AND ".join(where) if where else "")
                                                  + " ORDER BY signature, handtrap", args):
            yield sig, h, st, val, json.loads(d)

    def robustness_stats(self) -> dict:
        rows = self.db.execute("SELECT status, COUNT(*), COUNT(DISTINCT signature), COALESCE(SUM(elapsed_s), 0)"
                               " FROM robustness_jobs GROUP BY status").fetchall()
        return {st: {"jobs": n, "combos": c, "seconds": round(el, 1)} for st, n, c, el in rows}

    def scores(self, axis: str) -> dict[str, float]:
        """signature -> 0-100 score of one axis (without the raw values)."""
        return {s: sc for s, sc in self.db.execute("SELECT signature, score FROM combo_axes WHERE axis=? AND score IS NOT NULL",
                                                   (axis,))}

    def values(self, axis: str) -> dict[str, float]:
        return dict(self.db.execute("SELECT signature, value FROM combo_axes WHERE axis=?", (axis,)))

    def axes(self) -> list[str]:
        return [a for a, in self.db.execute("SELECT DISTINCT axis FROM combo_axes ORDER BY axis")]

    def axis_rows(self, axis: str):
        """(signature, raw, score) of every combo on one axis."""
        for sig, raw, score in self.db.execute("SELECT signature, raw, score FROM combo_axes WHERE axis=?", (axis,)):
            yield sig, json.loads(raw), score

    def normalise(self, axis: str) -> int:
        """Score = percentile of value within (axis, version); ties share the mean rank. Returns rows scored."""
        updates = []
        for version, in self.db.execute("SELECT DISTINCT version FROM combo_axes WHERE axis=?", (axis,)).fetchall():
            rows = self.db.execute("SELECT signature, value FROM combo_axes WHERE axis=? AND version=? ORDER BY value",
                                   (axis, version)).fetchall()
            n = len(rows)
            i = 0
            while i < n:
                j = i
                while j + 1 < n and rows[j + 1][1] == rows[i][1]:
                    j += 1
                # share of combos strictly worse + half of the ties
                score = 100.0 * (i + (j - i + 1) / 2) / n
                updates += [(round(score, 3), axis, rows[k][0]) for k in range(i, j + 1)]
                i = j + 1
        with self.db:
            self.db.executemany("UPDATE combo_axes SET score=? WHERE axis=? AND signature=?", updates)
        return len(updates)

    # ---- AnalysisReader -------------------------------------------------------------------
    def profile(self, signature: str) -> dict[str, AxisScore]:
        out = {}
        for axis, version, raw, value, score, expl in self.db.execute(
                "SELECT axis, version, raw, value, score, explanation FROM combo_axes WHERE signature=?", (signature,)):
            out[axis] = AxisScore(axis, version, json.loads(raw), value, score, expl)
        return out

    def top(self, axis: str, limit: int = 50, min_score: float | None = None) -> list[tuple[str, AxisScore]]:
        q = "SELECT signature, version, raw, value, score, explanation FROM combo_axes WHERE axis=?"
        params: list = [axis]
        if min_score is not None:
            q += " AND score >= ?"
            params.append(min_score)
        q += " ORDER BY value DESC, signature LIMIT ?"
        params.append(limit)
        return [(s, AxisScore(axis, v, json.loads(r), val, sc, e)) for s, v, r, val, sc, e in self.db.execute(q, params)]

    def stats(self) -> dict:
        axes = {}
        for axis, n, version, mn, mx, avg in self.db.execute(
                "SELECT axis, COUNT(*), MAX(version), MIN(value), MAX(value), AVG(value) FROM combo_axes GROUP BY axis"):
            axes[axis] = {"combos": n, "version": version, "min": mn, "max": mx, "mean": round(avg, 4)}
        last = self.db.execute("SELECT id, started_at, finished_at, analysed, combos FROM analyses ORDER BY id DESC LIMIT 1").fetchone()
        families = self.db.execute("SELECT COUNT(DISTINCT family) FROM combo_profiles").fetchone()[0]
        return {"axes": axes, "families": families,
                "last_analysis": None if last is None else {"id": last[0], "analysed": last[3], "combos": last[4],
                                                            "seconds": round((last[2] or last[1]) - last[1], 1)}}

    def close(self):
        self.db.close()
