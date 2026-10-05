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
import os
import pstats
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import pandas as pd

from backtest import api, config, params, prep, replay, walkforward, workers, world
from backtest.targets import Targets

_WORLD: world.World | None = None


def _simulate(w: world.World, start: str, end: str) -> replay.Result:
    return replay.simulate(w.data, w.targets, w.risk, start, end, w.cfg["capital"]["inr"], w.surveillance,
                           carry_over_days=config.carry_over_days(w.cfg, w.risk), dividends=w.dividends, restart_after=config.restart_after(w.cfg))


def _with_point(w: world.World, schema: params.Schema, point: dict) -> world.World:
    """The world with a parameter point applied (identity when the point is empty)."""
    if not point:
        return w
    risk, analyst = schema.apply(point)
    return world.World(w.cfg, risk, analyst, w.data, Targets(w.data, analyst), w.dividends, w.surveillance)


def _init_worker(point: dict, single_thread: bool = True) -> None:
    global _WORLD
    if single_thread:
        workers.init_worker()
    w = world.World.build(config.load())
    _WORLD = _with_point(w, params.Schema.load(w.cfg["paths"]["params"], w.risk, w.analyst), point)


parse_set = world.parse_set


def _task(span: tuple[str, str]) -> tuple[int, float]:
    t = time.perf_counter()
    r = _simulate(_WORLD, *span)
    return len(r.nav), time.perf_counter() - t


def parallel(k: int, span: tuple[str, str], point: dict, single_thread: bool) -> tuple[float, float]:
    """Run k identical simulations in k processes. Returns (wall seconds including startup, slowest simulation seconds)."""
    t = time.perf_counter()
    with ProcessPoolExecutor(k, initializer=_init_worker, initargs=(point, single_thread)) as pool:
        got = list(pool.map(_task, [span] * k))
    return time.perf_counter() - t, max(s for _, s in got)


def windows_for(w: world.World, schema: params.Schema) -> walkforward.Windows:
    return api.windows(w, schema)


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
    parser.add_argument("--scaling", help="comma list of worker counts, e.g. 1,2,4,6,8: time that many parallel runs of a short window (default 2 years) for each")
    parser.add_argument("--no-limit-threads", action="store_true", help="leave pandas/Arrow/BLAS thread pools at their defaults in the workers (to compare)")
    parser.add_argument("--tried", type=int, default=30, help="parameter sets in the gate projection")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="parameter overrides from params.json, e.g. --set sizing.minNewOrderInr=3000 (repeatable); the live config at Rs 1 lakh cannot trade")
    args = parser.parse_args(argv)
    try:
        cfg = config.load()
        single_thread = not args.no_limit_threads
        if single_thread:
            workers.limit_threads()
        if args.scaling and args.years is None:
            args.years = 2.0
        t0 = time.perf_counter()
        w = world.World.build(cfg)
        t_build = time.perf_counter() - t0
        schema = params.Schema.load(cfg["paths"]["params"], w.risk, w.analyst)
        point = parse_set(args.set)
        wf = windows_for(w, schema)
        w = _with_point(w, schema, point)
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
        print(f"cpu threads {os.cpu_count()}; point overrides {point or 'none (live config)'}")
        print(f"universe {sum(map(len, w.data.buckets.values()))} names, {len(w.data.series)} with history; index {len(w.data.index)} rows")
        print(f"build (load prices, regime, panels): {t_build:.1f}s")
        print(f"one run {start}..{end}: {sessions} sessions, {len(result.fills)} fills, {t_run:.1f}s = {1000 * per_session:.1f} ms/session, {t_run / (sessions / 252):.1f}s per simulated year")
        if not len(result.fills):
            print("WARNING: 0 fills. This configuration cannot trade at this capital (the smallest position the sizer can open is below "
                  "sizing.minNewOrderInr), so the timing leaves out position handling and understates a real run. "
                  "Re-run with e.g. --set sizing.minNewOrderInr=3000 --set sizing.minAdjustmentInr=1500")
        if prof:
            out = io.StringIO()
            pstats.Stats(prof, stream=out).sort_stats("tottime").print_stats(15)
            print(out.getvalue())
        g = gate_simulations(schema, wf, args.tried)
        full_s = per_session * 252 * g["years"]
        print(f"strict gate with {args.tried} tried points: {g['runs']} simulations, {g['years']} simulated years "
              f"({g['folds']} folds, {g['neighbours']} neighbours, tuning region {g['spanYears']}y)")
        print(f"  one worker: {full_s / 3600:.1f} h; with 8 workers at ideal scaling: {full_s / 8 / 3600:.1f} h")
        measured: list[tuple[float, int]] = []  # (throughput, workers)
        mode = "single native thread per worker" if single_thread else "default native thread pools"
        if args.workers > 1:
            wall, slowest = parallel(args.workers, (start, end), point, single_thread)
            print(f"{args.workers} parallel runs ({mode}): {wall:.1f}s wall in total, of which {max(wall - slowest, 0):.1f}s was starting workers and loading data")
            print(f"  slowest worker simulated in {slowest:.1f}s vs {t_run:.1f}s alone: {slowest / t_run:.2f}x (1.0 = perfect scaling); "
                  f"throughput {args.workers * t_run / slowest:.1f} runs per single-run time")
            measured.append((args.workers * t_run / slowest, args.workers))
        if args.scaling:
            print(f"scaling sweep on {start}..{end} ({mode}); single run alone {t_run:.1f}s")
            for k in sorted({int(x) for x in args.scaling.split(",")}):
                wall, slowest = parallel(k, (start, end), point, single_thread)
                print(f"  {k:>2} workers: slowest {slowest:6.1f}s = {slowest / t_run:.2f}x, throughput {k * t_run / slowest:.1f}x, wall {wall:.1f}s")
                measured.append((k * t_run / slowest, k))
        if measured:
            best, k = max(measured)
            print(f"strict gate at the best measured throughput ({best:.1f}x with {k} workers): {full_s / best / 3600:.1f} h")
        return 0
    except prep.MissingInput as e:
        print(f"bench: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"bench: FAILED: {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
