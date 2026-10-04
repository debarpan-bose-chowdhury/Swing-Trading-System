"""Parity of the backtest with the live app on YOUR data. Read-only: nothing under app/ is written, no network.

  python -m backtest.parity --check                 which inputs exist (stored live targets and signals, regime history)
  python -m backtest.parity --targets               backtest targets vs the app's own replay (and vs stored live target files)
  python -m backtest.parity --signals               stored live signals vs the backtest's regime, rebalance rule and exposure caps
  python -m backtest.parity --targets --signals     both; writes backtest/data/parity/report.json

--targets
  replay  For sampled past rebalance dates the app's own `python -m app.analyst.signals --as-of <date>` logic is run in this process
          (read-only) and compared with backtest.targets.Targets.build. Recent dates share the live universe, so any difference there is
          a real logic or data difference. Older dates can differ legitimately because the app replays with today's bucket files and its
          data-quality gates; the replay may also refuse a date (bucket-file age, missing data): that is reported, not counted as a mismatch.
  stored  Every stored app/data/analyst/targets/targets_<date>.json is compared with the backtest's targets for the same date: regime,
          composition, tickers and ranks must agree; numbers are compared with a tolerance because Yahoo revises adjusted closes.

--signals
  Each stored app/data/risk/signals/signals_<asOf>.json is checked for: the regime against the backtest's regime for that day, the
  weekly flag against the rebalance rule, the execution date, and the exposure caps against risk.json. The portfolio-dependent parts
  (book, NAV, ladder state, actions) depend on your real account and are NOT compared.

Exit codes: 0 ok, 1 a parity mismatch or failure, 3 inputs missing.
"""

import argparse
import contextlib
import io
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from app.analyst import common as analyst_common
from app.analyst import regime, signals
from app.market.common import IST, atomic
from app.market.tradingcal import Calendar
from app.risk import common as risk_common
from app.risk.common import iso
from app.risk.run import execution_date
from backtest import config, prep
from backtest.targets import PICK_KEYS, Targets

LOG = logging.getLogger("backtest.parity")
REPLAY_TOL = 1e-9  # the same code on the same files: floating-point noise only
STORED_TOL = 0.02  # a stored file was computed on the data of its day; adjusted closes have been revised since


def view(t: dict) -> dict:
    """The part of a targets dict that decides trades (what decide() reads), in a comparable shape."""
    return {"regime": {k: t["regime"].get(k) for k in ("raw", "active")}, "composition": t.get("composition"),
            "buckets": {b: {"strategy": e.get("strategy"), "selected": [{k: p[k] for k in PICK_KEYS if k in p} for p in e.get("selected", [])]}
                        for b, e in t["buckets"].items()}}


def _close(a, b, tol: float) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(a - b) <= tol * max(abs(a), abs(b), 1e-12)
    return a == b


def diff(mine: dict, live: dict, tol: float, numbers_fail: bool = True) -> tuple[list[str], float]:
    """(differences, largest relative difference among the compared numbers). With numbers_fail False a number beyond tol is not a difference."""
    a, b, out, worst = view(mine), view(live), [], 0.0
    if a["regime"] != b["regime"]:
        out.append(f"regime {a['regime']} vs {b['regime']}")
    if a["composition"] != b["composition"]:
        out.append(f"composition {a['composition']} vs {b['composition']}")
    for bucket in sorted(set(a["buckets"]) | set(b["buckets"])):
        x, y = a["buckets"].get(bucket), b["buckets"].get(bucket)
        if x is None or y is None:
            out.append(f"{bucket}: only in {'backtest' if y is None else 'live'}")
            continue
        if x["strategy"] != y["strategy"]:
            out.append(f"{bucket}: strategy {x['strategy']} vs {y['strategy']}")
        tx, ty = [p["ticker"] for p in x["selected"]], [p["ticker"] for p in y["selected"]]
        if tx != ty:
            out.append(f"{bucket}: picks differ; only backtest {sorted(set(tx) - set(ty))}, only live {sorted(set(ty) - set(tx))}"
                       + ("" if set(tx) != set(ty) else f"; same names, different order {tx} vs {ty}"))
            continue
        for p, q in zip(x["selected"], y["selected"], strict=True):
            for k in PICK_KEYS:
                if k in p and k in q and not _close(p[k], q[k], tol):
                    if isinstance(p[k], (int, float)) and isinstance(q[k], (int, float)):
                        worst = max(worst, abs(p[k] - q[k]) / max(abs(p[k]), abs(q[k]), 1e-12))
                    if numbers_fail or not (isinstance(p[k], (int, float)) and isinstance(q[k], (int, float))):
                        out.append(f"{bucket} {p['ticker']} {k}: {p[k]} vs {q[k]}")
    return out, worst


