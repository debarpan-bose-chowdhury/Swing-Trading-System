"""Symbol links: a renamed company must not look like a death plus a birth.

Sources, strongest first: your manual table (backtest/config/symbol_map.csv: Old,New,Note), NSE's symbol-change file (once its
columns are confirmed with --probe-symbolchange), and the ISIN chain in the bhavcopy (the same ISIN under a different symbol later).
Early files (2007 to about 2010) carry no ISIN, so renames from those years need the manual table or the NSE file.
"""

from pathlib import Path

import pandas as pd

LINK_COLS = ["Old", "New", "Date", "Source", "GapDays"]


def isin_links(rows: pd.DataFrame) -> pd.DataFrame:
    """rows: Ticker, Date, Isin. A link Old -> New where one ISIN moves to a new symbol: the old symbol's last date precedes the new one's first."""
    r = rows[rows.Isin != ""]
    if r.empty:
        return pd.DataFrame(columns=LINK_COLS)
    span = r.groupby(["Isin", "Ticker"]).Date.agg(first="min", last="max").reset_index().sort_values(["Isin", "first"])
    out = []
    for _, g in span.groupby("Isin"):
        for a, b in zip(g.itertuples(), g.iloc[1:].itertuples(), strict=False):
            if a.Ticker != b.Ticker and a.last < b.first:
                out.append((a.Ticker, b.Ticker, b.first, "ISIN", (pd.Timestamp(b.first) - pd.Timestamp(a.last)).days))
    return pd.DataFrame(out, columns=LINK_COLS)


def manual_links(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=LINK_COLS)
    m = pd.read_csv(path, dtype=str, keep_default_na=False)
    return pd.DataFrame({"Old": m.Old.str.strip(), "New": m.New.str.strip(), "Date": "", "Source": "MANUAL", "GapDays": 0}, columns=LINK_COLS)


def nse_links(path: Path, mapping: dict | None) -> pd.DataFrame:
    """NSE's symbol-change file with the confirmed column mapping {old, new, date, dateFormat}; empty until the mapping is set."""
    if not path.exists() or not mapping or not all(mapping.get(k) for k in ("old", "new", "date", "dateFormat")):
        return pd.DataFrame(columns=LINK_COLS)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    day = pd.to_datetime(df[mapping["date"]].str.strip(), format=mapping["dateFormat"]).dt.strftime("%Y-%m-%d")
    return pd.DataFrame({"Old": df[mapping["old"]].str.strip(), "New": df[mapping["new"]].str.strip(), "Date": day, "Source": "NSE", "GapDays": 0}, columns=LINK_COLS)


def combine(*tables: pd.DataFrame) -> pd.DataFrame:
    """Stack the sources (first wins an Old symbol that two sources link differently), drop self-links and links that would form a cycle."""
    t = pd.concat([x for x in tables if len(x)], ignore_index=True) if any(len(x) for x in tables) else pd.DataFrame(columns=LINK_COLS)
    t = t[t.Old != t.New].drop_duplicates("Old", keep="first").reset_index(drop=True)
    nxt = dict(zip(t.Old, t.New))
    keep = []
    for old in t.Old:
        seen, cur = {old}, nxt[old]
        while cur in nxt and cur not in seen:
            seen.add(cur)
            cur = nxt[cur]
        keep.append(cur not in seen or cur not in nxt)  # a chain that returns to a symbol already on it is a cycle
    return t[keep].reset_index(drop=True)


def resolve(links: pd.DataFrame) -> dict[str, str]:
    """old symbol -> the last symbol of its chain."""
    nxt = dict(zip(links.Old, links.New))
    out = {}
    for old in nxt:
        cur, seen = old, {old}
        while cur in nxt and nxt[cur] not in seen:
            cur = nxt[cur]
            seen.add(cur)
        out[old] = cur
    return out


def review_list(rows: pd.DataFrame, links: pd.DataFrame, today: str, top: int = 60, window: int = 120) -> pd.DataFrame:
    """Symbols that stopped trading with no link, biggest first by median traded value over their last `window` sessions.

    rows: Ticker, Date, Value. These are the candidates for a manual symbol_map.csv row (or real deaths).
    """
    last = rows.groupby("Ticker").Date.max()
    cutoff = str((pd.Timestamp(today) - pd.Timedelta(days=30)).date())
    stopped = last[(last < cutoff) & ~last.index.isin(set(links.Old))]
    sub = rows[rows.Ticker.isin(stopped.index)].sort_values(["Ticker", "Date"])
    med = sub.groupby("Ticker").tail(window).groupby("Ticker").Value.median()
    return pd.DataFrame({"Symbol": stopped.index, "LastDate": stopped.values, "MedianValue": med.reindex(stopped.index).values}).sort_values("MedianValue", ascending=False, ignore_index=True).head(top)
