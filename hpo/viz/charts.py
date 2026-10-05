"""Study-report charts 1-10 as plain data: each chart is {id, title, how, criterion, fig (a Plotly figure with colour *roles*), table, note}.

Everything is computed from the files under hpo/data (trials.jsonl, status.json, sensitivity.json); nothing here touches the simulator.
A chart whose study has too few scored trials carries fig = None and shows its table (degrades gracefully). Infeasible trials are greyed,
never hidden. Percent for CAGR and drawdown, trial counts or trading days elsewhere; every axis is labelled with its unit.
"""

import math
from datetime import datetime

import numpy as np
from scipy.stats import spearmanr

from hpo import pareto

MIN_SCORED = 5
STOP_WINDOW, STOP_GAIN = 150, 0.01


def _pct(x):
    return None if x is None else round(100.0 * x, 3)


def chart(cid, title, how, criterion, fig, cols, rows, note=""):
    return {"id": cid, "title": title, "how": how, "criterion": criterion, "fig": fig, "table": {"cols": cols, "rows": rows}, "note": note}


def _need(recs_ok, cid, title, how, criterion, cols, rows):
    """The degraded form: too few trials for a picture, so the table (and a note) only."""
    return chart(cid, title, how, criterion, None, cols, rows, note=f"Fewer than {MIN_SCORED} scored trials so far: showing the table only.")


def _scored(recs):
    return [r for r in recs if r["status"] == "ok"]


def tiles(recs, st, cfg) -> list[dict]:
    s = st or {}
    return [{"label": k, "value": v} for k, v in [
        ("trials done", f"{s.get('done', len(recs))} / {s.get('planned', '?')}"), ("feasible", s.get("feasible", sum(r["feasible"] for r in recs))),
        ("FAIL", s.get("fail", sum(r["status"] == "fail" for r in recs))), ("hypervolume", f"{s.get('hypervolume', 0):.4f}"),
        ("best CAGR on front", "--" if s.get("bestCagr") is None else f"{s['bestCagr'] * 100:.1f}%"),
        ("best drawdown on front", "--" if s.get("bestDepth") is None else f"-{s['bestDepth'] * 100:.1f}%"),
        ("effective N / cap", f"{s.get('effectiveN', 0):.0f} / {s.get('cap', cfg['ledger']['effectiveNCap'])}"), ("stage / sampler", f"{s.get('stage', '?')} / {s.get('sampler', '?')}")]]


def c1(recs, st, cfg):
    t = tiles(recs, st, cfg)
    return chart("c1", "Run summary", "Health of the study and how much budget is left.", "Effective N stays below the cap; FAIL stays under 5% of a 50-trial window.",
                 None, ["metric", "value"], [[x["label"], x["value"]] for x in t], note="")


def c2(recs, cfg):
    ok = _scored(recs)
    title, how = "Pareto front", "Each dot is a configuration. Up and to the right is better (higher CAGR, shallower drawdown). Orange is the feasible front, grey is infeasible."
    crit = "Beat the violet default (trial 0) on both axes; stay right of the -30% drawdown line."
    feas = [r for r in ok if r["feasible"]]
    pts = [tuple(r["objectives"]) for r in feas]
    fi = set(pareto.front(pts))
    front = [feas[i] for i in fi]
    rows = [[r["trial"], _pct(r["objectives"][0]), _pct(-r["objectives"][1]), r["metrics"].get("fills"), "front" if r in front else "feasible"] for r in feas]
    cols = ["trial", "CAGR % (fold CVaR)", "max drawdown %", "fills", "set"]
    if len(ok) < MIN_SCORED:
        return _need(ok, "c2", title, how, crit, cols, rows)
    d0 = next((r for r in ok if r["trial"] == 0), None)

    def trace(rs, name, role, size, symbol="circle", color=None):
        return {"type": "scatter", "mode": "markers", "name": name, "x": [_pct(-r["objectives"][1]) for r in rs], "y": [_pct(r["objectives"][0]) for r in rs],
                "text": [f"trial {r['trial']}  fills {r['metrics'].get('fills')}" for r in rs], "hoverinfo": "text+x+y",
                "marker": {"color": color or f"role:{role}", "size": size, "symbol": symbol, "line": {"width": 1, "color": "role:surface"}}}
    data = [trace([r for r in ok if not r["feasible"]], "infeasible", "infeasible", 6),
            {**trace(feas, "feasible (colour: trial order)", "feasible", 7), "marker": {"color": [r["trial"] for r in feas], "colorscale": "Blues", "size": 7, "line": {"width": 1, "color": "role:surface"}}},
            trace(sorted(front, key=lambda r: r["objectives"][1]), "front", "front", 10)]
    data[2]["mode"] = "lines+markers"
    if d0:
        data.append(trace([d0], "live default (trial 0)", "default", 14, "star"))
    cap = _pct(cfg["constraints"]["maxDrawdown"])
    layout = {"xaxis": {"title": {"text": "post-tax max drawdown, full span (%)"}}, "yaxis": {"title": {"text": "post-tax CAGR, CVaR of worst folds (%)"}},
              "shapes": [{"type": "line", "x0": cap, "x1": cap, "yref": "paper", "y0": 0, "y1": 1, "line": {"dash": "dot", "color": "role:bad"}}],
              "annotations": [{"x": cap, "yref": "paper", "y": 1, "text": f"drawdown cap {cap}%", "showarrow": False, "font": {"color": "role:bad"}}]}
    return chart("c2", title, how, crit, {"data": data, "layout": layout}, cols, rows)


