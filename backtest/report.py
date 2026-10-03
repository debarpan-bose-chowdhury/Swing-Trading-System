"""Metrics, post-tax objectives, stress windows and the labels every report must carry. Reuses app.risk.evaluator's maths."""

import pandas as pd

from app.risk import evaluator

LABELS = ["Upper bound: survivorship-biased (today's bucket members, today's bucket labels)",
          "Charges at current Angel One rates for all years",
          "Tax is an estimate: no surcharge, loss carry-forward, 2018 grandfathering or dividend income tax"]


def _returns(curve: pd.Series) -> pd.Series:
    return curve.pct_change().dropna()


def stress(nav: pd.DataFrame, windows: dict) -> dict:
    """Pre-tax strategy vs benchmark for each stress span that overlaps the simulated range."""
    s = nav.set_index("date")
    strat, bench = _returns(s.nav), _returns(s.bench_close)
    out = {}
    for name, spans in windows.items():
        rows = []
        for a, b in spans:
            r, m = strat[(strat.index >= a) & (strat.index <= b)], bench[(bench.index >= a) & (bench.index <= b)]
            if len(r) < 2:
                rows.append({"span": [a, b], "days": len(r), "note": "outside the simulated range"})
                continue
            p = evaluator.perf(r, 0.0)
            rows.append({"span": [a, b], "days": len(r), "strategy": evaluator.compound(r), "maxDrawdown": p.get("maxDrawdown"), "benchmark": evaluator.compound(m)})
        out[name] = rows
    return out


def build(result, taxes: dict, post: pd.Series, risk_cfg: dict, bt_cfg: dict, surv_on: bool, meta: dict) -> dict:
    """The run report. result: replay.Result; taxes: tax.assess output; post: tax.post_tax_curve output."""
    rf = risk_cfg["evaluator"]["riskFreeRatePct"]
    nav = result.nav
    pre = pd.Series(nav.nav.to_numpy(float), index=nav.date)
    perf_pre, perf_post = evaluator.perf(_returns(pre), rf), evaluator.perf(_returns(post), rf)
    bench = pd.Series(nav.bench_close.to_numpy(float), index=nav.date)
    f = result.fills
    avg_nav = float(nav.nav.mean())
    traded = float((f.qty * f.price).sum()) if len(f) else 0.0
    return {
        **meta,
        "labels": LABELS + ["Tax schedule is a DRAFT until backtest.json tax.confirmed is true" if not bt_cfg["tax"]["confirmed"] else "Tax schedule confirmed"]
                  + (["Surveillance: PROVISIONAL price-behaviour proxy"] if surv_on else ["Surveillance NOT modelled (entries allowed, nothing flagged)"]),
        "window": {"start": nav.date.iloc[0], "end": nav.date.iloc[-1], "days": len(nav)},
        "objectives": {"postTaxCagr": perf_post.get("cagr"), "maxDrawdown": perf_post.get("maxDrawdown"), "ulcerIndex": perf_post.get("ulcerIndex")},
        "preTax": perf_pre, "postTax": perf_post, "benchmark": evaluator.perf(_returns(bench), rf),
        "trades": {"fills": len(f), "turnover": round(traded / 2 / avg_nav, 4) if avg_nav else None,
                   "costDrag": round(float(f.charges.sum()) / avg_nav, 6) if len(f) and avg_nav else 0.0},
        "exposure": {"timeInMarket": round(float((nav.positions_value > 0).mean()), 4)},
        "dividendsInr": round(sum(d["amountInr"] for d in result.dividends), 2),
        "tax": taxes,
        "stress": stress(nav, bt_cfg["stress"]),
        "warnings": dict(result.warnings),
    }
