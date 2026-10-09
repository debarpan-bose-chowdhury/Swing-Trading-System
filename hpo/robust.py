"""Plateau, not peak: finalists are re-scored on a neighbourhood of nudged configurations.

Finalists are the first `top` trials of the feasible front, taken layer by layer (the front, then the front of what is left, and so on).
For each one a fixed number of perturbations is run: a single dimension moved one grid step (+-10% of a float), or several at once. A finalist is
robust when enough of its neighbours stay within tolerance of it and no single nudge is a cliff (CAGR down by more than `cliffPp`, or no run at all): CAGR within `cagrRel` relative or `cagrAbs` absolute, drawdown within `ddAbs`,
ulcer index within `ulcerRel` (a neighbour that does not run, or is infeasible, counts as outside). From the robust subset the candidate is the
highest Calmar, ties broken by the lower ulcer index. A configuration that was actually run is chosen, never an average of several (which could be
an untested config). An empty robust subset is a valid outcome: keep the defaults.

Neighbour evaluations are not new hypotheses (they cluster with the finalist they perturb), so they are not added to the effective-N ledger.
"""

import json
import random
from pathlib import Path

import numpy as np

from hpo import objective, pareto
from hpo import space as space_mod
from hpo.errors import Failed
from hpo.status import write_json

SINGLES, MULTI_MIN, MULTI_MAX = 10, 2, 4  # top dimensions nudged one at a time; dimensions moved together in a multi-parameter perturbation


def layers(rows: list[dict], top: int) -> list[dict]:
    """The first `top` rows by successive non-dominated layers of (f1, f2); rows are {"f1", "f2", ...}."""
    left, out = list(rows), []
    while left and len(out) < top:
        keep = pareto.front([(r["f1"], r["f2"]) for r in left])
        out += sorted((left[i] for i in keep), key=lambda r: -(r["calmar"] or 0.0))
        left = [r for i, r in enumerate(left) if i not in set(keep)]
    return out[:top]


def finalists(records: list[dict], top: int) -> list[dict]:
    feas = [r for r in records if r["feasible"] and not r.get("cacheHit")]
    rows = [{"trial": r["trial"], "trialId": r["trialId"], "f1": r["objectives"][0], "f2": r["objectives"][1], "calmar": r["metrics"].get("calmar"),
             "ulcer": r["metrics"].get("ulcerIndex"), "values": r["values"], "metrics": r["metrics"]} for r in feas]
    return layers(rows, top)


def _nudge(space, name: str, value, up: bool, float_rel: float):
    """One nudge of one dimension: +-1 grid step (integers, offsets), +-float_rel of a float (at least one grid step), a flipped boolean."""
    d = space.dims[name]
    if d.kind == "bool":
        return not value
    sign = 1 if up else -1
    if d.kind == "int":
        return d.repair(value + sign * (d.step or 1))
    moved = d.repair(value * (1 + sign * float_rel))
    return moved if moved != value else d.repair(value + sign * (d.step or (d.high - d.low) / 20))


def perturbations(space, values: dict, ranked: list[str], active: list[str], seed: int, count: int, float_rel: float, multi: int = 6) -> list[dict]:
    """`count` distinct valid neighbours of a point: single nudges of the top-ranked dimensions plus `multi` random multi-dimension nudges."""
    rng = random.Random(seed)
    base = space.complete(values)
    singles = []
    for n in ranked[:SINGLES]:
        for up in (True, False):
            v = _nudge(space, n, base[n], up, float_rel)
            if v != base[n]:
                singles.append(({n: v}, "single"))
    rng.shuffle(singles)
    picks = singles[:max(0, count - multi)]
    pool = [n for n in active if n in space.dims]
    tries = 0
    while len([p for p in picks if p[1] == "multi"]) < multi and tries < 200 and len(pool) >= MULTI_MIN:
        tries += 1
        names = rng.sample(pool, min(len(pool), rng.randint(MULTI_MIN, MULTI_MAX)))
        change = {n: _nudge(space, n, base[n], rng.random() < 0.5, float_rel) for n in names}
        if any(change[n] != base[n] for n in change):
            picks.append((change, "multi"))
    out, seen = [], {space.key(base)}
    for change, kind in picks:
        point = space.complete({**base, **change})
        key = space.key(point)
        if key in seen:
            continue
        try:
            space.decode(point)
        except space_mod.InvalidPoint:
            continue
        seen.add(key)
        out.append({"kind": kind, "changed": sorted(n for n in point if point[n] != base[n]), "values": point})
    return out[:count]


def within(nominal: dict, other: dict, tol: dict) -> bool:
    """A neighbour is within tolerance of its finalist on all three metrics (None = it did not produce a score)."""
    if other is None:
        return False
    c0, c1 = nominal["cagr"], other["cagr"]
    d0, d1 = -nominal["maxDrawdown"], -other["maxDrawdown"]
    u0, u1 = nominal["ulcerIndex"], other["ulcerIndex"]
    ok_c = c1 >= c0 - max(tol["cagrRel"] * abs(c0), tol["cagrAbs"])
    ok_d = d1 <= d0 + tol["ddAbs"]
    ok_u = u1 <= u0 * (1 + tol["ulcerRel"])
    return bool(ok_c and ok_d and ok_u)


