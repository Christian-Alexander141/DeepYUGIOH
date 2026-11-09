"""Database 1 - Combo Repository (SQLite).

Implements `ComboSink` (written by the Combo Generator) and `ComboReader`
(read by the Analyzer System). Stores *what is possible*: validated combos,
their requirements, lines and end boards - never ratings.

Identity of a combo (`signature`) = required cards + end board relative to the
start (contracts.combos.ComboRecord). Re-discovering a known combo only links
the new run to it (`combo_runs`), so every setup a combo works in is kept.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from deepyugioh import paths
from deepyugioh.contracts.combos import SCHEMA_VERSION, ComboRecord, LineStep, RunRecord, StoreResult
from deepyugioh.contracts.setups import StartSetup

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY,
    finished_at       TEXT NOT NULL,
    candidate         TEXT,             -- candidate id (generator), NULL for manual searches
    tier              INTEGER,
    setup             TEXT NOT NULL,    -- JSON StartSetup
    opponent_board    TEXT NOT NULL,
    limits            TEXT NOT NULL,    -- JSON SearchLimits
    nodes             INTEGER NOT NULL,
    replays           INTEGER NOT NULL,
    elapsed_s         REAL NOT NULL,
    complete          INTEGER NOT NULL, -- 1 = whole space inside the limits explored
    incomplete        TEXT NOT NULL,    -- JSON {reason: count}
    pruned            TEXT NOT NULL,    -- JSON {rule: count}
    combos_found      INTEGER NOT NULL,
    combos_new        INTEGER NOT NULL,
    generator_version TEXT NOT NULL,
    engine_commit     TEXT NOT NULL,
    scripts_commit    TEXT NOT NULL,
    cdb_commit        TEXT NOT NULL,
    graph_version     TEXT
);
CREATE TABLE IF NOT EXISTS combos (
    id             INTEGER PRIMARY KEY,
    signature      TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL,
    card_count     INTEGER NOT NULL,
    cards          TEXT NOT NULL,   -- JSON sorted codes of all required cards
    hand           TEXT NOT NULL,   -- JSON
    deck           TEXT NOT NULL,   -- JSON
    extra          TEXT NOT NULL,   -- JSON
    benefit        TEXT NOT NULL,   -- JSON raw deltas
    positive       TEXT NOT NULL,   -- JSON list
    end_board      TEXT NOT NULL,   -- JSON
    turn_ended     INTEGER NOT NULL,
    action_count   INTEGER NOT NULL,
    line           TEXT NOT NULL,   -- JSON [{decision, label, response}]
    setup          TEXT NOT NULL,   -- JSON StartSetup the line replays in
    validated      INTEGER NOT NULL,
    first_run      INTEGER NOT NULL REFERENCES runs(id),
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS combos_size ON combos(card_count, action_count);
CREATE TABLE IF NOT EXISTS combo_cards (
    combo_id INTEGER NOT NULL REFERENCES combos(id),
    code     INTEGER NOT NULL,
    role     TEXT NOT NULL CHECK (role IN ('hand', 'deck', 'extra')),
    copies   INTEGER NOT NULL,
    PRIMARY KEY (combo_id, code, role)
);
CREATE INDEX IF NOT EXISTS combo_cards_code ON combo_cards(code);
CREATE TABLE IF NOT EXISTS combo_benefits (
    combo_id  INTEGER NOT NULL REFERENCES combos(id),
    criterion TEXT NOT NULL,
    PRIMARY KEY (combo_id, criterion)
);
CREATE INDEX IF NOT EXISTS combo_benefits_criterion ON combo_benefits(criterion);
CREATE TABLE IF NOT EXISTS combo_runs (
    combo_id INTEGER NOT NULL REFERENCES combos(id),
    run_id   INTEGER NOT NULL REFERENCES runs(id),
    PRIMARY KEY (combo_id, run_id)
);
"""


