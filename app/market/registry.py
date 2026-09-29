"""Append-only ticker registry (registry.csv) fed by upstream's bucket files."""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from app.market.common import IST, atomic, write_csv

COLS = ["nse_symbol", "yahoo_symbol", "status", "first_seen", "last_seen_upstream", "no_data_days", "inactive_since", "absent_since_inactive"]


def load(path: Path) -> pd.DataFrame:
    """Registry indexed by nse_symbol."""
    if not path.exists():
        return pd.DataFrame(columns=COLS[1:], index=pd.Index([], name="nse_symbol"), dtype=object)
    df = pd.read_csv(path, dtype=str, keep_default_na=False).set_index("nse_symbol")
    df["no_data_days"] = df.no_data_days.astype(int)
    df["absent_since_inactive"] = df.absent_since_inactive == "True"
    return df.astype(object)


def save(reg: pd.DataFrame, path: Path) -> None:
    write_csv(reg.reset_index()[COLS], path)


def upstream_ok(cfg: dict, today: str) -> bool:
    """Upstream health gate: status healthy and checkedAt is today (IST)."""
    try:
        h = json.loads(Path(cfg["paths"]["upstreamHealth"]).read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(h["checkedAt"]).astimezone(IST).date().isoformat()
        return h["status"] == "healthy" and checked == today
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def upstream_symbols(cfg: dict) -> dict[str, str]:
    """symbol -> newest upstream file date containing it, from storage/<Bucket>_<date>.csv."""
    seen: dict[str, str] = {}
    for f in Path(cfg["paths"]["upstreamStorage"]).glob("*_*.csv"):
        day = f.stem.rsplit("_", 1)[1]
        for sym in pd.read_csv(f, dtype=str, keep_default_na=False).get("Symbol", []):
            if sym:
                seen[sym] = max(day, seen.get(sym, ""))
    return seen


def refresh(reg: pd.DataFrame, seen: dict[str, str]) -> list[str]:
    """Append new symbols, update last_seen_upstream, re-activate inactive tickers that left and came back.

    Returns the re-activated symbols. Symbols are never deleted.
    """
    back = []
    for sym, day in seen.items():
        if sym not in reg.index:
            reg.loc[sym] = [f"{sym}.NS", "active", day, day, 0, "", False]
            continue
        reg.at[sym, "last_seen_upstream"] = max(day, reg.at[sym, "last_seen_upstream"])
        if reg.at[sym, "status"] == "inactive" and reg.at[sym, "absent_since_inactive"]:
            reg.loc[sym, ["status", "no_data_days", "inactive_since", "absent_since_inactive"]] = ["active", 0, "", False]
            back.append(sym)
    reg.loc[(reg.status == "inactive") & ~reg.index.isin(seen), "absent_since_inactive"] = True
    return back
