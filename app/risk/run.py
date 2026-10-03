"""Run: gate, NAV, Risk Monitor, Sizer (rebalance days), signal file, shadow portfolio update, housekeeping."""

import logging
import re
import shutil
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

from app.analyst import costs, regime
from app.analyst.ledger import read_fills
from app.analyst.signals import execution_date
from app.market.common import IST, iso
from app.market.store import Store
from app.market.tradingcal import Calendar
from app.risk import ladder, monitor, nav, shadow, sizer, surveil
from app.risk.common import (RETRY_MINUTES, SESSION_FINAL, WEEKDAYS, Context, Gate, Portfolio, Report, add_trading_days, history,
                             load_state, read_json, risk_dir, run_stage, save_state, trading_days_between, write_json)

SCHEMA_VERSION = 1
DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


# --- gate --------------------------------------------------------------------------------------------------------
def final_attempt(cfg: dict, now: datetime, asof: date) -> bool:
    """True for the last scheduled retry (Friday's run ends Monday 08:00, the others next morning 08:00)."""
    end = datetime.combine(asof + timedelta(days=3 if asof.weekday() == 4 else 1), time.fromisoformat(cfg["gate"]["retryUntil"]), IST)
    return now >= end - timedelta(minutes=RETRY_MINUTES)


def targets_deadline(cfg: dict, asof: date) -> datetime:
    day, hhmm = cfg["gate"]["targetsWaitUntil"].split()
    return datetime.combine(asof - timedelta(days=asof.weekday()) + timedelta(days=WEEKDAYS.index(day)), time.fromisoformat(hhmm), IST)


def read_targets(cfg: dict, name: str) -> dict | None:
    t = read_json(Path(cfg["paths"]["analyst"]) / "targets" / name)
    return t if t and t.get("status") == "ok" and t.get("schemaVersion") == 1 else None


def newest_targets(cfg: dict, asof: str) -> dict | None:
    files = sorted(f for f in (Path(cfg["paths"]["analyst"]) / "targets").glob("targets_*.json") if "superseded" not in f.name and f.stem[8:] <= asof)
    return next((t for f in reversed(files) if (t := read_targets(cfg, f.name))), None)


def bucket_symbols(cfg: dict, asof: str) -> dict:
    """bucket -> symbols of its newest storage file dated on or before asof."""
    out = {}
    for b in cfg["buckets"]:
        files = sorted(f for f in Path(cfg["paths"]["upstreamStorage"]).glob(f"{b}_*.csv") if f.stem.rsplit("_", 1)[1] <= asof)
        out[b] = set(pd.read_csv(files[-1], dtype=str, keep_default_na=False)["Symbol"]) - {""} if files else set()
    return out


def index_close(cfg: dict, asof: str) -> float | None:
    """Raw Close of the benchmark index on asof, None when the series has no row for it."""
    df = history(Store(Path(cfg["paths"]["market"]) / "indices", cutoff=""), cfg["evaluator"]["benchmark"].lstrip("^"), asof, asof)
    return float(df.Close.iloc[-1]) if len(df) and df.Date.iloc[-1] == asof else None


