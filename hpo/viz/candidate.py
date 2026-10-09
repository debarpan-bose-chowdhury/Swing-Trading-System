"""Candidate report: charts 11-24, from candidates/<id>/ (evidence.json, gate.json, series.parquet) only. The holdout panel (24) never carries holdout data before
the one-shot holdout has been scored; this phase does not score it."""

import html
import json
from pathlib import Path

import numpy as np
import pandas as pd

from hpo.viz import charts, report

C, D, B = "role:candidate", "role:default", "role:benchmark"
REGIME_ORDER = ["BULL", "TREND", "WEAK", "BEAR", "Unknown"]


def _pct(x):
    return None if x is None else round(100 * x, 3)


def _bars(names, a, b, an="candidate", bn="default", yt="", extra_layout=None):
    return {"data": [{"type": "bar", "name": an, "x": names, "y": a, "marker": {"color": C}}, {"type": "bar", "name": bn, "x": names, "y": b, "marker": {"color": D}}],
            "layout": {"barmode": "group", "yaxis": {"title": {"text": yt}}, **(extra_layout or {})}}


def _bands(ev) -> list[dict]:
    out = []
    for w in ev["detail"]["candidate"]["stressWindows"].values():
        for a, b in w.get("spans", []):
            out.append({"type": "rect", "xref": "x", "yref": "paper", "x0": a, "x1": b, "y0": 0, "y1": 1, "fillcolor": "role:infeasible", "opacity": 0.18, "line": {"width": 0}})
    return out


def c11(ev, s):
    cn, dn, bm = s["candidate.nav"], s["default.nav"], s["benchmark"]
    bench = bm / bm.dropna().iloc[0] * cn.iloc[0]
    fig = {"data": [{"type": "scatter", "mode": "lines", "name": "candidate", "x": list(s.date), "y": list(cn), "line": {"color": C, "width": 2}},
                    {"type": "scatter", "mode": "lines", "name": "live default", "x": list(s.date), "y": list(dn), "line": {"color": D, "width": 2}},
                    {"type": "scatter", "mode": "lines", "name": "benchmark (price index; a total-return series is not available)", "x": list(s.date), "y": list(bench), "line": {"color": B, "width": 1.5}}],
           "layout": {"yaxis": {"type": "log", "title": {"text": "post-tax NAV, log scale (Rs)"}}, "xaxis": {"title": {"text": "date"}}, "shapes": _bands(ev)}}
    yr = s.date.str[:4]
    rows = [[y, round(float(cn[yr == y].iloc[-1]), 0), round(float(dn[yr == y].iloc[-1]), 0)] for y in sorted(set(yr))]
    return charts.chart("c11", "Equity curves", "Post-tax NAV of the candidate, the live default and the benchmark; grey bands are the named stress windows. The holdout is locked and not in this span.",
                        "The candidate should end above the default without a deeper trough in the bands.", fig, ["year end", "candidate NAV", "default NAV"], rows)


def _dd(nav):
    return nav / nav.cummax() - 1


def c12(ev, s):
    reg = {k: i for i, k in enumerate(REGIME_ORDER)}
    yc, yd = _dd(s["candidate.nav"]), _dd(s["default.nav"])
    fig = {"data": [{"type": "scatter", "mode": "lines", "name": "candidate", "x": list(s.date), "y": [_pct(v) for v in yc], "line": {"color": C}},
                    {"type": "scatter", "mode": "lines", "name": "live default", "x": list(s.date), "y": [_pct(v) for v in yd], "line": {"color": D}},
                    {"type": "scatter", "mode": "lines", "name": "ladder rung", "x": list(s.date), "y": list(s["candidate.rung"]), "yaxis": "y2", "line": {"color": "role:infeasible", "shape": "hv"}},
                    {"type": "scatter", "mode": "markers", "name": "regime (0 BULL .. 3 BEAR)", "x": list(s.date), "y": [reg.get(r, 4) for r in s["candidate.regime"]], "yaxis": "y3", "marker": {"size": 3, "color": "role:feasible"}}],
           "layout": {"yaxis": {"domain": [0.42, 1], "title": {"text": "drawdown (%)"}}, "yaxis2": {"domain": [0.22, 0.38], "title": {"text": "rung"}}, "yaxis3": {"domain": [0.0, 0.18], "title": {"text": "regime"}},
                      "height": 520}}
    rows = [["max drawdown %", _pct(float(yc.min())), _pct(float(yd.min()))]]
    return charts.chart("c12", "Underwater plot", "Distance from the previous NAV peak, with the ladder rung and the regime of every day underneath.", "Depth and time under water; what the ladder did while it happened.",
                        fig, ["", "candidate", "default"], rows)


