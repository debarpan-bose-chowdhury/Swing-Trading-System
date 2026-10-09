"""Colours by role (one colour per role, used the same way in every chart), light and dark, from the validated reference palette, and the
page template pieces. Charts carry role names ("role:front"); the page script swaps them for the theme's colour, so a theme switch
repaints without rebuilding anything."""

ROLES = {  # role: (light, dark)
    "feasible": ("#2a78d6", "#3987e5"), "front": ("#eb6834", "#d95926"), "default": ("#4a3aa7", "#9085e9"), "benchmark": ("#1baf7a", "#199e70"),
    "candidate": ("#eb6834", "#d95926"), "infeasible": ("#a6a59f", "#6b6a64"), "good": ("#008300", "#4aa84a"), "bad": ("#e34948", "#e66767"),
}
SURFACE = ("#fcfcfb", "#1a1a19")
TEXT = ("#0b0b0b", "#ffffff")
MUTED = ("#52514e", "#c3c2b7")
GRID = ("#e4e3de", "#34332f")


def css() -> str:
    def block(i: int) -> str:
        roles = "".join(f"--{k}:{v[i]};" for k, v in ROLES.items())
        return f"color-scheme:{'dark' if i else 'light'};--surface:{SURFACE[i]};--text:{TEXT[i]};--muted:{MUTED[i]};--grid:{GRID[i]};{roles}"
    return (f":root{{{block(0)}}}@media (prefers-color-scheme: dark){{:root:not([data-theme=\"light\"]){{{block(1)}}}}}:root[data-theme=\"dark\"]{{{block(1)}}}"
            "body{margin:0;background:var(--surface);color:var(--text);font:14px/1.45 system-ui,Segoe UI,sans-serif}"
            "main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:0 0 2px}"
            ".sub{color:var(--muted);margin:0 0 12px}.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin:12px 0}"
            ".tile{border:1px solid var(--grid);border-radius:8px;padding:8px 10px}.tile b{display:block;font-size:20px}.tile span{color:var(--muted);font-size:12px}"
            "section.chart{margin:22px 0;padding-top:6px;border-top:1px solid var(--grid)}.how{color:var(--muted);margin:0 0 6px;font-size:13px}"
            ".plot{width:100%;height:380px}.note{color:var(--muted);font-style:italic}details{margin-top:4px}summary{cursor:pointer;color:var(--muted);font-size:12px}"
            "table{border-collapse:collapse;font-size:12px;margin-top:4px}td,th{border:1px solid var(--grid);padding:2px 8px;text-align:right}th{text-align:left}"
            ".warn{border:1px solid var(--bad);border-radius:8px;padding:6px 10px;margin:8px 0}button{background:none;color:var(--text);border:1px solid var(--grid);border-radius:6px;padding:2px 8px}")


SCRIPT = r"""
const ROOT=document.documentElement;
function cv(n){return getComputedStyle(ROOT).getPropertyValue('--'+n).trim()}
function resolve(o){if(typeof o==='string'&&o.startsWith('role:'))return cv(o.slice(5));
 if(Array.isArray(o))return o.map(resolve);if(o&&typeof o==='object'){const r={};for(const k in o)r[k]=resolve(o[k]);return r}return o}
function base(l){const ax={gridcolor:cv('grid'),zerolinecolor:cv('grid'),linecolor:cv('grid'),tickfont:{color:cv('muted')},title:{font:{color:cv('muted')}}};
 const out=Object.assign({paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'rgba(0,0,0,0)',font:{color:cv('text')},margin:{l:60,r:20,t:20,b:50},legend:{orientation:'h',y:-0.2}},resolve(l));
 for(const k of Object.keys(out).filter(k=>/^[xy]axis\d*$/.test(k)))out[k]=Object.assign({},ax,out[k]);
 if(!out.xaxis)out.xaxis=ax;if(!out.yaxis)out.yaxis=ax;return out}
function draw(){for(const c of CHARTS){if(!c.fig)continue;const el=document.getElementById('plot-'+c.id);
 Plotly.react(el,resolve(c.fig.data),base(c.fig.layout),{displaylogo:false,responsive:true,toImageButtonOptions:{format:'svg',filename:c.id}})}}
window.addEventListener('load',draw);
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',draw);
new MutationObserver(draw).observe(ROOT,{attributes:true,attributeFilter:['data-theme']});
function toggle(){const d=ROOT.getAttribute('data-theme')==='dark'||(!ROOT.getAttribute('data-theme')&&matchMedia('(prefers-color-scheme: dark)').matches);
 ROOT.setAttribute('data-theme',d?'light':'dark')}
"""