def check_gate(cfg: dict, cal: Calendar, asof: str, now: datetime, ctx: Context) -> tuple[dict | None, list[str]]:
    """Raises Gate when data is not ready (retry later). Returns (targets for this rebalance day or None, digest notes)."""
    if not any(h.startswith(f"{asof[:4]}-") for h in cal.holidays):
        raise ValueError(f"nse_calendar.json has no holidays for {asof[:4]}; a holiday would look like a trading day")
    market, notes = Path(cfg["paths"]["market"]), []
    status = read_json(market / "status.json")
    last = (status or {}).get("lastTradingDay")
    if (market / ".lock").exists():
        raise Gate("a Ticker Data run is in progress")
    if not status or status.get("status") not in ("ok", "partial") or (last is not None and last != asof):
        raise Gate(f"Ticker Data has not finished {asof}")
    if index_close(cfg, asof) is None:
        raise Gate(f"the {cfg['evaluator']['benchmark']} series has no row for {asof}")
    led = (read_json(Path(cfg["paths"]["analyst"]) / "analyst_status.json") or {}).get("ledger") or {}
    good = led.get("lastGoodRunDate")
    if led.get("status") not in ("ok", "partial") or not good or trading_days_between(cal, good, asof) > cfg["gate"]["maxLedgerLagTradingDays"]:
        raise Gate(f"the Ledger has not succeeded for {asof} (last good run {good})")
    if led["status"] == "partial":
        ctx.warn("LEDGER_PARTIAL")
    if not ctx.rebalance:
        return None, notes
    T = read_targets(cfg, f"targets_{asof}.json")
    if T is None:
        if now < targets_deadline(cfg, date.fromisoformat(asof)):
            raise Gate(f"targets_{asof}.json is not ready")
        notes.append(f"no targets for week of {asof}; exits only")
    return T, notes


# --- one portfolio -----------------------------------------------------------------------------------------------
def regime_now(cfg: dict, asof: str, T: dict | None) -> tuple[dict, list[str]]:
    """({raw, active}, last Analyst active regimes oldest first) from regime_history.csv; the target file wins for its day."""
    path = Path(cfg["paths"]["analyst"]) / "regime" / "regime_history.csv"
    h = pd.read_csv(path, dtype=str) if path.exists() else pd.DataFrame(columns=["date", "raw_regime", "active_regime"])
    h = h[h.date <= asof]
    cur = {"raw": h.raw_regime.iloc[-1], "active": h.active_regime.iloc[-1]} if len(h) else {"raw": "Unknown", "active": "Unknown"}
    if T:
        cur = {"raw": T["regime"]["raw"], "active": T["regime"]["active"]}
    return cur, list(h.active_regime)


def adv_cap_qty(ctx: Context, p: dict) -> float:
    df = ctx.hist(p["ticker"])
    adv = float((df.Close * df.Volume).tail(ctx.cfg["liquidity"]["advDays"]).median())
    return ctx.cfg["liquidity"]["maxParticipationPct"][p["bucket"]] * adv


