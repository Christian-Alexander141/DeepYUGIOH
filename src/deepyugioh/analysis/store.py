"""Database 2 - Analyzer Repository (data/analysis.sqlite). Only evaluations, never combos.

Tables of this version (the concept, docs/stufe2-konzept.md section 8, adds
more in later steps - every table is created with IF NOT EXISTS, and the
schema version only changes when an existing table changes):

    analyses      one run of the Analyzer System: analyzers + versions, settings, source, counts
    combo_axes    per combo (signature) and axis: raw facts, value, score 0-100, explanation, version
    combo_profiles  per combo: facts several analyzers share (family, card count, hand)
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from deepyugioh import paths
from deepyugioh.contracts.analysis import AxisScore

SCHEMA_VERSION = 1

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

    def write_profiles(self, rows: Iterable[tuple]):
        """rows: (signature, card_count, hand, family, data)."""
        with self.db:
            self.db.executemany(
                "INSERT INTO combo_profiles (signature, card_count, hand, family, data) VALUES (?,?,?,?,?)"
                " ON CONFLICT(signature) DO UPDATE SET card_count=excluded.card_count, hand=excluded.hand,"
                " family=COALESCE(excluded.family, combo_profiles.family), data=excluded.data",
                [(s, n, json.dumps(h), f, json.dumps(d, sort_keys=True)) for s, n, h, f, d in rows],
            )

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
