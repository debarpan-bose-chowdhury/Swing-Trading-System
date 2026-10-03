"""Backtest CLI. Run from the repo root: python -m backtest.run --check

Exit codes as in the app: 0 ok, 1 failed, 2 busy, 3 gate not met (run the upstream stage first).
--check validates the config; --single runs the judge once. Walk-forward, trials and the gate arrive in later phases.
"""

import argparse
import json
import sys
from pathlib import Path

from app.market.common import atomic
from backtest import config, prep, replay, report, tax, world


def check() -> int:
    """Config loads and validates, and the app side it builds on loads too. No network, no writes."""
    try:
        cfg = config.load()
        from app.analyst import common as analyst_common, regime
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


def evaluate(cfg: dict, w: world.World, start: str | None, end: str | None, haircut: float = 0.0) -> dict:
    """One judge run on a built world; returns the report. haircut: the write-off on a position whose ticker stopped trading."""
    start = start or cfg["window"]["start"] or w.first_known_regime()
    end = end or cfg["window"]["end"]
    result = replay.simulate(w.data, w.targets, w.risk, start, end, cfg["capital"]["inr"], w.surveillance, carry_over_days=cfg["fill"]["carryOverDays"],
                             dividends=w.dividends, vanish_haircut=haircut)
    if result.nav.empty:
        raise ValueError(f"no simulated days between {start} and {end}")
    pieces = tax.lots(result.fills)
    taxes = tax.assess(pieces, cfg["tax"]["schedule"])
    post = tax.post_tax_curve(result.nav, taxes)
    meta = {"configHash": world.config_hash(cfg, w.risk, w.analyst), "dataHash": w.data.data_hash(), "codeSha": world.code_sha(),
            "entryViewMismatch": tax.entry_view_mismatch(pieces), "missingHistory": w.data.missing()}
    return report.build(result, taxes, post, w.risk, cfg, bool(cfg["surv"]["proxy"]), meta)


def write(cfg: dict, rep: dict, suffix: str = "") -> Path:
    path = Path(cfg["paths"]["data"]) / "runs" / f"run_{rep['window']['start']}_{rep['window']['end']}_{rep['configHash'][:8]}{suffix}.json"
    atomic(path, lambda tmp: tmp.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8"))
    return path


def single(cfg: dict, start: str | None, end: str | None) -> Path:
    """One judge run over [start, end] with the config as it is, written to backtest/data/runs/. Returns the report path."""
    return write(cfg, evaluate(cfg, world.World.build(cfg), start, end))


def compare(cfg: dict, start: str | None, end: str | None) -> tuple[str, list[Path]]:
    """Today's names versus the point-in-time universe, the latter at no write-off and at each universe.vanishHaircuts level."""
    if not cfg["universe"]["adjustValidated"]:
        raise prep.MissingInput("--compare needs universe.adjustValidated true (see --validate-adjust)")
    cases = [("today", "today", 0.0)] + [("pit", "pit", h) for h in [0.0, *cfg["universe"]["vanishHaircuts"]]]
    rows, paths = [], []
    built: dict[str, world.World] = {}
    for label, mode, h in cases:
        c = json.loads(json.dumps(cfg))
        c["universe"]["mode"] = mode
        w = built.get(mode) or built.setdefault(mode, world.World.build(c))
        rep = evaluate(c, w, start, end, h)
        paths.append(write(c, rep, f"_{mode}" + (f"_h{int(h * 100)}" if mode == "pit" else "")))
        rows.append((f"{label} write-off {int(h * 100)}%" if mode == "pit" else "today's names", rep))
    lines = [f"{'universe':<28}{'postTaxCagr':>12}{'maxDD':>9}{'sharpe':>8}{'exits':>7}{'writtenOffInr':>15}"]
    for name, r in rows:
        lines.append(f"{name:<28}{_fmt(r['objectives']['postTaxCagr']):>12}{_fmt(r['objectives']['maxDrawdown']):>9}{_fmt(r['postTax'].get('sharpe')):>8}"
                     f"{r['vanished']['exits']:>7}{r['vanished']['writtenOffInr']:>15,.0f}")
    return "\n".join(lines), paths


def _fmt(x) -> str:
    return "-" if x is None else f"{x:.3f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backtest.run")
    parser.add_argument("--check", action="store_true", help="validate config only (no network, no writes)")
    parser.add_argument("--single", action="store_true", help="one judge run; writes backtest/data/runs/run_*.json")
    parser.add_argument("--compare", action="store_true", help="today's names versus the point-in-time universe, with 0% / 50% / 100% write-off of vanished names")
    parser.add_argument("--start", help="first simulated day (default: window.start, else the first known regime)")
    parser.add_argument("--end", help="last simulated day (default: window.end, else the end of the data)")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    try:
        if args.compare:
            table, paths = compare(config.load(), args.start, args.end)
            print(table + "\n" + "\n".join(f"run: wrote {p}" for p in paths))
            return 0
        print(f"run: wrote {single(config.load(), args.start, args.end)}")
        return 0
    except prep.MissingInput as e:
        print(f"run: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"run: FAILED: {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
