# Leaflet versus D3: grower field map comparison

Both renderers consume the identical `grower_map_data` payload, use the same Esri
World Imagery tiles, and expose the same sidebar: farm legend, field list,
click-to-zoom, and a panel with 2021–2025 crop rotation and SSURGO soil
summaries. The difference is the rendering engine and dependency weight.

The committed subskill uses **Leaflet 1.9.4**. The D3 variant is a prototype
kept outside the repository for comparison only.

## What was measured

Environment: headless Chromium 156, viewport 1400 × 900, fresh browser context
per run, warm network to the CDN and tile server, three runs per case. Values
are medians. "Ready" means the grower payload rendered and the field list
existed; "tiles" means the first Esri tile response completed.

Both growers have three fields on one farm.

| Metric | Leaflet (committed) | D3 + d3-tile (prototype) |
|---|---|---|
| HTML size, Illinois grower | 23.5 KB | 25.8 KB |
| HTML size, Iowa grower | 25.3 KB | 27.6 KB |
| Library payload (JS + CSS) | 162 KB (Leaflet JS 147 KB + CSS 15 KB) | 281 KB (d3 280 KB + d3-tile 1.7 KB) |
| Dependency requests | 2 | 2 |
| Ready, Illinois | 0.23 s | 0.23 s |
| Ready, Iowa | 0.21 s | 0.23 s |
| First tile, Illinois | 0.24 s | 0.23 s |
| First tile, Iowa | 0.21 s | 0.24 s |
| Tiles requested, clicked field, Illinois | 25 | 72 |
| Tiles requested, clicked field, Iowa | 60 | 120 |
| Headless functional checks | 15/15 pass | 15/15 pass |

At three fields the load times are dominated by CDN latency and are effectively
identical. The meaningful difference is dependency weight: D3 adds about
**119 KB**, roughly 1.7× the Leaflet library payload. D3 also requested more
tiles in this test because its click zoom fits the field more tightly; Leaflet's
per-field zoom is capped at level 16.

## Feature comparison

| Capability | Leaflet | D3 + d3-tile |
|---|---|---|
| Esri World Imagery basemap | Built-in tile layer | Manual tile URLs and layout |
| Pan and zoom | Built in, with inertia | Custom `d3.zoom` wiring |
| Field polygons | `L.geoJSON` | `d3.geoPath` and manual projection |
| Popups and tooltips | Built in | Custom DOM or SVG |
| Fit all / fit field | `fitBounds` | Custom bounds-to-transform math |
| Canvas rendering for many fields | `preferCanvas` option | SVG by default; extra work for canvas |
| Seam-free tiles at fractional zoom | Handled by Leaflet | Requires rounding and overlap handling |
| Keyboard and screen-reader defaults | Provided by Leaflet controls | Must be authored |
| Bundle size | Smaller | Larger |
| Repo precedent | `admin/interactive-web-map` | None |

## Why Leaflet is the default

1. **Repo convention.** `admin/interactive-web-map` already standardizes on
   Leaflet 1.9.4, so contributors and reviewers see one mapping stack.
2. **Smaller payload.** The committed map ships 119 KB less JavaScript for the
   same visible result.
3. **Less custom code.** Zoom, bounds fitting, popups, attribution, and tile
   seams are Leaflet responsibilities instead of map-specific math.
4. **Equal outcome here.** Both renderers passed the same 15 headless checks and
   produced the same panel content and field counts.

## When D3 would be the better choice

Keep the D3 path in mind when a future map needs something Leaflet does not do
well:

- A non-Mercator projection or a custom cartogram.
- Data-driven encodings across many fields where D3 scales, joins, and
  transitions are the main product.
- Tight integration with an existing D3 dashboard or chart package.
- Fine-grained control over tile order, clipping, or SVG-level effects.

For those cases, start from the prototype in `/tmp/opencode/grower-field-map-d3/`
(not committed) and keep the shared `grower_map_data.py` payload so both
renderers stay in sync.

## Reproducing the comparison

The D3 generator reads the same runtime artifacts and writes
`<grower>_field_map_d3.html` next to the Leaflet output:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd /tmp/opencode/grower-field-map-d3
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  generate_grower_web_map_d3.py --grower-slug illinois-grower
```

The verification and benchmark scripts used for the table live beside it under
`/tmp/opencode/` and are intentionally not part of this repository.
