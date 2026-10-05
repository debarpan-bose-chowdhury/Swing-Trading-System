"""The parameter register (Parameter Exposure S1): every number of the system, classified once, in doc/parameter_register.csv.

  python -m backtest.register --write   regenerate doc/parameter_register.csv from the shipped configs and the code scan
  python -m backtest.register --check   fail when the file is stale or a numeric literal in a return-affecting module is unregistered

The classification lives here (RULES for config keys, CODE for fixed literals), so a reviewer changes a class in one place and the CSV
follows. tests/backtest/test_register.py runs the same checks on every pytest run: it is the guard against new hidden numbers.

Rows: one per JSON leaf of the shipped configs (kind int/float/bool/str/list) and one per distinct numeric literal (file, value) of the
scanned modules that is not a config key (kind code). The scan skips 0, +-1, 2, the digits argument of round(), and the bodies of the
validate functions (their bounds belong to the config keys they check). The modules scanned are the return-affecting ones; broker,
ledger, probe, secrets, journal, surveil and surveillance are operational and left out.
"""

import ast
import csv
import fnmatch
import io
import json
import sys
from pathlib import Path

COLUMNS = ["path", "file", "current", "kind", "bounds", "scale", "class", "group", "affects", "parity_test", "note"]
CLASSES = ("tunable", "risk-limit", "model-input", "regulatory", "design", "structural", "operational")
REGISTER = Path("doc/parameter_register.csv")
CONFIGS = {"risk": "app/config/risk.json", "analyst": "app/config/analyst.json", "backtest": "backtest/config/backtest.json",
           "config": "app/config/config.json", "market": "app/config/market.json"}
SCAN_FILES = [
    "app/analyst/selector.py", "app/analyst/regime.py", "app/analyst/costs.py", "app/analyst/signals.py",
    "app/risk/sizer.py", "app/risk/ladder.py", "app/risk/stops.py", "app/risk/monitor.py", "app/risk/shadow.py", "app/risk/tax.py",
    "app/risk/nav.py", "app/risk/evaluator.py", "app/risk/run.py", "app/risk/common.py", "app/analyst/common.py",
    "backtest/replay.py", "backtest/fills.py", "backtest/targets.py", "backtest/tax.py",
]
TRIVIAL = {0, 1, -1, 2}

# --- config keys: (pattern on "<config>:<dotted path>", class, group, affects, bounds, scale, parity_test, note). First match wins. -----------
Rule = tuple[str, str, str, str, str, str, str, str]


def R(pattern, cls, group, affects="both", bounds="", scale="", test="", note="") -> Rule:
    return (pattern, cls, group, affects, bounds, scale, test, note)