class SchemaMismatch(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ComboRepository:
    """ComboSink + ComboReader implementation."""

    def __init__(self, path: Path | None = None):
        path = path or paths.combo_db()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            has_rows = self.db.execute("SELECT COUNT(*) FROM combos").fetchone()[0]
            if has_rows:
                raise SchemaMismatch(f"{path} has no schema version - created by an older generator")
            self.db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self.db.commit()
        elif int(row[0]) != SCHEMA_VERSION:
            raise SchemaMismatch(f"{path} uses schema {row[0]}, this code writes schema {SCHEMA_VERSION}")

    # ---- ComboSink --------------------------------------------------------
    def store_run(self, run: RunRecord, combos: Sequence[ComboRecord]) -> StoreResult:
        with self.db:  # one transaction per run
            cur = self.db.execute(
                "INSERT INTO runs (finished_at, candidate, tier, setup, opponent_board, limits, nodes, replays,"
                " elapsed_s, complete, incomplete, pruned, combos_found, combos_new, generator_version,"
                " engine_commit, scripts_commit, cdb_commit, graph_version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)",
                (
                    _now(), run.candidate, run.tier, json.dumps(run.setup.to_dict()), run.setup.opponent.name,
                    json.dumps(run.limits), run.nodes, run.replays, run.elapsed_s, int(run.complete),
                    json.dumps(run.incomplete), json.dumps(run.pruned), run.combos_found, run.generator_version,
                    run.engine_commit, run.scripts_commit, run.cdb_commit, run.graph_version,
                ),
            )
            run_id = cur.lastrowid
            new = 0
            for c in combos:
                if not c.validated:
                    continue  # Database 1 only holds combos the engine confirmed
                row = self.db.execute("SELECT id FROM combos WHERE signature = ?", (c.signature,)).fetchone()
                if row is None:
                    cid = self._insert(c, run_id)
                    new += 1
                else:
                    cid = row[0]
                self.db.execute("INSERT OR IGNORE INTO combo_runs VALUES (?,?)", (cid, run_id))
            self.db.execute("UPDATE runs SET combos_new = ? WHERE id = ?", (new, run_id))
        return StoreResult(run_id, new)

    def _insert(self, c: ComboRecord, run_id: int) -> int:
        cid = self.db.execute(
            "INSERT INTO combos (signature, schema_version, card_count, cards, hand, deck, extra, benefit, positive,"
            " end_board, turn_ended, action_count, line, setup, validated, first_run, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                c.signature, c.schema_version, c.card_count, json.dumps(c.cards), json.dumps(c.hand),
                json.dumps(c.deck), json.dumps(c.extra), json.dumps(c.benefit), json.dumps(c.positive),
                json.dumps(c.end_board), int(c.turn_ended), len(c.line),
                json.dumps([s.to_dict() for s in c.line]), json.dumps(c.setup.to_dict()), int(c.validated),
                run_id, _now(),
            ),
        ).lastrowid
        for role, codes in (("hand", c.hand), ("deck", c.deck), ("extra", c.extra)):
            for code in sorted(set(codes)):
                self.db.execute("INSERT INTO combo_cards VALUES (?,?,?,?)", (cid, code, role, codes.count(code)))
        for crit in c.positive:
            self.db.execute("INSERT INTO combo_benefits VALUES (?,?)", (cid, crit))
        return cid

    # ---- ComboReader ------------------------------------------------------
    _COLS = "id, signature, schema_version, hand, deck, extra, benefit, positive, end_board, turn_ended, line, setup, validated"

    def _record(self, row) -> ComboRecord:
        (cid, sig, ver, hand, deck, extra, benefit, positive, board, ended, line, setup, validated) = row
        return ComboRecord(
            signature=sig, hand=tuple(json.loads(hand)), deck=tuple(json.loads(deck)), extra=tuple(json.loads(extra)),
            benefit=json.loads(benefit), positive=tuple(json.loads(positive)), end_board=json.loads(board),
            turn_ended=bool(ended), line=tuple(LineStep(**s) for s in json.loads(line)),
            setup=StartSetup.from_dict(json.loads(setup)), validated=bool(validated), schema_version=ver, id=cid,
        )

    def get(self, combo_id: int) -> ComboRecord | None:
        row = self.db.execute(f"SELECT {self._COLS} FROM combos WHERE id = ?", (combo_id,)).fetchone()
        return self._record(row) if row else None

    def iter_combos(self, *, card=None, max_cards=None, positive=None, limit=None) -> Iterator[ComboRecord]:
        where, args = [], []
        if card is not None:
            where.append("id IN (SELECT combo_id FROM combo_cards WHERE code = ?)")
            args.append(card)
        if max_cards is not None:
            where.append("card_count <= ?")
            args.append(max_cards)
        if positive is not None:
            where.append("id IN (SELECT combo_id FROM combo_benefits WHERE criterion = ?)")
            args.append(positive)
        sql = f"SELECT {self._COLS} FROM combos"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY card_count, action_count, id"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        for row in self.db.execute(sql, args).fetchall():
            yield self._record(row)

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM combos").fetchone()[0]

    def runs_for(self, combo_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT r.id, r.candidate, r.tier, r.opponent_board, r.complete FROM runs r"
            " JOIN combo_runs cr ON cr.run_id = r.id WHERE cr.combo_id = ? ORDER BY r.id",
            (combo_id,),
        ).fetchall()
        return [dict(zip(("run_id", "candidate", "tier", "opponent_board", "complete"), r)) for r in rows]

    # ---- inspection (test GUI, diagnostics) - not part of ComboReader ------
    _RUN_COLS = ("id", "finished_at", "candidate", "tier", "setup", "opponent_board", "limits", "nodes", "replays",
                 "elapsed_s", "complete", "incomplete", "pruned", "combos_found", "combos_new")

    def recent_runs(self, limit: int = 200, *, incomplete_only: bool = False, tier: int | None = None) -> list[dict]:
        """Latest runs first, JSON columns decoded."""
        where, args = [], []
        if incomplete_only:
            where.append("complete = 0")
        if tier is not None:
            where.append("tier = ?")
            args.append(tier)
        sql = f"SELECT {', '.join(self._RUN_COLS)} FROM runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        out = []
        for row in self.db.execute(sql, (*args, limit)).fetchall():
            r = dict(zip(self._RUN_COLS, row))
            for k in ("setup", "limits", "incomplete", "pruned"):
                r[k] = json.loads(r[k])
            r["complete"] = bool(r["complete"])
            out.append(r)
        return out

    def stats(self) -> dict:
        """Totals for tuning the generator: what was found and why searches stopped early."""
        q = lambda sql: self.db.execute(sql).fetchall()
        runs, complete, elapsed, nodes = q(
            "SELECT COUNT(*), COALESCE(SUM(complete), 0), COALESCE(AVG(elapsed_s), 0), COALESCE(AVG(nodes), 0) FROM runs"
        )[0]
        return {
            "combos": self.count(),
            "runs": runs,
            "runs_complete": complete,
            "avg_elapsed_s": elapsed,
            "avg_nodes": nodes,
            "by_card_count": dict(q("SELECT card_count, COUNT(*) FROM combos GROUP BY card_count ORDER BY card_count")),
            "by_benefit": dict(q("SELECT criterion, COUNT(*) FROM combo_benefits GROUP BY criterion ORDER BY 2 DESC")),
            # reason -> (runs affected, total count)
            "incomplete": {k: (n, s) for k, n, s in q(
                "SELECT j.key, COUNT(*), SUM(j.value) FROM runs, json_each(runs.incomplete) j GROUP BY j.key ORDER BY 2 DESC"
            )},
            "pruned": {k: (n, s) for k, n, s in q(
                "SELECT j.key, COUNT(*), SUM(j.value) FROM runs, json_each(runs.pruned) j GROUP BY j.key ORDER BY 3 DESC"
            )},
        }

    def close(self):
        self.db.close()