def _hv_series(recs, ref):
    pts, out = [], []
    for r in recs:
        if r["feasible"]:
            pts.append(tuple(r["objectives"]))
        out.append(pareto.hypervolume(pts, ref))
    return out


def c3(recs, cfg):
    title, how = "Hypervolume vs trials", "Area dominated by the feasible front. A flat line means the search has stopped finding better trade-offs."
    crit = f"Switch stage or stop when the gain over the last {STOP_WINDOW} trials is under {STOP_GAIN:.0%}."
    ref = tuple(cfg["objectives"]["hvReference"])
    hv = _hv_series(recs, ref)
    n = len(hv)
    gain = None
    if n > STOP_WINDOW and hv[-STOP_WINDOW - 1] > 0:
        gain = hv[-1] / hv[-STOP_WINDOW - 1] - 1
    rows = [[i, round(h, 6)] for i, h in enumerate(hv)]
    if len(_scored(recs)) < MIN_SCORED:
        return _need(None, "c3", title, how, crit, ["trial", "hypervolume"], rows[-20:])
    shapes = [{"type": "line", "x0": r["trial"], "x1": r["trial"], "yref": "paper", "y0": 0, "y1": 1, "line": {"dash": "dot", "color": "role:infeasible"}}
              for prev, r in zip(recs, recs[1:]) if prev["stage"] != r["stage"]]
    note = "" if gain is None else f"Last {STOP_WINDOW} trials: hypervolume gain {gain:.1%} ({'stop rule met' if gain < STOP_GAIN else 'still improving'})."
    fig = {"data": [{"type": "scatter", "mode": "lines", "x": list(range(n)), "y": hv, "line": {"color": "role:front", "width": 2}, "name": "hypervolume"}],
           "layout": {"xaxis": {"title": {"text": "trials"}}, "yaxis": {"title": {"text": "hypervolume (CAGR fraction x drawdown fraction)"}}, "shapes": shapes}}
    return chart("c3", title, how, crit, fig, ["trial", "hypervolume"], rows, note)


def c4(recs, cfg):
    title, how = "Feasibility funnel", "How many trials survive each constraint, in order. The step with the biggest drop is the one rejecting configurations."
    crit = "A study that is mostly infeasible at Rs 1 lakh needs Stage 0 (sizing minimums) before anything else."
    ok = _scored(recs)
    steps = [("trials", recs), ("valid point", [r for r in recs if r["status"] != "invalid"]), ("ran to the end", ok)]
    cur = ok
    for label, key in (("enough fills", "min_fills"), ("fills per fold-year", "fills_per_fold_year"), ("exposure >= floor", "min_exposure"), ("drawdown within cap", "dd_cap")):
        cur = [r for r in cur if r["constraints"][key] <= 0]
        steps.append((label, cur))
    rows = [[n, len(x)] for n, x in steps]
    if not recs:
        return _need(None, "c4", title, how, crit, ["step", "trials"], rows)
    fig = {"data": [{"type": "bar", "orientation": "h", "y": [r[0] for r in rows], "x": [r[1] for r in rows], "marker": {"color": "role:feasible"}, "text": [r[1] for r in rows], "textposition": "auto"}],
           "layout": {"yaxis": {"autorange": "reversed"}, "xaxis": {"title": {"text": "trials"}}, "margin": {"l": 150}}}
    return chart("c4", title, how, crit, fig, ["step", "trials"], rows)


