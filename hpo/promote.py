"""Promotion: the overlay, the diff and the dossier, written only for a candidate that passed every stage.

`promote` never touches app/config: it writes candidates/<id>/
  overlay.json   the changed values only, as dotted paths for risk.json and analyst.json (apply by hand; bump config_version in your changelog)
  rollback.json  the old values of the same keys (applying it restores the live configuration exactly)
  diff.md        old and new value of every changed key with the register's class and note
  dossier.json / dossier.md   objectives and front position, plateau and stress results, gate report, effective N, exploit audit, holdout result,
                 the shadow limits fixed beforehand, rollback rules, and the honest-edge statement
Accepted only if: the gate passed, the exploit audit is clean, and the one-shot holdout was scored and passed.
"""

import copy
import csv
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from hpo import stages
from hpo.errors import Refusal
from hpo.space import get_path, set_path
from hpo.status import read_json, write_json
from hpo.viz.charts import clean

DERIVED = {"risk": {"buckets", "costs"}, "analyst": set()}  # added in memory by the validators, not keys of the files
HONEST_EDGE = ("HPO is unlikely to yield a statistically demonstrable improvement over sensible defaults on about 13 years of data (the standard error of a 13-year Sharpe ratio is "
               "about 0.28). The realistic value is a configuration that trades at this capital, avoidance of fragile regions, a sensitivity map, and perhaps a lower drawdown at a "
               "similar CAGR. The result is specific to the capital it was found at: re-run when capital changes.")


def flatten(cfg, prefix: str = "") -> dict:
    """Leaves by dotted path; dicts and lists of dicts are walked (the ladder levels), lists of numbers are leaves (a clamp pair)."""
    if isinstance(cfg, dict):
        out = {}
        for k, v in cfg.items():
            out.update(flatten(v, f"{prefix}{k}."))
        return out
    if isinstance(cfg, list) and cfg and all(isinstance(x, dict) for x in cfg):
        out = {}
        for i, v in enumerate(cfg):
            out.update(flatten(v, f"{prefix}{i}."))
        return out
    return {prefix[:-1]: cfg}


def overlay(base_risk: dict, base_analyst: dict, risk: dict, analyst: dict) -> tuple[dict, dict]:
    """(overlay, rollback): the changed keys of risk.json and analyst.json with their new and old values."""
    new, old = {"risk.json": {}, "analyst.json": {}}, {"risk.json": {}, "analyst.json": {}}
    for name, a, b in (("risk", base_risk, risk), ("analyst", base_analyst, analyst)):
        fa, fb = flatten({k: v for k, v in a.items() if k not in DERIVED[name]}), flatten({k: v for k, v in b.items() if k not in DERIVED[name]})
        for path in fb:
            if fa.get(path) != fb[path]:
                new[f"{name}.json"][path], old[f"{name}.json"][path] = fb[path], fa.get(path)
    return new, old


def apply(cfg: dict, changes: dict) -> dict:
    """A copy of a config dict with dotted-path changes written in (the by-hand step, as code, so the rollback can be rehearsed in a test)."""
    out = copy.deepcopy(cfg)
    for path, value in changes.items():
        set_path(out, path.split("."), copy.deepcopy(value))
    return out


def notes(register: str) -> dict:
    with open(register, encoding="utf-8", newline="") as f:
        return {r["path"].replace(":", ".json:", 1): (r["class"], r["note"]) for r in csv.DictReader(f)}


def unmet(folder: Path) -> list[str]:
    """What still stands between a candidate and promotion (empty = accepted)."""
    gate, hold = read_json(folder / "gate.json"), read_json(folder / "holdout.json")
    out = []
    if gate is None:
        return ["gate not run"]
    if not gate["passed"]:
        out.append("gate not met: " + ", ".join(gate["failed"]))
    if not gate["audit"]["passed"]:
        out.append("exploit audit flagged: " + ", ".join(gate["audit"]["flagged"]))
    if hold is None:
        out.append("holdout not scored")
    elif hold.get("status") != "scored":
        out.append("holdout run failed")
    elif not hold["passed"]:
        out.append("holdout criterion not met")
    return out


