"""Probe (manual): fetch the candidate NSE list pages and print the file locations and column names found, never cookies.

Run `python -m app.risk.probe --check-nse` before go-live and after any NSE site change, then copy what it finds into
`surveillance.sources` in risk.json (url, format, symbolColumn, valueColumn, and filterColumn / filterValues where needed).
"""

import argparse
import csv
import io
import json
import re
import sys

from app.metadata.data_source import NseClient
from app.risk.common import load_config
from app.risk.surveillance import NSE_HOME

# UNVERIFIED candidate pages (NSE blocks scripted requests and no file location was confirmed in the research pass).
CANDIDATES = {
    "asm": "https://www.nseindia.com/regulations/additional-surveillance-measure-asm",
    "gsm": "https://www.nseindia.com/regulations/graded-surveillance-measure-gsm",
    "t2t": "https://www.nseindia.com/market-data/securities-available-for-trading",
    "bands": "https://www.nseindia.com/products-services/equity-market-price-band",
}
LINK = re.compile(r"""["'(]([^"'()\s]+\.(?:csv|json|xlsx?|zip))""", re.I)


def describe(name: str, url: str, payload: bytes) -> list[str]:
    """What a fetched page or file shows: JSON keys, CSV header, or the data links an HTML page mentions."""
    text = payload.decode("utf-8-sig", errors="replace")
    head = text.lstrip()[:1]
    if head in "{[":
        try:
            data = json.loads(text)
            rows = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), [])
            return [f"{name}: JSON {'list' if isinstance(data, list) else 'object keys ' + ', '.join(map(str, data))}"] + (
                [f"{name}: first row columns: {', '.join(map(str, rows[0]))}"] if rows and isinstance(rows[0], dict) else [])
        except ValueError:
            pass
    if head == "<":
        links = sorted(set(LINK.findall(text)))
        return [f"{name}: HTML page, {len(links)} data link(s)"] + [f"{name}:   {link}" for link in links[:20]]
    header = next(csv.reader(io.StringIO(text)), [])
    return [f"{name}: CSV columns: {', '.join(c.strip() for c in header)}"]


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="app.risk.probe")
    parser.add_argument("--check-nse", action="store_true", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--url", action="append", default=[], help="an extra page or file to describe (repeatable)")
    args = parser.parse_args(argv)
    if args.check:
        try:
            load_config("probe")
        except Exception as e:
            print(f"probe: check FAILED: {e!r}", file=sys.stderr)
            return 1
        print("probe: check ok")
        return 0
    cfg = load_config("probe")
    client = NseClient({"maxRetries": cfg["surveillance"]["maxRetries"], "backoffSeconds": cfg["surveillance"]["backoffSeconds"]})
    try:
        client.get(NSE_HOME)  # picks up the session cookies (never printed)
    except Exception as e:
        print(f"NSE home page not reachable: {e!r}")
        return 1
    targets = dict(CANDIDATES) | {f"url{i}": u for i, u in enumerate(args.url, 1)}
    for name, url in targets.items():
        try:
            print(*describe(name, url, client.get(url)), sep="\n")
        except Exception as e:
            print(f"{name}: failed {url}: {type(e).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
