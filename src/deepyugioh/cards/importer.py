"""Step 1 + 2: import the card pool into a local database.

Sources (pinned as git submodules):
  * BabelCDB      - card data and texts (cards.cdb, release-*.cdb)
  * CardScripts   - executable Lua card scripts

Besides the raw card data, every script is analysed statically
(see `effects.py`) so later stages can build an interaction graph without
executing the engine.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from deepyugioh import paths
from deepyugioh.cards.effects import parse_script
from deepyugioh.cards.references import parse_refs
from deepyugioh.engine.constants import TYPE_LINK

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS cards (
    code        INTEGER PRIMARY KEY,
    alias       INTEGER NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    ot          INTEGER NOT NULL,
    type        INTEGER NOT NULL,
    level       INTEGER NOT NULL,
    lscale      INTEGER NOT NULL,
    rscale      INTEGER NOT NULL,
    attribute   INTEGER NOT NULL,
    race        INTEGER NOT NULL,
    atk         INTEGER NOT NULL,
    def         INTEGER NOT NULL,
    link_marker INTEGER NOT NULL,
    setcodes    TEXT NOT NULL,      -- JSON list of 16-bit archetype codes
    category    INTEGER NOT NULL,   -- category bits from the cdb (search hints)
    is_alt_art  INTEGER NOT NULL,   -- 1 = alternative artwork of `alias`
    script      TEXT,               -- script path relative to CardScripts, NULL if none
    source_db   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS effects (
    code        INTEGER NOT NULL,
    idx         INTEGER NOT NULL,   -- order of appearance in initial_effect
    var         TEXT NOT NULL,      -- Lua variable name (e1, e2, ...)
    types       TEXT NOT NULL,      -- JSON list, e.g. ["EFFECT_TYPE_IGNITION"]
    event       TEXT NOT NULL,      -- JSON list, e.g. ["EVENT_TO_GRAVE"]
    categories  TEXT NOT NULL,      -- JSON list, e.g. ["CATEGORY_SPECIAL_SUMMON"]
    ranges      TEXT NOT NULL,      -- JSON list, e.g. ["LOCATION_MZONE"]
    properties  TEXT NOT NULL,
    count_limit TEXT,               -- raw SetCountLimit argument list
    PRIMARY KEY (code, idx)
);
CREATE TABLE IF NOT EXISTS card_refs (
    code        INTEGER PRIMARY KEY,
    codes       TEXT NOT NULL,      -- JSON cards the script names
    setcodes    TEXT NOT NULL,      -- JSON archetypes the script names
    max_level   INTEGER,            -- loosest IsLevelBelow / IsLevel bound, NULL = none
    min_level   INTEGER,
    races       INTEGER NOT NULL,   -- bitmask of RACE_* mentioned, 0 = none
    attributes  INTEGER NOT NULL,   -- bitmask of ATTRIBUTE_* mentioned, 0 = none
    tuner       INTEGER NOT NULL,
    type_filters TEXT NOT NULL,     -- JSON card types the script filters for (MONSTER, SPELL, EQUIP ...)
    locations   TEXT NOT NULL,      -- JSON LOCATION_* used outside SetRange
    script_len  INTEGER NOT NULL    -- code lines, a rough complexity measure
);
CREATE INDEX IF NOT EXISTS cards_name ON cards(name);
"""

# Card data files that describe officially released cards.
SOURCE_DBS = ["cards.cdb", "release-*.cdb"]


@dataclass
class ImportStats:
    cards: int = 0
    with_script: int = 0
    effects: int = 0
    alt_arts: int = 0


def _unpack_setcodes(packed: int) -> list[int]:
    return [(packed >> (16 * i)) & 0xFFFF for i in range(4) if (packed >> (16 * i)) & 0xFFFF]


def _is_alt_art(code: int, alias: int) -> bool:
    # BabelCDB convention: alternative artworks keep their id within +-20 of the original.
    return alias != 0 and abs(code - alias) < 20


def _script_index() -> dict[str, Path]:
    index: dict[str, Path] = {}
    for folder in reversed(paths.SCRIPT_DIRS):  # earlier dirs take priority
        if folder.exists():
            for f in folder.glob("c*.lua"):
                index[f.name] = f
    return index


def import_cards(target: Path | None = None) -> ImportStats:
    target = target or paths.card_db()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    out = sqlite3.connect(target)
    out.executescript(SCHEMA)
    scripts = _script_index()
    stats = ImportStats()

    sources = []
    for pattern in SOURCE_DBS:
        sources.extend(sorted(paths.BABEL_CDB.glob(pattern)))
    if not sources:
        raise FileNotFoundError(f"no card databases in {paths.BABEL_CDB} - run git submodule update --init")

    seen: set[int] = set()
    for src in sources:
        db = sqlite3.connect(src)
        rows = db.execute(
            "SELECT d.id, d.ot, d.alias, d.setcode, d.type, d.atk, d.def, d.level, d.race,"
            " d.attribute, d.category, t.name, t.desc FROM datas d JOIN texts t ON d.id = t.id"
        )
        for (code, ot, alias, setcode, ctype, atk, def_, level, race, attr, cat, name, desc) in rows:
            if code in seen:
                continue
            seen.add(code)
            link_marker = 0
            if ctype & TYPE_LINK:
                link_marker, def_ = def_, 0
            script = scripts.get(f"c{code}.lua")
            alt = _is_alt_art(code, alias)
            out.execute(
                "INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    code, alias, name, desc or "", ot, ctype,
                    level & 0xFF, (level >> 24) & 0xFF, (level >> 16) & 0xFF,
                    attr, race, atk, def_, link_marker,
                    json.dumps(_unpack_setcodes(setcode)), cat, int(alt),
                    str(script.relative_to(paths.CARD_SCRIPTS)) if script else None,
                    src.name,
                ),
            )
            stats.cards += 1
            stats.alt_arts += alt
            if script and not alt:
                stats.with_script += 1
                source = script.read_text(encoding="utf-8", errors="replace")
                refs = parse_refs(source, code)
                out.execute(
                    "INSERT INTO card_refs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        code, json.dumps(sorted(refs.codes)), json.dumps(sorted(refs.setcodes)),
                        refs.max_level, refs.min_level, refs.races, refs.attributes, int(refs.tuner),
                        json.dumps(sorted(refs.type_filters)), json.dumps(sorted(refs.locations)),
                        sum(1 for line in source.splitlines() if line.strip() and not line.strip().startswith("--")),
                    ),
                )
                for e in parse_script(source):
                    out.execute(
                        "INSERT INTO effects VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            code, e.idx, e.var, json.dumps(e.types), json.dumps(e.event),
                            json.dumps(e.categories), json.dumps(e.ranges),
                            json.dumps(e.properties), e.count_limit,
                        ),
                    )
                    stats.effects += 1
        db.close()

    out.executemany(
        "INSERT INTO meta VALUES (?,?)",
        [
            ("sources", json.dumps([s.name for s in sources])),
            ("babelcdb_commit", _git_head(paths.BABEL_CDB)),
            ("cardscripts_commit", _git_head(paths.CARD_SCRIPTS)),
        ],
    )
    out.commit()
    out.close()
    return stats


def _git_head(repo: Path) -> str:
    import subprocess

    try:
        return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