def sample_dates(dates: list[str], recent: int, spread: int) -> list[tuple[str, str]]:
    """[(date, 'recent' | 'history')]: the newest `recent` dates, plus `spread` evenly spaced older ones."""
    recent_part = dates[-recent:] if recent else []
    older = dates[:len(dates) - len(recent_part)]
    picks = [older[round(i * (len(older) - 1) / max(spread - 1, 1))] for i in range(min(spread, len(older)))] if spread and older else []
    return [(d, "history") for d in dict.fromkeys(picks)] + [(d, "recent") for d in recent_part]


def live_replay(acfg: dict, asof: str) -> dict:
    """The app's own read-only replay of the rebalance date on or before asof, as the targets dict it would print."""
    out = io.StringIO()
    now = datetime.now(IST)
    with contextlib.redirect_stdout(out):
        signals.run(acfg, now, LOG, analyst_common.Report("signals"), argparse.Namespace(force=False, as_of=asof, check=False))
    return json.loads(out.getvalue())


def check_targets(cfg: dict, acfg: dict, tg: Targets, recent: int, spread: int, replay=live_replay) -> dict:
    known = [d for d, a in zip(tg.dates, tg.active, strict=True) if a != "Unknown"]
    rows = []
    for d, kind in sample_dates(known, recent, spread):
        mine = tg.build(d)
        try:
            live = replay(acfg, d)
        except Exception as e:  # the app refuses (Gate, stale bucket file, missing data): say so and move on
            rows.append({"date": d, "kind": kind, "source": "replay", "status": "LIVE_REFUSED", "detail": f"{type(e).__name__}: {str(e)[:160]}"})
            continue
        diffs, worst = diff(mine, live, REPLAY_TOL)
        rows.append({"date": d, "kind": kind, "source": "replay", "status": "DIFF" if diffs else "MATCH", "detail": diffs[:8], "worstRelDiff": worst})
    folder = Path(acfg["paths"]["analyst"]) / "targets"
    for f in sorted(folder.glob("targets_*.json")) if folder.exists() else []:
        d = f.stem.removeprefix("targets_")
        live = json.loads(f.read_text(encoding="utf-8"))
        mine = tg.build(d)
        if mine is None:
            rows.append({"date": d, "kind": "stored", "source": "stored", "status": "NO_BACKTEST_ROW", "detail": "the backtest regime history has no row for this date"})
            continue
        diffs, worst = diff(mine, live, STORED_TOL, numbers_fail=False)
        rows.append({"date": d, "kind": "stored", "source": "stored", "status": "DIFF" if diffs else "MATCH", "detail": diffs[:8], "worstRelDiff": worst})
    return {"rows": rows}


def check_signals(cfg: dict, rcfg: dict, tg: Targets, cal: Calendar) -> dict:
    folder = Path(rcfg["paths"]["risk"]) / "signals"
    rows = []
    for f in sorted(folder.glob("signals_*.json")) if folder.exists() else []:
        s = json.loads(f.read_text(encoding="utf-8"))
        d, problems = s["asOf"], []
        mine, _ = tg.regimes(d)
        if s["regime"].get("active") != mine["active"]:
            problems.append(f"regime active {s['regime'].get('active')} live vs {mine['active']} backtest")
        if s["regime"].get("raw") != mine["raw"]:
            problems.append(f"regime raw {s['regime'].get('raw')} live vs {mine['raw']} backtest")
        is_rebalance = regime.live_rebalance_date(cal, date.fromisoformat(d)) == date.fromisoformat(d)
        if s["weekly"]["included"] and not is_rebalance:
            problems.append(f"live included weekly targets on {d}, which the rebalance rule does not call a rebalance day")
        ex = iso(execution_date(cal, date.fromisoformat(d)))
        if s["executionDate"] != ex:
            problems.append(f"executionDate {s['executionDate']} live vs {ex} backtest")
        cap = rcfg["exposure"]["regimeCap"].get(s["regime"]["active"], 1.0)
        if s["exposure"]["regimeCap"] != cap:
            problems.append(f"regimeCap {s['exposure']['regimeCap']} live vs {cap} in risk.json")
        if abs(s["exposure"]["finalCap"] - min(s["exposure"]["regimeCap"], s["exposure"]["ladderCap"])) > 1e-9:
            problems.append("finalCap is not the lower of regimeCap and ladderCap")
        rows.append({"date": d, "status": "DIFF" if problems else "MATCH", "detail": problems})
    return {"rows": rows}


