"""Graded exposure ladder on the time-weighted NAV index: immediate step-down, weekly step-up, flat lock, manual restart."""

from app.risk.common import iso_week


def new_state(asof: str, twr: float) -> dict:
    return {"rung": 0, "peakIndex": twr, "peakDate": asof, "baselineIndex": twr, "flatLocked": False, "flatLockedSince": None,
            "lastRestartFrom": None, "lastReRiskWeek": None, "shadowStartDate": None}


def rung_floor(levels: list[dict], drawdown: float) -> int:
    return max((i for i, lv in enumerate(levels, 1) if drawdown >= lv["drawdownPct"]), default=0)


def max_invested(levels: list[dict], rung: int) -> float:
    return levels[rung - 1]["maxInvestedPct"] if rung else 1.0


def step(state: dict, twr: float, asof: str, cfg: dict, rebalance: bool, regimes: list[str], twr_history: list[float]) -> tuple[dict, dict]:
    """One run of the state machine. regimes: Analyst active regime per weekly row (oldest first); twr_history: previous days' index."""
    s, lad = dict(state), cfg["ladder"]
    levels, rr, top = lad["levels"], lad["reRisk"], len(lad["levels"])
    restart = lad["restartFrom"]
    if s["flatLocked"] and restart and restart <= asof and restart != s["lastRestartFrom"] and restart > (s["flatLockedSince"] or ""):
        s.update(flatLocked=False, flatLockedSince=None, baselineIndex=twr, peakIndex=twr, peakDate=asof, rung=top - 1, lastRestartFrom=restart)
    if twr > s["peakIndex"]:
        s["peakIndex"], s["peakDate"] = twr, asof
    drawdown = round((s["peakIndex"] - twr) / s["peakIndex"], 6)  # a 14.999999999999997% value must not fall on the wrong side of 15%
    floor = rung_floor(levels, drawdown)
    s["rung"] = max(s["rung"], floor)
    window, weeks = rr["navAboveMinOfPreviousDays"], rr["consecutiveWeeks"]
    conditions = (len(regimes) >= weeks and all(r in rr["regimes"] for r in regimes[-weeks:])
                  and len(twr_history) >= window and twr > min(twr_history[-window:]))
    eligible = conditions and s["rung"] > floor and s["rung"] < top and not s["flatLocked"]
    if rebalance and eligible and s["lastReRiskWeek"] != iso_week(asof):
        s["rung"] -= 1
        s["lastReRiskWeek"] = iso_week(asof)
    if s["rung"] == top and not s["flatLocked"]:
        s["flatLocked"], s["flatLockedSince"] = True, asof
    return s, {"drawdownPct": drawdown, "rung": s["rung"], "maxInvestedPct": max_invested(levels, s["rung"]),
               "reRiskEligible": bool(eligible), "flatLocked": s["flatLocked"]}


def caps(cfg: dict, active: str, rung: int) -> dict:
    """Regime cap, ladder cap, the lower of the two, and which one binds (a tie goes to the ladder)."""
    regime = cfg["exposure"]["regimeCap"].get(active, 1.0)
    ladder = max_invested(cfg["ladder"]["levels"], rung)
    return {"regimeCap": regime, "ladderCap": ladder, "finalCap": min(regime, ladder), "reason": "LADDER" if ladder <= regime else "REGIME_CAP"}