def c5(sens, cfg):
    title, how = "Parameter importance", "PED-ANOVA importance (share of the total) for the objectives. Kept parameters stay in the search, frozen ones take the live value."
    crit = f"Keep until {cfg['sensitivity']['keepShare']:.0%} of the importance is covered, at most {cfg['sensitivity']['maxActive']}."
    if not sens:
        return chart("c5", title, how, crit, None, ["parameter"], [], note="No sensitivity analysis yet: run `python -m hpo.cli sensitivity --name <study>`.")
    d = sens["dims"][:30]
    rows = [[r["name"], round(r["importanceF1"], 4), round(r["importanceF2"], 4), r["decision"]] for r in d]
    fig = {"data": [{"type": "bar", "orientation": "h", "y": [r["name"] for r in d], "x": [r["combined"] for r in d], "name": "importance",
                     "marker": {"color": ["role:feasible" if r["decision"] == "keep" else "role:infeasible" for r in d]}}],
           "layout": {"yaxis": {"autorange": "reversed"}, "xaxis": {"title": {"text": "importance (larger of CAGR and drawdown, share)"}}, "margin": {"l": 260}, "height": 40 + 18 * len(d)}}
    return chart("c5", title, how, crit, fig, ["parameter", "CAGR importance", "drawdown importance", "decision"], rows, "Blue = keep, grey = freeze.")


def _top_params(recs, sens, spec, k):
    if sens:
        return [r["name"] for r in sens["dims"] if r["name"] in spec["active"]][:k]
    ok = _scored(recs)
    if len(ok) < MIN_SCORED:
        return []
    f1 = np.array([r["objectives"][0] for r in ok])
    sc = []
    for n in spec["active"]:
        x = np.array([float(r["params"][n]) for r in ok])
        sc.append((abs(float(np.nan_to_num(spearmanr(x, f1).statistic))) if np.ptp(x) and np.ptp(f1) else 0.0, n))
    return [n for _, n in sorted(sc, reverse=True)[:k]]


def c6(recs, sens, spec):
    title, how = "Slice plots and heatmap", "Each panel shows CAGR against one top parameter (front trials orange): a plateau is safer than a spike. The heatmap shows mean CAGR over the top pair."
    crit = "Prefer a parameter region where CAGR is flat over several steps; a lone spike is overfitting."
    ok = _scored(recs)
    names = _top_params(recs, sens, spec, 6)
    cols, rows = ["parameter", "trials", "Spearman vs CAGR"], []
    if len(ok) < MIN_SCORED or not names:
        return _need(ok, "c6", title, how, crit, cols, rows)
    feas = [r for r in ok if r["feasible"]]
    front = {r["trial"] for r in [feas[i] for i in pareto.front([tuple(r["objectives"]) for r in feas])]}
    data, layout = [], {}
    for i, n in enumerate(names, 1):
        data.append({"type": "scatter", "mode": "markers", "xaxis": f"x{i}", "yaxis": f"y{i}", "name": n, "showlegend": False, "x": [float(r["params"][n]) for r in ok],
                     "y": [_pct(r["objectives"][0]) for r in ok], "marker": {"size": 6, "color": ["role:front" if r["trial"] in front else ("role:feasible" if r["feasible"] else "role:infeasible") for r in ok]}})
        col, row = (i - 1) % 3, (i - 1) // 3
        layout[f"xaxis{i}"] = {"domain": [col / 3 + 0.04, (col + 1) / 3 - 0.01], "anchor": f"y{i}", "title": {"text": n.split('.', 1)[-1][-26:], "font": {"size": 10}}}
        layout[f"yaxis{i}"] = {"domain": [0.58, 0.97] if row == 0 else [0.10, 0.45], "anchor": f"x{i}", "title": {"text": "CAGR %" if col == 0 else ""}}
        rho = spearmanr([float(r["params"][n]) for r in ok], [r["objectives"][0] for r in ok]).statistic
        rows.append([n, len(ok), round(float(np.nan_to_num(rho)), 3)])
    layout["margin"] = {"l": 60, "r": 20, "t": 20, "b": 70}
    layout["height"] = 560
    return chart("c6", title, how, crit, {"data": data, "layout": layout}, cols, rows, note=_heat_note(ok, names))