def p95_depth(returns: pd.Series) -> float:
    """The drawdown depth the backtest exceeded on only 5% of days: the shadow period's rollback line."""
    curve = (1 + returns.fillna(0)).cumprod()
    return float(np.quantile(-(curve / curve.cummax() - 1), 0.95))


def build(study, folder: Path, cfg: dict, register: str) -> dict:
    """Write overlay.json, rollback.json, diff.md and dossier.json/.md for a candidate that passed; raises Refusal otherwise."""
    open_items = unmet(folder)
    if open_items:
        raise Refusal("not promotable: " + "; ".join(open_items))
    ev = json.loads((folder / "evidence.json").read_text(encoding="utf-8"))
    gate, hold = read_json(folder / "gate.json"), read_json(folder / "holdout.json")
    space = study.space
    risk, analyst, _ = space.decode(ev["candidate"]["values"])
    ov, rb = overlay(space.base_risk, space.base_analyst, risk, analyst)
    meta = notes(register)
    rows = []
    for fname in ("risk.json", "analyst.json"):
        for path, new in ov[fname].items():
            cls, note = meta.get(f"{fname.split('.')[0]}.json:{path}", ("", ""))
            if not cls and path.startswith("strategies"):
                cls, note = "tunable", "per regime and bucket"
            rows.append({"file": fname, "path": path, "old": rb[fname][path], "new": new, "class": cls, "note": note})
    rets = pd.read_parquet(folder / "returns.parquet").set_index("date")["candidate"]
    recs = [json.loads(x) for x in (study.trials_path.read_text(encoding="utf-8").splitlines()) if x]
    front = sorted(stages.front_rows(recs), key=lambda r: r["f2"])
    pos = next((i + 1 for i, r in enumerate(front) if r["trial"] == ev["candidate"]["trial"]), None)
    p95 = p95_depth(rets)
    shadow = {"weeks": cfg["shadow"]["weeks"], "trackingGapPp": cfg["shadow"]["trackingGapPp"], "drawdownRollbackDepth": p95,
              "rollbackIf": [f"the shadow portfolio's drawdown passes {p95:.1%} (the backtest's 95th-percentile depth)", f"the cumulative tracking gap between shadow-realised and backtest-replayed results exceeds +-{cfg['shadow']['trackingGapPp']} pp",
                             "an exploit of the simulator is found", "this tests fidelity, not edge"]}
    ledger = type(study.ledger)(study.ledger.path, study.ledger.cap, study.ledger.default_ratio)  # read fresh: the dossier states the count as it stands now
    dossier = {"candidate": ev["candidate"]["trialId"], "study": ev["candidate"]["study"], "trial": ev["candidate"]["trial"], "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "objectives": {"cagrCvarFolds": ev["candidate"]["objectives"][0], "drawdownDepth": ev["candidate"]["objectives"][1], "default": ev["default"]["objectives"], "frontPosition": pos, "frontSize": len(front)},
               "metrics": ev["candidate"]["metrics"], "defaultMetrics": ev["default"]["metrics"], "gate": gate, "plateau": {"neighbours": len(ev["neighbours"]), "share": gate["checks"]["neighbourhood"]["share"]},
               "stress": {k: {"candidate": v["candidate"]["objectives"][0], "default": v["default"]["objectives"][0]} for k, v in ev["stress"].items()}, "effectiveN": ledger.effective_total(),
               "capOverrides": ledger.doc["overrides"], "holdout": hold, "shadow": shadow, "unfrozenClasses": study.spec.get("unfrozen", []), "fixedFrom": study.spec.get("from"),
               "changes": rows, "label": gate.get("label"), "honestEdge": HONEST_EDGE}
    write_json(folder / "overlay.json", clean(ov))
    write_json(folder / "rollback.json", clean(rb))
    write_json(folder / "dossier.json", clean(dossier))
    (folder / "diff.md").write_text(_diff_md(rows, ev), encoding="utf-8")
    (folder / "dossier.md").write_text(_dossier_md(dossier, rows), encoding="utf-8")
    return dossier


