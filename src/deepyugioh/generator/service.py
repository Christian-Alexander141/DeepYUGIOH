"""ComboGenerator - the facade of the Combo Generator module.

Inputs  : CardPool (+ InteractionGraph for global runs)  - from the Card Knowledge System
Outputs : RunRecord + ComboRecord via ComboSink           - to Database 1
Control : GeneratorConfig, search() / run(), ProgressListener, coverage()
"""

from __future__ import annotations

import multiprocessing as mp
import subprocess
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol

from deepyugioh import paths
from deepyugioh.contracts.cards import CardPool, InteractionGraph
from deepyugioh.contracts.combos import BENEFIT_CRITERIA, ComboRecord, ComboSink, RunRecord
from deepyugioh.contracts.setups import Candidate, StartSetup
from deepyugioh.generator.candidates import CandidateSource, GraphCandidateSource
from deepyugioh.generator.config import GeneratorConfig
from deepyugioh.generator.execution import Versions, execute
from deepyugioh.generator.jobs import JobReport, JobStore


class ProgressListener(Protocol):
    def on_job(self, job: JobReport) -> None: ...


@dataclass
class SearchReport:
    run: RunRecord
    combos: list[ComboRecord]
    run_id: int | None
    new_combos: int


@dataclass
class GenerationReport:
    jobs: int = 0
    complete: int = 0
    incomplete: int = 0
    errors: int = 0
    skipped: int = 0
    combos_found: int = 0
    combos_new: int = 0
    elapsed_s: float = 0.0
    tiers: dict[int, int] = field(default_factory=dict)  # tier -> jobs processed
    target_hits: int = 0          # new combos counting towards `target_combos`
    target_reached: bool = False  # the run ended because the target was reached
    stop_reason: str = ""         # target | max_jobs | time_budget | stopped | exhausted


TARGET_BENEFITS = (*BENEFIT_CRITERIA, "wins")


def target_hits(combos: Iterable[ComboRecord], new_signatures: Iterable[str], benefit: str | None) -> int:
    """New combos that count towards a run target: all new ones, or those with `benefit` > 0."""
    new = set(new_signatures)
    return sum(1 for c in combos if c.signature in new and (benefit is None or benefit in c.positive))


def _git_head(repo: Path) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


# ---- worker process --------------------------------------------------------
_WORKER_POOL: CardPool | None = None


def _worker_init(pool_factory: Callable[[], CardPool]) -> None:
    global _WORKER_POOL
    _WORKER_POOL = pool_factory()


def _worker_run(candidate: Candidate, limits, check: bool, versions: Versions):
    return execute(_WORKER_POOL, candidate.setup, limits, candidate=candidate, check=check, versions=versions)