def _heat_note(ok, names):
    if len(names) < 2:
        return ""
    a, b = names[:2]
    xa, xb = np.array([float(r["params"][a]) for r in ok]), np.array([float(r["params"][b]) for r in ok])
    y = np.array([r["objectives"][0] for r in ok])
    ia, ib = np.digitize(xa, np.quantile(xa, [0.25, 0.5, 0.75])), np.digitize(xb, np.quantile(xb, [0.25, 0.5, 0.75]))
    grid = [[(f"{100 * y[(ia == i) & (ib == j)].mean():.1f}%" if ((ia == i) & (ib == j)).any() else "-") for j in range(4)] for i in range(4)]
    return f"Mean CAGR by quartile, rows {a} (low to high), columns {b}: " + " | ".join(" ".join(r) for r in grid)


def c7(recs, sens, spec):
    title, how = "Front trials, parallel coordinates", "One line per front trial across the top parameters: what the good configurations have in common."
    crit = "Parameters where the lines bunch together are pinned by the data; where they spread, the data does not care."
    ok = [r for r in _scored(recs) if r["feasible"]]
    names = _top_params(recs, sens, spec, 12)
    front = [ok[i] for i in pareto.front([tuple(r["objectives"]) for r in ok])] if ok else []
    cols = ["trial", *names]
    rows = [[r["trial"], *[r["params"][n] for n in names]] for r in front]
    if len(front) < 3 or not names:
        return _need(ok, "c7", title, how, crit, cols, rows)
    dims = [{"label": n.split(".", 1)[-1][-22:], "values": [float(r["params"][n]) for r in front]} for n in names]
    fig = {"data": [{"type": "parcoords", "line": {"color": [r["objectives"][0] for r in front], "colorscale": "Viridis", "showscale": False}, "dimensions": dims}], "layout": {"margin": {"l": 50, "r": 50, "t": 40, "b": 20}}}
    return chart("c7", title, how, crit, fig, cols, rows)


def c8(recs, sens, spec):
    title, how = "Per-bucket offsets", "How far each per-bucket parameter moves from the shared value (0 = shared), and how much it matters."
    crit = "An offset that is never non-zero on the front, or has tiny importance, has not earned its dimension."
    offs = [n for n in spec["active"] if ".off." in n]
    ok = [r for r in _scored(recs) if r["feasible"]]
    imp = {r["name"]: r["combined"] for r in (sens["dims"] if sens else [])}
    rows = [[n, round(float(np.mean([abs(float(r["params"][n])) for r in ok])), 3) if ok else None, round(float(np.mean([r["params"][n] != 0 for r in ok])), 3) if ok else None,
             round(imp[n], 4) if n in imp else None] for n in offs]
    cols = ["offset", "mean |offset| (grid steps)", "share non-zero", "importance"]
    if not offs or len(ok) < MIN_SCORED:
        c = _need(ok, "c8", title, how, crit, cols, rows)
        c["note"] = "No per-bucket offsets are active in this study." if not offs else c["note"]
        return c
    fig = {"data": [{"type": "bar", "x": [r[0].split(".", 2)[-1] for r in rows], "y": [r[1] for r in rows], "marker": {"color": "role:feasible"}, "name": "mean |offset|"}],
           "layout": {"yaxis": {"title": {"text": "mean |offset| on feasible trials (grid steps)"}}, "margin": {"b": 160}, "xaxis": {"tickangle": -60}}}
    return chart("c8", title, how, crit, fig, cols, rows)


