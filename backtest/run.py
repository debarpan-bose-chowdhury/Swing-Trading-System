"""Backtest CLI. Run from the repo root: python -m backtest.run --check

Exit codes as in the app: 0 ok, 1 failed, 2 busy, 3 gate not met (run the upstream stage first).
--check validates the config; --single runs the judge once. Walk-forward, trials and the gate arrive in later phases.
"""

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from app.market.common import atomic
from backtest import api, config, prep, report, tax, workers, world


def check() -> int:
    """Config loads and validates, and the app side it builds on loads too. No network, no writes."""
    try:
        cfg = config.load()
        from app.analyst import common as analyst_common
        from app.analyst import regime
        app_cfg = analyst_common.load_config()
        regime.windows_of(app_cfg)
        if not cfg["tax"]["confirmed"]:
            print("run: check ok (tax schedule is a draft: set tax.confirmed once you have verified it)")
            return 0
    except Exception as e:
        print(f"run: check FAILED: {e!r}", file=sys.stderr)
        return 1
    print("run: check ok")
    return 0


def evaluate(cfg: dict, w: world.World, start: str | None, end: str | None, haircut: float = 0.0, progress=None) -> dict:
    """One judge run on a built world; returns the report. haircut: the write-off on a position whose ticker stopped trading."""
    start = start or cfg["window"]["start"] or w.first_known_regime()
    end = end or cfg["window"]["end"]
    ev = api.evaluate_config(w, w.risk, w.analyst, start, end, haircut=haircut, progress=progress)
    result, pieces, taxes, post = ev.result, ev.pieces, ev.taxes, ev.post_tax_nav
    meta = {"configHash": world.config_hash(cfg, w.risk, w.analyst), "dataHash": w.data.data_hash(), "codeSha": world.code_sha(),
            "entryViewMismatch": tax.entry_view_mismatch(pieces), "missingHistory": w.data.missing(),
            "tradedNames": _traded_names(w.data, result.fills)}
    return report.build(result, taxes, post, w.risk, cfg, bool(cfg["surv"]["proxy"]), meta)


def _traded_names(data, fills) -> dict:
    """How many names the run traded and how many of them later stopped trading (the only ones a write-off can touch)."""
    traded = sorted(set(fills.ticker)) if len(fills) else []
    end = data.index.Date.iloc[-1]
    stopped = [t for t in traded if (data.last_date(t) or "9999-12-31") < end]
    return {"count": len(traded), "laterStoppedTrading": len(stopped), "examples": stopped[:10]}


