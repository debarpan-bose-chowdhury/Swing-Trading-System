"""NSE surveillance lists: parse a downloaded source into rows, normalise to surveillance_{date}.json, apply the entry/exit rules."""

import csv
import io
import json
import re
from pathlib import Path

from app.risk.common import read_json, trading_days_between

SOURCES = ("asm", "gsm", "t2t", "bands")
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}


def parse_rows(payload: bytes, fmt: str, rows_key: str | None = None) -> list[dict]:
    """CSV or JSON payload as a list of {column: text} rows (header and cell whitespace stripped)."""
    if fmt == "csv":
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
        rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in reader]
    else:
        data = json.loads(payload)
        if isinstance(data, dict):
            data = data.get(rows_key) if rows_key else next((v for v in data.values() if isinstance(v, list)), [])
        rows = [{str(k).strip(): str(v).strip() for k, v in r.items()} for r in data or [] if isinstance(r, dict)]
    return rows


def stage_of(text: str) -> int:
    """Stage number from '2', 'Stage 2' or 'Stage II'."""
    m = re.search(r"\d+", text)
    return int(m.group()) if m else ROMAN.get(re.sub(r"[^IVX]", "", text.upper()), 0)


def select(rows: list[dict], src: dict) -> list[dict]:
    """Rows that have a symbol and pass the source's optional filterColumn / filterValues."""
    col, allowed = src.get("filterColumn"), {v.upper() for v in src.get("filterValues", [])}
    return [r for r in rows if r.get(src["symbolColumn"]) and (not col or r.get(col, "").upper() in allowed)]


def normalise(sources: dict, payloads: dict, asof: str, fetched_at: str) -> dict:
    """payloads: source -> bytes, or None when its download failed. A failed source is empty and marked "failed"."""
    out = {"asOf": asof, "fetchedAt": fetched_at, "sources": {}, "asm": {"LT": {}, "ST": {}}, "gsm": {}, "t2t": [], "bandPct": {}}
    for name in SOURCES:
        src, payload = sources[name], payloads.get(name)
        try:
            if payload is None:
                raise ValueError("no payload")
            rows = select(parse_rows(payload, src["format"], src.get("rowsKey")), src)
            sym, val = src["symbolColumn"], src.get("valueColumn")
            if name in ("asm", "gsm"):
                for r in rows:
                    stage = stage_of(r.get(val, "")) if val else 1
                    if name == "gsm":
                        out["gsm"][r[sym]] = stage
                    else:
                        term = "LT" if r.get(src.get("termColumn", ""), "L").upper().startswith("L") else "ST"
                        out["asm"][term][r[sym]] = stage
            elif name == "t2t":
                out["t2t"] = sorted({r[sym] for r in rows})
            else:
                out["bandPct"] = {r[sym]: float(r[val].replace("%", "")) for r in rows if val and r.get(val)}
            out["sources"][name] = "ok"
        except (ValueError, KeyError, TypeError):
            out["sources"][name] = "failed"
    return out


def counts(s: dict) -> dict:
    return {"asm": len(s["asm"]["LT"]) + len(s["asm"]["ST"]), "gsm": len(s["gsm"]), "t2t": len(s["t2t"]), "bands": len(s["bandPct"])}


def load(cfg: dict, cal, asof: str) -> dict:
    """Newest normalised list on or before asof. entries: usable for new buys (dated asof, every source ok);
    exits: the list when at most staleExitDays trading days old, else None."""
    folder = Path(cfg["paths"]["risk"]) / "surveillance"
    days = sorted((f.stem.removeprefix("surveillance_"), f) for f in folder.glob("surveillance_*.json"))
    days = [(d, f) for d, f in days if d <= asof]
    data = read_json(days[-1][1]) if days else None
    if not data:
        return {"data": None, "status": "missing", "asOf": None, "entries": False, "exits": None}
    fresh = trading_days_between(cal, data["asOf"], asof) <= cfg["surveillance"]["staleExitDays"]
    entries = data["asOf"] == asof and all(v == "ok" for v in data["sources"].values())
    return {"data": data, "status": "ok" if entries else "stale", "asOf": data["asOf"], "entries": entries, "exits": data if fresh else None}


def band(s: dict | None, t: str) -> float | None:
    return s["bandPct"].get(t) if s else None


def entry_block(s: dict, t: str, cfg: dict) -> bool:
    """New buys are blocked on ASM stage 1+, GSM, trade-for-trade, or a price band at or below blockEntryBandPct."""
    b = band(s, t)
    return (any(s["asm"][term].get(t, 0) >= 1 for term in ("LT", "ST")) or t in s["gsm"] or t in s["t2t"]
            or (b is not None and b <= cfg["surveillance"]["blockEntryBandPct"]))


def exit_flag(s: dict | None, t: str, cfg: dict) -> str | None:
    """'GSM' or 'T2T' when a held name is on a list that forces an exit (per surveillance.exitOn)."""
    if not s:
        return None
    exit_on = cfg["surveillance"]["exitOn"]
    return "GSM" if "GSM" in exit_on and t in s["gsm"] else "T2T" if "T2T" in exit_on and t in s["t2t"] else None


def warn_flags(s: dict | None, t: str, cfg: dict) -> list[str]:
    """Warnings for a held name: ASM, or a price band at or below blockEntryBandPct."""
    if not s:
        return []
    b = band(s, t)
    return ([f"ASM:{t}"] if any(s["asm"][term].get(t, 0) >= 1 for term in ("LT", "ST")) else []) + (
        [f"TIGHT_BAND:{t}"] if b is not None and b <= cfg["surveillance"]["blockEntryBandPct"] else [])