def c13(ev, cfg):
    m, d = ev["candidate"]["metrics"], ev["default"]["metrics"]
    n = len(m["foldCagr"])
    names = [f"fold {i + 1}" for i in range(n)]
    fig = {"data": [{"type": "bar", "name": "candidate CAGR", "x": names, "y": [_pct(v) for v in m["foldCagr"]], "marker": {"color": C}},
                    {"type": "bar", "name": "default CAGR", "x": names, "y": [_pct(v) for v in d.get("foldCagr", [])], "marker": {"color": D}},
                    {"type": "scatter", "mode": "lines", "name": "candidate CVaR level", "x": names, "y": [_pct(ev["candidate"]["objectives"][0])] * n, "line": {"dash": "dot", "color": C}}],
           "layout": {"barmode": "group", "yaxis": {"title": {"text": "post-tax CAGR of the fold window (%)"}}}}
    rows = [[names[i], _pct(m["foldCagr"][i]), _pct(m["foldDrawdown"][i]), _pct(d.get("foldCagr", [None] * n)[i]), _pct(d.get("foldDrawdown", [None] * n)[i])] for i in range(n)]
    return charts.chart("c13", "Fold bars", "CAGR of each one-year fold window of the single run, candidate against default; the dotted line is the CVaR of the worst folds that the search maximised.",
                        "Consistency across folds, not the average.", fig, ["fold", "candidate CAGR %", "candidate max DD %", "default CAGR %", "default max DD %"], rows)


def c14(ev, cfg):
    cw, dw = ev["detail"]["candidate"]["stressWindows"], ev["detail"]["default"]["stressWindows"]
    names = [k for k in cw if k in dw]
    br = ev["detail"]["candidate"]["regimes"].get("BEAR"), ev["detail"]["default"]["regimes"].get("BEAR")
    cd = [-cw[k]["maxDrawdown"] for k in names]
    dd = [-dw[k]["maxDrawdown"] for k in names]
    if br[0] and br[1]:
        names, cd, dd = names + ["BEAR regime days"], cd + [-br[0]["maxDrawdown"]], dd + [-br[1]["maxDrawdown"]]
    fig = {"data": [*_bars(names, [_pct(v) for v in cd], [_pct(v) for v in dd], yt="max drawdown depth (%)")["data"],
                    {"type": "scatter", "mode": "markers", "name": "failure line (default + 5 pp)", "x": names, "y": [_pct(v + 0.05) for v in dd], "marker": {"symbol": "line-ew", "size": 22, "color": "role:bad", "line": {"width": 2, "color": "role:bad"}}}],
           "layout": {"barmode": "group", "yaxis": {"title": {"text": "max drawdown depth (%)"}}}}
    rows = [[n, _pct(a), _pct(b), _pct(a - b)] for n, a, b in zip(names, cd, dd)]
    return charts.chart("c14", "Stress-window bars", "Drawdown depth inside each named stress window, and on BEAR-regime days, candidate against default.", "A candidate fails when any bar is more than 5 pp deeper than the default's.",
                        fig if names else None, ["window", "candidate depth %", "default depth %", "difference pp"], rows)


