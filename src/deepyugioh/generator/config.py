"""Generator configuration - part of the module's public interface.

Can be built in code or loaded from a TOML file (see deepyugioh.example.toml):

    [limits]
    max_cards = 5
    time_limit = 120

    [candidates]
    tiers = [1, 2]
    opponent_boards = ["empty", "standard"]

    [opponent_boards.standard]
    cards = [
      { card = "Gene-Warped Warwolf", zone = "mzone", position = "faceup_attack" },
    ]
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from deepyugioh.contracts.cards import CardPool
from deepyugioh.contracts.setups import OpponentBoard, OpponentCard
from deepyugioh.generator.search.dfs import SearchLimits

TIERS = {
    1: "every starter card on its own (plus the helpers it needs)",
    2: "NAMED / RESTRICTED interactions (card or archetype named)",
    3: "CONSTRAINED interactions (generic with level/type/attribute filter)",
    4: "GENERIC interactions (targets anything)",
}

# Opponent boards are made of vanilla monsters and Normal Spells: they give our
# effects something to target but can never be activated during our turn.
DEFAULT_OPPONENT_BOARDS: dict[str, list[dict]] = {
    "empty": [],
    "standard": [
        {"card": "Gene-Warped Warwolf", "zone": "mzone", "position": "faceup_attack"},
        {"card": "Pot of Greed", "zone": "szone", "position": "facedown"},
        {"card": "Mystical Elf", "zone": "hand"},
        {"card": "Mystical Elf", "zone": "hand"},
    ],
}


@dataclass(frozen=True)
class CandidateConfig:
    tiers: tuple[int, ...] = (1, 2, 3, 4)
    opponent_boards: tuple[str, ...] = ("empty", "standard")
    max_deck_neighbors: int = 15   # cards added to the deck from the interaction neighbourhood
    max_extra: int = 15            # extra deck size (as in the real game)
    enablers: bool = True          # add generic helpers (e.g. Foolish Burial) when cards need them


@dataclass(frozen=True)
class GeneratorConfig:
    limits: SearchLimits = SearchLimits(time_limit=120.0, max_nodes=20_000)
    candidates: CandidateConfig = CandidateConfig()
    opponent_boards: dict[str, list[dict]] = field(default_factory=lambda: dict(DEFAULT_OPPONENT_BOARDS))
    workers: int = 1
    validate: bool = True

    def boards(self, pool: CardPool) -> dict[str, OpponentBoard]:
        out = {}
        for name, cards in self.opponent_boards.items():
            out[name] = OpponentBoard(
                name,
                tuple(
                    OpponentCard(pool.resolve(c["card"]).code, c.get("zone", "mzone"), c.get("position", "faceup_attack"))
                    for c in cards
                ),
            )
        return out

    def to_dict(self) -> dict:
        return {
            "limits": asdict(self.limits),
            "candidates": asdict(self.candidates),
            "opponent_boards": self.opponent_boards,
            "workers": self.workers,
            "validate": self.validate,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GeneratorConfig":
        base = cls()
        limits = _merge(base.limits, d.get("limits", {}))
        cand = d.get("candidates", {})
        cand = {k: tuple(v) if isinstance(v, list) else v for k, v in cand.items()}
        candidates = _merge(base.candidates, cand)
        boards = dict(base.opponent_boards)
        for name, spec in d.get("opponent_boards", {}).items():
            boards[name] = spec["cards"] if isinstance(spec, dict) else spec
        return cls(limits, candidates, boards, d.get("workers", base.workers), d.get("validate", base.validate))

    @classmethod
    def load(cls, path: Path) -> "GeneratorConfig":
        with open(path, "rb") as f:
            return cls.from_dict(tomllib.load(f))

    def with_(self, **changes) -> "GeneratorConfig":
        return replace(self, **changes)

    def check(self) -> "GeneratorConfig":
        """Raise ValueError for inconsistent values; returns self for chaining."""
        lim, cand = self.limits, self.candidates
        errors = []
        if lim.min_cards < 1:
            errors.append("limits.min_cards must be >= 1")
        if lim.max_cards < lim.min_cards:
            errors.append("limits.max_cards must be >= limits.min_cards")
        for name in ("max_depth", "max_nodes", "max_choices"):
            if getattr(lim, name) < 1:
                errors.append(f"limits.{name} must be >= 1")
        if lim.time_limit <= 0:
            errors.append("limits.time_limit must be > 0")
        if not cand.tiers:
            errors.append("candidates.tiers must not be empty")
        unknown_tiers = set(cand.tiers) - set(TIERS)
        if unknown_tiers:
            errors.append(f"unknown tiers: {sorted(unknown_tiers)}")
        if not cand.opponent_boards:
            errors.append("candidates.opponent_boards must not be empty")
        missing = set(cand.opponent_boards) - set(self.opponent_boards)
        if missing:
            errors.append(f"unknown opponent boards: {sorted(missing)}")
        if cand.max_deck_neighbors < 0 or cand.max_extra < 0:
            errors.append("candidates.max_deck_neighbors and max_extra must be >= 0")
        if self.workers < 1:
            errors.append("workers must be >= 1")
        if errors:
            raise ValueError("; ".join(errors))
        return self


def _merge(obj, values: dict):
    known = {f.name for f in fields(obj)}
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"unknown {type(obj).__name__} options: {sorted(unknown)}")
    return replace(obj, **values)
