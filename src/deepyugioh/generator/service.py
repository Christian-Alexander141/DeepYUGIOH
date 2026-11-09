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
from deepyugioh.contracts.combos import ComboRecord, ComboSink, RunRecord
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
    ):
        """`pool_factory` builds a CardPool inside each worker process (config.workers > 1);
        it must be picklable, e.g. the CardStore class itself."""
        self.pool = pool
        self.pool_factory = pool_factory
        self.sink = sink
        self.config = config or GeneratorConfig()
        self.graph = graph
        self._source = source
        self._state = state
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
            self._source = GraphCandidateSource(
                self.graph, self.pool, self.config.candidates, self.config.boards(self.pool)
            )
        return self._source

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
            row["cursor"] = self.state.cursor(tier, self.source.version())
            out[tier] = row
        return out

    def run(
        self,
        tiers: Iterable[int] | None = None,
        max_jobs: int | None = None,
        progress: ProgressListener | None = None,
        time_budget: float | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> GenerationReport:
        """Process candidates tier by tier, resuming where the last run stopped.

        `should_stop` is polled before every new search (e.g. a stop button):
        once it returns True no new searches start, running ones are finished
        and stored, so the run can be resumed later without gaps."""
        if self.sink is None:
            raise ValueError("a global run needs a ComboSink")
        report = GenerationReport()
        t0 = time.perf_counter()
        deadline = t0 + time_budget if time_budget else None
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
                if max_jobs is not None and report.jobs >= max_jobs:
                    break
                if deadline and time.perf_counter() > deadline:
                    break
                if should_stop is not None and should_stop():
                    break
                self._run_tier(tier, report, max_jobs, progress, executor, workers, deadline, should_stop)
        finally:
            if executor is not None:
                executor.shutdown(cancel_futures=True)
        report.elapsed_s = time.perf_counter() - t0
        return report

    def _run_tier(self, tier, report, max_jobs, progress, executor, workers, deadline, should_stop=None):
        source_version = self.source.version()
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
            run_id, new = None, 0
            if error is None:
                res = self.sink.store_run(run, combos)
                run_id, new = res.run_id, res.new_combos
                status = "complete" if run.complete else "incomplete"
                found, elapsed = run.combos_found, run.elapsed_s
            else:
                status, found, elapsed = "error", 0, 0.0
            job = JobReport(candidate.id, tier, position, status, found, new, elapsed, run_id, error)
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
            finished.add(position)
            advance_cursor()
            if progress:
                progress.on_job(job)

        def stop() -> bool:
            if max_jobs is not None and report.jobs + len(pending) >= max_jobs:
                return True
            if should_stop is not None and should_stop():
                return True
            return bool(deadline and time.perf_counter() > deadline)

        for position, candidate in enumerate(self.source.candidates(tier)):
            if position < start:
                continue
            if stop():
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
