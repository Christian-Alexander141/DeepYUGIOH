"""Generator state: which candidates have been searched (resumable global runs).

Owned by the generator, separate from Database 1. For every tier the store
keeps a *cursor*: all candidates before it in the source's fixed order have
been processed. Jobs finished beyond the cursor (parallel workers finish out
of order) are recorded individually and skipped on resume.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from deepyugioh import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    candidate    TEXT PRIMARY KEY,
    tier         INTEGER NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('complete', 'incomplete', 'error')),
    run_id       INTEGER,            -- runs.id in Database 1
    combos_found INTEGER NOT NULL,
    combos_new   INTEGER NOT NULL,
    elapsed_s    REAL NOT NULL,
    error        TEXT,
    finished_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_tier ON jobs(tier, status);
CREATE TABLE IF NOT EXISTS cursors (
    tier     INTEGER NOT NULL,
    source   TEXT NOT NULL,          -- candidate source version; order is only stable per version
    position INTEGER NOT NULL,
    PRIMARY KEY (tier, source)
);
"""


@dataclass(frozen=True)
class JobReport:
    candidate: str
    tier: int
    position: int
    status: str            # complete | incomplete | error
    combos_found: int
    combos_new: int
    elapsed_s: float
    run_id: int | None = None
    error: str | None = None
    target_hits: int = 0   # new combos that count towards the run target (see ComboGenerator.run)


class JobStore:
    def __init__(self, path: Path | None = None):
        path = path or paths.DATA / "generator.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def is_done(self, candidate: str) -> bool:
        return self.db.execute("SELECT 1 FROM jobs WHERE candidate = ?", (candidate,)).fetchone() is not None

    def record(self, job: JobReport) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
            (
                job.candidate, job.tier, job.status, job.run_id, job.combos_found, job.combos_new,
                job.elapsed_s, job.error, datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        self.db.commit()

    def cursor(self, tier: int, source: str) -> int:
        row = self.db.execute("SELECT position FROM cursors WHERE tier = ? AND source = ?", (tier, source)).fetchone()
        return row[0] if row else 0

    def set_cursor(self, tier: int, source: str, position: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?)", (tier, source, position))
        self.db.commit()

    def coverage(self) -> dict[int, dict[str, int]]:
        out: dict[int, dict[str, int]] = {}
        for tier, status, n, combos, new in self.db.execute(
            "SELECT tier, status, COUNT(*), SUM(combos_found), SUM(combos_new) FROM jobs GROUP BY tier, status"
        ):
            t = out.setdefault(tier, {"complete": 0, "incomplete": 0, "error": 0, "combos_found": 0, "combos_new": 0})
            t[status] = n
            t["combos_found"] += combos or 0
            t["combos_new"] += new or 0
        return out

    def errors(self, limit: int = 20) -> list[tuple[str, str]]:
        return self.db.execute("SELECT candidate, error FROM jobs WHERE status = 'error' LIMIT ?", (limit,)).fetchall()

    def close(self):
        self.db.close()
