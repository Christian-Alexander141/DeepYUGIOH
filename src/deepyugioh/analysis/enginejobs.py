"""Engine work of the Analyzer System in parallel worker processes.

Line facts (step 2.5) and robustness (step 2.8) ask the engine, which is slow
compared to the computed axes. Both run as jobs over combos: resumable (a job
whose result is stored with the current version is skipped) and parallel
(`workers` processes, each with its own card pool and engine).
"""

from __future__ import annotations

import multiprocessing as mp
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from typing import Callable, Iterable, Iterator

_POOL = None
_STATE: dict = {}


def _init(pool_factory: Callable[[], object]) -> None:
    global _POOL
    _POOL = pool_factory()


def worker_pool():
    """The card pool of this worker process."""
    return _POOL


def worker_state() -> dict:
    """Per-process cache for things that are expensive to build (analyzers, catalogs)."""
    return _STATE


def run_jobs(work: Callable, payloads: Iterable, *, workers: int, pool_factory: Callable[[], object],
             should_stop: Callable[[], bool] | None = None) -> Iterator[tuple[object, object, str | None]]:
    """Yields (payload, result, error) as jobs finish. `work(payload)` must be a module-level function;
    it reads the card pool with worker_pool(). With workers <= 1 everything runs in this process.

    should_stop is polled before every new job: once True no new job starts, running ones finish."""
    stop = should_stop or (lambda: False)
    if workers <= 1:
        _init(pool_factory)
        for p in payloads:
            if stop():
                return
            try:
                yield p, work(p), None
            except Exception as e:  # one broken card script must not stop the run
                yield p, None, f"{type(e).__name__}: {e}"
        return
    with ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn"), initializer=_init,
                             initargs=(pool_factory,)) as ex:
        pending: dict = {}

        def collect():
            done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            for fut in done:
                p = pending.pop(fut)
                try:
                    yield p, fut.result(), None
                except Exception as e:
                    yield p, None, f"{type(e).__name__}: {e}"

        for p in payloads:
            if stop():
                break
            pending[ex.submit(work, p)] = p
            while len(pending) >= 2 * workers:
                yield from collect()
        for fut in [f for f in pending if stop() and f.cancel()]:
            del pending[fut]
        while pending:
            yield from collect()
