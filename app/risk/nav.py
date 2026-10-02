"""Cash ledger, NAV and the time-weighted index. NAV covers the tracked book plus ledger cash."""

from datetime import date
from pathlib import Path

import pandas as pd

from app.analyst import costs
from app.market.common import safe_path, write_csv
from app.risk.common import FLOW_TYPES, read_table

NAV_COLS = ["date", "positions_value", "cash", "nav", "flow", "twr_index", "bench_close", "active_regime", "rung"]
FLOW_COLS = ["date", "type", "amount_inr", "note"]
CASH_FILL_KINDS = ("FILL", "ESTIMATED", "UNKNOWN", "BROKER_AVG")  # SEED rows predate the ledger; SET rows are corporate actions
JUMP = 0.15


def read_flows(cfg: dict, today: str) -> pd.DataFrame:
    """cash_flows.csv with typed columns; raises ValueError naming the file when it breaks the TDD's rules."""
    name = cfg["capital"]["cashFlowsFile"]
    df = read_table(safe_path(name), FLOW_COLS)
    if (df.type == "OPENING").sum() != 1:
        raise ValueError(f"{name} must have exactly one OPENING row (the cash balance at the start of the ledger)")
    try:
        bad = df[~df.type.isin(FLOW_TYPES)]
        if len(bad) or any(date.fromisoformat(d) > date.fromisoformat(today) for d in df.date):
            raise ValueError
        df["amount"] = df.amount_inr.astype(float)
    except ValueError:
        raise ValueError(f"{name}: types must be one of {FLOW_TYPES}, dates ISO and not in the future, amounts numeric") from None
    return df


def signed(row) -> float:
    """Cash effect of a flow row: the type gives the sign (OTHER keeps its own sign)."""
    return -row.amount if row.type == "WITHDRAWAL" else row.amount


def external_flow(flows: pd.DataFrame, after: str, upto: str) -> float:
    """Deposits, withdrawals and OTHER dated in (after, upto]. Opening cash and dividends are not external flows."""
    f = flows[flows.type.isin(["DEPOSIT", "WITHDRAWAL", "OTHER"]) & (flows.date > after) & (flows.date <= upto)]
    return float(sum(signed(r) for r in f.itertuples()))


def fill_cash(cfg: dict, fills: pd.DataFrame) -> float:
    """Net cash effect of BUY / SELL fills: sales minus purchases, with charges from the Analyst's cost model."""
    total = 0.0
    f = fills[fills.side.isin(["BUY", "SELL"])]
    for (_, _, side), g in f.groupby(["ticker", "trade_date", "side"]):
        n = float((g.qty.astype(float) * g.price.astype(float)).sum())
        total += n - costs.sell_charges(cfg["costs"], n) if side == "SELL" else -(n + costs.buy_charges(cfg["costs"], n))
    return total


def ledger_cash(cfg: dict, flows: pd.DataFrame, fills: pd.DataFrame, asof: str) -> float:
    """Opening cash + deposits + dividends - withdrawals + SELL fills - BUY fills (with charges), from the OPENING date to asof."""
    opening = flows[flows.type == "OPENING"].iloc[0]
    rows = flows[(flows.date >= opening.date) & (flows.date <= asof)]
    money = float(sum(signed(r) for r in rows.itertuples()))
    mine = fills[(fills.trade_date >= opening.date) & (fills.trade_date <= asof) & fills.kind.isin(CASH_FILL_KINDS)]
    return money + fill_cash(cfg, mine)


def read_nav(path: Path) -> pd.DataFrame:
    df = read_table(path, NAV_COLS)
    for c in NAV_COLS[1:6] + ["bench_close"]:
        df[c] = pd.to_numeric(df[c])
    df["rung"] = pd.to_numeric(df.rung)
    return df


def upsert_nav(path: Path, row: dict) -> None:
    """Add or replace the row of row['date'] (a rerun never duplicates)."""
    df = read_table(path, NAV_COLS)
    df = df[df.date != row["date"]]
    out = pd.concat([df, pd.DataFrame([row], columns=NAV_COLS).astype(str)], ignore_index=True).sort_values("date")
    write_csv(out, path)


def nav_row(asof: str, positions_value: float, cash: float, flow: float, prev: pd.Series | None) -> tuple[dict, bool]:
    """(row without bench/regime/rung, NAV_JUMP): twr = previous twr x (nav - flow) / previous nav; 1.0 on the first row."""
    nav = positions_value + cash
    twr = 1.0 if prev is None or prev.nav <= 0 else float(prev.twr_index) * (nav - flow) / prev.nav
    jump = prev is not None and prev.nav > 0 and flow == 0 and abs(nav / prev.nav - 1) > JUMP
    return {"date": asof, "positions_value": round(positions_value, 2), "cash": round(cash, 2), "nav": round(nav, 2),
            "flow": round(flow, 2), "twr_index": round(twr, 10)}, bool(jump)