def c9(recs):
    title, how = "Trial timeline", "CAGR of every scored trial in the order tried, with the running best feasible value. A flat running best late in the study means it has stopped learning."
    crit = "Stage boundaries (dotted) should show a step up in the running best, not a long plateau."
    ok = _scored(recs)
    best, run = None, []
    for r in ok:
        if r["feasible"] and (best is None or r["objectives"][0] > best):
            best = r["objectives"][0]
        run.append(_pct(best))
    rows = [[r["trial"], _pct(r["objectives"][0]), x, r["feasible"]] for r, x in zip(ok, run)]
    cols = ["trial", "CAGR %", "running best feasible %", "feasible"]
    if len(ok) < MIN_SCORED:
        return _need(ok, "c9", title, how, crit, cols, rows[-20:])
    shapes = [{"type": "line", "x0": r["trial"], "x1": r["trial"], "yref": "paper", "y0": 0, "y1": 1, "line": {"dash": "dot", "color": "role:infeasible"}} for prev, r in zip(recs, recs[1:]) if prev["stage"] != r["stage"]]
    fig = {"data": [{"type": "scatter", "mode": "markers", "name": "trial", "x": [r["trial"] for r in ok], "y": [_pct(r["objectives"][0]) for r in ok],
                     "marker": {"size": 5, "color": ["role:feasible" if r["feasible"] else "role:infeasible" for r in ok]}},
                    {"type": "scatter", "mode": "lines", "name": "running best (feasible)", "x": [r["trial"] for r in ok], "y": run, "line": {"color": "role:front", "width": 2}}],
           "layout": {"xaxis": {"title": {"text": "trial number"}}, "yaxis": {"title": {"text": "post-tax CAGR, fold CVaR (%)"}}, "shapes": shapes}}
    return chart("c9", title, how, crit, fig, cols, rows)


def c10(recs, st):
    title, how = "Run time", "Trials per hour over a sliding window of 20 finished trials, and the estimated time left."
    crit = "Plan the next night from the steady-state rate, not from the first hour."
    ts = [(r["trial"], datetime.fromisoformat(r["at"]).timestamp(), r.get("cacheHit", False)) for r in recs]
    rows, xs, ys = [], [], []
    for i in range(20, len(ts)):
        dt = ts[i][1] - ts[i - 20][1]
        if dt > 0:
            xs.append(ts[i][0])
            ys.append(round(20 / dt * 3600, 1))
    rows = [[x, y] for x, y in zip(xs, ys)]
    eta = (st or {}).get("etaS")
    note = "" if eta is None else f"Estimated time left: {eta / 3600:.1f} h."
    if len(xs) < 2:
        return _need(None, "c10", title, how, crit, ["trial", "trials per hour"], rows)
    fig = {"data": [{"type": "scatter", "mode": "lines", "x": xs, "y": ys, "line": {"color": "role:feasible", "width": 2}, "name": "trials per hour"}],
           "layout": {"xaxis": {"title": {"text": "trial number"}}, "yaxis": {"title": {"text": "trials per hour"}, "rangemode": "tozero"}}}
    return chart("c10", title, how, crit, fig, ["trial", "trials per hour"], rows, note)


def study_charts(recs, st, sens, spec, cfg, only: tuple[str, ...] | None = None) -> list[dict]:
    all_ = {"c1": lambda: c1(recs, st, cfg), "c2": lambda: c2(recs, cfg), "c3": lambda: c3(recs, cfg), "c4": lambda: c4(recs, cfg), "c5": lambda: c5(sens, cfg),
            "c6": lambda: c6(recs, sens, spec), "c7": lambda: c7(recs, sens, spec), "c8": lambda: c8(recs, sens, spec), "c9": lambda: c9(recs), "c10": lambda: c10(recs, st)}
    return [all_[k]() for k in all_ if only is None or k in only]


def clean(x):
    """JSON-safe: NaN and infinity become null, numpy scalars become Python numbers."""
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, (np.floating, float)):
        return None if not math.isfinite(float(x)) else float(x)
    if isinstance(x, np.integer):
        return int(x)
    return x
