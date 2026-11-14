---
name: headlands-ring
description: Create reusable headlands and interior geometries for agricultural field analysis, clipping, masking, and exclusion workflows across polygons, points, and rasters.
version: 1.0.0
author: Boreal Bytes
tags: [geospatial, headlands, field-operations, masking, clipping, analysis]
---

# Workflow: headlands-ring

## Description

Create headlands-ring and interior geometries from field boundaries so the edge area of a field can be measured, visualized, or excluded from analysis. This workflow is intentionally generic: it can be used with soil polygons, point samples, NDVI rasters, yield zones, or any other geometry-driven workflow where the border of the field should be compared with or excluded from the interior.

## When to Use This Workflow

- **Headlands exclusion**: Exclude edge effects from agronomic or remote-sensing analysis
- **Operational analysis**: Quantify headlands acres and the share of a field devoted to turning or overlap zones
- **Polygon comparison**: Split soil zones or management zones into headlands and interior subsets
- **Point flagging**: Mark sample points that fall within the headlands area
- **Raster comparison**: Compare raster summaries between headlands and field interior

## Quick Start

```bash
uv run --with geopandas --with matplotlib --with shapely python << 'EOF'
import geopandas as gpd

from headlands_ring import create_headlands_ring, summarize_headlands

fields = gpd.read_file('my-farm-advisor/field-management/field-boundaries/examples/real_10_fields_iowa.geojson')
field = fields.iloc[[0]].to_crs('EPSG:32615')

ring = create_headlands_ring(field, width_m=9.0)
summary = summarize_headlands(field, ring)

print(summary[['field_area_acres', 'headlands_area_acres', 'headlands_pct']])
EOF
```

## Farm runner: split every field of a pipeline farm

`src/headlands_farm.py` applies the library to a whole My Farm Advisor farm after
`run_farm_pipeline.py` (and after `../field-boundary-review/` edits). It reads
`boundary/field_boundaries.geojson`, never modifies it, and writes per width:

| Output | Content |
|---|---|
| `boundary/field_headlands_<w>m.geojson` | one feature per field × zone (`headlands`, `interior`), WGS84 |
| `derived/tables/<farm>_headlands_<w>m_summary.csv` | field / headlands / interior acres, headlands %, perimeter, perimeter-to-area, compactness |
| `derived/tables/<farm>_cdl_by_zone_<w>m.csv` | CDL crop shares per field, year and zone (`field`, `interior`, `headlands`) |
| `derived/reports/<farm>_headlands_<w>m.png` | static map of all fields |
| `boundary/headlands_manifest.json` | parameters, analysis CRS, SHA-256 of the boundary file |

```bash
python src/headlands_farm.py --farm-dir <growers>/<grower>/farms/<farm> --widths 9 30
```

- **Widths**: 9 m ≈ one 30 ft equipment pass (agronomic headland); 30 m = one CDL/Landsat
  pixel (removes boundary pixels that mix field with road or grass).
- **CDL by zone** uses the state CDL rasters already cached by the pipeline
  (`data-pipeline/shared/cdl/rasters/CDL_<year>_<statefips>.tif`, no download). Each 30 m
  pixel is weighted by the exact area of its square inside the zone polygon (EPSG:5070), so
  thin rings are measured fairly. Every class is kept, using the official NASS legend;
  unknown codes appear as `CDL code N`, so shares sum to 100 %.
- **Checks**: headlands + interior area must equal field area (±1 m²), otherwise the run
  stops; fields too small to keep an interior are listed in the manifest.
- **Deterministic**: fields sorted by id, coordinates rounded to 1e-7°, no randomness;
  reruns produce byte-identical files.
- Weather (NASA POWER) is point data at the field centroid and is not affected by the ring.

## Python API Reference

### `create_headlands_ring(field_gdf, width_m=9.0)`

Create a headlands ring polygon by subtracting an inward buffer from the original field polygon.

### `create_field_interior(field_gdf, width_m=9.0)`

Create the interior polygon that remains after excluding the headlands width.

### `split_headlands_and_interior(field_gdf, width_m=9.0)`

Return both headlands and interior geometries in one call.

### `summarize_headlands(field_gdf, ring_gdf)`

Summarize total field area, headlands area, and headlands share.

### `flag_points_in_headlands(points_gdf, ring_gdf)`

Flag whether input points intersect the headlands geometry.

### `clip_polygons_to_headlands(polygons_gdf, ring_gdf)`

Clip arbitrary polygons to the headlands region.

### `plot_headlands_map(field_gdf, ring_gdf, interior_gdf=None, ...)`

Render a simple map showing field boundary, headlands ring, and optional interior polygon.

## Output Standards

- Preserve CRS and avoid implicit reprojection during geometry operations
- Require projected CRS for width-based calculations in meters
- Use deterministic field metrics for repeatable downstream reporting

## Dependencies

- `geopandas`
- `shapely`
- `matplotlib` for optional plots
