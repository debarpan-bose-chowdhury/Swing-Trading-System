"""Default-parity fingerprint of a run (Parameter Exposure S1): sha256 digests of the NAV rows, fills, signals and weekly targets.

  python -m backtest.golden [--years 2] [--set KEY=VALUE ...]

Run it on your real data before and after a change that must be behaviour-preserving, then compare the two JSON outputs. The same
fingerprint function backs tests/backtest/test_golden.py, which holds the digests of the synthetic fixture.
"""

import argparse
import hashlib
import json
import sys

import pandas as pd

from backtest import api, config, prep, replay, workers, world


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def fingerprint(r: replay.Result) -> dict:
    sigs = [{k: v for k, v in s.items() if k != "generatedAt"} for s in r.signals]
    # lineterminator pinned: pandas ends lines with os.linesep, so on Windows the same data would hash differently from the digests recorded on Linux
    return {"nav": digest(r.nav.to_csv(index=False, lineterminator="\n")), "fills": digest(r.fills.to_csv(index=False, lineterminator="\n")),
            "signals": digest(json.dumps(sigs, sort_keys=True, default=str)),
            "targets": digest(json.dumps(r.targets, sort_keys=True, default=str)),
            "rows": len(r.nav), "fillCount": len(r.fills)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backtest.golden")
    parser.add_argument("--years", type=float, default=2.0, help="simulated years from the common start (default 2, the bench window)")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="params.json overrides, e.g. --set sizing.minNewOrderInr=3000")
    args = parser.parse_args(argv)
    try:
        workers.limit_threads()
        cfg = config.load()
        w = world.World.build(cfg)
        win = api.windows(w)
        w = world.with_point(w, world.parse_set(args.set))
        end = min(str(pd.Timestamp(win.start) + pd.Timedelta(days=int(args.years * 365.25)))[:10], win.tuning_end)
        r = replay.simulate(w.data, w.targets, w.risk, win.start, end, cfg["capital"]["inr"], w.surveillance, keep_signals=True,
                            carry_over_days=config.carry_over_days(cfg, w.risk), dividends=w.dividends, restart_after=config.restart_after(cfg))
        print(json.dumps({"window": [win.start, end], **fingerprint(r)}, indent=1, sort_keys=True))
        return 0
    except prep.MissingInput as e:
        print(f"golden: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"golden: FAILED: {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