RULES: list[Rule] = [
    # risk.json
    R("risk:placeholders", "operational", "run-gate", "live"),
    R("risk:capital.*", "operational", "capital", "live", note="cash-flow file and reconcile field"),
    R("risk:sizing.riskPerPositionPct", "tunable", "sizing", scale="linear"),
    R("risk:sizing.nameCapPct.*", "tunable", "sizing", scale="linear", note="bounded by the policy maximum of the bucket"),
    R("risk:sizing.minNewOrderInr", "tunable", "sizing", scale="log", note="scales with capital: at Rs 1 lakh the live value makes 0 trades"),
    R("risk:sizing.minAdjustmentInr", "tunable", "sizing", scale="log", note="constraint: not above minNewOrderInr"),
    R("risk:sizing.cashBufferPct", "risk-limit", "sizing"),
    R("risk:sizing.noTradeBand.relative", "tunable", "sizing", bounds="proposed 0.1..0.5", scale="linear"),
    R("risk:sizing.noTradeBand.absolutePct", "tunable", "sizing", bounds="proposed 0.0..0.05", scale="linear", note="no effect below floorPct (H7)"),
    R("risk:sizing.noTradeBand.floorPct", "tunable", "sizing", bounds="proposed 0.0..0.05", scale="linear", test="tests/app/test_risk_sizer.py::ResizeTests::test_h7_*", note="H7: was the hidden 0.025 in sizer.plan_sells"),
    R("risk:sizing.countSaleProceeds", "model-input", "sizing", note="bool: whether same-day sale proceeds fund buys (settlement assumption)"),
    R("risk:stops.atrPeriod", "tunable", "stops", scale="int"),
    R("risk:stops.atrMultiplier", "tunable", "stops", scale="linear"),
    R("risk:stops.atrMethod", "structural", "stops", test="tests/app/test_risk_common.py::test_dead_stop_keys_must_hold_the_only_implemented_values", note="validated: only sma is implemented"),
    R("risk:stops.priceBasis", "structural", "stops", test="tests/app/test_risk_common.py::test_dead_stop_keys_must_hold_the_only_implemented_values", note="validated: only AdjClose is implemented"),
    R("risk:stops.clampPct.*", "tunable", "stops", scale="linear", note="[lo, hi] per bucket, 0 < lo < hi < 1"),
    R("risk:stops.bucketFallback", "structural", "stops"),
    R("risk:cooldown.stopTradingDays", "tunable", "cooldown", scale="int"),
    R("risk:cooldown.reentryAboveStopClose", "tunable", "cooldown", bounds="bool", note="bool"),
    R("risk:ladder.levels.*", "risk-limit", "ladder", note="drawdown rung or invested cap: unfreeze per study with a report warning"),
    R("risk:ladder.reRisk.regimes", "structural", "ladder"),
    R("risk:ladder.reRisk.consecutiveWeeks", "tunable", "ladder", bounds="proposed 1..4", scale="int"),
    R("risk:ladder.reRisk.navAboveMinOfPreviousDays", "tunable", "ladder", bounds="proposed 10..40", scale="int"),
    R("risk:ladder.reRisk.rungsPerWeek", "tunable", "ladder", bounds="proposed 1..2", scale="int", test="tests/app/test_risk_ladder.py::ExposedLadderNumbers", note="H8: was one rung per week"),
    R("risk:ladder.restartFrom", "operational", "ladder", "live", note="the owner's manual restart date"),
    R("risk:ladder.restartRungOffset", "tunable", "ladder", bounds="proposed 1..2", scale="int", test="tests/app/test_risk_ladder.py::ExposedLadderNumbers", note="H8: a restart resumes top - offset"),
    R("risk:exposure.regimeCap.*", "tunable", "exposure", bounds="0..1", scale="linear", note="inert today (all 1.0); search range [0, 1] to be confirmed by the owner"),
    R("risk:heat.capPct", "risk-limit", "sizing"),
    R("risk:liquidity.advDays", "tunable", "liquidity", bounds="proposed 10..60", scale="int"),
    R("risk:liquidity.maxParticipationPct.*", "tunable", "liquidity", bounds="proposed 0.002..0.02", scale="log"),
    R("risk:surveillance.sources.*", "regulatory", "surveillance", "live"),
    R("risk:surveillance.blockEntryBandPct", "regulatory", "surveillance", "live"),
    R("risk:surveillance.staleExitDays", "regulatory", "surveillance", "live"),
    R("risk:surveillance.exitOn", "regulatory", "surveillance", "live"),
    R("risk:surveillance.*", "operational", "surveillance", "live"),
    R("risk:tax.deferral.windowDays", "tunable", "tax", bounds="proposed 14..56", scale="int"),
    R("risk:tax.deferral.minGainPct", "tunable", "tax", bounds="proposed 0.05..0.25", scale="linear"),
    R("risk:tax.deferral.*", "regulatory", "tax", note="the 12-month rule's mechanics"),
    R("risk:tax.rates.*", "regulatory", "tax", "live"),
    R("risk:gate.*", "operational", "run-gate", "live"),
    R("risk:shadow.enabled", "operational", "shadow", "live"),
    R("risk:shadow.carryOverDays", "model-input", "fill", test="tests/app/test_risk_shadow.py::CarryOverDays", note="H9: backtest fill.carryOverDays inherits it when null"),
    R("risk:evaluator.tradingDaysPerYear", "design", "metrics", test="tests/app/test_risk_evaluator.py::TradingDaysPerYear", note="H11: annualisation only; the trailing252 window keeps its length"),
    R("risk:evaluator.riskFreeRatePct", "design", "metrics"),
    R("risk:evaluator.*", "operational", "metrics", "live"),
    R("risk:signals.*", "operational", "schedule", "live"),
    R("risk:paths.*", "operational", "paths", "live"),
    R("risk:lock.*", "operational", "lock", "live"),
    R("risk:mail.*", "operational", "mail", "live"),
    # analyst.json
    R("analyst:placeholders", "operational", "run-gate", "live"),
    R("analyst:broker.*", "operational", "broker", "live"),
    R("analyst:capital.floatingCapitalInr", "design", "capital"),
    R("analyst:capital.*", "operational", "capital", "live"),
    R("analyst:limits.*", "risk-limit", "limits", "live", note="live-monitoring limits"),
    R("analyst:composition.*", "tunable", "composition", bounds="simplex over the buckets", scale="simplex", note="a weight of 0 disables a bucket; must sum to 1"),
    R("analyst:rebalance.*", "structural", "rebalance"),
    R("analyst:regime.index", "structural", "regime"),
    R("analyst:regime.minRows", "structural", "regime", note="must be >= max(smaSlow, momentumDays) + unknownExtra + 1"),
    R("analyst:regime.momentumThreshold", "tunable", "regime", bounds="proposed -0.05..0.05", scale="linear", test="tests/app/test_analyst_regime.py::ExposedRegimeNumbers", note="H5"),
    R("analyst:regime.unknownExtra", "tunable", "regime", bounds="proposed 0..20", scale="int", test="tests/app/test_analyst_regime.py::ExposedRegimeNumbers", note="H6: warm-up length"),
    R("analyst:regime.*", "tunable", "regime", scale="int", note="regime stage, narrow bounds"),
    R("analyst:strategies.*", "tunable", "selector", scale="int", note="per regime and bucket (params.json shares them across buckets)"),
    R("analyst:selector.maxStaleTradingDays", "operational", "gates", "live"),
    R("analyst:selector.maxBucketFileAgeDays", "operational", "gates", "live"),
    R("analyst:selector.maxMissingShare", "operational", "gates", "live"),
    R("analyst:selector.momentumSkipDays", "tunable", "selector", bounds="proposed 0..21", scale="int"),
    R("analyst:selector.minMomentum", "tunable", "selector", bounds="proposed 0.0..0.1", scale="linear", test="tests/app/test_analyst_selector.py::ExposedSelectorNumbers", note="H2: candidate filter, all regimes"),
    R("analyst:selector.trendBuffer", "tunable", "selector", bounds="proposed 0.0..0.05", scale="linear", test="tests/app/test_analyst_selector.py::ExposedSelectorNumbers", note="H3: price > trend x (1 + buffer)"),
    R("analyst:selector.liquidity.statistic", "structural", "selector"),
    R("analyst:selector.liquidity.*", "tunable", "selector", scale="int"),
    R("analyst:selector.bearScore.windows.*", "tunable", "selector", bounds="proposed +-50% of the default", scale="int", test="tests/app/test_analyst_selector.py::ExposedSelectorNumbers", note="H1: BEAR score windows; rows needed = longest + 7"),
    R("analyst:selector.bearScore.confirmThreshold", "tunable", "selector", bounds="proposed 0.0..0.05", scale="linear", test="tests/app/test_analyst_selector.py::ExposedSelectorNumbers", note="H4: BEAR tier confirmation"),
    R("analyst:selector.bearScore.*", "tunable", "selector", scale="linear", note="score weight"),
    R("analyst:ledger.*", "operational", "ledger", "live"),
    R("analyst:signals.*", "operational", "schedule", "live"),
    R("analyst:costs.asOf", "operational", "costs", "live"),
    R("analyst:costs.*", "model-input", "costs", note="frozen; used for stress only"),
    R("analyst:paths.*", "operational", "paths", "live"),
    R("analyst:lock.*", "operational", "lock", "live"),
    R("analyst:mail.*", "operational", "mail", "live"),
    # backtest.json
    R("backtest:window.*", "design", "window", "backtest"),
    R("backtest:broker.*", "design", "broker", "backtest"),
    R("backtest:capital.*", "design", "capital", "backtest"),
    R("backtest:fill.carryOverDays", "model-input", "fill", "backtest", test="tests/backtest/test_param_exposure.py::CarryOver", note="H9: null reads risk.json shadow.carryOverDays"),
    R("backtest:fill.*", "model-input", "fill", "backtest"),
    R("backtest:tax.schedule.*", "regulatory", "tax", "backtest"),
    R("backtest:tax.confirmed", "operational", "tax", "backtest"),
    R("backtest:ladder.autoRestart.*", "model-input", "ladder", "backtest", note="simulates the owner's manual restart"),
    R("backtest:walkforward.purgeDays", "design", "walkforward", "backtest", test="tests/backtest/test_param_exposure.py::PurgeFloor", note="H10: floor is config.MIN_PURGE_DAYS; the effective purge is also lifted to the longest look-back"),
    R("backtest:walkforward.*", "design", "walkforward", "backtest"),
    R("backtest:gate.*", "design", "gate", "backtest"),
    R("backtest:stress.*", "design", "stress", "backtest"),
    R("backtest:surv.*", "model-input", "surveillance", "backtest"),
    R("backtest:prep.*", "design", "prep", "backtest"),
    R("backtest:bhav.*", "operational", "bhav", "backtest"),
    R("backtest:universe.*", "design", "universe", "backtest"),
    R("backtest:compute.*", "design", "compute", "backtest"),
    R("backtest:paths.*", "operational", "paths", "backtest"),
    # metadata config.json and market.json
    R("config:filter.*", "structural", "metadata", "both", note="2T / 500B / 100B market caps, topN, minInceptionDays"),
    R("config:*", "operational", "metadata", "live"),
    R("market:*", "operational", "market", "live"),
]

