"""Process pool of pure simulators. Workers receive a plain dict and return a plain dict; only the parent touches Optuna and the files.

Windows `spawn` rules: side-effect-free imports, the factory a top-level callable (or a functools.partial of one), the world built once
per worker in the initialiser. One native thread per worker (api.limit_threads in the parent, api.init_worker in each worker): measured
5.1x throughput on 8 workers against 2.2x with default thread pools.
"""

import multiprocessing
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from backtest import api

_RUNNER = None


def _init(factory) -> None:
    global _RUNNER
    api.init_worker()
    _RUNNER = factory()


def _run(job: dict) -> dict:
    return _RUNNER.run(job)


def _identity() -> dict:
    return _RUNNER.identity()


class EvalPool:
    """workers = 1 runs in the parent, one job at a time (deterministic, easy to debug); more start a spawn pool."""

    def __init__(self, factory, workers: int):
        self.workers, self.factory = workers, factory
        if workers <= 1:
            self.runner, self.pool = factory(), None
        else:
            api.limit_threads()
            self.runner = None
            self.pool = self._start()

    def _start(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(max_workers=self.workers, mp_context=multiprocessing.get_context("spawn"), initializer=_init, initargs=(self.factory,))

    def restart(self, broken) -> None:
        """One worker died (killed, out of memory): the executor fails every pending and later job for good. Replace it once per breakage (`broken` is the pool the failed job ran in)."""
        if self.pool is broken:
            broken.shutdown(wait=False, cancel_futures=True)
            self.pool = self._start()

    def submit(self, job: dict) -> Future:
        if self.pool is not None:
            try:
                pool = self.pool
                fut = pool.submit(_run, job)
            except BrokenProcessPool:
                self.restart(pool)
                pool = self.pool
                fut = pool.submit(_run, job)
            fut.pool = pool  # which executor ran it, so a BrokenProcessPool restarts that one only once
            return fut
        fut: Future = Future()
        try:
            fut.set_result(self.runner.run(job))
        except Exception as e:  # noqa: BLE001  the caller maps it to a FAIL trial (a KeyboardInterrupt still stops the run)
            fut.set_exception(e)
        return fut

    def identity(self) -> dict:
        return self.runner.identity() if self.pool is None else self.pool.submit(_identity).result()

    def close(self) -> None:
        if self.pool is not None:
            self.pool.shutdown(wait=True, cancel_futures=True)
            self.pool = None