def c15(ev, cfg):
    g = ev.get("grid")
    if not g:
        return charts.chart("c15", "Plateau heatmaps", "", "", None, ["note"], [["no grid evidence"]])
    n = g["n"]
    mid = n // 2
    z1 = [[_pct(v) for v in row] for row in g["cagr"]]
    z2 = [[_pct(v) for v in row] for row in g["depth"]]
    xs, ys = [str(v) for v in g["values"]["x"]], [str(v) for v in g["values"]["y"]]
    fig = {"data": [{"type": "heatmap", "z": z1, "x": xs, "y": ys, "xaxis": "x", "yaxis": "y", "colorscale": "Blues", "colorbar": {"title": "CAGR %", "x": 0.45, "len": 0.8}},
                    {"type": "heatmap", "z": z2, "x": xs, "y": ys, "xaxis": "x2", "yaxis": "y2", "colorscale": "Oranges", "colorbar": {"title": "depth %", "x": 1.0, "len": 0.8}},
                    {"type": "scatter", "mode": "markers", "x": [xs[mid]], "y": [ys[mid]], "xaxis": "x", "yaxis": "y", "name": "candidate", "marker": {"size": 18, "symbol": "circle-open", "line": {"width": 3, "color": "role:text"}}},
                    {"type": "scatter", "mode": "markers", "x": [xs[mid]], "y": [ys[mid]], "xaxis": "x2", "yaxis": "y2", "showlegend": False, "marker": {"size": 18, "symbol": "circle-open", "line": {"width": 3, "color": "role:text"}}}],
           "layout": {"xaxis": {"domain": [0, 0.42], "title": {"text": g["x"].split(".", 1)[-1]}, "type": "category"}, "yaxis": {"title": {"text": g["y"].split(".", 1)[-1]}, "type": "category"},
                      "xaxis2": {"domain": [0.58, 0.97], "anchor": "y2", "title": {"text": g["x"].split(".", 1)[-1]}, "type": "category"}, "yaxis2": {"anchor": "x2", "type": "category"}}}
    rows = [[ys[j], *[z1[i][j] for i in range(n)]] for j in range(n)]
    return charts.chart("c15", "Plateau heatmaps", "CAGR (left) and drawdown depth (right) on a 5 x 5 grid of grid steps around the candidate (circled) over its two most important parameters.",
                        "A plateau is a flat patch around the circle; a peak is a lone bright cell.", fig, [f"{g['y'].split('.')[-1]} \\ {g['x'].split('.')[-1]}", *xs], rows)


def c16(ev, cfg):
    ok = [n for n in ev["neighbours"] if n["status"] == "ok"]
    c, d = [_pct(n["metrics"]["cagr"]) for n in ok], [_pct(-n["metrics"]["maxDrawdown"]) for n in ok]
    base = ev["candidate"]["metrics"]
    q = cfg["robust"]["quantile"]
    fig = {"data": [{"type": "histogram", "x": c, "name": "neighbour CAGR %", "xaxis": "x", "yaxis": "y", "marker": {"color": "role:feasible"}},
                    {"type": "histogram", "x": d, "name": "neighbour depth %", "xaxis": "x2", "yaxis": "y2", "marker": {"color": "role:front"}}],
           "layout": {"xaxis": {"domain": [0, 0.45], "title": {"text": "post-tax CAGR (%)"}}, "xaxis2": {"domain": [0.55, 1], "anchor": "y2", "title": {"text": "drawdown depth (%)"}}, "yaxis": {"title": {"text": "neighbours"}}, "yaxis2": {"anchor": "x2"},
                      "shapes": [{"type": "line", "xref": "x", "yref": "paper", "x0": _pct(base["cagr"]), "x1": _pct(base["cagr"]), "y0": 0, "y1": 1, "line": {"color": C, "width": 3}},
                                 {"type": "line", "xref": "x2", "yref": "paper", "x0": _pct(-base["maxDrawdown"]), "x1": _pct(-base["maxDrawdown"]), "y0": 0, "y1": 1, "line": {"color": C, "width": 3}}]}}
    rows = [[", ".join(n["changed"])[-60:], n["kind"], _pct(n["metrics"]["cagr"]), _pct(-n["metrics"]["maxDrawdown"])] for n in ok]
    note = f"Candidate (orange line): CAGR {_pct(base['cagr'])}%, depth {_pct(-base['maxDrawdown'])}%. Neighbour {int(q * 100)}th percentile CAGR: {np.quantile(c, q):.2f}%." if c else ""
    return charts.chart("c16", "Neighbour distribution", "Results of the nudged neighbours (one grid step or 10% on single parameters, several at once for the rest); the orange line is the candidate itself.",
                        "A narrow cluster at the candidate's value means a small nudge changes little.", fig if c else None, ["changed", "kind", "CAGR %", "depth %"], rows, note)


