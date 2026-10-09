"""The corrected gate, the exploit audit and the candidate evidence.

`collect` runs what the gate needs through the evaluation pool (candidate and live default in detail; the stress runs of both; the neighbours; a grid
over the two most important parameters) and computes the statistics over the study's whole trial matrix. `checks` and `audit` are pure functions of
that evidence, so the verdict is reproducible from candidates/<id>/evidence.json alone.

Checks (all must hold; the SPA p-value only labels): enough fills; deflated Sharpe at the cumulative effective N (also reported at 2N); PBO by CSCV over the
full trial matrix (medoids when above 500 columns); the median fold CAGR above the floor and its ratio to the full-span CAGR; neighbourhood share per
metric; ranking against the default stable at doubled slippage and at a 100% write-off; no BEAR or stress-window drawdown more than 5 pp worse than the
default's; SPA against the default (p <= spaP, otherwise "adopt for feasibility or robustness only").

The audit lists what it assessed (parameters at a bound, cliffs, vanished-name write-offs, fills, the realism and surveillance flags) and, separately and
in the dossier, what this build cannot assess; a check that was not run is never reported as passed.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

from hpo import robust, stats
from hpo.status import write_json
from hpo.viz.charts import clean

NOT_ASSESSED = ["2-decimal cash handling", "retry-window re-pricing", "bhavcopy-derived names without dividends", "Muhurat sessions", "missing 2019 index days",
                "the draft tax table across the 2018 and 2024 rules", "universe holes", "+1-day execution delay (the engine has no delay model)"]
DEPTH_MARGIN = 0.05


def stress_specs(cfg: dict) -> dict:
    s = cfg["robust"]["stress"]
    other = "today" if cfg["universe"]["selection"] == "pit" else "pit"
    return {"slippage2": {"slippageMult": s["slippageMult"]}, "charges1.3": {"chargesMult": s["chargesMult"]}, "writeOff100": {"writeOff": s["writeOff"]},
            "drop5pct": {"dropNames": s["dropNames"], "seed": 1}, "otherUniverse": {"universe": other}}


def grid_points(space, values: dict, xname: str, yname: str, half: int = 2) -> list[tuple[int, int, dict]]:
    """(i, j, point): a (2 half + 1)^2 grid of grid steps around a point over two dimensions (clipped into the bounds, so edges repeat)."""
    base = space.complete(values)
    out = []
    for i in range(-half, half + 1):
        for j in range(-half, half + 1):
            p = dict(base)
            for name, k in ((xname, i), (yname, j)):
                d = space.dims[name]
                step = d.step or ((d.high - d.low) / 20 if d.kind == "float" else 1)
                p[name] = d.repair(base[name] + k * step) if d.kind != "bool" else (not base[name] if k % 2 else base[name])
            out.append((i + half, j + half, space.complete(p)))
    return out


def top_two(study, sens) -> list[str]:
    names = [r["name"] for r in sens["dims"] if r["name"] in study.spec["active"]] if sens else list(study.spec["active"])
    cont = [n for n in names if study.space.dims[n].kind != "bool"]
    return (cont + [n for n in study.space.names if n not in cont and study.space.dims[n].kind != "bool"])[:2]


def collect(study, pool, cfg: dict, rec: dict, ledger, out=None) -> dict:
    """All evidence for one candidate (a trials.jsonl record of `study`). Runs about 2 + 10 + 16 + 25 simulations."""
    space, say = study.space, (lambda m: print(m, file=out, flush=True)) if out else (lambda m: None)
    default = space.complete(space.defaults)
    cand = space.complete(rec["values"])
    sens = json.loads((study.dir / "sensitivity.json").read_text()) if (study.dir / "sensitivity.json").exists() else None
    ranked = [r["name"] for r in sens["dims"] if r["name"] in study.spec["active"]] if sens else list(study.spec["active"])

    def go(values, **extra):
        return pool.submit({"values": values, **extra})

    jobs = {"candidate": go(cand, detail=True), "default": go(default, detail=True, noAbort=True)}  # the live default may never trade at this capital: run it to the end
    for tag, spec in stress_specs(cfg).items():
        jobs[f"{tag}:candidate"], jobs[f"{tag}:default"] = go(cand, stress=spec), go(default, stress=spec, noAbort=True)
    xy = top_two(study, sens)
    grid = grid_points(space, cand, *xy) if len(xy) == 2 else []
    gjobs = {(i, j): go(p) for i, j, p in grid}
    nbs = robust.perturbations(space, cand, ranked, study.spec["active"], int(rec["trialId"], 16), cfg["robust"]["neighbours"], cfg["robust"]["floatRel"])
    njobs = [(n, go(n["values"])) for n in nbs]
    res = {k: f.result() for k, f in jobs.items()}
    say(f"  candidate, default and {len(jobs) - 2} stress runs done")
    summ = lambda o: robust.summary(o)  # noqa: E731
    neighbours = [{"kind": n["kind"], "changed": n["changed"], **summ(f.result())} for n, f in njobs]
    cells = {k: summ(f.result()) for k, f in gjobs.items()}
    n_side = int(round(len(grid) ** 0.5)) if grid else 0
    ev = {"candidate": {"trialId": rec["trialId"], "study": study.spec["name"], "trial": rec["trial"], "values": cand, **summ(res["candidate"])},
          "default": {"values": default, **summ(res["default"])},
          "stress": {tag: {"candidate": summ(res[f"{tag}:candidate"]), "default": summ(res[f"{tag}:default"]), "spec": spec} for tag, spec in stress_specs(cfg).items()},
          "neighbours": neighbours, "grid": {"x": xy[0], "y": xy[1], "n": n_side, "values": {"x": sorted({p[xy[0]] for _, _, p in grid}), "y": sorted({p[xy[1]] for _, _, p in grid})},
                                              "cagr": [[(cells[(i, j)]["metrics"].get("cagr") if cells[(i, j)]["status"] == "ok" else None) for j in range(n_side)] for i in range(n_side)],
                                              "depth": [[(-cells[(i, j)]["metrics"]["maxDrawdown"] if cells[(i, j)]["status"] == "ok" else None) for j in range(n_side)] for i in range(n_side)]} if grid else None,
          "detail": {k: {kk: vv for kk, vv in res[k]["detail"].items() if not isinstance(vv, pd.Series)} for k in ("candidate", "default")}}
    # statistics over the whole trial matrix of the study
    matrix = pd.DataFrame({r["trialId"]: study.registry.returns(r["trialId"]) for r in {x["trialId"]: x for x in study.records_ok()}.values()}).dropna()
    reduced = stats.medoids(matrix, 500, keep=rec["trialId"])
    sharpes = stats.trial_sharpes(matrix)
    n_eff = max(ledger.effective_total(), 2)
    cr = res["candidate"]["returns"].to_numpy()
    pbo = stats.pbo_logits(reduced)
    dsr = stats.dsr_curve(cr, sharpes, [n_eff, 2 * n_eff, *sorted({max(2, int(n_eff * f)) for f in (0.25, 0.5, 1, 1.5, 2, 3, 4)})])
    excess = reduced.sub(res["default"]["returns"].reindex(reduced.index), axis=0).dropna()
    ev["stats"] = {"pbo": {k: pbo[k] for k in ("pbo", "logits", "splits", "trials", "blocks")}, "trialColumns": int(matrix.shape[1]),
                   "dsr": {"effectiveN": n_eff, "atN": dsr[0]["dsr"], "at2N": dsr[1]["dsr"], "curve": dsr[2:]}, "spa": stats.spa_pvalue(excess, seed=cfg["compute"]["seed"])}
    ev["bounds"] = [{"name": n, "low": study.space.dims[n].low, "high": study.space.dims[n].high, "value": cand[n], "default": default[n], "class": study.space.dims[n].cls}
                    for n in study.spec["active"] if study.space.dims[n].kind != "bool"]
    ev["diff"] = [{"name": n, "class": space.dims[n].cls, "old": default[n], "new": cand[n], "low": space.dims[n].low, "high": space.dims[n].high, "note": space.dims[n].note}
                  for n in space.names if cand[n] != default[n]]
    ev["_series"] = {k: res[k]["detail"] for k in ("candidate", "default")}
    ev["_returns"] = {"candidate": res["candidate"]["returns"], "default": res["default"]["returns"]}
    return ev


def checks(ev: dict, cfg: dict) -> dict:
    """The gate verdict from evidence alone."""
    g, c = cfg["gate"], cfg["constraints"]
    cand, default = ev["candidate"], ev["default"]
    m = cand["metrics"]
    out = {}
    out["feasible"] = {"passed": bool(cand["feasible"]), "fills": m.get("fills"), "limit": c["minFills"]}
    s = ev["stats"]
    out["deflatedSharpe"] = {"passed": s["dsr"]["atN"] >= g["dsrMin"], "value": s["dsr"]["atN"], "at2N": s["dsr"]["at2N"], "effectiveN": s["dsr"]["effectiveN"], "limit": g["dsrMin"]}
    out["pbo"] = {"passed": s["pbo"]["pbo"] <= g["pboMax"], "value": s["pbo"]["pbo"], "limit": g["pboMax"], "trials": s["pbo"]["trials"]}
    fold = m.get("foldCagr") or []
    median = float(np.median(fold)) if fold else None
    full = m.get("cagr")
    ret = median / full if (median is not None and full and full > 0) else None
    out["outOfSample"] = {"passed": bool(full and full > 0 and median is not None and median > g["oosFloorCagr"] and ret is not None and ret >= g["oosIsMin"]), "inSample": full, "medianFold": median,
                          "retention": ret, "limit": g["oosIsMin"], "floor": g["oosFloorCagr"]}
    tol = {k: g[k] for k in ("cagrRel", "cagrAbs", "ddAbs", "ulcerRel")}
    nbs = ev["neighbours"]
    good = [robust.within(m, n["metrics"] if n["status"] == "ok" and n["feasible"] else None, tol) for n in nbs]
    share = sum(good) / len(good) if good else 0.0
    out["neighbourhood"] = {"passed": bool(good) and share >= g["neighbourShare"], "share": share, "neighbours": len(good), "limit": g["neighbourShare"], "tolerance": tol}
    stable = {}
    for tag in ("slippage2", "writeOff100"):
        st = ev["stress"][tag]
        stable[tag] = bool(st["candidate"]["status"] == "ok" and st["default"]["status"] == "ok" and st["candidate"]["objectives"][0] >= st["default"]["objectives"][0])
    out["stressRanking"] = {"passed": all(stable.values()), "stable": stable}
    cd, dd = ev["detail"]["candidate"], ev["detail"]["default"]
    worse = {}
    for k, v in cd["stressWindows"].items():
        if k in dd["stressWindows"]:
            worse[k] = (-v["maxDrawdown"]) - (-dd["stressWindows"][k]["maxDrawdown"])
    bear_c, bear_d = (cd["regimes"].get("BEAR") or {}).get("maxDrawdown"), (dd["regimes"].get("BEAR") or {}).get("maxDrawdown")
    if bear_c is not None and bear_d is not None:
        worse["BEAR regime"] = (-bear_c) - (-bear_d)
    out["regimeAndStress"] = {"passed": all(v <= DEPTH_MARGIN for v in worse.values()), "worseThanDefault": worse, "limit": DEPTH_MARGIN}
    spa = s["spa"]
    out["spa"] = {"passed": spa["p"] <= g["spaP"], "p": spa["p"], "limit": g["spaP"], "blocking": False}
    blocking = [k for k, v in out.items() if not v["passed"] and v.get("blocking", True)]
    return {"passed": not blocking, "failed": blocking, "checks": out,
            "label": None if out["spa"]["passed"] else "adopt for feasibility or robustness only (not shown to beat the live default: SPA p > %.2f)" % g["spaP"]}


def audit(ev: dict, cfg: dict) -> dict:
    """The exploit audit of the candidate against the default: what was assessed, what was flagged, and what this build cannot assess."""
    items = {}
    at = [b["name"] for b in ev["bounds"] if b["high"] > b["low"] and (b["value"] <= b["low"] or b["value"] >= b["high"])]
    items["parametersAtABound"] = {"flagged": bool(at), "names": at}
    base = ev["candidate"]["metrics"].get("cagr")
    cliffs = [n["changed"] for n in ev["neighbours"] if n["kind"] == "single" and n["status"] == "ok" and base is not None and abs(n["metrics"].get("cagr", base) - base) > cfg["robust"]["cliffPp"]]
    items["cliffs"] = {"flagged": bool(cliffs), "singleStepMovesCagrOver": cfg["robust"]["cliffPp"], "params": cliffs}
    vc, vd = ev["detail"]["candidate"]["vanished"], ev["detail"]["default"]["vanished"]
    items["vanishedNames"] = {"flagged": vc["writtenOffInr"] > vd["writtenOffInr"] * 1.5 + 1.0 and vc["exits"] > vd["exits"], "candidate": vc, "default": vd}
    items["fills"] = {"flagged": not ev["candidate"]["feasible"], "fills": ev["candidate"]["metrics"].get("fills")}
    det = ev["detail"]["candidate"]
    items["surveillance"] = {"flagged": not det["surveillanceModelled"], "note": "surveillance blocks are not modelled in this backtest: trades in such names are not excluded"}
    off = [k for k, v in det["realism"].items() if not v]
    items["unmodelledMarketRules"] = {"flagged": bool(off), "notModelled": off, "note": "price bands, volume caps, circuit locks and settlement lag are labelled, not modelled"}
    flagged = [k for k, v in items.items() if v["flagged"]]
    return {"items": items, "flagged": flagged, "notAssessed": NOT_ASSESSED, "passed": not flagged}


def save(folder: Path, ev: dict, verdict: dict, aud: dict, cfg: dict | None = None) -> None:
    """candidates/<id>/: evidence.json (no arrays), series.parquet (the curves), returns.parquet, gate.json, and (with cfg) criterion.json: the holdout pass
    criterion, registered now, before anyone has looked at the holdout (its hash goes into gate.json). Evidence is frozen once the holdout is scored."""
    if (folder / "holdout.json").exists():
        from hpo.errors import Refusal
        raise Refusal("the holdout was already scored for this candidate: its evidence is frozen and cannot be regenerated")
    folder.mkdir(parents=True, exist_ok=True)
    sha = None
    if cfg is not None:
        from hpo import holdout
        crit = holdout.criterion(ev, cfg)
        write_json(folder / holdout.CRITERION_FILE, crit)
        sha = holdout.sha_of(crit)
    series = {}
    for who in ("candidate", "default"):
        d = ev["_series"][who]
        for k in ("nav", "exposure", "rung", "regime"):
            series[f"{who}.{k}"] = d[k]
        if who == "candidate":
            series["benchmark"] = d["bench"]
    pd.DataFrame(series).rename_axis("date").reset_index().to_parquet(folder / "series.parquet", index=False)
    pd.DataFrame(ev["_returns"]).rename_axis("date").reset_index().to_parquet(folder / "returns.parquet", index=False)
    write_json(folder / "evidence.json", clean({k: v for k, v in ev.items() if not k.startswith("_")}))
    write_json(folder / "gate.json", clean({**verdict, "audit": aud, "criterionSha": sha}))
