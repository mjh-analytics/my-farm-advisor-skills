# Workflow: grower-field-map

Build one self-contained, grower-level interactive field map from the canonical
data-pipeline runtime. The map shows every farm and field owned by a grower on
Esri World Imagery, with a field list, click-to-zoom, and a metadata panel that
summarizes 2021–2025 crop rotation and SSURGO soil properties.

This is a subskill of `data-pipeline`. It reads existing runtime outputs and
does not download imagery, boundaries, CDL, or soil data.

## When to use it

- A grower asks for one map that covers all of their farms, not one map per farm.
- You need a shareable HTML file with no server, no build step, and no raster payloads.
- You want field polygons from the actual pipeline boundary files, not hand-drawn shapes.
- You need crop rotation and soil context next to each field without opening the full farm report.

Use `admin/interactive-web-map` when you need a general-purpose dashboard with
custom layers or choropleths. Use this subskill when the deliverable is the
standard grower field map.

## What the map contains

| Element | Detail |
|---|---|
| Basemap | Esri World Imagery only (no OpenStreetMap toggle) |
| Scope | All farms and all fields discovered for one grower |
| Field styling | Filled and outlined by farm, with a farm color legend |
| Field list | Grouped by farm, with area and rotation sequence; filter box included |
| Zoom | Leaflet zoom control, per-field click-to-zoom, and a **Fit all fields** button |
| Click panel | Grower, farm, field id, area, 2021–2025 crop rotation, next-crop outlook, and soil summary |
| Window | 2021–2025, read from the CDL rotation and composition tables |
| Payload | Field geometry, rotation, and soil embedded as JSON; HTML is typically 20–30 KB for three fields |
| Network | Required only for the Leaflet CDN and Esri tiles; all field data is embedded |

The HTML file has no local asset dependencies, so it can be emailed, opened from
disk, or hosted on any static web server.

## Inputs it reads

All paths are under `${DATA_PIPELINE_DATA_ROOT}/data-pipeline`:

| Input | Source |
|---|---|
| Grower and farm discovery | `growers/<grower>/farms/*/farm.json` |
| Field order | `growers/<grower>/farms/<farm>/manifests/field-inventory.csv` |
| Field polygons | `growers/<grower>/farms/<farm>/fields/<field>/boundary/field_boundary.geojson` |
| Rotation history | `growers/<grower>/farms/<farm>/derived/tables/<farm>_crop_rotation.csv` |
| Per-year crops | `growers/<grower>/farms/<farm>/derived/tables/<farm>_cdl_2021_2025_full_composition.csv` |
| Soil summary | `growers/<grower>/farms/<farm>/derived/tables/<farm>_ssurgo_summary.csv` |

The renderer-neutral payload is assembled by
`src/scripts/lib/grower_map_data.py`; the Leaflet renderer is
`src/scripts/reporting/generate_grower_web_map.py`. The D3 prototype in
[COMPARISON.md](COMPARISON.md) consumes the same payload.

## Run it

Install or refresh the runtime source first. This copies `src/` into the runtime
tree, so the generator must be present before you run it:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd my-farm-advisor/data-pipeline
./scripts/install.sh --data-root "${DATA_PIPELINE_DATA_ROOT}" --non-interactive --force-refresh --no-install-deps
```

Then render one grower:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/reporting/generate_grower_web_map.py \
  --grower-slug illinois-grower
```

`AG_GROWER_SLUG` is used when `--grower-slug` is omitted. `AG_WEATHER_START_YEAR`
and `AG_WEATHER_END_YEAR` set the displayed window; both default to 2021 and 2025.

Output:

```text
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/derived/reports/<grower>_field_map.html
```

Use `--output /absolute/path/map.html` to write somewhere else.

## Verify the output

1. The command prints the grower name, farm count, field count, window, and file size.
2. Field count should equal the sum of the grower's `field-inventory.csv` rows.
3. Each field record should carry a rotation sequence and a dominant soil name.
   Missing rotation means the CDL tables are absent or still 2019–2023.
4. Open the HTML and check that Esri imagery loads, the farm legend matches the
   polygons, and clicking a field updates the panel.
5. Keep the file under 300 KB. If it grows past that, the likely cause is a field
   boundary with far more vertices than expected, not the renderer.

Generated HTML belongs under the runtime data root and must stay out of Git.

## Related docs

- [COMPARISON.md](COMPARISON.md) — Leaflet versus D3, including measured sizes and why Leaflet is the default.
- [../../data-pipeline/README.md](../README.md) — runtime install, shared data, and pipeline commands.
- [../../admin/interactive-web-map/GUIDE.md](../../admin/interactive-web-map/GUIDE.md) — general web-map workflow and dashboard patterns.