def c17(ev, cfg):
    tags = ["baseline", *ev["stress"]]
    cand = [ev["candidate"]["objectives"][0]] + [ev["stress"][t]["candidate"]["objectives"][0] for t in ev["stress"]]
    default = [ev["default"]["objectives"][0]] + [ev["stress"][t]["default"]["objectives"][0] for t in ev["stress"]]
    fig = _bars(tags, [_pct(v) for v in cand], [_pct(v) for v in default], yt="CAGR, CVaR of worst folds (%)")
    rows = [[t, _pct(a), _pct(b)] for t, a, b in zip(tags, cand, default)]
    return charts.chart("c17", "Stress tests", "The search objective under the simulator's assumptions stressed one at a time: slippage x 2, charges x 1.3, 100% write-off, 5% of names dropped, the other universe mode.",
                        "The candidate should stay at or above the default in every stress.", fig, ["stress", "candidate %", "default %"], rows,
                        "A +1-day execution delay is not available: the engine has no delay model.")


def c18(ev, cfg):
    p = ev["stats"]["pbo"]
    fig = {"data": [{"type": "histogram", "x": p["logits"], "marker": {"color": "role:feasible"}, "name": "logit of the winner's out-of-sample rank"}],
           "layout": {"xaxis": {"title": {"text": "logit of relative out-of-sample rank (0 = median)"}}, "yaxis": {"title": {"text": "CSCV splits"}},
                      "shapes": [{"type": "line", "xref": "x", "yref": "paper", "x0": 0, "x1": 0, "y0": 0, "y1": 1, "line": {"dash": "dot", "color": "role:bad"}}]}}
    return charts.chart("c18", "Probability of backtest overfitting", f"CSCV over {p['trials']} trial columns, {p['splits']} splits. PBO = share of splits where the in-sample winner ranks in the lower half out of sample (left of the dotted line).",
                        f"PBO must be at most {cfg['gate']['pboMax']:.2f}.", fig, ["PBO", "limit", "trials", "splits"], [[round(p["pbo"], 4), cfg["gate"]["pboMax"], p["trials"], p["splits"]]], f"PBO = {p['pbo']:.3f}")


def c19(ev, cfg):
    d = ev["stats"]["dsr"]
    xs, ys = [c["n"] for c in d["curve"]], [c["dsr"] for c in d["curve"]]
    fig = {"data": [{"type": "scatter", "mode": "lines+markers", "x": xs, "y": ys, "name": "deflated Sharpe", "line": {"color": C}},
                    {"type": "scatter", "mode": "markers", "x": [d["effectiveN"], 2 * d["effectiveN"]], "y": [d["atN"], d["at2N"]], "name": "N and 2N", "marker": {"size": 12, "color": "role:front"}}],
           "layout": {"xaxis": {"title": {"text": "effective number of configurations tried"}}, "yaxis": {"title": {"text": "deflated Sharpe (probability)"}, "range": [0, 1]},
                      "shapes": [{"type": "line", "xref": "paper", "yref": "y", "x0": 0, "x1": 1, "y0": cfg["gate"]["dsrMin"], "y1": cfg["gate"]["dsrMin"], "line": {"dash": "dot", "color": "role:bad"}}]}}
    return charts.chart("c19", "Deflated Sharpe against the number of trials", "How the penalty for trying many configurations bites: the candidate's deflated Sharpe if N configurations had been tried.",
                        f"At the cumulative effective N and at twice it, at least {cfg['gate']['dsrMin']}.", fig, ["N", "deflated Sharpe"], [[a, round(b, 4)] for a, b in zip(xs, ys)], f"At N = {d['effectiveN']}: {d['atN']:.3f}; at 2N: {d['at2N']:.3f}")


