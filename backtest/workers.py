"""Settings every worker process must apply, measured on a 12-thread PC (bench --scaling): with default native thread pools,
8 parallel simulations reached 2.2x throughput (and 6 workers were slower than 4); with one native thread per process, 5.1x.
The HPO runner, walk-forward folds and any other process pool must call limit_threads() in the parent before starting
workers and init_worker() inside each worker.
"""

import os

THREAD_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "ARROW_NUM_THREADS")


def limit_threads() -> None:
    """Parent side: one native thread per process (inherited by workers started afterwards)."""
    for k in THREAD_ENV:
        os.environ[k] = "1"


def init_worker() -> None:
    """Worker side: pyarrow's pools are sized at import, so cap them explicitly too."""
    import pyarrow
    pyarrow.set_cpu_count(1)
    pyarrow.set_io_thread_count(1)
