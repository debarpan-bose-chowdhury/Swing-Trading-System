"""Runtime spike: how long does the judge take on the real data, where does the time go, and does it scale across processes?

  python -m backtest.bench                      time one run over the whole tuning region
  python -m backtest.bench --years 3 --profile  a shorter window with the top functions by time
  python -m backtest.bench --workers 8          also run 8 simulations in parallel processes (spawn) and report the speed-up

Read-only: nothing under app/ is written, and no result is stored in the trial registry. The projection at the end counts the
simulations a full strict-gate run needs (section "Session.gate_report") and multiplies by the measured seconds per session.
"""

import argparse
import cProfile
import io
import pstats
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import pandas as pd

from backtest import config, params, prep, replay, walkforward, world

_WORLD: world.World | None = None


def _simulate(w: world.World, start: str, end: str) -> replay.Result:
    return replay.simulate(w.data, w.targets, w.risk, start, end, w.cfg["capital"]["inr"], w.surveillance,
                           carry_over_days=w.cfg["fill"]["carryOverDays"], dividends=w.dividends)


def _init_worker() -> None:
    global _WORLD
    _WORLD = world.World.build(config.load())


def _task(span: tuple[str, str]) -> tuple[int, float]:
    t = time.perf_counter()
    r = _simulate(_WORLD, *span)
    return len(r.nav), time.perf_counter() - t


def windows_for(w: world.World, schema: params.Schema) -> walkforward.Windows:
    cfg = w.cfg
    dates = list(w.data.index.Date)
    return walkforward.Windows(dates, schema.common_start(dates), cfg["walkforward"], cfg["window"]["holdoutYears"], schema.required_purge())


def gate_simulations(schema: params.Schema, windows: walkforward.Windows, tried: int) -> dict:
    """Simulations (and years simulated) one Session.gate_report needs: every tried point over the tuning region, two runs per fold, one per neighbour."""
    folds = windows.rolling()
    neighbours = len(schema.neighbours(schema.defaults()))
    span_years = (pd.Timestamp(windows.tuning_end) - pd.Timestamp(windows.start)).days / 365.25
    fold_years = sum((pd.Timestamp(f.train[1]) - pd.Timestamp(f.train[0])).days + (pd.Timestamp(f.test[1]) - pd.Timestamp(f.test[0])).days for f in folds) / 365.25
    return {"runs": tried + 2 * len(folds) + neighbours, "years": round(tried * span_years + fold_years + neighbours * span_years, 1), "folds": len(folds),
            "neighbours": neighbours, "spanYears": round(span_years, 1)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backtest.bench")
    parser.add_argument("--years", type=float, help="simulate only this many years from the common start (default: the whole tuning region)")
    parser.add_argument("--profile", action="store_true", help="print the top functions by own time")
    parser.add_argument("--workers", type=int, default=0, help="also time this many simulations in parallel processes")
    parser.add_argument("--tried", type=int, default=30, help="parameter sets in the gate projection")
    args = parser.parse_args(argv)
    try:
        cfg = config.load()
        t0 = time.perf_counter()
        w = world.World.build(cfg)
        t_build = time.perf_counter() - t0
        schema = params.Schema.load(cfg["paths"]["params"], w.risk, w.analyst)
        wf = windows_for(w, schema)
        start = wf.start
        end = wf.tuning_end if args.years is None else str(pd.Timestamp(start) + pd.Timedelta(days=int(args.years * 365.25)))[:10]
        end = min(end, wf.tuning_end)
        prof = cProfile.Profile() if args.profile else None
        t1 = time.perf_counter()
        if prof:
            prof.enable()
        result = _simulate(w, start, end)
        if prof:
            prof.disable()
        t_run = time.perf_counter() - t1
        sessions = len(result.nav)
        per_session = t_run / sessions
        print(f"universe {sum(map(len, w.data.buckets.values()))} names, {len(w.data.series)} with history; index {len(w.data.index)} rows")
        print(f"build (load prices, regime, panels): {t_build:.1f}s")
        print(f"one run {start}..{end}: {sessions} sessions, {len(result.fills)} fills, {t_run:.1f}s = {1000 * per_session:.1f} ms/session, {t_run / (sessions / 252):.1f}s per simulated year")
        if prof:
            out = io.StringIO()
            pstats.Stats(prof, stream=out).sort_stats("tottime").print_stats(15)
            print(out.getvalue())
        g = gate_simulations(schema, wf, args.tried)
        full_s = per_session * 252 * g["years"]
        print(f"strict gate with {args.tried} tried points: {g['runs']} simulations, {g['years']} simulated years "
              f"({g['folds']} folds, {g['neighbours']} neighbours, tuning region {g['spanYears']}y)")
        print(f"  one worker: {full_s / 3600:.1f} h; with 8 workers at ideal scaling: {full_s / 8 / 3600:.1f} h")
        if args.workers > 1:
            t2 = time.perf_counter()
            with ProcessPoolExecutor(args.workers, initializer=_init_worker) as pool:
                got = list(pool.map(_task, [(start, end)] * args.workers))
            wall = time.perf_counter() - t2
            slowest = max(s for _, s in got)
            print(f"{args.workers} parallel runs: {wall:.1f}s wall in total, of which {max(wall - slowest, 0):.1f}s was starting workers and loading data")
            print(f"  slowest worker simulated in {slowest:.1f}s vs {t_run:.1f}s alone: {slowest / t_run:.2f}x (1.0 = perfect scaling); "
                  f"throughput {args.workers * t_run / slowest:.1f} runs per single-run time")
        return 0
    except prep.MissingInput as e:
        print(f"bench: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"bench: FAILED: {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
