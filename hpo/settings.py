"""hpo.json: budgets, caps, thresholds and paths. Validated at load: an unknown key or an out-of-range value raises (as backtest/config.py does)."""

import json
from pathlib import Path

CONFIG_PATH = "hpo/config/hpo.json"
MIN_CAPITAL_INR = 100000  # same floor as backtest/config.py

SECTIONS = {
    "paths": {"data", "register", "schema", "extraBounds", "studies"},
    "capital": {"inr"},
    "universe": {"selection", "writeOff", "stressWriteOffs", "finalCheck"},
    "objectives": {"cagr", "drawdown", "hvReference"},
    "constraints": {"maxDrawdown", "minFills", "minFillsPerFoldYear", "minAvgExposure", "abortDrawdown", "abortNoFillYears"},
    "space": {"compositionFloor", "offsetShare", "variants"},
    "ledger": {"effectiveNCap", "clusterRhoMax", "defaultEffectiveRatio"},
    "robust": {"top", "neighbours", "quantile", "intStep", "floatRel", "cliffPp", "stress"},
    "gate": {"pboMax", "dsrMin", "oosFloorCagr", "oosIsMin", "spaP", "cagrRel", "cagrAbs", "ddAbs", "ulcerRel", "neighbourShare"},
    "sensitivity": {"keepShare", "minImportance", "maxActive"},
    "shadow": {"weeks", "trackingGapPp"},
    "compute": {"workers", "targetsCache", "failShareAbort", "failWindow", "seed", "lockStaleHours", "liveRefreshSeconds"},
    "optuna": {"version", "storage"},
}


def _num(v, lo: float = 0, hi: float | None = None) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= lo and (hi is None or v <= hi)


def _int(v, lo: int = 0, hi: int | None = None) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= lo and (hi is None or v <= hi)


def validate(cfg: dict) -> None:
    """Raises ValueError naming the first violation."""
    if set(cfg) != set(SECTIONS):
        raise ValueError(f"hpo.json: sections must be exactly {sorted(SECTIONS)} (got {sorted(set(cfg) ^ set(SECTIONS))} different)")
    for name, keys in SECTIONS.items():
        if set(cfg[name]) != keys:
            raise ValueError(f"hpo.json: {name} must have exactly the keys {sorted(keys)}")
    if not (cfg["capital"]["inr"] is None or _num(cfg["capital"]["inr"], MIN_CAPITAL_INR)):
        raise ValueError(f"capital.inr: null (use backtest.json) or Rs {MIN_CAPITAL_INR:,} (1 lakh) or more")
    u = cfg["universe"]
    if not (u["selection"] in ("today", "pit") and u["finalCheck"] in ("today", "pit") and _num(u["writeOff"], 0, 1) and all(_num(x, 0, 1) for x in u["stressWriteOffs"])):
        raise ValueError("universe: selection and finalCheck today or pit, writeOff and stressWriteOffs in [0, 1]")
    o = cfg["objectives"]
    if not (o["cagr"].get("fold") == "cvar" and _num(o["cagr"].get("worstShare"), 0.01, 1) and o["drawdown"] == "span"):
        raise ValueError("objectives: cagr {fold: cvar, worstShare in (0, 1]} and drawdown 'span'")
    hv = o["hvReference"]
    if not (isinstance(hv, list) and len(hv) == 2 and all(_num(x, -1, 1) for x in hv)):
        raise ValueError("objectives.hvReference: [post-tax CAGR, drawdown depth], both within [-1, 1]")
    c = cfg["constraints"]
    if not (_num(-c["maxDrawdown"], 0, 1) and _int(c["minFills"], 0) and _num(c["minFillsPerFoldYear"], 0) and _num(c["minAvgExposure"], 0, 1)
            and _num(-c["abortDrawdown"], 0, 1) and _num(c["abortNoFillYears"], 0) and c["abortDrawdown"] <= c["maxDrawdown"]):
        raise ValueError("constraints: drawdowns negative fractions with abortDrawdown not above maxDrawdown, counts and exposure non-negative")
    s = cfg["space"]
    if not (_num(s["compositionFloor"], 0, 0.33) and _num(s["offsetShare"], 0, 1) and _int(s["variants"], 0, 20)):
        raise ValueError("space: compositionFloor in [0, 0.33], offsetShare in [0, 1], variants an integer from 0 to 20")
    lg = cfg["ledger"]
    if not (_int(lg["effectiveNCap"], 1) and _num(lg["clusterRhoMax"], 0, 1) and _num(lg["defaultEffectiveRatio"], 0.001, 1)):
        raise ValueError("ledger: effectiveNCap an integer of 1 or more, clusterRhoMax in [0, 1], defaultEffectiveRatio in (0, 1]")
    r = cfg["robust"]
    if not (_int(r["top"], 1) and _int(r["neighbours"], 1) and _num(r["quantile"], 0, 1) and _int(r["intStep"], 1) and _num(r["floatRel"], 0, 1) and _num(r["cliffPp"], 0, 1)
            and set(r["stress"]) == {"slippageMult", "chargesMult", "writeOff", "delayDays", "dropNames"}):
        raise ValueError("robust: top and neighbours integers of 1 or more, quantile and floatRel in [0, 1], stress with its five keys")
    g = cfg["gate"]
    if not all(_num(g[k], 0) for k in SECTIONS["gate"]):
        raise ValueError("gate: every limit a number of 0 or more")
    sn = cfg["sensitivity"]
    if not (_num(sn["keepShare"], 0, 1) and _num(sn["minImportance"], 0, 1) and _int(sn["maxActive"], 1)):
        raise ValueError("sensitivity: keepShare and minImportance in [0, 1], maxActive an integer of 1 or more")
    sh = cfg["shadow"]
    if not (_int(sh["weeks"], 1) and _num(sh["trackingGapPp"], 0)):
        raise ValueError("shadow: weeks an integer of 1 or more, trackingGapPp a number of 0 or more")
    cp = cfg["compute"]
    if not (_int(cp["workers"], 1, 8) and _int(cp["targetsCache"], 1) and _num(cp["failShareAbort"], 0, 1) and _int(cp["failWindow"], 1) and _int(cp["seed"], 0)
            and _num(cp["lockStaleHours"], 0) and _int(cp["liveRefreshSeconds"], 1)):
        raise ValueError("compute: workers 1 to 8, targetsCache and failWindow integers of 1 or more, failShareAbort in [0, 1], seed an integer")
    if cfg["optuna"]["storage"] != "journal" or not str(cfg["optuna"]["version"]).startswith("5.0"):
        raise ValueError("optuna: storage 'journal' and version 5.0.* (the adapter is written against Optuna 5.0)")


def load(path: str = CONFIG_PATH) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    validate(cfg)
    return cfg


def data_dir(cfg: dict) -> Path:
    return Path(cfg["paths"]["data"])


def bt_overrides(cfg: dict, mode: str | None = None) -> dict:
    """backtest.json overrides every hpo world is built with: the universe mode and, when hpo.json capital.inr is set, the starting capital."""
    out = {"universe": {"mode": mode or cfg["universe"]["selection"]}}
    if cfg["capital"]["inr"] is not None:
        out["capital"] = {"inr": cfg["capital"]["inr"]}
    return out
