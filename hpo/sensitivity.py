"""Parameter importance and the freezing decision (Stage A).

PED-ANOVA importance per objective (f1 CAGR, f2 drawdown) and for feasibility (the largest constraint violation), plus the Spearman rank
correlation of each dimension with both objectives as a cross-check. A dimension is kept while the cumulative share of the combined
importance (the larger of its f1 and f2 importance) is within `keepShare`, at most `maxActive` are kept, and each kept one is above
`minImportance`; the rest are frozen at their live value. A per-bucket offset that does not matter collapses to the shared value (it is
frozen at offset 0), so the dimension count follows the evidence. A regime-specific dimension of a regime that occurred in under half of
the trials is reported as inactive and excluded from the counts.
"""

import numpy as np
from scipy.stats import spearmanr

from hpo import samplers
from hpo.errors import Failed

MIN_TRIALS = 20


def analyse(space, spec: dict, records: list[dict], cfg: dict) -> dict:
    s = cfg["sensitivity"]
    ok = [r for r in records if r["status"] == "ok"]
    if len(ok) < MIN_TRIALS:
        raise Failed(f"sensitivity needs at least {MIN_TRIALS} scored trials, the study has {len(ok)}")
    names = [n for n in spec["active"] if len({r["params"][n] for r in ok}) > 1]
    scored = [{"params": r["params"], "values": r["objectives"]} for r in ok]
    imp = {}
    study = samplers.memory_study(space, names, scored, ["maximize", "minimize"])
    for label, sign, idx in (("f1", -1.0, 0), ("f2", 1.0, 1)):  # PED-ANOVA explains *low* target values: negate the one to maximise
        imp[label] = samplers.ped_anova(study, names, target=lambda t, i=idx, g=sign: g * t.values[i])
    feas_rows = [{"params": r["params"], "values": [max(r["constraints"].values())]} for r in ok]
    imp["feas"] = samplers.ped_anova(samplers.memory_study(space, names, feas_rows, ["minimize"]), names)
    x = np.array([[float(r["params"][n]) for n in names] for r in ok])
    f1, f2 = np.array([r["objectives"][0] for r in ok]), np.array([r["objectives"][1] for r in ok])
    rows = []
    for j, n in enumerate(names):
        inactive = float(np.mean([n in r["inactive"] for r in ok]))
        rho1 = spearmanr(x[:, j], f1).statistic if np.ptp(f1) else 0.0
        rho2 = spearmanr(x[:, j], f2).statistic if np.ptp(f2) else 0.0
        rows.append({"name": n, "group": space.dims[n].group, "importanceF1": imp["f1"][n], "importanceF2": imp["f2"][n], "importanceFeasibility": imp["feas"][n],
                     "spearmanF1": float(np.nan_to_num(rho1)), "spearmanF2": float(np.nan_to_num(rho2)), "combined": max(imp["f1"][n], imp["f2"][n]), "inactiveShare": inactive})
    rows.sort(key=lambda r: -r["combined"])
    total = sum(r["combined"] for r in rows if r["inactiveShare"] < 0.5) or 1.0
    cum, kept = 0.0, 0
    for r in rows:
        if r["inactiveShare"] >= 0.5:
            r["decision"], r["reason"] = "freeze", "inactive: its regime occurred in under half of the trials"
        elif kept >= s["maxActive"]:
            r["decision"], r["reason"] = "freeze", f"beyond the {s['maxActive']} most important"
        elif cum >= s["keepShare"] or r["combined"] < s["minImportance"]:
            r["decision"], r["reason"] = "freeze", "importance below the cut" if r["combined"] < s["minImportance"] else f"cumulative share already {cum:.0%}"
        else:
            r["decision"], r["reason"] = "keep", ""
            kept += 1
            cum += r["combined"] / total
    keep = [r["name"] for r in rows if r["decision"] == "keep"]
    collapsed = [r["name"] for r in rows if ".off." in r["name"] and r["decision"] == "freeze"]
    return {"study": spec["name"], "trials": len(records), "scored": len(ok), "feasible": sum(r["feasible"] for r in ok), "dims": rows, "keep": keep,
            "freeze": [r["name"] for r in rows if r["decision"] == "freeze"], "collapsedOffsets": collapsed,
            "proposedStudy": {"name": f"{spec['name']}-next", "stage": "B", "sampler": "tpe", "active": keep, "trials": 300, "seed": spec["seed"]}}