# --- numeric literals in code that are not config keys: (file, value) -> (class, group, note). Matching is by file and value. ------------------
CODE: dict[tuple[str, float], tuple[str, str, str]] = {
    ("app/analyst/selector.py", 7): ("structural", "selector", "BEAR_ROW_BUFFER: rows kept beyond the longest BEAR window"),
    ("app/analyst/selector.py", 1e7): ("structural", "selector", "CRORE: rupees per crore (unit)"),
    ("app/analyst/selector.py", 20): ("structural", "selector", "BEAR_WINDOWS default of selector.bearScore.windows (H1)"),
    ("app/analyst/selector.py", 63): ("structural", "selector", "BEAR_WINDOWS default of selector.bearScore.windows (H1)"),
    ("app/analyst/selector.py", 5): ("operational", "selector", "rows_needed: data-load padding"),
    ("app/analyst/regime.py", 9): ("structural", "regime", "UNKNOWN_EXTRA: default of regime.unknownExtra (H6)"),
    ("app/analyst/regime.py", 50): ("structural", "regime", "DEFAULT_WINDOWS: default of regime.smaFast"),
    ("app/analyst/regime.py", 200): ("structural", "regime", "DEFAULT_WINDOWS: default of regime.smaSlow"),
    ("app/analyst/regime.py", 63): ("structural", "regime", "DEFAULT_WINDOWS: default of regime.momentumDays"),
    ("app/analyst/regime.py", 5): ("structural", "regime", "weekday < 5: Monday to Friday"),
    ("app/analyst/regime.py", 7): ("structural", "regime", "days per week in the pending-regime countdown"),
    ("app/analyst/costs.py", 10000): ("model-input", "costs", "basis points per unit (unit constant)"),
    ("app/analyst/signals.py", 5): ("structural", "schedule", "weekday >= 5: weekend"),
    ("app/analyst/signals.py", 6): ("structural", "schedule", "weekday 6: Sunday"),
    ("app/analyst/signals.py", 4): ("operational", "schedule", "year slice of an ISO date"),
    ("app/risk/monitor.py", 3): ("structural", "monitor", "sell-reason priority rank"),
    ("app/risk/monitor.py", 4): ("structural", "monitor", "sell-reason priority rank"),
    ("app/risk/monitor.py", 5): ("structural", "monitor", "sell-reason priority rank"),
    ("app/risk/monitor.py", 6): ("structural", "monitor", "BUY_PRIORITY rank"),
    ("app/risk/monitor.py", 1e-6): ("operational", "monitor", "rupee tolerance on the cap excess"),
    ("app/risk/monitor.py", 100): ("operational", "monitor", "percent to fraction (lower-circuit warning)"),
    ("app/risk/monitor.py", 0.1): ("operational", "monitor", "lower-circuit warning offset in percent (warning only)"),
    ("app/risk/monitor.py", 60): ("operational", "monitor", "history padding before an entry date, days"),
    ("app/risk/monitor.py", 10): ("operational", "monitor", "trend-MA history padding, days"),
    ("app/risk/monitor.py", 1.6): ("operational", "monitor", "calendar days per trading day for the trend-MA history load"),
    ("app/risk/tax.py", 56): ("operational", "tax", "NEAR_DAYS: how far ahead the report lists positions nearing 12 months"),
    ("app/risk/tax.py", 4): ("regulatory", "tax", "financial year starts in April"),
    ("app/risk/tax.py", 5): ("operational", "tax", "ISO date slice"),
    ("app/risk/tax.py", 7): ("operational", "tax", "ISO date slice"),
    ("app/risk/tax.py", 100): ("operational", "tax", "two-digit year in the financial-year label"),
    ("app/risk/nav.py", 0.15): ("operational", "nav", "JUMP: NAV jump warning threshold (warning only)"),
    ("app/risk/nav.py", 6): ("operational", "nav", "NAV column slice"),
    ("app/risk/evaluator.py", 100.0): ("operational", "metrics", "XIRR bisection bound; percent scaling of the ulcer index"),
    ("app/risk/evaluator.py", 200): ("operational", "metrics", "XIRR bisection iterations"),
    ("app/risk/evaluator.py", 0.99): ("operational", "metrics", "XIRR bisection bound and p99 report"),
    ("app/risk/evaluator.py", 3): ("operational", "metrics", "minimum observations for a benchmark regression"),
    ("app/risk/evaluator.py", 0.05): ("operational", "metrics", "CVaR tail share"),
    ("app/risk/evaluator.py", 7): ("operational", "metrics", "report window of the signal-followed rate, days"),
    ("app/risk/evaluator.py", 4): ("operational", "metrics", "year slice of an ISO date"),
    ("app/risk/evaluator.py", 0.95): ("operational", "metrics", "p95 of the slippage report"),
    ("app/risk/evaluator.py", 8): ("operational", "metrics", "date slice of a signal file name"),
    ("app/risk/evaluator.py", 365): ("operational", "metrics", "days per year of the XIRR exponent"),
    ("app/risk/run.py", 4): ("operational", "run", "weekday 4 (Friday) and slice lengths"),
    ("app/risk/run.py", 3): ("operational", "run", "days from Friday to the next session"),
    ("app/risk/run.py", 30): ("operational", "run", "backup retention, days"),
    ("app/risk/run.py", 90): ("operational", "run", "surveillance file retention, days"),
    ("app/risk/run.py", 52): ("operational", "run", "cooldown file prune, weeks (spec: not exposed on purpose)"),
    ("app/risk/run.py", 8): ("operational", "run", "date slice of a targets file name"),
    ("app/analyst/common.py", 3600): ("operational", "lock", "seconds per hour (lock age)"),
    ("app/analyst/common.py", 3): ("operational", "run", "exit code 3: gate not met"),
    ("app/risk/common.py", 60): ("operational", "run", "RETRY_MINUTES"),
    ("app/risk/common.py", 3600): ("operational", "lock", "seconds per hour (lock age)"),
    ("app/risk/common.py", 28): ("regulatory", "tax", "a 29 February entry's anniversary falls on 28 February"),
    ("app/risk/common.py", 3): ("operational", "run", "exit code 3: gate not met"),
    ("app/risk/common.py", 120): ("operational", "run", "default history padding, days"),
    ("app/risk/common.py", 0.025): ("tunable", "sizing", "NO_TRADE_FLOOR_PCT: default of sizing.noTradeBand.floorPct (H7)"),
    ("app/risk/common.py", 7): ("model-input", "fill", "CARRY_OVER_DAYS: default of shadow.carryOverDays (H9)"),
    ("app/risk/common.py", 252): ("design", "metrics", "TRADING_DAYS_PER_YEAR: default of evaluator.tradingDaysPerYear (H11)"),
    ("backtest/replay.py", 100000.0): ("design", "capital", "simulate() default capital; callers pass backtest.json capital.inr"),
    ("backtest/replay.py", 100): ("operational", "progress", "progress callback granularity, percent"),
    ("backtest/replay.py", 21): ("operational", "replay", "simulated run clock (21:00 IST) of the day's decide() call; no return effect"),
    ("backtest/targets.py", 4): ("operational", "cache", "default size of the targets cache"),
    ("backtest/tax.py", 4): ("regulatory", "tax", "financial year ends 31 March (year slice)"),
    ("backtest/tax.py", 3): ("operational", "tax", "lot tuple index"),
}