def decide(ctx: Context, pf: Portfolio, flows: pd.DataFrame | None, bench: float, regimes: tuple, now: datetime, run_id: str) -> dict:
    """Everything one portfolio produces for asOf, without writing: {signal, nav_row, state, ...}."""
    cfg, asof, T = ctx.cfg, ctx.asof, ctx.targets
    st = load_state(pf.state)
    navs = nav.read_nav(pf.nav_file)
    prior = navs[navs.date < asof]
    prev = prior.iloc[-1] if len(prior) else None
    held, positions = monitor.holdings(ctx, pf, st)
    by = {p["ticker"]: p for p in held}
    flow = nav.external_flow(flows, prev.date, asof) if flows is not None and prev is not None else 0.0
    row, jump = nav.nav_row(asof, float(sum(p["value"] for p in held)), pf.cash, flow, prev)
    if jump:
        ctx.warn("NAV_JUMP")
    value, twr = row["nav"], row["twr_index"]
    cur, history_regimes = regimes
    ls = st["ladder"] or ladder.new_state(asof, twr)
    ls, lad = ladder.step(ls, twr, asof, cfg, ctx.rebalance, history_regimes[-cfg["ladder"]["reRisk"]["consecutiveWeeks"]:], list(prior.twr_index))
    caps = ladder.caps(cfg, cur["active"], lad["rung"])
    detail = ({"drawdownPct": lad["drawdownPct"], "rung": lad["rung"], "finalCap": caps["finalCap"]} if caps["reason"] == "LADDER"
              else {"regime": cur["active"], "finalCap": caps["finalCap"]})

    cands = monitor.new_cands()
    monitor.exits(ctx, held, caps, value, cands, detail)
    tgts, topups, holds = [], {}, []
    if T:
        tgts = sizer.targets(ctx, value, caps)
        holds, deferred, topups = sizer.plan_sells(ctx, held, st, cands, tgts, value)
    else:
        holds, deferred = monitor.deferrals(ctx, held, st, cands, None)
    sells = monitor.resolve(cands, by)
    buys, blocked, fin = sizer.buys(ctx, held, sells, st["cooldown"], tgts, topups, value, pf.cash, caps)

    c, actions = cfg["costs"], []
    for t, s in sells.items():
        p = by[t]
        n = s["qty"] * p["close"]
        if n > adv_cap_qty(ctx, p) > 0:
            ctx.warn(f"LIQUIDITY:{t}")
        actions.append({"ticker": t, "bucket": p["bucket"], "side": "SELL", "kind": s["kind"], "qty": s["qty"], "refPriceInr": round(p["close"], 2),
                        "notionalInr": round(n, 2), "reason": s["reason"], "estChargesInr": round(costs.sell_charges(c, n), 2),
                        "priority": s["priority"], "detail": s["detail"]})
    actions = sorted(actions, key=lambda a: (a["priority"], a["ticker"])) + buys

    cool = st["cooldown"][~st["cooldown"].ticker.isin([a["ticker"] for a in buys if a["kind"] == "ENTRY"])]
    cool = cool[cool.trigger_date >= iso(date.fromisoformat(asof) - timedelta(weeks=52))]
    new = [{"ticker": a["ticker"], "trigger_date": asof, "trigger_adj_close": by[a["ticker"]]["adj"],
            "release_after": iso(add_trading_days(ctx.cal, date.fromisoformat(asof), cfg["cooldown"]["stopTradingDays"]))}
           for a in actions if a["reason"] == "STOP" and a["ticker"] not in set(cool.ticker)]
    cool = pd.concat([cool, pd.DataFrame(new, columns=cool.columns).astype(str)], ignore_index=True) if new else cool

    row |= {"bench_close": bench, "active_regime": cur["active"], "rung": lad["rung"]}
    surv = ctx.surv
    signal = {
        "schemaVersion": SCHEMA_VERSION, "runId": run_id, "generatedAt": datetime.now(IST).isoformat(timespec="seconds"), "status": "ok",
        "asOf": asof, "executionDate": iso(execution_date(ctx.cal, date.fromisoformat(asof))), "executionAt": "open",
        "weekly": {"included": bool(T), "targetsFile": f"targets_{asof}.json" if T else None, **({"rebalanceDate": asof} if T else {})},
        "regime": cur,
        "nav": {"navInr": value, "cashInr": row["cash"], "positionsValueInr": row["positions_value"],
                "investedPct": round(row["positions_value"] / value, 4) if value else 0.0, "twrIndex": twr,
                "peakIndex": ls["peakIndex"], "drawdownPct": lad["drawdownPct"]},
        "ladder": {k: lad[k] for k in ("rung", "maxInvestedPct", "reRiskEligible", "flatLocked")},
        "exposure": {"regimeCap": caps["regimeCap"], "ladderCap": caps["ladderCap"], "finalCap": caps["finalCap"],
                     "heatPct": round(fin["heatPct"], 4), "heatCapPct": cfg["heat"]["capPct"]},
        "surveillance": {"status": surv["status"], "asOf": surv["asOf"]},
        "actions": actions, "holds": holds, "blocked": blocked,
        "positions": [{"ticker": p["ticker"], "bucket": p["bucket"], "qty": p["qty"], "avgCostInr": round(p["avg"], 2), "closeInr": round(p["close"], 2),
                       "stopPrice": round(p["stop"], 2) if p["stop"] is not None else None,
                       "stopDistancePct": round((p["adj"] - p["stop"]) / p["adj"], 4) if p["stop"] is not None else None,
                       "highWaterMark": round(p["hwm"], 2) if p["hwm"] is not None else None, "trackStart": p["track_start"]} for p in held],
        "untracked": ((read_json(Path(cfg["paths"]["analyst"]) / "analyst_status.json") or {}).get("ledger") or {}).get("untracked", []),
        "warnings": list(ctx.warnings),
    }
    return {"signal": signal, "nav_row": row, "state": {"positions": positions, "cooldown": cool, "deferred": deferred, "ladder": ls}}