def c20(ev, cfg):
    cr, dr = ev["detail"]["candidate"]["regimes"], ev["detail"]["default"]["regimes"]
    names = [r for r in REGIME_ORDER if r in cr or r in dr]
    get = lambda src, r, k: _pct((src.get(r) or {}).get(k))  # noqa: E731
    traces = []
    for i, (k, t) in enumerate((("cagr", "return, annualised (%)"), ("maxDrawdown", "max drawdown (%)"), ("share", "share of days (%)"))):
        ax = "" if i == 0 else str(i + 1)
        traces += [{"type": "bar", "name": "candidate", "x": names, "y": [get(cr, r, k) for r in names], "xaxis": f"x{ax}", "yaxis": f"y{ax}", "marker": {"color": C}, "showlegend": i == 0},
                   {"type": "bar", "name": "default", "x": names, "y": [get(dr, r, k) for r in names], "xaxis": f"x{ax}", "yaxis": f"y{ax}", "marker": {"color": D}, "showlegend": i == 0}]
    third = [[0, 0.30], [0.35, 0.65], [0.70, 1.0]]
    layout = {"barmode": "group", **{f"xaxis{'' if i == 0 else i + 1}": {"domain": third[i], "anchor": f"y{'' if i == 0 else i + 1}"} for i in range(3)},
              "yaxis": {"title": {"text": "annualised return (%)"}}, "yaxis2": {"anchor": "x2", "title": {"text": "max drawdown (%)"}}, "yaxis3": {"anchor": "x3", "title": {"text": "share of days (%)"}}}
    rows = [[r, get(cr, r, "share"), get(cr, r, "cagr"), get(cr, r, "maxDrawdown"), get(dr, r, "cagr"), get(dr, r, "maxDrawdown")] for r in names]
    return charts.chart("c20", "Regime breakdown", "Return, drawdown and share of time in each regime of the default classifier, candidate against default.", "The gain should not come from a single regime.",
                        {"data": traces, "layout": layout}, ["regime", "share %", "candidate return %", "candidate DD %", "default return %", "default DD %"], rows)


def c21(ev, s):
    pr = ev["detail"]["candidate"]["profile"]
    years = sorted(pr["fillsPerYear"])
    fig = {"data": [{"type": "bar", "name": "fills per year", "x": years, "y": [pr["fillsPerYear"][y] for y in years], "marker": {"color": C}, "xaxis": "x", "yaxis": "y"},
                    {"type": "scatter", "mode": "lines", "name": "gross exposure (candidate)", "x": list(s.date), "y": [_pct(v) for v in s["candidate.exposure"]], "xaxis": "x2", "yaxis": "y2", "line": {"color": "role:feasible"}},
                    {"type": "bar", "name": "turnover (x NAV)", "x": years, "y": [pr["turnover"].get(y) for y in years], "xaxis": "x3", "yaxis": "y3", "marker": {"color": "role:front"}},
                    {"type": "scatter", "mode": "lines+markers", "name": "charges (% NAV)", "x": years, "y": [_pct(pr["costDrag"].get(y)) for y in years], "xaxis": "x3", "yaxis": "y4", "line": {"color": "role:bad"}}],
           "layout": {"xaxis": {"domain": [0, 0.30]}, "yaxis": {"title": {"text": "fills"}}, "xaxis2": {"domain": [0.36, 0.65], "anchor": "y2"}, "yaxis2": {"anchor": "x2", "title": {"text": "exposure %"}},
                      "xaxis3": {"domain": [0.72, 1], "anchor": "y3"}, "yaxis3": {"anchor": "x3", "title": {"text": "turnover"}}, "yaxis4": {"anchor": "x3", "overlaying": "y3", "side": "right", "title": {"text": "charges %"}}}}
    rows = [[y, pr["fillsPerYear"][y], pr["turnover"].get(y), pr["costDrag"].get(y), None] for y in years]
    return charts.chart("c21", "Trade profile", "Fills per year, gross exposure over time, and turnover with the charges they cost, for the candidate.", "A swing system that trades at Rs 1 lakh: tens of fills a year, exposure near the floor or above, charges a small share of NAV.",
                        fig if years else None, ["year", "fills", "turnover", "charges / NAV", ""], rows)


