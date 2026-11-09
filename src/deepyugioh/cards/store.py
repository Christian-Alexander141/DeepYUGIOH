"""Read access to the imported card database."""

from __future__ import annotations

import json
import sqlite3
from functools import cached_property
from pathlib import Path

from deepyugioh import paths
from deepyugioh.contracts.cards import CardInfo


Card = CardInfo  # backwards compatible name


class CardStore:
    """CardPool implementation backed by data/cards.sqlite."""

    def __init__(self, path: Path | None = None):
        path = path or paths.card_db()
        if not path.exists():
            raise FileNotFoundError(f"{path} not found - run `deepyugioh import-cards` first")
        self._db = sqlite3.connect(path)

    @cached_property
    def cards(self) -> dict[int, CardInfo]:
        out = {}
        for r in self._db.execute(
            "SELECT code, alias, name, type, level, lscale, rscale, attribute, race, atk, def,"
            " link_marker, setcodes, is_alt_art, script FROM cards"
        ):
            out[r[0]] = CardInfo(*r[:12], tuple(json.loads(r[12])), bool(r[13]), r[14])
        return out

    def get(self, code: int) -> CardInfo | None:
        return self.cards.get(code)

    def by_name(self, name: str) -> CardInfo:
        row = self._db.execute(
            "SELECT code FROM cards WHERE name = ? COLLATE NOCASE AND is_alt_art = 0 ORDER BY code LIMIT 1",
            (name,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown card: {name!r}")
        return self.cards[row[0]]

    def resolve(self, ref: str | int) -> CardInfo:
        """Accept a card code or an exact (case-insensitive) English name."""
        if isinstance(ref, int) or str(ref).isdigit():
            card = self.get(int(ref))
            if card is None:
                raise KeyError(f"unknown card code: {ref}")
            return card
        return self.by_name(str(ref))

    def search(self, text: str, limit: int = 50) -> list[CardInfo]:
        """Cards whose name contains `text` (case-insensitive), exact matches first."""
        text = text.strip()
        if not text:
            return []
        pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self._db.execute(
            "SELECT code FROM cards WHERE is_alt_art = 0 AND name LIKE ? ESCAPE '\\'"
            " ORDER BY name <> ? COLLATE NOCASE, length(name), name LIMIT ?",
            (pattern, text, limit),
        ).fetchall()
        return [self.cards[r[0]] for r in rows]

    def name(self, code: int) -> str:
        card = self.get(code)
        return card.name if card else str(code)
