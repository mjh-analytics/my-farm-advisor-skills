---
name: field-boundary-review
description: Human-in-the-loop QC of field boundaries before the data pipeline downloads anything. Builds a single-file HTML viewer (satellite basemap, keep/drop + comments, pins, drawn lines/outlines) and deterministically applies the exported markups to the farm boundary GeoJSON, with straightening, validation and a provenance manifest.
version: 0.1.0
author: Max Hoeffel
tags: [field-boundaries, qc, csb, osm, leaflet, digitizing, data-pipeline]
---

# Workflow: field-boundary-review

## Description

Public boundary sources are approximate. OpenStreetMap `landuse=farmland` often wraps several
fields in one outline; USDA Crop Sequence Boundaries (CSB) follow the 30 m CDL pixel grid, so
edges are blocky, can fall short of the real edge, and can include farmsteads. Every pipeline
product (soil, DEM, satellite/NDVI, CDL, reports) is clipped to the boundary, so boundaries
should be checked **before** the download.

This workflow sits between field selection and the pipeline run:

```
csb-field-sampling / bootstrap  ->  field-boundary-review  ->  run_farm_pipeline.py
   (candidate pool)                 (review + edit)         (download + reports)
```

## When to Use This Workflow

- Reviewing a candidate pool and choosing which fields to keep
- Fixing outlines: extend/trim an edge, cut out buildings or waterways, redraw a field
- QC after a pipeline run (the viewer also shows weather, scenes, rotation, soil, report PNGs)

## Two stages, one viewer

The same `build_review_viewer.py` serves both stages, and both are supported:

| Stage | What exists | What the viewer shows |
|---|---|---|
| **Before download** | `boundary/` only (original + edited outlines, candidates) | outlines, acres, shape flags, keep/drop, comments, pins, lines |
| **After download** | full pipeline outputs | the same, plus CDL crop share and rotation, weather days, scene counts, soil, report images |

Every data column is optional: missing files show as blank, not as errors. Rebuild the viewer
at either stage; reviewer input (localStorage) carries over because field ids do not change.

## Files

| File | Role |
|---|---|
| `src/build_review_viewer.py` | Writes `qc_field_viewer.html` for every grower/farm under a runtime `growers/` dir |
| `src/review_viewer_template.html` | Viewer page (Leaflet 1.9.4 + Esri World Imagery tiles) |
| `src/apply_boundary_edits.py` | Applies the exported markups CSV to one farm's boundaries |

## Interaction with other skills

| Skill | Relationship |
|---|---|
| `../csb-field-sampling/`, `data-pipeline/.../select_csb_pivot_fields.py`, `bootstrap_farm_from_county.py` | **Upstream**: produce the candidate pool / `field_boundaries.geojson` this skill reviews |
| `../field-boundaries/` | Background on boundary sources (OSM, CSB) and their limits |
| `data-pipeline` (`run_farm_pipeline.py`) | **Downstream**: reads the edited `field_boundaries.geojson`; every product is clipped to it |
| `../headlands-ring/` | **Downstream, optional**: inset/headland geometries for edge-free statistics |
| `admin/interactive-web-map`, `data-pipeline/grower-field-map` | Display-only maps; this skill is the editable review step |

Contract: input and output are the pipeline's canonical farm boundary GeoJSON (one Polygon
per field, `field_id` property). Field ids never change, so downstream paths stay valid.

## Step-by-step

1. **Freeze the source outlines** for the farm (once):
   `boundary/field_boundaries.original.geojson` = untouched source polygons of the selected
   fields. Edits are always re-applied from this file, so re-runs are idempotent.
2. **Build the viewer**
   ```bash
   python src/build_review_viewer.py --growers-dir "$DATA_PIPELINE_DATA_ROOT/data-pipeline/growers"
   ```
   Keep the HTML in the `growers/` folder so chart links resolve. Labels such as `IL-07` are
   numbered from the largest `boundary/*candidates*.geojson` pool, so they stay stable.
