"""Process pool of pure simulators. Workers receive a plain dict and return a plain dict; only the parent touches Optuna and the files.

Windows `spawn` rules: side-effect-free imports, the factory a top-level callable (or a functools.partial of one), the world built once
per worker in the initialiser. One native thread per worker (api.limit_threads in the parent, api.init_worker in each worker): measured
5.1x throughput on 8 workers against 2.2x with default thread pools.
"""

import multiprocessing
from concurrent.futures import Future, ProcessPoolExecutor

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
        self.workers = workers
        if workers <= 1:
            self.runner, self.pool = factory(), None
        else:
            api.limit_threads()
            self.runner = None
            self.pool = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"), initializer=_init, initargs=(factory,))

    def submit(self, job: dict) -> Future:
        if self.pool is not None:
            return self.pool.submit(_run, job)
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
