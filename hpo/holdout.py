"""The one-shot holdout. The last `window.holdoutYears` stay locked for the whole search; this module is the only door.

Rules, all enforced here and in the backtest's own guard (`api.holdout_guard`, the marker file hpo/data/holdout.marker):
  - only a candidate whose gate passed and whose exploit audit is clean is scored (a look at the holdout is spent once);
  - the pass criterion is written before the look (at gate time, hash stored in gate.json) and the holdout refuses to run on a criterion that changed;
  - one parameter set, ever: a second set raises HoldoutRead (exit 3), and a candidate scored once is never scored again (holdout.json);
  - a global lock (holdout.lock) keeps two processes from racing for the look; the marker is taken before anything is simulated, so a crash still uses it.
With about two years the standard error of a Sharpe ratio is about 0.85: the criterion detects catastrophic failure, nothing finer.
"""

import hashlib
import json
import time
from pathlib import Path

import pandas as pd

from backtest import api
from hpo.errors import Failed, Refusal
from hpo.status import RunLock, read_json, write_json
from hpo.viz.charts import clean

CRITERION_FILE, RESULT_FILE = "criterion.json", "holdout.json"


def criterion(ev: dict, cfg: dict) -> dict:
    """The pre-registered pass criterion of a candidate (computed from its pre-holdout evidence only)."""
    c = cfg["constraints"]
    pre = -ev["candidate"]["metrics"]["maxDrawdown"]
    return {"version": 1, "candidate": ev["candidate"]["trialId"], "minCagr": 0.0, "maxDepth": round(min(-c["maxDrawdown"], 1.5 * pre), 6),
            "minFillsPerYear": c["minFillsPerFoldYear"], "maxCagrBelowDefault": 0.02,
            "note": "post-tax CAGR above zero, drawdown within 1.5x the pre-holdout depth and the cap, enough fills per year, and not more than 2 pp below the live default: a catastrophic-failure test (a 2-year Sharpe has a standard error of about 0.85)"}


def sha_of(crit: dict) -> str:
    return hashlib.sha256(json.dumps(crit, sort_keys=True).encode()).hexdigest()[:16]


def evaluate(crit: dict, cand: dict, default: dict) -> dict:
    """Candidate and default holdout summaries ({"metrics", "fills", "years", "depth"}) against the criterion."""
    cagr = cand["metrics"].get("cagr")
    dcagr = default["metrics"].get("cagr") or 0.0
    per_year = cand["fills"] / cand["years"] if cand["years"] else 0.0
    checks = {"cagrPositive": {"passed": cagr is not None and cagr > crit["minCagr"], "value": cagr, "limit": crit["minCagr"]},
              "depthWithinLimit": {"passed": cand["depth"] <= crit["maxDepth"], "value": cand["depth"], "limit": crit["maxDepth"]},
              "fillsPerYear": {"passed": per_year >= crit["minFillsPerYear"], "value": per_year, "limit": crit["minFillsPerYear"]},
              "notWorseThanDefault": {"passed": cagr is not None and cagr >= dcagr - crit["maxCagrBelowDefault"], "value": None if cagr is None else cagr - dcagr, "limit": -crit["maxCagrBelowDefault"]}}
    return {"passed": all(v["passed"] for v in checks.values()), "checks": checks}


def score(study, pool, cfg: dict, trial_id: str, rec: dict) -> dict:
    """Score the holdout once for the candidate `rec` (a trials.jsonl record) and write candidates/<id>/holdout.json."""
    data = Path(cfg["paths"]["data"])
    folder = data / "candidates" / trial_id
    gate = read_json(folder / "gate.json")
    if gate is None:
        raise Refusal(f"no gate report for {trial_id}: run `gate --candidate {trial_id}` first")
    if not gate["passed"] or not gate["audit"]["passed"]:
        why = ", ".join(gate["failed"] + [f"audit:{x}" for x in gate["audit"]["flagged"]])
        raise Refusal(f"the holdout is spent only on a candidate whose gate passed and whose exploit audit is clean (open: {why})")
    crit = read_json(folder / CRITERION_FILE)
    if crit is None or sha_of(crit) != gate.get("criterionSha"):
        raise Refusal("the pass criterion is missing or changed since the gate registered it: the holdout will not run against a criterion fixed after the fact")
    if (folder / RESULT_FILE).exists():
        raise Refusal(f"the holdout was already scored for {trial_id} (once only)")
    space = study.space
    with RunLock(data / "holdout.lock", cfg["compute"]["lockStaleHours"]):
        job = {"holdout": True, "values": space.complete(rec["values"]), "default": space.complete(space.defaults), "marker": str(data / "holdout.marker"), "paramsKey": rec["pointKey"]}
        try:
            res = pool.submit(job).result()
        except api.HoldoutRead as e:
            raise Refusal(str(e)) from e
        except Exception as e:  # noqa: BLE001  the look is already used: record that, never allow a second one
            write_json(folder / RESULT_FILE, {"status": "error", "error": f"{type(e).__name__}: {e}", "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            raise Failed(f"the holdout run failed after the look was taken ({e}); it is recorded and will not be repeated") from e
    verdict = evaluate(crit, res["candidate"], res["default"])
    out = {"status": "scored", "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "window": res["window"], "criterionSha": gate["criterionSha"], "passed": verdict["passed"], "checks": verdict["checks"],
           "candidate": {k: res["candidate"][k] for k in ("metrics", "fills", "years", "depth")}, "default": {k: res["default"][k] for k in ("metrics", "fills", "years", "depth")}}
    write_json(folder / RESULT_FILE, clean(out))
    pd.DataFrame({"candidate": res["candidate"]["returns"], "default": res["default"]["returns"]}).rename_axis("date").reset_index().to_parquet(folder / "holdout_returns.parquet", index=False)
    return out