def summarize(name: str, part: dict) -> list[str]:
    rows, lines = part["rows"], []
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    lines.append(f"{name}: {len(rows)} checked; " + (", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "nothing to compare"))
    for r in rows:
        if r["status"] != "MATCH":
            tag = f"{r.get('kind', '')}/{r.get('source', '')}".strip("/")
            detail = r["detail"] if isinstance(r["detail"], str) else "; ".join(r["detail"])
            lines.append(f"  {r['date']} {r['status']} ({tag}) {detail}"[:400])
    return lines


def inputs(cfg: dict, acfg: dict, rcfg: dict) -> dict:
    t, s = Path(acfg["paths"]["analyst"]) / "targets", Path(rcfg["paths"]["risk"]) / "signals"
    return {"storedTargets": len(list(t.glob("targets_*.json"))) if t.exists() else 0, "storedSignals": len(list(s.glob("signals_*.json"))) if s.exists() else 0,
            "regimeHistory": (Path(acfg["paths"]["analyst"]) / "regime" / "regime_history.csv").exists()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backtest.parity")
    parser.add_argument("--check", action="store_true", help="list which inputs exist; no comparison")
    parser.add_argument("--targets", action="store_true", help="backtest targets vs the app's replay and vs stored live target files")
    parser.add_argument("--signals", action="store_true", help="stored live signals vs the backtest's regime, rebalance rule and exposure caps")
    parser.add_argument("--recent", type=int, default=8, help="--targets: newest rebalance dates replayed (the live universe still applies)")
    parser.add_argument("--spread", type=int, default=8, help="--targets: older dates, evenly spaced over the history")
    args = parser.parse_args(argv)
    try:
        cfg = config.load()
        acfg, rcfg = analyst_common.load_config(), risk_common.load_config("run")
        found = inputs(cfg, acfg, rcfg)
        print(f"parity: stored live targets {found['storedTargets']}, stored live signals {found['storedSignals']}, regime history file {'yes' if found['regimeHistory'] else 'no'}")
        if args.check or not (args.targets or args.signals):
            return 0
        cfg["universe"]["mode"] = "today"  # the app replays with its own bucket files, which the static universe mirrors
        data = prep.load_pit(cfg)
        tg = Targets(data, acfg)  # the live analyst config, no backtest overrides
        report: dict = {"generatedAt": datetime.now(IST).isoformat(timespec="seconds"), "inputs": found}
        lines: list[str] = []
        if args.targets:
            report["targets"] = check_targets(cfg, acfg, tg, args.recent, args.spread)
            lines += summarize("targets", report["targets"])
        if args.signals:
            report["signals"] = check_signals(cfg, rcfg, tg, Calendar(rcfg["paths"]["calendar"]))
            lines += summarize("signals", report["signals"])
        out = Path(cfg["paths"]["data"]) / "parity" / "report.json"
        atomic(out, lambda tmp: tmp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8"))
        print("\n".join(lines))
        bad = sum(1 for part in ("targets", "signals") for r in report.get(part, {}).get("rows", []) if r["status"] in ("DIFF", "NO_BACKTEST_ROW"))
        print(f"parity: wrote {out}; " + ("MISMATCHES FOUND" if bad else "no mismatches (refused replays are listed above, not counted)"))
        return 1 if bad else 0
    except prep.MissingInput as e:
        print(f"parity: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"parity: FAILED: {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