def c22(ev, aud, cfg):
    rows = [[k, "flagged" if v["flagged"] else "ok", json.dumps({kk: vv for kk, vv in v.items() if kk != "flagged"}, default=str)[:200]] for k, v in aud["items"].items()]
    rows += [[n, "not assessed", "needs data checks outside this build"] for n in aud["notAssessed"]]
    b = ev["bounds"]
    pos = [(x["value"] - x["low"]) / (x["high"] - x["low"]) if x["high"] > x["low"] else 0.5 for x in b]
    fig = {"data": [{"type": "bar", "orientation": "h", "y": [x["name"][-34:] for x in b], "x": pos, "marker": {"color": ["role:bad" if p <= 0 or p >= 1 else "role:feasible" for p in pos]}, "name": "position within bounds"}],
           "layout": {"xaxis": {"range": [0, 1], "title": {"text": "position between the lower (0) and upper (1) bound"}}, "yaxis": {"autorange": "reversed"}, "margin": {"l": 240}, "height": 60 + 20 * len(b)}} if b else None
    return charts.chart("c22", "Exploit audit", "What the audit assessed and flagged, and what it cannot assess. The bar chart shows each searched parameter between its bounds; red is at a bound.",
                        "No flagged item: otherwise a person decides before promotion.", fig, ["item", "result", "evidence"], rows)


def c23(ev, cfg):
    d = ev["diff"]
    fig = {"data": [{"type": "scatter", "mode": "markers", "name": "live value", "y": [x["name"][-34:] for x in d], "x": [(x["old"] - x["low"]) / (x["high"] - x["low"]) if x["high"] > x["low"] else 0 for x in d], "marker": {"size": 10, "color": D}},
                    {"type": "scatter", "mode": "markers", "name": "candidate", "y": [x["name"][-34:] for x in d], "x": [(x["new"] - x["low"]) / (x["high"] - x["low"]) if x["high"] > x["low"] else 0 for x in d],
                     "marker": {"size": 12, "symbol": "diamond", "color": ["role:bad" if x["class"] in ("risk-limit", "model-input") else C for x in d]}}],
           "layout": {"xaxis": {"range": [-0.05, 1.05], "title": {"text": "position on the parameter's bounds range"}}, "yaxis": {"autorange": "reversed"}, "margin": {"l": 240}, "height": 80 + 22 * len(d)}} if d else None
    return charts.chart("c23", "Parameter diff", "Old (live) and new value of every changed key on its bounds range; risk-limit and model-input changes are red.", "Only changes you are willing to make by hand to the config.",
                        fig, ["parameter", "class", "live", "candidate", "low", "high"], [[x["name"], x["class"], x["old"], x["new"], x["low"], x["high"]] for x in d])


def c24(cfg, folder: Path):
    """Holdout and shadow. Before the one-shot holdout is scored this is a locked placeholder with no holdout data; after, the result is drawn once against its criterion."""
    hold = json.loads((folder / "holdout.json").read_text(encoding="utf-8")) if (folder / "holdout.json").exists() else None
    shadow = json.loads((folder / "shadow.json").read_text(encoding="utf-8")) if (folder / "shadow.json").exists() else None
    crit = f"Holdout: pre-registered pass criterion. Shadow: cumulative tracking gap within +-{cfg['shadow']['trackingGapPp']} pp over {cfg['shadow']['weeks']} weeks."
    how = "The last two years stay locked until the one-shot holdout is scored; the 13-week shadow period starts only after promotion."
    if not hold or hold.get("status") != "scored":
        return charts.chart("c24", "Holdout and shadow", how + " Neither has happened for this candidate.", crit, None, ["stage", "status"],
                            [["holdout", "locked: not scored (one look, after the gate and a clean audit)"], ["shadow", f"not started ({cfg['shadow']['weeks']} weekly rebalances, gap band +-{cfg['shadow']['trackingGapPp']} pp)"]])
    names = ["CAGR (%)", "drawdown depth (%)"]
    cand, dflt = hold["candidate"], hold["default"]
    data = [{"type": "bar", "name": "candidate", "x": names, "y": [_pct(cand["metrics"].get("cagr")), _pct(cand["depth"])], "marker": {"color": C}},
            {"type": "bar", "name": "live default", "x": names, "y": [_pct(dflt["metrics"].get("cagr")), _pct(dflt["depth"])], "marker": {"color": D}}]
    rows = [[k, v["value"], v["limit"], "pass" if v["passed"] else "FAIL"] for k, v in hold["checks"].items()]
    layout = {"barmode": "group", "yaxis": {"title": {"text": f"holdout {hold['window'][0]} to {hold['window'][1]} (%)"}}}
    note = f"Holdout scored once: {'passed' if hold['passed'] else 'NOT passed'}. With about two years the standard error of a Sharpe ratio is about 0.85: this detects catastrophic failure only."
    if shadow and shadow.get("table"):
        t = shadow["table"]
        data += [{"type": "scatter", "mode": "lines+markers", "name": "shadow tracking gap (pp)", "x": [r["date"] for r in t], "y": [r["gapPp"] for r in t], "xaxis": "x2", "yaxis": "y2", "line": {"color": "role:front"}}]
        band = shadow["bandPp"]
        layout.update({"xaxis": {"domain": [0, 0.4]}, "xaxis2": {"domain": [0.55, 1], "anchor": "y2", "title": {"text": "week"}}, "yaxis2": {"anchor": "x2", "title": {"text": "gap, pp"}},
                       "shapes": [{"type": "rect", "xref": "x2 domain", "yref": "y2", "x0": 0, "x1": 1, "y0": -band, "y1": band, "fillcolor": "role:good", "opacity": 0.15, "line": {"width": 0}}]})
        rows += [["shadow", shadow["status"], f"week {shadow['weeks']} of {shadow['of']}", f"gap {shadow.get('gapPp', 0):+.2f} pp"]]
        note += f" Shadow: {shadow['status']}."
    return charts.chart("c24", "Holdout and shadow", how, crit, {"data": data, "layout": layout}, ["check", "value", "limit", "result"], rows, note)