def _kind(v) -> str:
    return "null" if v is None else "bool" if isinstance(v, bool) else "int" if isinstance(v, int) else "float" if isinstance(v, float) else "list" if isinstance(v, list) else "str"


def leaves(doc, prefix: str = ""):
    """(dotted path, value) for every leaf; a list of objects is indexed (levels.0.drawdownPct), a list of scalars is one leaf."""
    if isinstance(doc, dict):
        for k, v in doc.items():
            yield from leaves(v, f"{prefix}{k}.")
    elif isinstance(doc, list) and doc and all(isinstance(x, dict) for x in doc):
        for i, v in enumerate(doc):
            yield from leaves(v, f"{prefix}{i}.")
    else:
        yield prefix[:-1], doc


def _params_bounds(doc: dict) -> dict[tuple[str, str], str]:
    """(target, path) -> 'low..high step s' for every path params.json already searches (the wildcard '*' expands to a glob)."""
    out = {}
    for p in doc["params"]:
        for path in p["paths"]:
            out[(p["target"], path)] = f"{p['low']}..{p['high']} step {p['step']} (params.json {p['key']})"
    return out


def _match(rule_pattern: str, key: str) -> bool:
    return fnmatch.fnmatchcase(key, rule_pattern)


def config_rows(root: Path = Path(".")) -> list[dict]:
    pbounds = _params_bounds(json.loads((root / "backtest/config/params.json").read_text(encoding="utf-8")))
    rows = []
    for name, file in CONFIGS.items():
        for path, value in leaves(json.loads((root / file).read_text(encoding="utf-8"))):
            rule = next((r for r in RULES if _match(r[0], f"{name}:{path}")), None)
            if rule is None:
                raise ValueError(f"no register rule for {name}:{path}: add one to backtest/register.py RULES")
            _, cls, group, affects, bounds, scale, test, note = rule
            found = next((b for (target, p), b in pbounds.items() if target == name and fnmatch.fnmatchcase(path, p.replace("*", "*"))), "")
            rows.append({"path": f"{name}:{path}", "file": file, "current": json.dumps(value), "kind": _kind(value), "bounds": found or bounds,
                         "scale": scale, "class": cls, "group": group, "affects": affects, "parity_test": test, "note": note})
    return rows