def commit(pf: Portfolio, out: dict, now: datetime) -> Path:
    """State, then the NAV row, then the signal file last (its presence marks the run as done)."""
    save_state(pf.state, out["state"])
    nav.upsert_nav(pf.nav_file, out["nav_row"])
    path = pf.signals / f"signals_{out['signal']['asOf']}.json"
    if path.exists():
        path.replace(path.with_name(f"{path.stem}.superseded_{now.strftime('%H%M%S')}.json"))
    write_json(path, out["signal"])
    return path


def housekeeping(cfg: dict, asof: str, today: date, log: logging.Logger) -> None:
    """Backup state/, nav/, shadow/; purge old backups, surveillance files and signal files."""
    risk = risk_dir(cfg)
    for name in ("state", "nav", "shadow"):
        if (risk / name).exists():
            shutil.copytree(risk / name, risk / "backup" / iso(today) / name, dirs_exist_ok=True)

    def purge(files, cutoff: str) -> None:
        for f in files:
            m = DATE.search(f.name)
            if m and m.group(1) < cutoff:
                shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)

    purge((risk / "backup").glob("*"), iso(today - timedelta(days=30)))
    purge((risk / "surveillance" / "raw").glob("*"), iso(today - timedelta(days=30)))
    purge((risk / "surveillance").glob("surveillance_*.json"), iso(today - timedelta(days=90)))
    cutoff = iso(date.fromisoformat(asof) - timedelta(weeks=cfg["signals"]["retentionWeeks"]))
    for folder in (risk / "signals", shadow.root(cfg) / "signals"):
        purge(folder.glob("signals_*.json"), cutoff)


# --- stage -------------------------------------------------------------------------------------------------------
def read_book(cfg: dict) -> pd.DataFrame:
    path = Path(cfg["paths"]["analyst"]) / "ledger" / "book.csv"
    cols = ["ticker", "qty", "avg_price", "entry_date", "entry_source"]
    if not path.exists():
        return pd.DataFrame(columns=cols)
    df = pd.read_csv(path, dtype=str, keep_default_na=False).reindex(columns=cols, fill_value="")
    return df.astype({"qty": "int64", "avg_price": "float64"})


