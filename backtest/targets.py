"""Weekly targets for a simulated rebalance date: the regime (computed once, causal) and the per-bucket picks.

This replaces analyst.signals.build_targets for history. That function reads bucket files with a 7-day age check, builds its
own Store and reads the registry, so it cannot run for a past date. The selection itself is the app's selector.select_bucket,
fed with slices of precomputed panels; tests/test_targets.py compares the result with `python -m app.analyst.signals --as-of`.

Not reproduced, on purpose: the live data-quality gates (maxMissingShare, bucket-file age) and the registry's active flag
(v1 universe = today's bucket members that have stored history), and the delta/holdings fields decide() does not read.
"""

from bisect import bisect_right

import pandas as pd

from app.analyst import regime, selector
from backtest.pit import PitData

PICK_KEYS = ("ticker", "rank", "momentum", "price", "trendMa", "score")


class Targets:
    """cfg is the (possibly trial-overridden) analyst config; data the point-in-time store."""

    def __init__(self, data: PitData, cfg: dict):
        self.cfg, self.rows = cfg, selector.rows_needed(cfg)
        close = pd.Series(data.index.Close.to_numpy(float), index=pd.DatetimeIndex(data.index.Date))
        if len(close) < cfg["regime"]["minRows"]:
            raise ValueError(f"index has {len(close)} rows, need at least {cfg['regime']['minRows']}")
        self.history = regime.regime_history(close, cfg["regime"]["persistenceWeeks"], regime.windows_of(cfg))
        self.dates = list(self.history.date)
        self.active = list(self.history.active_regime)
        adj, value = data.panels()
        self.panels = {}
        for b, symbols in data.buckets.items():
            cols = [s for s in symbols if s in adj.columns]
            a = adj[cols].dropna(how="all")  # the union of the tickers' own dates, as selector.load_panel builds it
            self.panels[b] = (a, value[cols].loc[a.index], a.index)

    def regimes(self, asof: str) -> tuple[dict, list[str]]:
        """({raw, active}, active regimes of the weekly rows on or before asof, oldest first), as app.risk.run.regime_now reads them."""
        n = bisect_right(self.dates, asof)
        if n == 0:
            return {"raw": "Unknown", "active": "Unknown"}, []
        row = self.history.iloc[n - 1]
        return {"raw": row.raw_regime, "active": row.active_regime}, self.active[:n]

    def build(self, rebalance: str) -> dict | None:
        """The targets dict for a rebalance date, None when the regime history has no row for it (live: the Gate)."""
        if rebalance not in set(self.dates):
            return None
        row = self.history[self.history.date == rebalance].iloc[0]
        cfg, active = self.cfg, row.active_regime
        buckets = {}
        for b, (adj, value, index) in self.panels.items():
            strategy = cfg["strategies"].get(active, {}).get(b)
            picks = []
            if strategy and strategy["top_n"] > 0 and cfg["composition"].get(b, 0) > 0:
                n = index.searchsorted(pd.Timestamp(rebalance), side="right")
                lo = max(0, n - self.rows)
                picks, _ = selector.select_bucket(adj.iloc[lo:n], value.iloc[lo:n], rebalance, strategy, active, cfg["selector"])
            buckets[b] = {"strategy": strategy, "selected": picks}
        return {"schemaVersion": 1, "status": "ok", "rebalanceDate": rebalance,
                "regime": {"index": cfg["regime"]["index"], "raw": row.raw_regime, "active": active, "pending": row.pending_regime,
                           "pendingRemainingDays": int(row.pending_remaining_days), "persistenceWeeks": cfg["regime"]["persistenceWeeks"]},
                "composition": cfg["composition"], "buckets": buckets}