def _skipped(tree: ast.AST) -> set[int]:
    """ids of constants not to flag: the digits argument of round() and anything inside a validate function."""
    skip = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and (n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", None)) == "round" and len(n.args) >= 2:
            skip.add(id(n.args[1]))
        if isinstance(n, ast.FunctionDef) and n.name == "validate":
            skip.update(id(c) for c in ast.walk(n))
    return skip


def literals(root: Path = Path(".")) -> dict[tuple[str, float], int]:
    """{(file, value): first line} of every numeric literal the scan flags."""
    found = {}
    for file in SCAN_FILES:
        tree = ast.parse((root / file).read_text(encoding="utf-8"))
        skip = _skipped(tree)
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool) and id(n) not in skip and n.value not in TRIVIAL:
                found.setdefault((file, n.value), n.lineno)
    return found


def code_rows(root: Path = Path(".")) -> list[dict]:
    rows, found = [], literals(root)
    missing = [f"{file}:{line} {value!r}" for (file, value), line in found.items() if (file, value) not in CODE]
    if missing:
        raise ValueError(f"unregistered numeric literals (make each a config key, or list it in backtest/register.py CODE): {missing}")
    for (file, value), _line in sorted(found.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        cls, group, note = CODE[(file, value)]
        rows.append({"path": f"code:{file}:{value!r}", "file": file, "current": json.dumps(value), "kind": "code", "bounds": "", "scale": "",
                     "class": cls, "group": group, "affects": "both" if file.startswith("app/") else "backtest", "parity_test": "", "note": note})
    return rows


def unused_code_entries(root: Path = Path(".")) -> list[tuple[str, float]]:
    """CODE entries whose literal no longer exists in the scanned modules (a stale register row)."""
    return sorted(set(CODE) - set(literals(root)))


def build(root: Path = Path(".")) -> str:
    rows = config_rows(root) + code_rows(root)
    bad = [r["path"] for r in rows if r["class"] not in CLASSES]
    if bad:
        raise ValueError(f"unknown class in {bad[:3]}")
    out = io.StringIO()
    w = csv.DictWriter(out, COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def main(argv: list[str]) -> int:
    if argv not in (["--write"], ["--check"]):
        print(__doc__)
        return 1
    text, stale = build(), unused_code_entries()
    if argv == ["--write"]:
        REGISTER.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {REGISTER} ({text.count(chr(10)) - 1} rows)")
        return 1 if stale else 0
    current = REGISTER.read_text(encoding="utf-8") if REGISTER.exists() else ""
    if current != text or stale:
        print(f"register is stale (run: python -m backtest.register --write); unused code entries: {stale}", file=sys.stderr)
        return 1
    print("register ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
