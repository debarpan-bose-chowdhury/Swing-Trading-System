"""Backtest config: load backtest.json and check it. Live app/config/*.json is read as the base and never written."""

import json
from datetime import date

from app.market.common import safe_path

CONFIG_PATH = "backtest/config/backtest.json"
MIN_PURGE_DAYS = 168  # the longest selector look-back


def _is_date(v) -> bool:
    try:
        date.fromisoformat(str(v))
        return True
    except ValueError:
        return False


def _num(v, lo: float = 0, hi: float | None = None) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= lo and (hi is None or v <= hi)


def validate(cfg: dict) -> None:
    """Raises ValueError naming the first violation."""
    w = cfg["window"]
    if not (all(w[k] is None or _is_date(w[k]) for k in ("start", "end")) and isinstance(w["holdoutYears"], int) and w["holdoutYears"] >= 1):
        raise ValueError("window: start and end null or ISO dates, holdoutYears an integer of 1 or more")
    comp = cfg["capital"]["composition"]
    if not _num(cfg["capital"]["inr"], 1) or abs(sum(comp.values()) - 1.0) > 0.001:
        raise ValueError("capital: inr greater than 0 and composition summing to 1.0")
    if cfg["fill"]["mode"] != "open" or not (isinstance(cfg["fill"]["carryOverDays"], int) and cfg["fill"]["carryOverDays"] >= 0):
        raise ValueError("fill: only mode open is supported; carryOverDays an integer of 0 or more")
    if any(cfg["fill"]["realism"].values()):
        raise ValueError("fill.realism: bands, volumeCap, circuitLocks and settlementLag are not implemented yet; keep them false")
    sched = cfg["tax"]["schedule"]
    froms = [r["from"] for r in sched]
    if not sched or froms != sorted(set(froms)) or not all(_is_date(d) for d in froms):
        raise ValueError("tax.schedule: rows with ascending, unique ISO 'from' dates")
    if not all(_num(r[k]) for r in sched for k in ("stcgPct", "ltcgPct", "ltcgExemptionInr", "cessPct")):
        raise ValueError("tax.schedule: rates, exemption and cess must be numbers of 0 or more")
    wf = cfg["walkforward"]
    if wf["type"] not in ("rolling", "anchored") or not all(isinstance(wf[k], int) and wf[k] >= 1 for k in ("trainYears", "testYears", "stepYears")):
        raise ValueError("walkforward: type rolling or anchored; train, test and step years integers of 1 or more")
    if not (isinstance(wf["purgeDays"], int) and wf["purgeDays"] >= MIN_PURGE_DAYS):
        raise ValueError(f"walkforward.purgeDays must be an integer of at least {MIN_PURGE_DAYS}")
    g = cfg["gate"]
    if not (_num(g["pboMax"], 0, 1) and _num(g["dsrMin"], 0, 1) and _num(g["oosIsMin"], 0, 1) and _num(g["neighbourhoodShare"], 0, 1) and _num(g["neighbourhoodTolerance"], 0, 1)):
        raise ValueError("gate: pboMax, dsrMin, oosIsMin, neighbourhoodShare and neighbourhoodTolerance must be in [0, 1]")
    for name, spans in cfg["stress"].items():
        if not spans or not all(len(s) == 2 and _is_date(s[0]) and _is_date(s[1]) and s[0] <= s[1] for s in spans):
            raise ValueError(f"stress.{name}: a list of [start, end] ISO date pairs, start not after end")
    sv = cfg["surv"]
    values = [*sv["circuit"].values(), *sv["thin"].values()]
    if sv["proxy"] and not all(_num(v) and v > 0 for v in values):
        raise ValueError("surv.proxy is on but a threshold is unset (null): fill circuit and thin from `python -m backtest.surv_proxy --calibrate`")
    if not all(v is None or _num(v) for v in values):
        raise ValueError("surv: thresholds are null or numbers")
    if not all(isinstance(cfg["overrides"].get(k), dict) for k in ("risk", "analyst")):
        raise ValueError("overrides: risk and analyst must be objects (in-memory edits applied over app/config/*.json)")
    pr = cfg["prep"]
    if not (_is_date(pr["dividendsFrom"]) and _num(pr["bigMovePct"], 0, 1) and _num(pr["dividendStepMin"], 0, 1) and _num(pr["dividendTolerance"], 0, 1)):
        raise ValueError("prep: dividendsFrom an ISO date; bigMovePct, dividendStepMin, dividendTolerance in [0, 1]")
    bh = cfg["bhav"]
    cols = {"symbol", "series", "open", "high", "low", "close", "prevClose", "volume", "value", "isin"}
    if not (_is_date(bh["from"]) and bh["seriesKeep"] and all(isinstance(x, str) for x in bh["seriesKeep"])
            and _is_date(bh["formats"]["legacy"]["until"]) and _is_date(bh["formats"]["udiff"]["from"])
            and bh["formats"]["legacy"]["until"] < bh["formats"]["udiff"]["from"]
            and all(set(f["columns"]) == cols and "{" in f["url"] for f in bh["formats"].values())
            and all(_num(bh["crosscheck"][k], 0, 1) for k in ("closeTolerance", "volumeTolerance"))
            and _num(bh["client"]["gapSeconds"]) and isinstance(bh["client"]["abortAfterFailures"], int) and bh["client"]["abortAfterFailures"] >= 1
            and set(bh["probeDays"]) == set(bh["formats"]) and all(days and all(_is_date(d) for d in days) for days in bh["probeDays"].values())):
        raise ValueError("bhav: from/probe dates ISO, seriesKeep non-empty, legacy.until before udiff.from, both formats map every column, tolerances in [0, 1]")
    un = cfg["universe"]
    ly, ad = un["symbolChange"]["layout"], un["adjust"]
    if not (un["mode"] in ("today", "pit") and isinstance(un["rankWindow"], int) and un["rankWindow"] >= 20 and isinstance(un["rankMinObs"], int)
            and 1 <= un["rankMinObs"] <= un["rankWindow"] and isinstance(un["scopeTop"], int) and un["scopeTop"] >= 1
            and _num(ad["minMove"], 0.05, 0.9) and _num(ad["niceTolerance"], 0, 0.1) and _num(ad["volumeTolerance"], 0, 0.9)
            and _num(ad["tightTolerance"], 0, 0.1) and _num(ad["crashSpike"], 1, 100) and isinstance(ad["volumeWindow"], int) and ad["volumeWindow"] >= 5 and isinstance(ad["minPost"], int) and 1 <= ad["minPost"] <= ad["volumeWindow"]
            and all(_num(h, 0, 1) for h in un["vanishHaircuts"]) and isinstance(un["excludePattern"], str) and isinstance(un["adjustValidated"], bool)
            and (ly is None or (set(ly["fromEnd"]) == {"old", "new", "date"} and isinstance(ly["dateFormat"], str)))):
        raise ValueError("universe: mode today/pit, rankWindow >= 20, 1 <= rankMinObs <= rankWindow, scopeTop >= 1, adjust settings in range, haircuts in [0, 1], "
                         "excludePattern a regex, symbolChange.layout null or {fromEnd: {old, new, date}, dateFormat}")
    c = cfg["compute"]
    if not (isinstance(c["workers"], int) and 1 <= c["workers"] <= 8 and isinstance(c["seed"], int)):
        raise ValueError("compute: workers an integer from 1 to 8, seed an integer")


def load(path: str = CONFIG_PATH) -> dict:
    cfg = json.loads(safe_path(path).read_text(encoding="utf-8"))
    validate(cfg)
    return cfg