def write(cfg: dict, rep: dict, suffix: str = "") -> Path:
    path = Path(cfg["paths"]["data"]) / "runs" / f"run_{rep['window']['start']}_{rep['window']['end']}_{rep['configHash'][:8]}{suffix}.json"
    atomic(path, lambda tmp: tmp.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8"))
    return path


def single(cfg: dict, start: str | None, end: str | None, point: dict | None = None) -> Path:
    """One judge run over [start, end] with the config as it is, written to backtest/data/runs/. Returns the report path."""
    return write(cfg, evaluate(cfg, world.with_point(world.World.build(cfg), point or {}), start, end, progress=_progress("single")))


def _clock(seconds: float) -> str:
    m, sec = divmod(int(seconds), 60)
    return f"{m // 60}:{m % 60:02d}:{sec:02d}"


def _progress(label: str):
    """A replay.simulate progress callback: one line per 5% with the simulated date, elapsed time and an ETA."""
    t0, last = time.perf_counter(), [-1]

    def show(done: int, total: int, asof: str) -> None:
        pct = done * 100 // total
        if pct == last[0] or (pct % 5 and done != total):
            return
        last[0] = pct
        elapsed = time.perf_counter() - t0
        print(f"[{label}] {pct:3d}%  {asof}  elapsed {_clock(elapsed)}  eta {_clock(elapsed * (total - done) / done)}", flush=True)
    return show


_BUILT: dict[str, world.World] = {}  # per process: the worlds already built, keyed by universe mode


def _run_case(job: tuple) -> tuple[str, dict, Path]:
    cfg, point, start, end, label, mode, haircut = job
    cfg = json.loads(json.dumps(cfg))
    cfg["universe"]["mode"] = mode
    if mode not in _BUILT:
        t0 = time.perf_counter()
        print(f"[{label}] building the {mode} world ...", flush=True)
        _BUILT[mode] = world.with_point(world.World.build(cfg), point)
        print(f"[{label}] world ready in {_clock(time.perf_counter() - t0)}", flush=True)
    rep = evaluate(cfg, _BUILT[mode], start, end, haircut, _progress(label))
    path = write(cfg, rep, f"_{mode}" + (f"_h{int(haircut * 100)}" if mode == "pit" else ""))
    print(f"[{label}] done -> {path.name}", flush=True)
    return label, rep, path


def compare(cfg: dict, start: str | None, end: str | None, point: dict | None = None, workers_n: int | None = None) -> tuple[str, list[Path]]:
    """Today's names versus the point-in-time universe, the latter at no write-off and at each universe.vanishHaircuts level.

    The cases run in parallel processes (one native thread each) when workers_n > 1; every case prints a progress line per 5%.
    """
    if not cfg["universe"]["adjustValidated"]:
        raise prep.MissingInput("--compare needs universe.adjustValidated true (see --validate-adjust)")
    cases = [("today's names", "today", 0.0)] + [(f"pit write-off {int(h * 100)}%", "pit", h) for h in [0.0, *cfg["universe"]["vanishHaircuts"]]]
    jobs = [(cfg, point or {}, start, end, label, mode, h) for label, mode, h in cases]
    n = min(workers_n or cfg["compute"]["workers"], len(jobs))
    print(f"compare: {len(jobs)} cases on {n} process(es); a progress line is printed per 5% of each case", flush=True)
    got: dict[str, tuple[dict, Path]] = {}
    if n <= 1:
        for job in jobs:
            label, rep, path = _run_case(job)
            got[label] = (rep, path)
    else:
        workers.limit_threads()
        with ProcessPoolExecutor(n, initializer=workers.init_worker) as pool:
            for fut in as_completed([pool.submit(_run_case, job) for job in jobs]):
                label, rep, path = fut.result()
                got[label] = (rep, path)
    rows = [(label, got[label][0]) for label, _, _ in cases]
    paths = [got[label][1] for label, _, _ in cases]
    lines = [f"{'universe':<28}{'postTaxCagr':>12}{'maxDD':>9}{'sharpe':>8}{'exits':>7}{'writtenOffInr':>15}{'restarts':>9}{'traded':>8}{'dead':>6}"]
    for name, r in rows:
        lines.append(f"{name:<28}{_fmt(r['objectives']['postTaxCagr']):>12}{_fmt(r['objectives']['maxDrawdown']):>9}{_fmt(r['postTax'].get('sharpe')):>8}"
                     f"{r['vanished']['exits']:>7}{r['vanished']['writtenOffInr']:>15,.0f}{len(r['ladder']['restarts']):>9}{r['tradedNames']['count']:>8}{r['tradedNames']['laterStoppedTrading']:>6}")
    if any(r["trades"]["fills"] == 0 for _, r in rows):
        lines.append("WARNING: a case made 0 fills. At Rs 1 lakh the live sizing minimums stop every order, so the figures above are cash. "
                     "Re-run with e.g. --set sizing.minNewOrderInr=3000 --set sizing.minAdjustmentInr=1500")
    return "\n".join(lines), paths


def _fmt(x) -> str:
    return "-" if x is None else f"{x:.3f}"


def _cfg(args) -> dict:
    cfg = config.load()
    if args.no_auto_restart:
        cfg["ladder"] = {"autoRestart": {"enabled": False, "afterSessions": 1}}
    if args.capital is not None:
        cfg["capital"] = {**cfg["capital"], "inr": args.capital}
        config.validate(cfg)
    return cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backtest.run")
    parser.add_argument("--check", action="store_true", help="validate config only (no network, no writes)")
    parser.add_argument("--single", action="store_true", help="one judge run; writes backtest/data/runs/run_*.json")
    parser.add_argument("--compare", action="store_true", help="today's names versus the point-in-time universe, with 0% / 50% / 100% write-off of vanished names")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="parameter from params.json, e.g. --set sizing.minNewOrderInr=3000 (repeatable); the live config at Rs 1 lakh cannot trade")
    parser.add_argument("--workers", type=int, help="--compare: parallel processes (default compute.workers in backtest.json, at most one per case; 1 = one after another)")
    parser.add_argument("--no-auto-restart", action="store_true", help="keep the live ladder: a flat-lock is never restarted (default: ladder.autoRestart in backtest.json)")
    parser.add_argument("--capital", type=float, metavar="INR", help="starting capital in rupees, 100000 (1 lakh) or more (default: capital.inr in backtest.json)")
    parser.add_argument("--start", help="first simulated day (default: window.start, else the first known regime)")
    parser.add_argument("--end", help="last simulated day (default: window.end, else the end of the data)")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    try:
        if args.compare:
            table, paths = compare(_cfg(args), args.start, args.end, world.parse_set(args.set), args.workers)
            print(table + "\n" + "\n".join(f"run: wrote {p}" for p in paths))
            return 0
        print(f"run: wrote {single(_cfg(args), args.start, args.end, world.parse_set(args.set))}")
        return 0
    except prep.MissingInput as e:
        print(f"run: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"run: FAILED: {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
