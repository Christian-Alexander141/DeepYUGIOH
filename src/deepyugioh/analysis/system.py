"""The Analyzer System: runs analyzers over Database 1 and stores the results in Database 2.

Incremental and versioned: a combo is analysed again on an axis only if it
has no result yet or its result was produced by another analyzer version.
Analyzers whose result depends on other combos (`global_ = True`, e.g. the
family-based consistency) are recomputed for every combo on every run.
After writing, every axis is normalised to a 0-100 percentile score.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable

from deepyugioh.analysis.store import AnalysisRepository
from deepyugioh.contracts.combos import ComboReader, ComboRecord


@dataclass
class AnalysisReport:
    combos: int = 0
    written: dict[str, int] = field(default_factory=dict)   # axis -> results written
    skipped: dict[str, int] = field(default_factory=dict)   # axis -> up to date
    errors: list[str] = field(default_factory=list)
    analysis_id: int | None = None


class AnalyzerSystem:
    def __init__(self, reader: ComboReader, repo: AnalysisRepository, analyzers: list, *, settings: dict | None = None,
                 source: str = ""):
        names = [a.name for a in analyzers]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate axes: {names}")
        self.reader = reader
        self.repo = repo
        self.analyzers = analyzers
        self.settings = settings or {}
        self.source = source

    def run(self, *, full: bool = False, progress: Callable[[int, int], None] | None = None,
            batch: int = 2000) -> AnalysisReport:
        n = self.reader.count()
        report = AnalysisReport(combos=n)
        report.analysis_id = self.repo.start({a.name: a.version for a in self.analyzers}, self.settings, self.source, n)
        for a in self.analyzers:
            if a.global_:
                a.prepare(self.reader.iter_combos())   # streamed: Database 1 never has to fit into memory
        stored = {a.name: ({} if full else self.repo.versions(a.name)) for a in self.analyzers}
        pending, profiles = [], []
        total = 0
        for i, combo in enumerate(self.reader.iter_combos()):
            families = None
            for a in self.analyzers:
                if not a.global_ and stored[a.name].get(combo.signature) == a.version:
                    report.skipped[a.name] = report.skipped.get(a.name, 0) + 1
                    continue
                try:
                    res = a.analyze(combo)
                except Exception as e:  # one odd combo must not stop the analysis
                    report.errors.append(f"{a.name} {combo.signature[:12]}: {type(e).__name__}: {e}")
                    continue
                pending.append((combo.signature, a.name, a.version, res.raw, res.value, res.explanation))
                report.written[a.name] = report.written.get(a.name, 0) + 1
                if res.profile.get("family"):
                    families = res.profile["family"]
            profiles.append((combo.signature, combo.card_count, list(combo.hand), families, {}))
            if len(pending) >= batch:
                self.repo.write(pending, report.analysis_id)
                total += len(pending)
                pending = []
                self.repo.write_profiles(profiles)
                profiles = []
            if progress and (i + 1) % 1000 == 0:
                progress(i + 1, n)
        self.repo.write(pending, report.analysis_id)
        self.repo.write_profiles(profiles)
        total += len(pending)
        for a in self.analyzers:
            self.repo.normalise(a.name)
        self.repo.finish(report.analysis_id, total)
        if progress:
            progress(n, n)
        return report


def settings_dict(deck) -> dict:
    return {"deck_model": asdict(deck)}


def analyse_combos(combos: Iterable[ComboRecord], analyzers: list) -> dict[str, dict]:
    """Analyse combos in memory (no Database 2) - for the GUI's single search and for tests."""
    combos = list(combos)
    for a in analyzers:
        a.prepare(iter(combos))
    return {c.signature: {a.name: a.analyze(c) for a in analyzers} for c in combos}