def run(cfg: dict, now: datetime, log: logging.Logger, report: Report, args) -> None:
    cal = Calendar(cfg["paths"]["calendar"])
    asof_d = cal.last_final_session(now, SESSION_FINAL)
    asof, risk = iso(asof_d), risk_dir(cfg)
    old = read_json(risk / "signals" / f"signals_{asof}.json")
    if old and old.get("status") == "ok" and not args.force:
        log.info("signals_%s.json already exists; no new bar", asof)
        report.quiet = True
        return
    report.keep = ("lastGoodAsOf",)
    report.block["asOf"] = asof
    status = read_json(risk / "risk_status.json") or {}
    ctx = Context(cfg, asof, cal, Store(cfg["paths"]["market"], cutoff=""), log, bucket_symbols(cfg, asof),
                  last_good=(status.get("run") or {}).get("lastGoodAsOf"), surv=surveil.load(cfg, cal, asof),
                  rebalance=regime.live_rebalance_date(cal, asof_d) == asof_d)
    try:
        T, notes = check_gate(cfg, cal, asof, now, ctx)
        ctx.targets = T
        book = read_book(cfg)
        needed = set(book.ticker) | {x["ticker"] for b in (T or {"buckets": {}})["buckets"].values() for x in b["selected"]}
        missing = [t for t in needed if ctx.hist(t).empty or ctx.hist(t).Date.iloc[-1] != asof]
        if needed and len(missing) / len(needed) > cfg["gate"]["maxMissingShare"]:
            raise Gate(f"{len(missing)} of {len(needed)} tickers have no row for {asof}")
    except Gate as g:
        if not final_attempt(cfg, now, asof_d):
            raise
        report.error = f"no signals for {asof}: {g}"
        return
    flows = nav.read_flows(cfg, iso(now.date()))
    fills = read_fills(Path(cfg["paths"]["analyst"]) / "ledger" / "fills.csv")
    actual = Portfolio("actual", risk / "state", risk / "nav" / "nav_actual.csv", risk / "signals", book, nav.ledger_cash(cfg, flows, fills, asof))
    cur, regimes = regime_now(cfg, asof, T)
    if (newest := T or newest_targets(cfg, asof)):
        ctx.windows = {b: e["strategy"]["stock_trend_ma"] for b, e in newest["buckets"].items() if e.get("strategy")}
    bench = index_close(cfg, asof)
    run_id = f"risk-{now.isoformat(timespec='seconds')}"

    out = decide(ctx, actual, flows, bench, (cur, regimes), now, run_id)
    lines = list(notes)
    shadow_out = None
    if cfg["shadow"]["enabled"]:
        shadow_warn, ctx.warnings = ctx.warnings, []
        if out["state"]["ladder"]["shadowStartDate"] is None:
            shadow.seed(cfg, actual, asof)
            out["state"]["ladder"]["shadowStartDate"] = asof
        spf = shadow.apply(ctx)
        shadow_out = decide(ctx, spf, None, bench, (cur, regimes), now, run_id)
        lines += [f"Shadow warning: {w}" for w in ctx.warnings] + [f"Shadow: {len(shadow_out['signal']['actions'])} signals, rung {shadow_out['signal']['ladder']['rung']}"]
        ctx.warnings = shadow_warn
    if shadow_out:
        commit(spf, shadow_out, now)
    path = commit(actual, out, now)
    sig = out["signal"]
    report.block.update(lastGoodAsOf=asof, signals=len(sig["actions"]), weekly=bool(T), rung=sig["ladder"]["rung"], signalsFile=f"signals/{path.name}")
    try:
        housekeeping(cfg, asof, now.date(), log)
    except OSError as e:
        log.warning("housekeeping failed: %r", e)
        lines.append("Backup/purge failed; see the log")
    report.lines += digest(sig, lines)


def digest(sig: dict, notes: list[str]) -> list[str]:
    n, lad, ex = sig["nav"], sig["ladder"], sig["exposure"]
    reasons = Counter(a["reason"] for a in sig["actions"])
    lines = [f"Gate: passed. asOf {sig['asOf']}, execution {sig['executionDate']} at the open",
             f"NAV Rs {n['navInr']:,.0f} (cash {n['cashInr']:,.0f}), drawdown {n['drawdownPct']:.1%}, ladder rung {lad['rung']}{' FLAT-LOCKED' if lad['flatLocked'] else ''}",
             f"Exposure caps: regime {ex['regimeCap']:.0%}, ladder {ex['ladderCap']:.0%}, final {ex['finalCap']:.0%}; heat {ex['heatPct']:.1%} of {ex['heatCapPct']:.0%}",
             "Signals: " + (", ".join(f"{k} {v}" for k, v in sorted(reasons.items())) or "none")]
    if sig["blocked"]:
        lines.append("Blocked: " + ", ".join(f"{b['ticker']} {b['intent']} {b['reason']}" for b in sig["blocked"]))
    if sig["holds"]:
        lines.append("Deferred drops: " + ", ".join(f"{h['ticker']} until {h['release']}" for h in sig["holds"]))
    if sig["surveillance"]["status"] != "ok":
        lines.append(f"Surveillance list {sig['surveillance']['status']}: new buys blocked (NO_SURVEILLANCE_DATA)")
    if sig["warnings"]:
        lines.append("Warnings: " + ", ".join(sig["warnings"]))
    return lines + notes


if __name__ == "__main__":
    run_stage("run", run)
