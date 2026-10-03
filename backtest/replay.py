"""The judge's day loop: one call of the app's own decide() per simulated trading day.

Day t, in the order the live pipeline runs them: signals queued by earlier days fill at t's Open (fills.execute, the shadow
portfolio's maths); then the Context for t is built from the as-of view, targets are built on a rebalance day, and
app.risk.run.decide() produces the day's actions, NAV row and state. The actions wait for the next session's Open.

decide() reads its state and NAV history from disk. Two names are replaced for the duration of a run, inside this process only:
app.risk.run.load_state and app.risk.nav.read_nav now return in-memory objects. Nothing under app/ is edited or written.
tests/backtest/test_replay.py compares the result with a run through the app's real commit() on a temp folder.
"""

import logging
from collections import Counter
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from app.analyst import regime
from app.market.common import IST
from app.market.tradingcal import Calendar
from app.risk import nav, run as risk_run
from app.risk.common import TABLES, Context, Portfolio
from backtest import fills as fillmod
from backtest.pit import PitData, PitStore
from backtest.targets import Targets

LOG = logging.getLogger("backtest.replay")
NO_PATH = Path("-")  # decide() receives paths only to hand them to the two replaced readers


def no_surveillance(asof: str) -> dict:
    """The 'proxy off' surveillance dict: new buys allowed, nothing flagged. surv_proxy.py will provide the real one."""
    data = {"asOf": asof, "fetchedAt": asof, "sources": {}, "asm": {"LT": {}, "ST": {}}, "gsm": {}, "t2t": [], "bandPct": {}}
    return {"data": data, "status": "ok", "asOf": asof, "entries": True, "exits": data}


class Memory:
    """What decide() would read from state/*.csv, ladder_state.json and nav_actual.csv, held in memory."""

    def __init__(self):
        self.state = {name: pd.DataFrame(columns=cols, dtype=str) for name, (_, cols) in TABLES.items()} | {"ladder": None}
        self.rows: list[dict] = []

    def load_state(self, _folder) -> dict:
        return dict(self.state)

    def read_nav(self, _path) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=nav.NAV_COLS) if self.rows else pd.DataFrame({c: pd.Series(dtype=float) for c in nav.NAV_COLS}).astype({"date": str, "active_regime": str})

    def commit(self, out: dict) -> None:
        """What save_state / upsert_nav and the next load would give back: tables as text, '' for missing."""
        self.state = {name: out["state"][name].fillna("").astype(str) for name in TABLES} | {"ladder": out["state"]["ladder"]}
        self.rows.append(out["nav_row"])


@contextmanager
def in_memory(mem: Memory):
    with patch.object(risk_run, "load_state", mem.load_state), patch.object(nav, "read_nav", mem.read_nav):
        yield


@dataclass
class Result:
    nav: pd.DataFrame
    fills: pd.DataFrame
    signals: list[dict] = field(default_factory=list)
    warnings: Counter = field(default_factory=Counter)
    targets: dict = field(default_factory=dict)  # rebalance date -> targets dict
    dividends: list[dict] = field(default_factory=list)
    vanished: list[dict] = field(default_factory=list)  # positions closed because the ticker stopped trading


def simulate(data: PitData, targets: Targets, risk_cfg: dict, start: str, end: str | None = None, capital: float = 100000.0,
             surveillance=no_surveillance, keep_signals: bool = False, carry_over_days: int = 7, reference_dir: Path | None = None,
             dividends=None, vanish_haircut: float = 0.0) -> Result:
    """Replay every index trading day from start to end (inclusive) and return the NAV rows, fills and counters.

    reference_dir: run decide() through the app's real files and commit() in that folder instead of the in-memory readers. It is
    the slow reference the decision-parity test compares with; production runs leave it None.
    vanish_haircut: a held ticker whose series has ended is closed on the first session after its last row at that row's Close less this
    share (0 = at the last price, 1 = a total loss); Result.vanished lists the exits.
    """
    cal = Calendar(risk_cfg["paths"]["calendar"])
    if risk_cfg["buckets"] != list(targets.names):
        raise ValueError(f"risk buckets {risk_cfg['buckets']} differ from the universe buckets {list(targets.names)}")
    book, mem, store = fillmod.Book(capital), Memory(), PitStore(data)
    carry, rows = carry_over_days, []
    queue: list[dict] = []
    result = Result(pd.DataFrame(), pd.DataFrame())
    newest, last_good = None, None
    days = [d for d in data.index.Date if d >= start and (end is None or d <= end)]
    with in_memory(mem) if reference_dir is None else nullcontext():
        for asof in days:
            store.asof = asof
            if dividends is not None:
                result.dividends += dividends.credit(book, asof)
            result.vanished += fillmod.vanish(book, asof, lambda t: (data.last_date(t) or asof) < asof, data.last_close, vanish_haircut)
            queue = [s for s in queue if (date.fromisoformat(asof) - date.fromisoformat(s["asOf"])).days <= carry]
            _, fill_warnings, cash = fillmod.execute(book, risk_cfg["costs"], asof, queue, lambda t: data.open_price(t, asof), carry)
            d = date.fromisoformat(asof)
            rebalance = regime.live_rebalance_date(cal, d) == d
            T = targets.build(asof) if rebalance else None
            newest = T or newest
            ctx = Context(risk_cfg, asof, cal, store, LOG, data.members(asof), last_good=last_good, surv=surveillance(asof), rebalance=rebalance, targets=T,
                          windows={b: e["strategy"]["stock_trend_ma"] for b, e in newest["buckets"].items() if e.get("strategy")} if newest else {})
            now = datetime(d.year, d.month, d.day, 21, 0, tzinfo=IST)
            if reference_dir is None:
                pf = Portfolio("backtest", NO_PATH, NO_PATH, NO_PATH, book.frame(), cash)
            else:
                pf = Portfolio("backtest", reference_dir / "state", reference_dir / "nav.csv", reference_dir / "signals", book.frame(), cash)
            out = risk_run.decide(ctx, pf, None, data.bench_close(asof), targets.regimes(asof), now, f"backtest-{asof}")
            if reference_dir is None:
                mem.commit(out)
            else:
                risk_run.commit(pf, out, now)
            rows.append(out["nav_row"])
            sig = out["signal"]
            queue.append({"asOf": asof, "executionDate": sig["executionDate"], "actions": sig["actions"]})
            last_good = asof
            result.warnings.update(set(fill_warnings) | set(sig["warnings"]))
            if T:
                result.targets[asof] = T
            if keep_signals:
                result.signals.append(sig)
    result.nav = pd.DataFrame(rows, columns=nav.NAV_COLS)
    result.fills = pd.DataFrame(book.fills)
    return result
