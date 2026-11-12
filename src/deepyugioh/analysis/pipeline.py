"""The whole of Stufe 2 in order - one entry point for the CLI and the test GUI.

    line facts (engine replay)  ->  axes  ->  compatibility  ->  robustness axis (from stored searches)
    ->  overall strength and situations  ->  stored filter profiles

Every step is incremental: it only does what is missing or outdated. The
handtrap search itself (`robustness`) is a separate, long running job.
Each thread or process that calls these functions gets its own database
connections.
"""

from __future__ import annotations

import time
from typing import Callable

Log = Callable[[str], None]


def _finish(analysis, log: Log) -> dict:
    """Derived results that follow from the stored axes: robustness axis, scorings, filter profiles."""
    from deepyugioh import settings
    from deepyugioh.analysis.filters import QualityFilter
    from deepyugioh.analysis.robustness import build_axis
    from deepyugioh.analysis.strength import Rescorer

    out = {}
    robust = build_axis(analysis, settings.robustness_settings())
    out["robustness"] = robust
    log(f"robustness: {robust['combos']} combos with a complete handtrap search, {robust['partial']} partly searched"
        + ("" if robust["combos"] or robust["partial"] else " - run `deepyugioh robustness run`"))
    out["scorings"] = Rescorer(analysis, settings.analysis_scorings()).run()
    for name, info in out["scorings"].items():
        missing = f" (without {', '.join(info['missing'])} - not analysed yet)" if info["missing"] else ""
        partial = f" ({info['partial']} combos without one of their components)" if info.get("partial") else ""
        log(f"scoring {name} {info['version']}: {info['combos']} combos{missing}{partial}")
    out["filters"] = run_filters(QualityFilter(analysis), log=log)
    return out


def run_filters(qf, name: str | None = None, log: Log = print) -> dict:
    qf.ensure_defaults()
    results = {name: qf.run(name)} if name else qf.run_all()
    for n, res in results.items():
        if isinstance(res, str):
            log(f"filter {n}: not applied - {res}")
        else:
            dropped = ", ".join(f"{k} {v}" for k, v in res.dropped.items()) or "nothing"
            log(f"filter {n}: {len(res.members)} of {res.considered} combos (removed: {dropped})")
    return results


def rescore(log: Log = print) -> dict:
    """After changed weights: robustness axis, scorings and filters again - no new analysis."""
    from deepyugioh.analysis.store import AnalysisRepository

    return _finish(AnalysisRepository(), log)


def run_analysis(*, full: bool = False, engine: bool = True, workers: int | None = None, log: Log = print,
                 progress: Callable[[str, int, int], None] | None = None) -> dict:
    from deepyugioh import settings
    from deepyugioh.analysis.axes import default_analyzers
    from deepyugioh.analysis.compatibility import run_compatibility
    from deepyugioh.analysis.linefacts import run_line_facts
    from deepyugioh.analysis.store import AnalysisRepository
    from deepyugioh.analysis.system import AnalyzerSystem, settings_dict
    from deepyugioh.cards.signatures import SignatureStore
    from deepyugioh.cards.store import CardStore
    from deepyugioh.repository.combo_db import ComboRepository

    step = progress or (lambda what, i, n: None)
    deck = settings.deck_model()
    combos, analysis = ComboRepository(), AnalysisRepository()
    store, knowledge = CardStore(), SignatureStore()
    errors: list[str] = []
    out: dict = {"combos": combos.count()}
    if engine:   # what the lines really summon and activate - replayed by the engine
        t0 = time.perf_counter()
        workers = workers or settings.analysis_workers()
        facts = run_line_facts(combos, analysis, CardStore, workers=workers, full=full, name=store.name,
                               progress=lambda i, n: step("line facts", i, n))
        log(f"line facts: {facts['written']} replayed in {time.perf_counter() - t0:.0f}s ({workers} workers), "
            f"{facts['skipped']} up to date, {len(facts['errors'])} errors")
        errors += facts["errors"]
    system = AnalyzerSystem(combos, analysis, default_analyzers(store, knowledge, deck),
                            settings=settings_dict(deck), source=str(combos.path))
    t0 = time.perf_counter()
    report = system.run(full=full, progress=lambda i, n: step("axes", i, n))
    log(f"{report.combos} combos in {time.perf_counter() - t0:.0f}s: written {report.written}, up to date {report.skipped}")
    errors += report.errors
    t0 = time.perf_counter()
    compat = run_compatibility(analysis, knowledge.card, settings.compatibility_settings(), full=full)
    log(f"compatibility: {compat['edges'] or 'no edges'}"
        + (" (unchanged)" if compat["skipped"] else f" in {time.perf_counter() - t0:.0f}s"))
    out.update(_finish(analysis, log))
    out["errors"] = errors
    for e in errors[:10]:
        log(f"  error: {e}")
    if len(errors) > 10:
        log(f"  ... {len(errors) - 10} more errors")
    return out


def run_robustness_search(*, workers: int | None = None, max_combos: int | None = None, time_budget: float | None = None,
                          retry_incomplete: bool = False, combo_ids: list[int] | None = None,
                          should_stop: Callable[[], bool] | None = None, log: Log = print,
                          progress: Callable[[dict], None] | None = None) -> dict:
    """The handtrap search (resumable), then everything that follows from its results."""
    from deepyugioh import settings
    from deepyugioh.analysis.robustness import run_robustness
    from deepyugioh.analysis.store import AnalysisRepository
    from deepyugioh.cards.store import CardStore
    from deepyugioh.repository.combo_db import ComboRepository

    analysis = AnalysisRepository()
    rs = settings.robustness_settings()
    workers = workers or settings.analysis_workers()
    log(f"robustness: {len(rs.catalog)} handtraps, {rs.max_nodes} nodes / {rs.time_limit:g}s per search, {workers} workers")
    summary = run_robustness(ComboRepository(), analysis, CardStore, rs, workers=workers, max_combos=max_combos,
                             time_budget=time_budget, retry_incomplete=retry_incomplete, combo_ids=combo_ids,
                             should_stop=should_stop, progress=progress)
    log(f"{summary['combos']} of {summary['pending']} pending combos in {summary['elapsed']:.0f}s: "
        f"{summary['complete']} complete, {summary['incomplete']} incomplete, {len(summary['errors'])} errors"
        + (" - stopped, resume with the same command" if summary["stopped"] else ""))
    for e in summary["errors"][:10]:
        log(f"  error: {e}")
    summary.update(_finish(analysis, log))
    return summary