class ComboGenerator:
    def __init__(
        self,
        pool: CardPool,
        sink: ComboSink | None = None,
        config: GeneratorConfig | None = None,
        *,
        graph: InteractionGraph | None = None,
        source: CandidateSource | None = None,
        state: JobStore | None = None,
        pool_factory: Callable[[], CardPool] | None = None,
        knowledge=None,
        hypotheses=None,
        aide_codes=None,
    ):
        """`pool_factory` builds a CardPool inside each worker process (config.workers > 1);
        it must be picklable, e.g. the CardStore class itself.

        `knowledge` (EffectKnowledge) and `hypotheses` (aide.HypothesisStore) are only used by
        the AIDE tiers 5-7; by default they are the card database and data/aide.sqlite.
        `aide_codes` restricts the cards AIDE plans with (benchmarks on a card subset)."""
        self.pool = pool
        self.pool_factory = pool_factory
        self.sink = sink
        self.config = config or GeneratorConfig()
        self.graph = graph
        self._source = source
        self._state = state
        self._knowledge = knowledge
        self._hypotheses = hypotheses
        self._aide_codes = aide_codes
        self.versions = Versions(
            engine_commit=_git_head(paths.THIRD_PARTY / "ygopro-core"),
            scripts_commit=_git_head(paths.CARD_SCRIPTS),
            cdb_commit=_git_head(paths.BABEL_CDB),
            graph_version=graph.version() if graph is not None else None,
        )

    # ---- single setup -----------------------------------------------------
    def search(self, setup: StartSetup, candidate: Candidate | None = None, store: bool = True) -> SearchReport:
        """Search one start setup, validate, and (if a sink is configured) store the result."""
        run, combos = execute(
            self.pool, setup, self.config.limits, candidate=candidate, check=self.config.validate, versions=self.versions
        )
        run_id, new = None, 0
        if store and self.sink is not None:
            res = self.sink.store_run(run, combos)
            run_id, new = res.run_id, res.new_combos
        return SearchReport(run, combos, run_id, new)

    # ---- global run -------------------------------------------------------
    @property
    def source(self) -> CandidateSource:
        if self._source is None:
            if self.graph is None:
                raise ValueError("a global run needs an InteractionGraph or a CandidateSource")
            from deepyugioh.generator.aide.source import HybridCandidateSource

            graph_source = GraphCandidateSource(
                self.graph, self.pool, self.config.candidates, self.config.boards(self.pool)
            )
            self._source = HybridCandidateSource(graph_source, self._aide_source, self.config.candidates.aide_share,
                                                 late_factory=lambda: self._late_source(graph_source))
        return self._source

    def _late_source(self, graph_source):
        """Tier 8 (later game situations), built on first use only."""
        from deepyugioh.generator.lategame import LateGameCandidateSource

        if self._knowledge is None:
            from deepyugioh.cards.signatures import SignatureStore

            self._knowledge = SignatureStore()
        return LateGameCandidateSource(self._knowledge, self.graph, self.pool, graph_source, self.config.candidates.late_zones)

    def _aide_source(self):
        """Built on first use of tier 5, 6 or 7 only."""
        from deepyugioh.generator.aide.hypotheses import HypothesisStore
        from deepyugioh.generator.aide.source import AideCandidateSource

        if self._knowledge is None:
            from deepyugioh.cards.signatures import SignatureStore  # default: the card database

            self._knowledge = SignatureStore()
        if self._hypotheses is None:
            self._hypotheses = HypothesisStore()
        return AideCandidateSource(
            self._knowledge, self.graph, self.pool, self.config.candidates, self.config.boards(self.pool),
            max_cards=self.config.limits.max_cards, hypotheses=self._hypotheses, codes=self._aide_codes,
        )

    def _tier_version(self, tier: int) -> str:
        """Cursor key of a tier: per tier if the source distinguishes them (hybrid), else the source version."""
        tier_version = getattr(self.source, "tier_version", None)
        return tier_version(tier) if callable(tier_version) else self.source.version()

    @property
    def state(self) -> JobStore:
        if self._state is None:
            self._state = JobStore()
        return self._state

    def coverage(self) -> dict[int, dict]:
        """Per tier: total candidates (if known), processed, complete/incomplete/error, combos."""
        done = self.state.coverage()
        out = {}
        for tier in sorted(set(self.config.candidates.tiers) | set(done)):
            row = dict(done.get(tier, {"complete": 0, "incomplete": 0, "error": 0, "combos_found": 0, "combos_new": 0}))
            row["total"] = self.source.tier_size(tier)
            row["cursor"] = self.state.cursor(tier, self._tier_version(tier))
            out[tier] = row
        return out

    def run(
        self,
        tiers: Iterable[int] | None = None,
        max_jobs: int | None = None,
        progress: ProgressListener | None = None,
        time_budget: float | None = None,
        should_stop: Callable[[], bool] | None = None,
        target_combos: int | None = None,
        target_benefit: str | None = None,
    ) -> GenerationReport:
        """Process candidates tier by tier, resuming where the last run stopped.

        The run ends at the first of: `max_jobs` searches, `time_budget` seconds,
        `target_combos` new combos in Database 1 (only those with a positive
        `target_benefit`, e.g. "wins", if given), `should_stop()` returning True,
        or when every candidate of the selected tiers is processed. With only a
        target set, the run keeps going as long as it takes.

        `should_stop` is polled before every new search (e.g. a stop button):
        once it returns True no new searches start, running ones are finished
        and stored, so the run can be resumed later without gaps."""
        if self.sink is None:
            raise ValueError("a global run needs a ComboSink")
        if target_combos is not None and target_combos < 1:
            raise ValueError("target_combos must be >= 1")
        if target_benefit is not None and target_benefit not in TARGET_BENEFITS:
            raise ValueError(f"unknown target_benefit {target_benefit!r} (known: {', '.join(TARGET_BENEFITS)})")
        report = GenerationReport()
        t0 = time.perf_counter()
        deadline = t0 + time_budget if time_budget else None
        goal = _Goal(max_jobs, deadline, should_stop, target_combos, target_benefit)
        workers = max(1, self.config.workers)
        executor = None
        if workers > 1:
            if self.pool_factory is None:
                raise ValueError("parallel runs (workers > 1) need a picklable pool_factory")
            executor = ProcessPoolExecutor(
                workers, mp_context=mp.get_context("spawn"),
                initializer=_worker_init, initargs=(self.pool_factory,),
            )
        try:
            for tier in tiers or self.config.candidates.tiers:
                if goal.reached(report):
                    break
                self._run_tier(tier, report, goal, progress, executor, workers)
        finally:
            if executor is not None:
                executor.shutdown(cancel_futures=True)
        report.stop_reason = goal.reason or "exhausted"
        if target_combos is not None and report.target_hits >= target_combos:
            report.stop_reason, report.target_reached = "target", True
        report.elapsed_s = time.perf_counter() - t0
        return report

    def _run_tier(self, tier, report, goal: "_Goal", progress, executor, workers):
        source_version = self._tier_version(tier)
        start = self.state.cursor(tier, source_version)
        pending: dict = {}                   # future -> (position, candidate)
        finished: set[int] = set()           # positions done beyond the cursor
        cursor = start
        window = deque()

        def advance_cursor():
            nonlocal cursor
            while cursor in finished:
                finished.discard(cursor)
                cursor += 1
            self.state.set_cursor(tier, source_version, cursor)

        def finish(position: int, candidate: Candidate, outcome):
            run, combos, error = outcome
            run_id, new, hits = None, 0, 0
            if error is None:
                res = self.sink.store_run(run, combos)
                run_id, new = res.run_id, res.new_combos
                hits = target_hits(combos, res.new_signatures, goal.benefit)
                status = "complete" if run.complete else "incomplete"
                found, elapsed = run.combos_found, run.elapsed_s
            else:
                status, found, elapsed = "error", 0, 0.0
            job = JobReport(candidate.id, tier, position, status, found, new, elapsed, run_id, error, hits)
            self.state.record(job)
            report.jobs += 1
            report.tiers[tier] = report.tiers.get(tier, 0) + 1
            if status == "complete":
                report.complete += 1
            elif status == "incomplete":
                report.incomplete += 1
            else:
                report.errors += 1
            report.combos_found += found
            report.combos_new += new
            report.target_hits += hits
            finished.add(position)
            advance_cursor()
            if progress:
                progress.on_job(job)

        for position, candidate in enumerate(self.source.candidates(tier)):
            if position < start:
                continue
            if goal.reached(report, len(pending)):
                break
            if self.state.is_done(candidate.id):
                report.skipped += 1
                finished.add(position)
                advance_cursor()
                continue
            if executor is None:
                finish(position, candidate, self._execute_safe(candidate))
                continue
            fut = executor.submit(_worker_run, candidate, self.config.limits, self.config.validate, self.versions)
            pending[fut] = (position, candidate)
            while len(pending) >= 2 * workers:
                self._collect(pending, finish, block=True)
                if goal.reached(report):
                    break  # target met: stop queueing, the searches still running are finished below
        if goal.reason and goal.reason != "max_jobs":  # max_jobs already counts the queued searches
            # searches not started yet are dropped (not recorded: the cursor stays before them,
            # so the next run picks them up); searches already running are finished and stored
            for fut in [f for f in pending if f.cancel()]:
                del pending[fut]
        while pending:
            self._collect(pending, finish, block=True)

    def _execute_safe(self, candidate: Candidate):
        try:
            run, combos = execute(
                self.pool, candidate.setup, self.config.limits, candidate=candidate,
                check=self.config.validate, versions=self.versions,
            )
            return run, combos, None
        except Exception as e:  # one broken card script must not stop a global run
            return None, [], f"{type(e).__name__}: {e}"

    @staticmethod
    def _collect(pending: dict, finish, block: bool):
        done, _ = wait(list(pending), return_when=FIRST_COMPLETED, timeout=None if block else 0)
        for fut in done:
            position, candidate = pending.pop(fut)
            try:
                run, combos = fut.result()
                outcome = (run, combos, None)
            except Exception as e:
                outcome = (None, [], f"{type(e).__name__}: {e}")
            finish(position, candidate, outcome)


@dataclass
class _Goal:
    """When a global run stops starting new searches; `reason` records the first stop condition met."""

    max_jobs: int | None
    deadline: float | None
    should_stop: Callable[[], bool] | None
    target: int | None
    benefit: str | None
    reason: str = ""

    def reached(self, report: GenerationReport, pending: int = 0) -> bool:
        if self.reason:
            return True
        if self.target is not None and report.target_hits >= self.target:
            self.reason = "target"
        elif self.max_jobs is not None and report.jobs + pending >= self.max_jobs:
            self.reason = "max_jobs"
        elif self.should_stop is not None and self.should_stop():
            self.reason = "stopped"
        elif self.deadline and time.perf_counter() > self.deadline:
            self.reason = "time_budget"
        return bool(self.reason)