def candidate_charts(ev: dict, verdict: dict, series: pd.DataFrame, cfg: dict, folder: Path) -> list[dict]:
    aud = verdict["audit"]
    return [c11(ev, series), c12(ev, series), c13(ev, cfg), c14(ev, cfg), c15(ev, cfg), c16(ev, cfg), c17(ev, cfg), c18(ev, cfg), c19(ev, cfg), c20(ev, cfg), c21(ev, series),
            c22(ev, aud, cfg), c23(ev, cfg), c24(cfg, folder)]


def candidate_report(folder: Path, cfg: dict) -> Path:
    ev = json.loads((folder / "evidence.json").read_text(encoding="utf-8"))
    verdict = json.loads((folder / "gate.json").read_text(encoding="utf-8"))
    series = pd.read_parquet(folder / "series.parquet")
    cs = charts.clean_all(candidate_charts(ev, verdict, series, cfg, folder))
    c = verdict["checks"]
    tiles = [{"label": "gate", "value": "passed" if verdict["passed"] else "not met"}, {"label": "deflated Sharpe (N / 2N)", "value": f"{c['deflatedSharpe']['value']:.2f} / {c['deflatedSharpe']['at2N']:.2f}"},
             {"label": "PBO", "value": f"{c['pbo']['value']:.2f}"}, {"label": "SPA p vs default", "value": f"{c['spa']['p']:.3f}"}, {"label": "neighbours within tolerance", "value": f"{c['neighbourhood']['share']:.0%}"},
             {"label": "exploit audit", "value": "clean" if verdict["audit"]["passed"] else f"{len(verdict['audit']['flagged'])} flagged"},
             {"label": "effective N", "value": c["deflatedSharpe"]["effectiveN"]}, {"label": "study / trial", "value": f"{ev['candidate']['study']} / {ev['candidate']['trial']}"}]
    warn = [f"Gate not met: {', '.join(verdict['failed'])}"] if not verdict["passed"] else []
    if verdict.get("label"):
        warn.append(verdict["label"])
    warn.append("HPO is unlikely to yield a statistically demonstrable improvement over sensible defaults on about 13 years of data: read this as a feasibility and robustness result unless SPA says otherwise.")
    out = folder / "report.html"
    out.write_text(report.page(f"Candidate {ev['candidate']['trialId']}", f"study {ev['candidate']['study']}, schema in the study, capital-specific result (re-run when capital changes). Pre-holdout data only.", warn, tiles, cs), encoding="utf-8")
    return out
