"""trading_journal.csv: one row per closed or reduced position. The Analyst owns the auto columns, you own notes/reason/tags.

Rows are only ever added. A row the Analyst estimated (ESTIMATED) that you correct by editing its entry/exit fields is
recomputed on the next run and promoted to MANUAL_VERIFIED. Every other cell is written back exactly as it was read.
"""

import hashlib
from pathlib import Path

import pandas as pd

from app.analyst import costs
from app.market.common import write_csv

COLS = ["trade_id", "ticker", "qty", "entry_date", "entry_price", "exit_date", "exit_price", "pl", "pl_pct", "est_charges",
        "net_pl", "source", "entry_source", "auto_hash", "run_id", "notes", "reason", "tags"]
HASHED = ["qty", "entry_date", "entry_price", "exit_date", "exit_price", "pl", "pl_pct", "est_charges", "net_pl", "source"]


def auto_hash(row) -> str:
    return hashlib.sha1("|".join(str(row[c]) for c in HASHED).encode()).hexdigest()[:16]


def derived(qty: float, entry: float, exit_: float, c: dict) -> dict:
    """pl, pl_pct, est_charges and net_pl (charges only: the real fill price is already known, so no slippage)."""
    pl = (exit_ - entry) * qty
    charges = costs.buy_charges(c, qty * entry) + costs.sell_charges(c, qty * exit_)
    return {"pl": f"{pl:.2f}", "pl_pct": f"{(exit_ / entry - 1) * 100:.2f}" if entry else "", "est_charges": f"{charges:.2f}", "net_pl": f"{pl - charges:.2f}"}


def new_row(sell: dict, c: dict, run_id: str) -> dict:
    row = {
        "trade_id": f"{sell['ticker']}-{sell['exit_date']}-{sell['n']}", "ticker": sell["ticker"], "qty": str(int(sell["qty"])),
        "entry_date": sell["entry_date"], "entry_price": f"{sell['entry_price']:.4f}", "exit_date": sell["exit_date"],
        "exit_price": f"{sell['exit_price']:.4f}", "source": sell["source"], "entry_source": sell["entry_source"], "run_id": run_id,
        **derived(sell["qty"], sell["entry_price"], sell["exit_price"], c), "notes": "", "reason": "", "tags": "",
    }
    row["auto_hash"] = auto_hash(row)
    return row


def read(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=COLS, dtype=str)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in COLS:
        if col not in df:
            df[col] = ""
    return df


def sync(path: Path, sells: list[dict], c: dict, run_id: str) -> dict:
    """Add the sells not yet journaled and promote corrected ESTIMATED rows; returns counts for the digest.

    Safe to repeat: rows are keyed by trade_id, so a run that could not write (file open in Excel) simply catches up later.
    """
    df = read(path)
    have = set(df.trade_id)
    fresh = [new_row(s, c, run_id) for s in sells if f"{s['ticker']}-{s['exit_date']}-{s['n']}" not in have]
    promoted, bad = [], []
    for i, row in df.iterrows():
        if row.source != "ESTIMATED" or not row.auto_hash or auto_hash(row) == row.auto_hash:
            continue
        try:
            qty, entry, exit_ = int(float(row.qty)), float(row.entry_price), float(row.exit_price)
        except ValueError:
            bad.append(row.trade_id)
            continue
        for k, v in {**derived(qty, entry, exit_, c), "source": "MANUAL_VERIFIED"}.items():
            df.at[i, k] = v
        df.at[i, "auto_hash"] = auto_hash(df.loc[i])
        promoted.append(row.trade_id)
    if fresh or promoted:
        out = pd.concat([df, pd.DataFrame(fresh, columns=COLS)], ignore_index=True) if fresh else df
        extra = [c_ for c_ in out.columns if c_ not in COLS]  # columns you added yourself stay, after the known ones
        write_csv(out[COLS + extra], path)
        df = out
    return {"added": len(fresh), "promoted": promoted, "unreadable": bad, "estimated": [t for t in df.trade_id[df.source == "ESTIMATED"]]}
