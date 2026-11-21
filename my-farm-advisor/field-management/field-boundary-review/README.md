# field-boundary-review

Review and fix field boundaries **before** the My Farm Advisor data pipeline downloads anything.

Public boundaries are approximate: OpenStreetMap farmland often wraps several fields in one
outline, and USDA Crop Sequence Boundaries (CSB) follow the 30 m CDL pixel grid, so edges are
blocky, can fall short of or spill past the real edge, and can include farmsteads. Every
pipeline product is clipped to the boundary, so a wrong outline means wrong soil, NDVI and
crop numbers. This skill puts a person in the loop, then applies their edits reproducibly.

```
candidate pool  ->  review in browser  ->  export CSVs  ->  apply edits  ->  run pipeline
(CSB / OSM)         keep/drop, comments,    decisions +     deterministic,   every product
                    pins, drawn outlines    markups         with manifest    clipped to it
```

See [GUIDE.md](GUIDE.md) for commands and parameters.

## Why the boundary matters: how each dataset uses it

| Dataset | How the polygon is used |
|---|---|
| DEM / terrain (USGS 3DEP) | Reads only the elevation tiles under the field's bounding box, then masks to the exact polygon; slope and flow come from that clip |
| SSURGO soil (NRCS Soil Data Access) | The polygon is sent as a spatial query; soil map units that intersect it come back and are area-weighted by how much of each unit lies inside the field |
| SoilGrids (ISRIC) | Cuts a window from the global 250 m grids around the field |
| Weather (NASA POWER) | Uses the field centroid (lat/lon); point data, so shape matters less |
| CDL crop history (USDA) | Counts the 30 m crop pixels inside the polygon each year → crop share and rotation |
| Satellite / NDVI (Landsat, Sentinel-2) | Finds scenes covering the field, reads only the window over the field, masks pixels outside the polygon, computes NDVI |

An outline that includes a farmstead or road ditch lowers NDVI averages and shifts soil
weights; one that merges a corn field and a soybean field mixes the CDL crop history. When in
doubt, stay close to the CSB outline and trim obvious non-field areas rather than extending
into neighbouring land.

## Do drawn lines capture the area you meant?

**Click → coordinate.** The viewer converts the clicked screen pixel to latitude/longitude in
the same Web Mercator frame as the satellite tiles you are looking at, so there is no datum or
projection shift between what you see and what is stored. Points are stored at 1e-7° (~1 cm).

**Where error can come from**

| Source | Size |
|---|---|
| Click precision (half a screen pixel) | ~0.15 m at zoom 19, 0.6 m at 17, ~10 m at 13 |
| Imagery georeferencing (Esri World Imagery, US) | typically a few metres |
| Line → polygon processing | < 2 m smoothing, ≤ 3 m per-edge straightening (measured, below) |

The viewer records the lowest map zoom used for every pin and line (`zoom` column in the
export). The zoom is read inside the page from the map itself (`map.getZoom()`), stored with the
markup in the browser and written to the CSV; nothing is sent to a server.
`apply_boundary_edits.py` warns when an outline was drawn below zoom 16.

**How a drawn line becomes a polygon**

1. Coordinates are projected to the farm's UTM zone (metres).
2. If the end point is within 15 m of the start point, the ends are joined into a closed
   outline; larger gaps are reported, not guessed.
3. Click jitter under 2 m is removed (Douglas–Peucker).
4. Gentle straightening: the outline is aligned to the field's main direction; an edge is
   snapped straight only if it is within 8° of that direction (or perpendicular to it) **and**
   snapping moves its ends 3 m or less. Genuinely angled edges stay as drawn.
5. The result replaces the field outline; cut-out areas are subtracted. The field id never
   changes, so downstream paths stay valid.

**Checks reported for every field**: start/end closure gap, overlap with the original outline
(IoU), area change, overlap with neighbouring fields, digitizing zoom. A low overlap usually
means the outline was drawn on the wrong field or merges two fields.

**Testing for a systematic offset.** Compare drawn outlines with an independent source such as
CSB. If every field shifts the same direction, the imagery or the clicks are misregistered; if
the differences scatter randomly, they are digitizing and source noise. Reference run (29 fields
in IL, IA, NE): mean centroid shift +0.3 m east, +2.9 m north; median edge-to-edge distance
5.2 m, consistent with CSB's 30 m pixel edges. Individual outliers (one field 34 m off) were
caused by the CSB polygon spilling into a neighbouring field, which the viewer overlay makes
visible.

**What remains.** A 30 m Landsat or 10 m Sentinel-2 pixel on the boundary still mixes field and
road however exact the line is. For statistics that must exclude edges, build an inset geometry
with [headlands-ring](../headlands-ring/GUIDE.md).

## Determinism

No randomness. Edits are always re-applied from `boundary/field_boundaries.original.geojson`,
markups are sorted before use, and the same inputs and parameters give byte-identical output.
Each run writes `boundary/boundary_edits_manifest.json` with the parameters, SHA-256 of the
inputs, the analysis CRS and per-field metrics.

## Files

| File | Purpose |
|---|---|
| `src/build_review_viewer.py` | Build the single-file review page for every grower/farm in a runtime |
| `src/review_viewer_template.html` | The page (Leaflet 1.9.4, Esri World Imagery) |
| `src/apply_boundary_edits.py` | Apply exported markups to one farm's boundary GeoJSON |
| `GUIDE.md` | Commands, parameters, skill interactions |
| `AGENTS.md` | Rules for agents using this skill |
