"""Backtest CLI. Run from the repo root: python -m backtest.run --check

Exit codes as in the app: 0 ok, 1 failed, 2 busy, 3 gate not met (run the upstream stage first).
Phase 0 only has --check; later phases add single run, walk-forward and stress.
"""

import argparse
import sys

from backtest import config


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backtest.run")
    parser.add_argument("--check", action="store_true", help="validate config only (no network, no writes)")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    parser.error("only --check exists so far")
    return 1


if __name__ == "__main__":
    sys.exit(main())
