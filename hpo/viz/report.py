"""Self-contained HTML: the study report (charts 1-10) and the live dashboard. One file, Plotly bundled inline, no network at view time,
light and dark themes. Reads only files under hpo/data; a report built before the holdout is scored never contains holdout data (nothing
here can read it), and it shows no path outside hpo/data."""

import html
import json
import time
from pathlib import Path

from plotly.offline import get_plotlyjs

from hpo.status import read_json, replace, write_json
from hpo.study import read_records
from hpo.viz import charts, theme

LIVE_CHARTS = ("c1", "c2", "c3", "c4", "c9", "c10")


def _table(t: dict) -> str:
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in t["cols"])
    body = "".join("<tr>" + "".join(f"<td>{html.escape('' if v is None else str(v))}</td>" for v in r) + "</tr>" for r in t["rows"][:500])
    return f"<table><tr>{head}</tr>{body}</table>"


def page(title: str, subtitle: str, warnings: list[str], tiles: list[dict], cs: list[dict], refresh: int | None = None, plotly: bool = True) -> str:
    sections = []
    for c in cs:
        plot = f'<div class="plot" id="plot-{c["id"]}"></div>' if c["fig"] else ""
        note = f'<p class="note">{html.escape(c["note"])}</p>' if c["note"] else ""
        sections.append(f'<section class="chart" id="{c["id"]}"><h2>{html.escape(c["title"])}</h2><p class="how">{html.escape(c["how"])} <b>Judged against:</b> {html.escape(c["criterion"])}</p>'
                        f'{plot}{note}<details{"" if c["fig"] else " open"}><summary>Data table</summary>{_table(c["table"])}</details></section>')
    tile_html = '<div class="tiles">' + "".join(f'<div class="tile"><b>{html.escape(str(t["value"]))}</b><span>{html.escape(t["label"])}</span></div>' for t in tiles) + "</div>" if tiles else ""
    warn = "".join(f'<div class="warn">{html.escape(w)}</div>' for w in warnings)
    payload = json.dumps(charts.clean([{k: c[k] for k in ("id", "fig")} for c in cs]), allow_nan=False).replace("</", "<\\/")
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    lib = f"<script>{get_plotlyjs()}</script>" if plotly else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{meta}<title>{html.escape(title)}</title>'
            f"<style>{theme.css()}</style></head><body><main><h1>{html.escape(title)} <button onclick=\"toggle()\">light / dark</button></h1><p class=\"sub\">{html.escape(subtitle)}</p>{warn}{tile_html}"
            f"{''.join(sections)}</main>{lib}<script>const CHARTS={payload};{theme.SCRIPT}</script></body></html>")


def _inputs(study_dir: Path, cfg: dict):
    spec = read_json(study_dir / "study.json")
    if spec is None:
        raise FileNotFoundError(f"{study_dir.name}: no study.json")
    return spec, read_records(study_dir / "trials.jsonl"), read_json(study_dir / "status.json"), read_json(study_dir / "sensitivity.json")


def study_report(study_dir: Path, cfg: dict, live: bool = False) -> Path:
    spec, recs, st, sens = _inputs(study_dir, cfg)
    cs = charts.study_charts(recs, st, sens, spec, cfg, LIVE_CHARTS if live else None)
    warnings = [f"Frozen-class parameters are searched in this study: {', '.join(spec['unfrozen'])}"] if spec.get("unfrozen") else []
    eff = (st or {}).get("effectiveN")
    sub = (f"stage {spec['stage']}, sampler {spec['sampler']}, schema {spec.get('schemaVersion', '?')}, effective N {'?' if eff is None else f'{eff:.0f}'} of {cfg['ledger']['effectiveNCap']}"
           f", generated {time.strftime('%Y-%m-%d %H:%M:%S')}. Pre-holdout data only.")
    tl = charts.tiles(recs, st, cfg) if live else []
    out = study_dir / "report" / ("live.html" if live else "index.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(page(f"{'Live: ' if live else ''}Study {spec['name']}", sub, warnings, tl, cs, cfg["compute"]["liveRefreshSeconds"] if live else None), encoding="utf-8")
    replace(tmp, out)
    return out