3. **Review in the browser**: Keep/Drop + comment per field; 📍 pins; ✏️ lines.
   - *Boundary should follow this line*: trace the true edge, or trace the whole field and end
     near the start point (gap ≤ `--snap-m`) to replace the outline.
   - *Cut out the area I outline*: click around a building/yard (≥ 3 points).
   Export **QC CSV** (decisions) and **pins & lines CSV** (`field_qc_markups.csv`).
4. **Dry run, then apply** per farm
   ```bash
   python src/apply_boundary_edits.py --farm-dir <growers>/<grower>/farms/<farm> \
       --markups ~/Downloads/field_qc_markups.csv --dry-run
   python src/apply_boundary_edits.py --farm-dir <growers>/<grower>/farms/<farm> \
       --markups ~/Downloads/field_qc_markups.csv
   ```
5. **Rebuild the viewer** — edited fields show the original outline dashed white. Iterate.
6. **Run the pipeline** (`run_farm_pipeline.py`) on the farm; field ids are unchanged.

## How a drawn outline becomes a boundary

1. Line coordinates (WGS84, 1e-7°) are projected to the farm's UTM zone.
2. **Closure**: start/end gap ≤ `--snap-m` (15 m) → closed by joining the ends; larger gaps
   are reported and not applied. Self-intersections are repaired with `buffer(0)`.
3. **Simplify**: Douglas–Peucker `--simplify-m` (2 m) removes click jitter.
4. **Straighten** (`--square-deg` 8°, `--max-edge-move-m` 3 m): rotate to the field's dominant
   axis (longest side of the minimum rotated rectangle). An edge is snapped onto that axis (or
   its perpendicular) only if it is within 8° AND snapping moves its ends ≤ 3 m
   (L/2·sin(deviation)), so long edges that are genuinely off-square stay as drawn.
   Consecutive snapped edges merge; corners are rebuilt as edge intersections. If the result
   moves the outline more than `--max-square-shift-m` (6 m, Hausdorff) the simplified outline
   is used instead.
5. **Cut-outs** are subtracted; the largest remaining part is kept.
6. **Checks**: IoU vs original (< `--min-iou` flagged), area change %, overlaps with
   neighbouring fields (> 0.05 ac flagged), notes/pins listed as not applied.

## Determinism and provenance

- No randomness; markups are sorted before use; same inputs + params ⇒ byte-identical output.
- `boundary/boundary_edits_manifest.json` records parameters, SHA-256 of the original
  boundaries and the markups CSV, the analysis CRS, and per-field metrics/actions/warnings.
- Edited features carry `boundary_source` (e.g. `user-edited from USDA CSB …`) and
  `boundary_edit` (actions applied).

## Accuracy notes

- **Click → coordinate**: Leaflet converts the clicked screen pixel to WGS84 lat/lon in the
  same Web Mercator frame as the basemap tiles, so there is no datum or projection shift
  between what you see and what is stored. Stored precision is 1e-7° (~1 cm).
- **Click precision** is half a screen pixel: ≈0.15 m at zoom 19, 0.6 m at 17, 2.4 m at 15,
  9.5 m at 13 (near 40° N). The viewer records the lowest zoom used for each markup and
  `apply_boundary_edits.py` warns below `--min-zoom` (16). Digitize at zoom 17–19.
- **Imagery georeferencing** (Esri World Imagery; NAIP/commercial in the US) is typically
  within a few metres.
- **Check for systematic offset**: compare drawn outlines with an independent source (e.g. CSB).
  A shared shift in one direction indicates misregistration; random scatter does not.
  Reference run (29 fields, IL/IA/NE, Nov 2026): mean centroid shift +0.3 m E, +2.9 m N
  (std 5.5 / 9.4 m); median edge-to-edge distance 5.2 m, consistent with CSB's 30 m pixel edges.
- **Remaining edge mixing** is a pixel-size issue, not a digitizing one: a 30 m Landsat or
  10 m Sentinel pixel on the boundary mixes field and road. Use `../headlands-ring/` to build an
  interior (inset) geometry for statistics that must exclude edges.

## Limitations

- Reviewer input lives in browser `localStorage` until exported; export before clearing data
  or switching browsers.
- Open (non-closed) edge lines that only fix one side are reported, not merged; draw the full
  outline instead.
- Pins are annotations only.
