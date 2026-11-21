"""Assemble a field-level EDA report (single-page HTML, optional PDF) from field-level-eda outputs.

Reads only the folder written by ``my-farm-advisor/eda/field-level-eda/src/field_level_eda.py``
(``findings.json``, ``figures/``, ``tables/``, ``map/``, ``manifest.json``) - never the runtime -
so the same command works for any farm set.

    python reports/build_eda_report.py --eda-dir <eda-out> --out report.html [--pdf report.pdf]
        [--title "..."] [--intro intro.md]  (intro optional; the farm overview is generated)

HTML: one self-contained file (figures embedded), an "Export PDF" button (browser print, with
print styles so each section paginates cleanly) and, on screen only, a satellite view of the
fields (Leaflet 1.9.4 + Esri World Imagery; needs internet). The PDF and the printed page use the
static, reproducible map instead.

PDF (``--pdf``): printed from the HTML with headless Chrome/Chromium if one is installed
(override with $CHROME). PDFs embed a creation time, so they are not byte-identical between runs;
the HTML is.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

CATEGORIES = ["Field scorecard", "Map", "Field boundaries", "CDL crop history", "Weather"]
ANCHORS = {"Field scorecard": "scorecard", "Map": "map", "Field boundaries": "boundaries", "CDL crop history": "cdl", "Weather": "weather"}


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def md(text: str) -> str:
    """Tiny Markdown subset: paragraphs, '- ' bullets, **bold**, `code`, [text](url), ## headings."""
    out, items = [], []

    def inline(t):
        t = esc(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
        return re.sub(r"\[(.+?)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)

    def flush():
        if items:
            out.append("<ul>" + "".join(f"<li>{inline(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for block in re.split(r"\n\s*\n", text.strip()):
        for line in block.splitlines():
            if line.startswith("- "):
                items.append(line[2:])
            elif line.startswith("## "):
                flush()
                out.append(f"<h3>{inline(line[3:])}</h3>")
            elif line.strip():
                flush()
                out.append(f"<p>{inline(line)}</p>")
        flush()
    return "\n".join(out)


def table_html(path: Path, max_rows: int = 60) -> str:
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return ""
    head, body = rows[0], rows[1:max_rows + 1]
    num = [all(re.fullmatch(r"-?[\d.eE+-]+", r[i] or "0") for r in body) for i in range(len(head))]
    th = "".join(f"<th>{esc(h.replace('_', ' '))}</th>" for h in head)
    tr = "".join("<tr>" + "".join(f'<td class="{"n" if num[i] else ""}">{esc(c)}</td>' for i, c in enumerate(r)) + "</tr>"
                 for r in body)
    more = f'<p class="note">{len(rows) - 1 - max_rows} more rows in {esc(path.name)}</p>' if len(rows) - 1 > max_rows else ""
    return f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>{more}'


def img_tag(path: Path, alt: str) -> str:
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f'<img src="data:image/png;base64,{b64}" alt="{esc(alt)}">'


def card(item: dict, d: Path) -> str:
    parts = [f'<article class="card {esc(item["kind"])}" id="{esc(item["id"])}">',
             f'<header><span class="tag">{esc(item["id"])} · {esc(item["kind"])}</span>'
             f'<h3>{esc(item["title"])}</h3><span class="level">Comparison level: {esc(item.get("level", ""))}</span></header>']
    if item.get("image"):
        parts.append(f'<figure>{img_tag(d / item["image"], item["title"])}</figure>')
    parts.append('<dl>')
    parts.append(f'<dt>What it shows</dt><dd>{esc(item["shows"])}</dd>')
    parts.append(f'<dt>Farmer takeaway</dt><dd>{esc(item["takeaway"])}</dd>')
    parts.append(f'<dt>Limits</dt><dd>{esc(item["limits"])}</dd></dl>')
    if item.get("summary"):
        parts.append(f'<div class="stats">{table_html(d / item["summary"])}</div>')
    if item.get("table"):
        t = table_html(d / item["table"])
        if item["kind"] == "comparison" and not item.get("summary"):
            parts.append(f'<div class="stats">{t}</div>')
        else:
            label = "Full data table" if item.get("summary") else "Data table"
            parts.append(f'<details><summary>{label} ({esc(Path(item["table"]).name)})</summary>{t}'
                         '<p class="note print-only">Full table: see the HTML report or the CSV.</p></details>')
    parts.append("</article>")
    return "\n".join(parts)


CSS = """
:root{--ink:#1d2421;--muted:#5d6762;--line:#dfe4e0;--bg:#fafbf9;--card:#fff;--accent:#2f6b3a;--accent2:#e3b505}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.bar{position:sticky;top:0;z-index:1000;background:var(--accent);color:#fff;display:flex;gap:18px;align-items:center;padding:10px 24px;flex-wrap:wrap}
.bar b{font-size:16px;margin-right:8px}.bar a{color:#fff;text-decoration:none;opacity:.9;font-size:14px}.bar a:hover{opacity:1;text-decoration:underline}
.bar button{margin-left:auto;background:var(--accent2);color:#1d2421;border:0;border-radius:6px;padding:7px 14px;font-weight:600;cursor:pointer;font-size:14px}
main{max-width:1060px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:28px;margin:8px 0 4px}h2{font-size:21px;margin:40px 0 12px;padding-bottom:6px;border-bottom:2px solid var(--accent)}
h3{font-size:17px;margin:4px 0}
.sub{color:var(--muted);margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin:18px 0}
.card header{margin-bottom:8px}.tag{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--accent);font-weight:700}
.level{display:block;font-size:13px;color:var(--muted)}
figure{margin:10px 0}figure img{max-width:100%;height:auto;display:block;margin:0 auto}
dl{margin:8px 0 0;display:grid;grid-template-columns:150px 1fr;gap:6px 14px}dt{font-weight:600;color:var(--accent)}dd{margin:0}
.comparison{border-left:5px solid var(--accent2)}
.tw{overflow-x:auto}table{border-collapse:collapse;font-size:13px;margin:10px 0;min-width:50%}
th,td{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left}th{background:#eef2ee}td.n{text-align:right;font-variant-numeric:tabular-nums}
details{margin-top:10px}summary{cursor:pointer;color:var(--accent)}
.note{color:var(--muted);font-size:13px}
table.kv{border-collapse:collapse;margin:6px 0 12px}table.kv th{text-align:left;padding:3px 18px 3px 0;color:var(--muted);font-weight:600;white-space:nowrap;vertical-align:top}table.kv td{padding:3px 0}
#sat{height:520px;border-radius:8px;border:1px solid var(--line)}
.sat-legend{font-size:13px;color:var(--muted);margin-top:6px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
code{background:#eef2ee;padding:0 4px;border-radius:4px}
.print-only{display:none}
@media (max-width:700px){dl{grid-template-columns:1fr}.grid2{grid-template-columns:1fr}#sat{height:380px}.bar{gap:10px;padding:8px 16px}}
@media print{
  @page{size:Letter;margin:0.55in}
  body{background:#fff;font-size:11pt}
  .bar,.screen-only,details{display:none!important}.print-only{display:block}
  main{max-width:none;padding:0}
  .card{border:1px solid #ccc;break-inside:avoid;page-break-inside:avoid;padding:10px 12px;margin:10px 0}
  h2{break-before:page;break-after:avoid;margin-top:0}h2.first{break-before:auto}
  figure img{max-height:4.6in;width:auto}
  .card header,figure{break-after:avoid}
  a{color:inherit;text-decoration:none}
}
"""

SAT_JS = """
(function(){
  if(!window.L){document.getElementById('sat').innerHTML='<p class="note" style="padding:16px">Satellite view needs an internet connection (Leaflet and Esri imagery load online).</p>';return;}
  var fields=__FIELDS__, rings=__RINGS__;
  var map=L.map('sat',{scrollWheelZoom:false});
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    {maxZoom:19,attribution:'Imagery &copy; Esri, Maxar, Earthstar Geographics'}).addTo(map);
  var fl=L.geoJSON(fields,{style:function(f){return{color:f.properties.color,weight:2,fillOpacity:0.15}},
    onEachFeature:function(f,l){var p=f.properties;l.bindTooltip(p.label);l.bindPopup('<b>'+p.label+'</b><br>'+p.grower+(p.county?' · '+p.county+' Co.':'')+'<br>'+p.acres.toFixed(1)+' ac<br>'+p.crop_year+' crop: '+p.crop);}}).addTo(map);
  var rl=L.geoJSON(rings,{style:{color:'#ffffff',weight:0.5,fillColor:'#f4a259',fillOpacity:0.45},interactive:false});
  L.control.layers(null,{'Fields':fl,'Headland ring':rl},{collapsed:false}).addTo(map);
  var groups={};fields.features.forEach(function(f){(groups[f.properties.grower]=groups[f.properties.grower]||[]).push(f)});
  var sel=document.getElementById('satgo');
  Object.keys(groups).forEach(function(g){var o=document.createElement('option');o.value=g;o.textContent=g;sel.appendChild(o)});
  function go(g){var b=L.geoJSON({type:'FeatureCollection',features:g?groups[g]:fields.features}).getBounds();map.fitBounds(b,{padding:[20,20]})}
  sel.onchange=function(){go(sel.value)};
  var first=Object.keys(groups)[0];sel.value=first;go(first);
})();
"""


def overview_card(d: Path, fnd: dict, man: dict, fp: str) -> str:
    """Farm overview computed from the EDA outputs: size, acreage, years, sources, processing."""
    ds, p, env = fnd["dataset"], fnd["parameters"], man.get("environment", {})
    with open(d / "tables" / "fields.csv", newline="") as f:
        fields = list(csv.DictReader(f))
    ac = sorted(float(r["acres"]) for r in fields)
    tot = sum(ac)
    med = ac[len(ac) // 2] if len(ac) % 2 else (ac[len(ac) // 2 - 1] + ac[len(ac) // 2]) / 2
    yrs = ds["years"]
    counties = sorted({r["county"] for r in fields if r["county"]})
    states = sorted({r["state"] for r in fields if r["state"]})
    exc = ds.get("excluded_labels") or []
    kpi = [("Grower", esc(ds["grower"])), ("Location", esc(", ".join(c + " Co." for c in counties) + (f", {', '.join(states)}" if states else ""))),
           ("Farms", str(ds["farms"])), ("Fields analysed", str(ds["fields"])),
           ("Total acreage", f"{tot:,.0f} ac"),
           ("Field size", f"{ac[0]:.0f}-{ac[-1]:.0f} ac (median {med:.0f})"),
           ("Years of data", f"{len(yrs)} ({yrs[0]}-{yrs[-1]})")]
    if exc:
        kpi.append(("Excluded fields", esc(", ".join(exc))))
    kp = "<table class=\"kv\"><tbody>" + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in kpi) + "</tbody></table>"
    rows = "".join(f"<tr><td>{esc(r['label'])}</td><td>{esc(r['field_id'])}</td><td class=\"n\">{float(r['acres']):.1f}</td>"
                   f"<td class=\"n\">{100 * float(r['acres']) / tot:.1f}</td></tr>"
                   for r in sorted(fields, key=lambda r: r["label"]))
    ft = ("<h3>Acreage per field</h3><div class=\"tw\"><table><thead><tr><th>field</th><th>field id</th><th>acres</th>"
          f"<th>% of total</th></tr></thead><tbody>{rows}<tr><td><b>Total</b></td><td></td><td class=\"n\"><b>{tot:.1f}</b></td>"
          "<td class=\"n\"><b>100</b></td></tr></tbody></table></div>"
          "<p class=\"note\">Acres are computed from the reviewed boundary polygons in an equal-distance UTM projection.</p>")
    src = ("<h3>Data sources</h3><div class=\"tw\"><table><thead><tr><th>layer</th><th>source</th><th>used for</th></tr></thead><tbody>"
           "<tr><td>Field boundaries</td><td>USDA NASS Crop Sequence Boundaries (CSB), read from the Source Cooperative "
           "fiboa <code>us_usda_cropland</code> dataset; each outline reviewed and, where needed, redrawn on satellite "
           "imagery</td><td>field size, shape, edge zones</td></tr>"
           f"<tr><td>Crop history</td><td>USDA NASS Cropland Data Layer (CDL), 30 m annual classification, {yrs[0]}-{yrs[-1]}</td>"
           "<td>rotation, crop acres, edge purity, non-crop area</td></tr>"
           f"<tr><td>Weather</td><td>NASA POWER daily reanalysis (~0.5 degree grid), {yrs[0]}-{yrs[-1]}: temperature, rainfall, "
           "solar radiation, humidity, wind</td><td>growing degree days, critical-month stress, water balance</td></tr>"
           "<tr><td>Terrain</td><td>USGS 3D Elevation Program (3DEP) DEM</td><td>slope, relief, elevation</td></tr></tbody></table></div>"
           "<p class=\"note\">Soil data are not used in this report.</p>")
    proc = ("<h3>Processing overview</h3><ol>"
            "<li><b>Boundaries.</b> CSB polygons were screened for size and shape, then QC'd and edited in the "
            "<code>field-boundary-review</code> tool before any data were downloaded.</li>"
            "<li><b>Download.</b> The My Farm Advisor pipeline fetched CDL, NASA POWER and DEM data for each field.</li>"
            f"<li><b>Edge zones.</b> <code>headlands-ring</code> split each field into a {p['headlands_width_m']:g} m headland ring "
            "and an interior; CDL was area-weighted by exact pixel overlap.</li>"
            f"<li><b>Weather.</b> Fields in the same NASA POWER cell share one series (de-duplicated); season window "
            f"{p['season'][0]} to {p['season'][1]}; corn GDD (base 50 F, cap 86 F); FAO-56 reference ET.</li>"
            "<li><b>Analysis.</b> <code>field-level-eda</code> produced every figure and table with fixed code "
            "(no randomness), so the same inputs give identical outputs.</li></ol>")
    ver = ("<h3>Run details</h3><table class=\"kv\"><tbody>"
           f"<tr><th>Skill</th><td><code>field-level-eda</code> v{esc(fnd['version'])}</td></tr>"
           f"<tr><th>Input files</th><td>{len(man['inputs'])} (SHA-256 in <code>manifest.json</code>)</td></tr>"
           f"<tr><th>Output fingerprint</th><td><code>{fp}</code></td></tr>"
           f"<tr><th>Fields</th><td>{esc(', '.join(p['fields']) or 'all')}; excluded: {esc(', '.join(p['exclude_fields']) or 'none')}</td></tr>"
           f"<tr><th>Environment</th><td>{esc(', '.join(f'{k} {v}' for k, v in env.items()))}</td></tr></tbody></table>")
    return f'<div class="card"><h3>At a glance</h3>{kp}{ft}{src}{proc}{ver}</div>'


def build(a) -> Path:
    d = a.eda_dir.expanduser().resolve()
    fnd = json.loads((d / "findings.json").read_text())
    man = json.loads((d / "manifest.json").read_text())
    items = fnd["items"]
    ds = fnd["dataset"]
    years = ds["years"]
    fp = hashlib.sha256(json.dumps(man["outputs"], sort_keys=True).encode()).hexdigest()[:12]
    intro = md(a.intro.read_text()) if a.intro else ""
    title = a.title

    sections, sat = [], ""
    # overview
    ov = [f'<h2 class="first" id="overview">Farm overview</h2>', intro, overview_card(d, fnd, man, fp)]
    sections.append("\n".join(ov))
    for cat in CATEGORIES:
        its = [i for i in items if i["category"] == cat]
        if not its:
            continue
        s = [f'<h2 id="{ANCHORS[cat]}">{esc(cat)}</h2>']
        if cat == "Map":
            fields = (d / "map" / "fields.geojson").read_text().strip() if (d / "map" / "fields.geojson").exists() else '{"type":"FeatureCollection","features":[]}'
            ring_files = sorted((d / "map").glob("headlands_*.geojson"))
            rings = ring_files[0].read_text().strip() if ring_files else '{"type":"FeatureCollection","features":[]}'
            s.append('<section class="card screen-only"><header><span class="tag">Interactive</span>'
                     '<h3>Satellite view of the field boundaries</h3>'
                     '<span class="level">Screen only - not included in the PDF</span></header>'
                     '<p><label>Zoom to grower: <select id="satgo"></select></label></p><div id="sat"></div>'
                     '<p class="sat-legend">Outline colour = dominant crop in the latest CDL year; toggle the '
                     'headland ring in the layer control. Click a field for details.</p></section>')
            sat = SAT_JS.replace("__FIELDS__", fields).replace("__RINGS__", rings)
        s += [card(i, d) for i in its]
        sections.append("\n".join(s))
    lim = "".join(f"<li><b>{esc(i['id'])} {esc(i['title'])}:</b> {esc(i['limits'])}</li>" for i in items)
    p = fnd["parameters"]
    env = man.get("environment", {})
    sections.append(
        '<h2 id="limits">Limitations, assumptions and reproducibility</h2><div class="card">'
        f"<h3>Limitations by output</h3><ul>{lim}</ul>"
        "<h3>Parameters</h3><ul>"
        f"<li>Grower {esc(p['grower'])}; fields: {esc(', '.join(p['fields']) or 'all')}; excluded: "
        f"{esc(', '.join(p['exclude_fields']) or 'none')}</li>"
        f"<li>Headland ring width {p['headlands_width_m']:g} m</li>"
        f"<li>Season {p['season'][0]} to {p['season'][1]}; critical months: corn {p['corn_critical_month']}, "
        f"soybeans {p['soy_critical_month']}; heat-stress threshold {p['heat_threshold_f']:g} F</li></ul>"
        "<h3>Reproducibility</h3>"
        f"<p>Generated by <code>field-level-eda</code> v{esc(fnd['version'])} from {len(man['inputs'])} input files "
        f"(SHA-256 recorded in <code>manifest.json</code>). Output fingerprint <code>{fp}</code>. Re-running the "
        "skill on the same inputs reproduces identical figures and tables; <code>--verify</code> checks this.</p>"
        f"<p class=\"note\">Environment: " + ", ".join(f"{esc(k)} {esc(v)}" for k, v in env.items()) + "</p></div>")

    toc = "".join(f'<a href="#{ANCHORS[c]}">{esc(c)}</a>' for c in CATEGORIES if any(i["category"] == c for i in items))
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>{CSS}</style></head>
<body>
<nav class="bar"><b>{esc(title)}</b><a href="#overview">Overview</a>{toc}<a href="#limits">Limits</a>
<button type="button" onclick="window.print()" title="Opens the print dialog; choose Save as PDF">Export PDF</button></nav>
<main>
<h1>{esc(title)}</h1>
<p class="sub">Field boundaries, USDA CDL crop history and NASA POWER weather for {ds['fields']} fields of
{esc(ds['grower'])} ({years[0]}-{years[-1]}). <span class="print-only">Interactive version with satellite view: the HTML file of this report.</span></p>
{''.join(sections)}
</main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>{sat}</script>
</body></html>
"""
    out = a.out.expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    return out


def find_chrome() -> str | None:
    cands = [os.environ.get("CHROME"), "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
             "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
             "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    for c in cands:
        if c and (shutil.which(c) or Path(c).exists()):
            return shutil.which(c) or c
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eda-dir", type=Path, required=True, help="field-level-eda output folder")
    ap.add_argument("--out", type=Path, required=True, help="HTML file to write")
    ap.add_argument("--pdf", type=Path, help="also print a PDF with headless Chrome/Chromium")
    ap.add_argument("--title", default="Field-level EDA report")
    ap.add_argument("--intro", type=Path, help="optional Markdown intro (dataset context, county choice, ...)")
    a = ap.parse_args(argv)
    html_path = build(a)
    print(f"wrote {html_path} ({html_path.stat().st_size / 1e6:.2f} MB)")
    if a.pdf:
        chrome = find_chrome()
        if not chrome:
            print("no Chrome/Chromium found: open the HTML and use the Export PDF button", file=sys.stderr)
            return
        pdf = a.pdf.expanduser().resolve()
        pdf.parent.mkdir(parents=True, exist_ok=True)
        with __import__("tempfile").TemporaryDirectory() as prof:
            subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                            f"--user-data-dir={prof}", "--virtual-time-budget=5000",
                            f"--print-to-pdf={pdf}", html_path.as_uri()],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        print(f"wrote {pdf} ({pdf.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