def _fmt(v) -> str:
    return json.dumps(v) if not isinstance(v, float) else f"{v:.6g}"


def _diff_md(rows: list[dict], ev: dict) -> str:
    out = ["# Config diff (overlay.json against the live configuration)", "", "Apply by hand to `app/config/risk.json` and `app/config/analyst.json`, then bump `config_version` in your changelog. `rollback.json` restores the old values.", "",
           "| file | key | old | new | class | note |", "|---|---|---|---|---|---|"]
    out += [f"| {r['file']} | `{r['path']}` | {_fmt(r['old'])} | {_fmt(r['new'])} | {r['class']} | {r['note']} |" for r in rows]
    danger = [r for r in rows if r["class"] in ("risk-limit", "model-input", "regulatory", "structural")]
    if danger:
        out += ["", "**Frozen-class keys changed (read twice):** " + ", ".join(f"`{r['path']}`" for r in danger)]
    out += ["", "## Searched dimensions that moved", "", "| dimension | old | new | class |", "|---|---|---|---|"]
    out += [f"| `{d['name']}` | {_fmt(d['old'])} | {_fmt(d['new'])} | {d['class']} |" for d in ev["diff"]]
    return "\n".join(out) + "\n"


def _dossier_md(d: dict, rows: list[dict]) -> str:
    g, h, s = d["gate"], d["holdout"], d["shadow"]
    o = d["objectives"]
    lines = [f"# Dossier: candidate {d['candidate']} (study {d['study']}, trial {d['trial']})", "", f"> {d['honestEdge']}", ""]
    if d["label"]:
        lines += [f"**Label: {d['label']}**", ""]
    lines += ["## Objectives and front position", f"- CAGR (CVaR of the worst folds, post-tax): {o['cagrCvarFolds']:.2%}; drawdown depth {o['drawdownDepth']:.2%}; position {o['frontPosition']} of {o['frontSize']} on the feasible front",
              f"- Live default: CAGR {o['default'][0]:.2%}, depth {o['default'][1]:.2%} (at Rs 1 lakh the shipped default may not trade at all)", "", "## Gate"]
    lines += [f"- {'pass' if v['passed'] else 'FAIL'}: {k}" for k, v in g["checks"].items()]
    lines += [f"- effective N (cumulative): {d['effectiveN']}; cap overrides: {len(d['capOverrides'])}", "", "## Plateau and stress", f"- {d['plateau']['neighbours']} neighbours, {d['plateau']['share']:.0%} within tolerance"]
    lines += [f"- {k}: candidate {v['candidate']:.2%}, default {v['default']:.2%}" for k, v in d["stress"].items()]
    lines += ["", "## Exploit audit"] + [f"- {'flagged' if v['flagged'] else 'ok'}: {k}" for k, v in g["audit"]["items"].items()] + ["- not assessed: " + "; ".join(g["audit"]["notAssessed"]), "", "## Holdout (scored once)",
              f"- window {h['window'][0]} to {h['window'][1]}: {'passed' if h['passed'] else 'NOT passed'} (criterion fixed before the look, hash {h['criterionSha']})"]
    lines += [f"  - {k}: {_fmt(v['value'])} against {_fmt(v['limit'])}" for k, v in h["checks"].items()]
    lines += ["", "## Shadow period (limits fixed here, beforehand)", f"- {s['weeks']} weekly rebalances in the existing shadow portfolio; this tests fidelity, not edge", *[f"- roll back if {x}" for x in s["rollbackIf"][:3]],
              "", f"## Changes ({len(rows)} keys; see diff.md)", *[f"- `{r['path']}`: {_fmt(r['old'])} -> {_fmt(r['new'])}" for r in rows]]
    return "\n".join(lines) + "\n"
