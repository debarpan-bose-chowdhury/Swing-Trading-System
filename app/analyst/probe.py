"""Probe: log in to Angel One and list the field names (and value types, never values) each read endpoint returns.

Run by hand before go-live and after any Angel One change: `python -m app.analyst.probe --check-broker`.
It confirms the field names `broker.EXPECTED` and the Ledger parsers rely on. It writes nothing and sends no email.
"""

import argparse
import logging
import sys

from app.analyst import secrets
from app.analyst.broker import BROKER_SECRETS, EXPECTED, Broker, BrokerError, LoginFailed
from app.analyst.common import check_stage, load_config


def describe(rows) -> dict[str, set]:
    """field -> set of value type names across the rows (a funds response is one dict)."""
    out: dict[str, set] = {}
    for row in [rows] if isinstance(rows, dict) else rows:
        for key, value in row.items():
            out.setdefault(key, set()).add(type(value).__name__)
    return out


def probe(broker: Broker, out=print) -> int:
    """Returns the number of problems found (endpoint failures and missing expected fields)."""
    problems = 0
    for name, fetch in (("holdings", broker.holdings), ("positions", broker.positions), ("tradebook", broker.tradebook), ("funds", broker.funds)):
        try:
            rows = fetch()
        except BrokerError as e:
            out(f"{name}: FAILED {e.code}")
            problems += 1
            continue
        fields = describe(rows)
        count = 1 if isinstance(rows, dict) else len(rows)
        out(f"{name}: {count} row(s)")
        for field in sorted(fields):
            out(f"  {field}: {'/'.join(sorted(fields[field]))}")
        if not fields:
            out("  no rows, so the field names cannot be confirmed now; run again on a day with data")
        elif missing := [f for f in EXPECTED[name] if f not in fields]:
            out(f"  MISSING expected fields: {', '.join(missing)}")
            problems += 1
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.analyst.probe")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--check-broker", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.check:
        return check_stage("probe")
    if not args.check_broker:
        parser.error("nothing to do: pass --check-broker")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log = logging.getLogger("analyst.probe")
    try:
        broker = Broker(load_config(), secrets.load(BROKER_SECRETS), log)
        broker.login()
    except (ValueError, LoginFailed) as e:
        print(f"probe: FAILED {e}", file=sys.stderr)
        return 1
    try:
        problems = probe(broker)
    finally:
        broker.logout()
    print("probe: ok" if not problems else f"probe: {problems} problem(s); fix the field mapping before scheduling the Ledger")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
