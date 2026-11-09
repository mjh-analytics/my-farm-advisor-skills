#!/usr/bin/env python3
# pyright: reportMissingImports=false, reportAttributeAccessIssue=false, reportGeneralTypeIssues=false
"""Generate a grower-level interactive field map as one self-contained HTML file.

The map uses Leaflet 1.9.4 from unpkg and the Esri World Imagery basemap. Field
polygons, crop rotation, and SSURGO soil summaries are embedded as JSON, so the
HTML file has no local file dependencies. Output defaults to
``growers/<grower>/derived/reports/<grower>_field_map.html``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_LOCAL_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(_LOCAL_LIB))

from runtime_paths import resolve_runtime_paths  # noqa: E402

_RUNTIME_PATHS = resolve_runtime_paths()
_REPO = _RUNTIME_PATHS.runtime_base
_LIB = _RUNTIME_PATHS.runtime_scripts / "lib"
sys.path.insert(0, str(_RUNTIME_PATHS.runtime_scripts))
sys.path.insert(0, str(_LIB))

from grower_map_data import build_grower_map_payload, grower_report_path  # noqa: E402

_DEFAULT_GROWER = os.environ.get("AG_GROWER_SLUG", "default-grower")
_LOWERCASE = {"a", "an", "and", "at", "for", "in", "of", "on", "the", "to"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _page_title(grower_name: str) -> str:
    words = grower_name.split()
    titled = [
        word if index and word.lower() in _LOWERCASE else word[:1].upper() + word[1:]
        for index, word in enumerate(words)
    ]
    return " ".join(titled)


def _format_js(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _build_html(payload: dict) -> str:
    grower = payload["grower"]
    title = f"{_page_title(grower['display_name'])} | Field Map"
    data_json = _format_js(payload)

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<style>
  :root {
    --ink: #16281d;
    --muted: #5b6b61;
    --line: #d7e0d9;
    --panel: #ffffff;
    --accent: #2e7d32;
    --bg: #eef2ee;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    color: var(--ink);
    background: var(--bg);
  }
  #app { display: flex; height: 100vh; width: 100%; }
  #sidebar {
    width: 360px;
    min-width: 300px;
    max-width: 44vw;
    background: var(--panel);
    border-right: 1px solid var(--line);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  #sidebar header { padding: 18px 18px 12px; border-bottom: 1px solid var(--line); }
  #sidebar h1 { margin: 0; font-size: 1.25rem; line-height: 1.25; }
  #sidebar .sub { margin: 6px 0 0; font-size: .82rem; color: var(--muted); }
  #sidebar .window {
    display: inline-block; margin-top: 10px; padding: 3px 8px; border-radius: 999px;
    background: #e8f5e9; color: #1b5e20; font-size: .72rem; font-weight: 600;
    letter-spacing: .02em;
  }
  .controls { display: flex; gap: 8px; padding: 12px 18px; border-bottom: 1px solid var(--line); }
  .controls button {
    border: 1px solid #b9d8bc; background: #f3faf4; color: #1b5e20; cursor: pointer;
    border-radius: 6px; padding: 7px 10px; font-size: .78rem; font-weight: 600;
  }
  .controls button:hover { background: #e4f2e6; }
  .controls input {
    flex: 1; min-width: 0; border: 1px solid var(--line); border-radius: 6px;
    padding: 7px 10px; font-size: .78rem; color: var(--ink); background: #fff;
  }
  #scroll { overflow-y: auto; flex: 1; }
  #field-list { padding: 10px 12px 4px; }
  .farm-group { margin-bottom: 12px; }
  .farm-head {
    display: flex; align-items: center; gap: 8px; padding: 8px 6px;
    font-size: .76rem; font-weight: 700; text-transform: uppercase; letter-spacing: .05em;
    color: var(--muted);
  }
  .swatch { width: 12px; height: 12px; border-radius: 3px; flex: none; }
  .farm-head .count { margin-left: auto; font-weight: 600; letter-spacing: 0; text-transform: none; }
  .field-item {
    display: grid; grid-template-columns: 1fr auto; gap: 2px 8px; align-items: center;
    width: 100%; text-align: left; border: 1px solid transparent; border-radius: 8px;
    background: none; padding: 8px 9px; cursor: pointer; color: var(--ink);
    font: inherit; font-size: .82rem;
  }
  .field-item:hover { background: #f2f7f3; border-color: #dceadf; }
  .field-item.active { background: #e8f5e9; border-color: #96c79b; }
  .field-item .name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; }
  .field-item .acres { color: var(--muted); font-size: .76rem; white-space: nowrap; }
  .field-item .crop { grid-column: 1 / -1; color: var(--muted); font-size: .73rem; }
  #field-detail { border-top: 1px solid var(--line); padding: 14px 18px 22px; }
  #field-detail .hint { color: var(--muted); font-size: .82rem; margin: 4px 0; }
  #field-detail h2 { margin: 0 0 2px; font-size: 1rem; }
  #field-detail .kicker { font-size: .72rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
  .meta-grid { display: grid; grid-template-columns: auto 1fr; gap: 3px 10px; margin: 10px 0 0; font-size: .8rem; }
  .meta-grid dt { color: var(--muted); }
  .meta-grid dd { margin: 0; font-weight: 600; }
  .section-title {
    margin: 16px 0 7px; font-size: .72rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: .06em; color: var(--muted);
  }
  .year-chips { display: flex; flex-wrap: wrap; gap: 5px; }
  .chip {
    border: 1px solid var(--line); border-radius: 999px; padding: 3px 8px;
    font-size: .71rem; background: #fafcfa; white-space: nowrap;
  }
  .chip b { font-weight: 700; }
  .chip.corn { background: #fff8dd; border-color: #ead98a; }
  .chip.soy { background: #e8f3e9; border-color: #a9cfaf; }
  .chip.wheat { background: #fdf0df; border-color: #e5c18f; }
  .rotation {
    background: #f7faf7; border: 1px solid var(--line); border-radius: 8px;
    padding: 9px 10px; font-size: .8rem; line-height: 1.45;
  }
  .rotation .next { margin-top: 6px; color: #1b5e20; font-weight: 600; }
  .soil-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
  .soil-cell { background: #f7faf7; border: 1px solid var(--line); border-radius: 7px; padding: 7px 8px; }
  .soil-cell span { display: block; font-size: .66rem; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  .soil-cell b { font-size: .82rem; }
  #map-wrap { position: relative; flex: 1; }
  #map { position: absolute; inset: 0; background: #dfe7df; }
  #legend {
    position: absolute; z-index: 500; left: 14px; bottom: 24px;
    background: rgba(255,255,255,.94); border: 1px solid var(--line); border-radius: 8px;
    padding: 9px 11px; font-size: .75rem; box-shadow: 0 2px 10px rgba(0,0,0,.12);
    max-width: 280px;
  }
  #legend h3 { margin: 0 0 6px; font-size: .68rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
  #legend .row { display: flex; align-items: center; gap: 7px; margin: 3px 0; }
  #legend .note { margin-top: 6px; color: var(--muted); font-size: .68rem; }
  .leaflet-popup-content { font-size: .8rem; line-height: 1.45; }
  .leaflet-popup-content b { color: #1b5e20; }
  @media (max-width: 760px) {
    #app { flex-direction: column; }
    #sidebar { width: 100%; max-width: none; height: 52vh; border-right: 0; border-bottom: 1px solid var(--line); }
    #map-wrap { min-height: 48vh; }
  }
</style>
</head>
<body>
<div id="app">
  <aside id="sidebar">
    <header>
      <h1 id="grower-name"></h1>
      <p class="sub" id="grower-meta"></p>
      <span class="window" id="window-label"></span>
    </header>
    <div class="controls">
      <button type="button" id="fit-all">Fit all fields</button>
      <input id="field-filter" type="search" placeholder="Filter fields" aria-label="Filter fields" />
    </div>
    <div id="scroll">
      <div id="field-list"></div>
      <div id="field-detail"><p class="hint">Select a field on the map or in the list to view crop rotation and soil summary.</p></div>
    </div>
  </aside>
  <div id="map-wrap">
    <div id="map"></div>
    <div id="legend"></div>
  </div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const PAYLOAD = __DATA__;

(function () {
  "use strict";

  const grower = PAYLOAD.grower;
  const farmById = {};
  PAYLOAD.farms.forEach(function (farm) { farmById[farm.slug] = farm; });

  const fieldById = {};
  PAYLOAD.fields.forEach(function (field) { fieldById[field.field_id] = field; });

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmt(value, suffix) {
    if (value == null || value === "") return "—";
    return esc(value) + (suffix || "");
  }

  function cropClass(name) {
    const key = String(name || "").toLowerCase();
    if (key.indexOf("corn") !== -1) return "corn";
    if (key.indexOf("soy") !== -1) return "soy";
    if (key.indexOf("wheat") !== -1) return "wheat";
    return "";
  }

  function fieldLabel(field) {
    return "Field " + field.field_id.replace(/^osm-/, "");
  }

  document.getElementById("grower-name").textContent = grower.display_name;
  document.getElementById("grower-meta").textContent =
    grower.field_count + " fields across " + grower.farm_count +
    (grower.farm_count === 1 ? " farm" : " farms") + " · " +
    (grower.acres != null ? grower.acres.toLocaleString() : "—") + " acres";
  document.getElementById("window-label").textContent =
    "Crop rotation " + PAYLOAD.window.start_year + "–" + PAYLOAD.window.end_year;

  const map = L.map("map", { zoomControl: true, preferCanvas: true });
  L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    {
      maxZoom: 19,
      attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community"
    }
  ).addTo(map);

  const fieldLayers = {};
  let selectedId = null;

  function baseStyle(field) {
    const color = (farmById[field.farm_slug] || {}).color || "#2E7D32";
    return { color: color, weight: 2, opacity: 0.95, fillColor: color, fillOpacity: 0.18 };
  }

  function highlightStyle(field) {
    const style = baseStyle(field);
    style.weight = 3.5;
    style.fillOpacity = 0.3;
    return style;
  }

  function selectedStyle(field) {
    const style = baseStyle(field);
    style.weight = 4;
    style.fillOpacity = 0.38;
    return style;
  }

  function styleFor(field) {
    if (selectedId === field.field_id) return selectedStyle(field);
    return baseStyle(field);
  }

  function popupHtml(field) {
    const rotation = field.rotation || {};
    const farm = farmById[field.farm_slug] || {};
    const lines = [
      "<b>" + esc(fieldLabel(field)) + "</b>",
      esc(farm.display_name || field.farm_name),
      (field.area_acres != null ? field.area_acres.toLocaleString() + " acres" : ""),
      rotation.sequence ? "Rotation: " + esc(rotation.sequence) : ""
    ].filter(Boolean);
    return lines.join("<br>");
  }

  Object.keys(fieldById).forEach(function (fieldId) {
    const field = fieldById[fieldId];
    const layer = L.geoJSON(
      { type: "Feature", properties: {}, geometry: field.geometry },
      {
        style: function () { return styleFor(field); },
        onEachFeature: function (feature, geoLayer) {
          geoLayer.on("mouseover", function () {
            if (selectedId !== field.field_id) geoLayer.setStyle(highlightStyle(field));
          });
          geoLayer.on("mouseout", function () {
            geoLayer.setStyle(styleFor(field));
          });
          geoLayer.on("click", function () { selectField(field.field_id, true); });
          geoLayer.bindPopup(popupHtml(field));
        }
      }
    );
    fieldLayers[fieldId] = layer;
    layer.addTo(map);
  });

  function allBounds() {
    const bounds = L.latLngBounds([]);
    Object.keys(fieldLayers).forEach(function (fieldId) { bounds.extend(fieldLayers[fieldId].getBounds()); });
    return bounds;
  }

  function fitAll() {
    if (!PAYLOAD.fields.length) return;
    map.fitBounds(allBounds(), { padding: [26, 26] });
  }

  function selectField(fieldId, zoom) {
    const field = fieldById[fieldId];
    if (!field) return;
    const previous = selectedId;
    selectedId = fieldId;
    if (previous && fieldLayers[previous]) fieldLayers[previous].setStyle(styleFor(fieldById[previous]));
    if (fieldLayers[fieldId]) {
      fieldLayers[fieldId].setStyle(selectedStyle(field));
      fieldLayers[fieldId].bringToFront();
      if (zoom) map.fitBounds(fieldLayers[fieldId].getBounds(), { padding: [40, 40], maxZoom: 16 });
    }
    renderDetail(field);
    markActiveItem(fieldId);
  }

  function markActiveItem(fieldId) {
    document.querySelectorAll(".field-item").forEach(function (item) {
      item.classList.toggle("active", item.getAttribute("data-field-id") === fieldId);
    });
  }

  function renderDetail(field) {
    const farm = farmById[field.farm_slug] || {};
    const rotation = field.rotation || {};
    const soil = field.soil || {};
    const crops = field.crops || {};
    const years = Object.keys(crops).sort();
    const chips = years.map(function (year) {
      return '<span class="chip ' + cropClass(crops[year]) + '"><b>' + esc(year) + "</b> " + esc(crops[year]) + "</span>";
    }).join("");

    const soilCells = [
      ["Dominant soil", soil.dominant_soil],
      ["Drainage", soil.drainage_class],
      ["Organic matter", soil.avg_organic_matter_pct == null ? null : soil.avg_organic_matter_pct + "%"],
      ["pH", soil.avg_ph],
      ["Water capacity", soil.aws_inches == null ? null : soil.aws_inches + " in"],
      ["CEC", soil.avg_cec],
      ["Clay", soil.avg_clay_pct == null ? null : soil.avg_clay_pct + "%"],
      ["Sand", soil.avg_sand_pct == null ? null : soil.avg_sand_pct + "%"],
      ["pH constraint", soil.ph_constraint],
      ["Erosion risk", soil.erosion_risk]
    ].map(function (pair) {
      return '<div class="soil-cell"><span>' + esc(pair[0]) + "</span><b>" + fmt(pair[1]) + "</b></div>";
    }).join("");

    const rotationBlock = rotation.sequence
      ? '<div class="rotation">' + esc(rotation.sequence) +
        (rotation.start_year && rotation.end_year
          ? '<div style="color:#5b6b61;font-size:.73rem;margin-top:4px">' +
            esc(rotation.start_year) + "–" + esc(rotation.end_year) + " · " +
            fmt(rotation.diversity) + " crops · " + fmt(rotation.confidence) + " confidence</div>"
          : "") +
        (rotation.predicted_next_crop
          ? '<div class="next">Next likely: ' + esc(rotation.predicted_next_crop) + "</div>"
          : "") +
        "</div>"
      : '<p class="hint">No rotation table available for this field.</p>';

    document.getElementById("field-detail").innerHTML =
      '<div class="kicker">Selected field</div>' +
      "<h2>" + esc(fieldLabel(field)) + "</h2>" +
      '<dl class="meta-grid">' +
        "<dt>Grower</dt><dd>" + esc(grower.display_name) + "</dd>" +
        "<dt>Farm</dt><dd>" + esc(farm.display_name || field.farm_name) + "</dd>" +
        "<dt>Field</dt><dd>" + esc(field.field_id) + "</dd>" +
        "<dt>Area</dt><dd>" + (field.area_acres != null ? field.area_acres.toLocaleString() + " acres" : "—") + "</dd>" +
      "</dl>" +
      '<div class="section-title">Crop rotation ' + esc(PAYLOAD.window.start_year) + "–" + esc(PAYLOAD.window.end_year) + "</div>" +
      rotationBlock +
      (chips ? '<div class="year-chips" style="margin-top:8px">' + chips + "</div>" : "") +
      '<div class="section-title">Soil summary</div>' +
      '<div class="soil-grid">' + soilCells + "</div>";
  }

  function renderList() {
    const list = document.getElementById("field-list");
    list.innerHTML = "";
    PAYLOAD.farms.forEach(function (farm) {
      const fields = PAYLOAD.fields.filter(function (field) { return field.farm_slug === farm.slug; });
      if (!fields.length) return;
      const group = document.createElement("div");
      group.className = "farm-group";
      group.innerHTML =
        '<div class="farm-head">' +
          '<span class="swatch" style="background:' + esc(farm.color) + '"></span>' +
          "<span>" + esc(farm.display_name) + "</span>" +
          '<span class="count">' + fields.length + " fields · " + (farm.acres != null ? farm.acres.toLocaleString() : "—") + " ac</span>" +
        "</div>";
      fields.forEach(function (field) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "field-item";
        button.setAttribute("data-field-id", field.field_id);
        const next = (field.rotation || {}).predicted_next_crop;
        button.innerHTML =
          '<span class="name">' + esc(fieldLabel(field)) + "</span>" +
          '<span class="acres">' + (field.area_acres != null ? field.area_acres.toLocaleString() + " ac" : "—") + "</span>" +
          '<span class="crop">' + esc((field.rotation || {}).sequence || "No rotation data") +
            (next ? " · next " + esc(next) : "") + "</span>";
        button.addEventListener("click", function () { selectField(field.field_id, true); });
        group.appendChild(button);
      });
      list.appendChild(group);
    });
  }

  function renderLegend() {
    const legend = document.getElementById("legend");
    legend.innerHTML =
      "<h3>Farms</h3>" +
      PAYLOAD.farms.map(function (farm) {
        return '<div class="row"><span class="swatch" style="background:' + esc(farm.color) + '"></span>' +
          "<span>" + esc(farm.display_name) + " (" + farm.field_count + ")</span></div>";
      }).join("") +
      '<div class="note">Click a field for rotation and soil details.</div>';
  }

  document.getElementById("fit-all").addEventListener("click", fitAll);
  document.getElementById("field-filter").addEventListener("input", function (event) {
    const needle = event.target.value.trim().toLowerCase();
    document.querySelectorAll(".field-item").forEach(function (item) {
      const field = fieldById[item.getAttribute("data-field-id")] || {};
      const haystack = [field.field_id, field.farm_name, (field.rotation || {}).sequence, (field.rotation || {}).predicted_next_crop]
        .join(" ").toLowerCase();
      item.style.display = !needle || haystack.indexOf(needle) !== -1 ? "" : "none";
    });
    document.querySelectorAll(".farm-group").forEach(function (group) {
      const visible = Array.prototype.some.call(group.querySelectorAll(".field-item"), function (item) {
        return item.style.display !== "none";
      });
      group.style.display = visible ? "" : "none";
    });
  });

  window.__growerMap = {
    fieldIds: PAYLOAD.fields.map(function (field) { return field.field_id; }),
    selectField: selectField,
    map: map,
    selectedId: function () { return selectedId; }
  };

  renderList();
  renderLegend();
  fitAll();
  if (PAYLOAD.fields.length) selectField(PAYLOAD.fields[0].field_id, false);
  setTimeout(function () { map.invalidateSize(); }, 0);
})();
</script>
</body>
</html>
""".replace("__TITLE__", title).replace("__DATA__", data_json)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grower-slug", default=_DEFAULT_GROWER)
    parser.add_argument("--start-year", type=int, default=_env_int("AG_WEATHER_START_YEAR", 2021))
    parser.add_argument("--end-year", type=int, default=_env_int("AG_WEATHER_END_YEAR", 2025))
    parser.add_argument(
        "--output",
        default=None,
        help="Optional output HTML path; defaults to the grower reports directory.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.start_year > args.end_year:
        raise SystemExit("--start-year must be <= --end-year")

    payload = build_grower_map_payload(
        args.grower_slug,
        window=(args.start_year, args.end_year),
    )
    if not payload["fields"]:
        raise SystemExit(f"No fields found for grower {args.grower_slug!r} under {_REPO}")

    output_path = (
        Path(args.output).expanduser().resolve(strict=False)
        if args.output
        else grower_report_path(args.grower_slug, f"{args.grower_slug}_field_map.html")
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_build_html(payload), encoding="utf-8")

    size_kb = output_path.stat().st_size / 1024.0
    print(f"Grower:   {payload['grower']['display_name']} ({args.grower_slug})")
    print(f"Farms:    {payload['grower']['farm_count']}")
    print(f"Fields:   {payload['grower']['field_count']}")
    print(f"Window:   {args.start_year}-{args.end_year}")
    print(f"Saved:    {output_path}")
    print(f"Size:     {size_kb:.1f} KB")


if __name__ == "__main__":
    main()