def score_finalist(f: dict, nbs: list[dict], cfg: dict) -> dict:
    """Neighbourhood figures of one finalist from its neighbours' outcome summaries ({"status", "objectives", "metrics", "feasible"})."""
    g, q = cfg["gate"], cfg["robust"]["quantile"]
    ok = [n for n in nbs if n["status"] == "ok" and n.get("feasible")]
    tol = {k: g[k] for k in ("cagrRel", "cagrAbs", "ddAbs", "ulcerRel")}
    share = sum(within(f["metrics"], n["metrics"] if n in ok else None, tol) for n in nbs) / len(nbs) if nbs else 0.0
    f1s, depths = [n["objectives"][0] for n in ok], [n["objectives"][1] for n in ok]
    cliffs = sorted({c for n in nbs if n.get("kind") == "single" and (n not in ok or f["metrics"]["cagr"] - n["metrics"]["cagr"] > cfg["robust"]["cliffPp"]) for c in n.get("changed", [])})
    return {"trialId": f["trialId"], "trial": f["trial"], "neighbours": len(nbs), "feasibleNeighbours": len(ok), "share": share, "cliffs": cliffs,
            "q25F1": float(np.quantile(f1s, q)) if f1s else None, "qWorstDepth": float(np.quantile(depths, 1 - q)) if depths else None,
            "f1": f["f1"], "depth": f["f2"], "calmar": f["calmar"], "ulcer": f["ulcer"], "robust": share >= g["neighbourShare"] and not cliffs}


def select(scores: list[dict]) -> dict | None:
    """The robust finalist with the highest Calmar (ties: lower ulcer), or None: keep the defaults."""
    robust = [s for s in scores if s["robust"]]
    if not robust:
        return None
    return sorted(robust, key=lambda s: (-(s["calmar"] if s["calmar"] is not None else float("-inf")), s["ulcer"] if s["ulcer"] is not None else float("inf")))[0]


def summary(out: dict) -> dict:
    """What is kept of an outcome: no series."""
    return {"status": out["status"], "objectives": out["values"], "metrics": out["metrics"], "feasible": out["status"] == "ok" and objective.feasible(out["constraints"])}


def run(study, pool, cfg: dict, top: int | None, out=None) -> dict:
    """`robust`: neighbourhood-score the finalists of a study (resumable: finished neighbours are kept) and write robust.json."""
    from hpo.study import read_records
    top = top or cfg["robust"]["top"]
    recs = read_records(study.trials_path)
    fin = finalists(recs, top)
    if not fin:
        raise Failed(f"study {study.spec['name']} has no feasible trial: nothing to score")
    sens = json.loads((study.dir / "sensitivity.json").read_text()) if (study.dir / "sensitivity.json").exists() else None
    ranked = [r["name"] for r in sens["dims"]] if sens else list(study.spec["active"])
    ranked = [n for n in ranked if n in study.spec["active"]]  # the searched dimensions, most important first; a frozen one stays at its live value
    path = study.dir / "robust" / "neighbours.jsonl"
    path.parent.mkdir(exist_ok=True)
    done = {(r["finalist"], r["pointKey"]): r for r in (json.loads(x) for x in path.read_text().splitlines() if x)} if path.exists() else {}
    scores = []
    for k, f in enumerate(fin, 1):
        nbs = perturbations(study.space, f["values"], ranked, study.spec["active"], int(f["trialId"], 16), cfg["robust"]["neighbours"], cfg["robust"]["floatRel"])
        futs = {}
        for nb in nbs:
            key = study.space.key(nb["values"])
            if (f["trialId"], key) not in done:
                futs[key] = (nb, pool.submit({"values": nb["values"]}))
        for key, (nb, fut) in futs.items():
            try:
                res = summary(fut.result())
            except Exception as e:  # noqa: BLE001  a crashed neighbour is outside the plateau
                res = {"status": "fail", "objectives": [-1.0, 1.0], "metrics": {}, "feasible": False, "error": str(e)}
            rec = {"finalist": f["trialId"], "pointKey": key, "kind": nb["kind"], "changed": nb["changed"], **res}
            done[(f["trialId"], key)] = rec
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
        mine = [r for (fid, _), r in done.items() if fid == f["trialId"]]
        scores.append(score_finalist(f, mine, cfg))
        if out:
            print(f"  finalist {k}/{len(fin)} trial {f['trial']}: {scores[-1]['share']:.0%} of {len(mine)} neighbours within tolerance", file=out, flush=True)
    pick = select(scores)
    doc = {"study": study.spec["name"], "top": top, "scores": scores, "selected": pick["trialId"] if pick else None, "decision": "candidate" if pick else "keep defaults"}
    write_json(study.dir / "robust.json", doc)
    return doc
