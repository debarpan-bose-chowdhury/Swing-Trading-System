"""Shadow portfolio: follows every signal exactly. Simulated fills (previous signals at the next raw Open plus slippage and
charges) are kept in the Ledger's fills layout, and the book is replayed from them, so a rerun of a day is idempotent."""

import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from app.analyst import costs
from app.analyst.ledger import FILL_COLS, read_fills, replay
from app.market.common import write_csv
from app.risk.common import CARRY_OVER_DAYS, Context, Portfolio, is_date, read_json, read_table, risk_dir

LOOKBACK_DAYS = CARRY_OVER_DAYS  # the shadow.carryOverDays default


def carry_over_days(cfg: dict) -> int:
    return cfg.get("shadow", {}).get("carryOverDays", LOOKBACK_DAYS)


def root(cfg: dict) -> Path:
    return risk_dir(cfg) / "shadow"


def portfolio(cfg: dict, book: pd.DataFrame, cash: float) -> Portfolio:
    r = root(cfg)
    return Portfolio("shadow", r / "state", risk_dir(cfg) / "nav" / "nav_shadow.csv", r / "signals", book, cash)


def seed(cfg: dict, actual: Portfolio, asof: str) -> None:
    """Start: copy the actual book (as SEED / UNKNOWN fills keeping entry dates) and cash."""
    rows = [{"fill_key": f"seed-{r.ticker}", "trade_date": r.entry_date if is_date(r.entry_date) else asof, "ticker": r.ticker,
             "broker_symbol": r.ticker, "side": "BUY", "qty": int(r.qty), "price": float(r.avg_price), "fill_time": "", "order_id": "",
             "run_id": "shadow-start", "kind": "SEED" if is_date(r.entry_date) else "UNKNOWN"} for r in actual.book.itertuples()]
    write_csv(pd.DataFrame(rows, columns=FILL_COLS), root(cfg) / "fills.csv")
    write_csv(pd.DataFrame([{"date": asof, "cash": round(actual.cash, 2)}]), root(cfg) / "cash.csv")


def apply(ctx: Context) -> Portfolio:
    """Apply the previous run's shadow signals at asOf's Open; returns the shadow portfolio valued from its replayed book."""
    cfg, asof, c = ctx.cfg, ctx.asof, ctx.cfg["costs"]
    r, carry = root(cfg), carry_over_days(cfg)
    fills = read_fills(r / "fills.csv")
    fills = fills[~((fills.trade_date == asof) & (fills.kind == "FILL"))]  # a rerun starts from the state before today's fills
    cash_rows = read_table(r / "cash.csv", ["date", "cash"])
    before = cash_rows[cash_rows.date < asof]
    cash = float((before if len(before) else cash_rows).cash.astype(float).iloc[-1 if len(before) else 0])
    held = {t: int(q) for t, q in replay(fills)[0].set_index("ticker").qty.items()} if len(fills) else {}
    new = []
    files = sorted(f for f in (r / "signals").glob("signals_*.json") if "superseded" not in f.name)
    for f in files:
        sig = read_json(f)
        if not sig or sig["asOf"] >= asof or sig["executionDate"] > asof or (date.fromisoformat(asof) - date.fromisoformat(sig["asOf"])).days > carry:
            continue
        for a in sig["actions"]:
            t, side = a["ticker"], a["side"]
            if any(x["ticker"] == t and x["side"] == side and x["trade_date"] >= sig["executionDate"] for x in new) or (
                    (fills.ticker == t) & (fills.side == side) & (fills.trade_date >= sig["executionDate"]) & (fills.kind == "FILL")).any():
                continue  # already executed (a STOP repeats daily until the position is gone)
            df = ctx.hist(t)
            op = float(df.Open.iloc[-1]) if len(df) and df.Date.iloc[-1] == asof else float("nan")
            if not np.isfinite(op) or op <= 0:
                ctx.warn(f"SHADOW_NO_OPEN:{t}")
                continue
            bps = costs.slippage(c, a["bucket"], 1.0)
            price = op * (1 + bps) if side == "BUY" else op * (1 - bps)
            qty = int(a["qty"])
            if side == "SELL":
                qty = min(qty, held.get(t, 0))
            else:
                qty = min(qty, math.floor(cash / price))
                while qty and qty * price + costs.buy_charges(c, qty * price) > cash:
                    qty -= 1
                if qty < a["qty"]:
                    ctx.warn(f"SHADOW_SHORTFALL:{t}")
            if qty <= 0:
                continue
            n = qty * price
            cash += n - costs.sell_charges(c, n) if side == "SELL" else -(n + costs.buy_charges(c, n))
            held[t] = held.get(t, 0) + (qty if side == "BUY" else -qty)
            new.append({"fill_key": f"shadow-{asof}-{t}-{side}", "trade_date": asof, "ticker": t, "broker_symbol": t, "side": side, "qty": qty,
                        "price": round(price, 4), "fill_time": "", "order_id": "", "run_id": f"shadow-{asof}", "kind": "FILL"})
    fills = pd.concat([fills, pd.DataFrame(new, columns=FILL_COLS)], ignore_index=True) if new else fills
    write_csv(fills.astype({"qty": "int64"}), r / "fills.csv")
    cash_rows = cash_rows[cash_rows.date != asof]
    write_csv(pd.concat([cash_rows, pd.DataFrame([{"date": asof, "cash": round(cash, 2)}])], ignore_index=True).sort_values("date"), r / "cash.csv")
    book = replay(fills)[0] if len(fills) else pd.DataFrame(columns=["ticker", "qty", "avg_price", "entry_date", "entry_source"])
    return portfolio(cfg, book, cash)
