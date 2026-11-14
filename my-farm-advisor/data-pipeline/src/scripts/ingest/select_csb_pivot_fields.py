#!/usr/bin/env python3
"""Select quarter-section (center-pivot style) fields from USDA CSB for one county.

Why this exists
---------------
OpenStreetMap farmland in irrigated Nebraska counties is drawn as large
hand-digitized blocks, so the default OSM bootstrap cannot target pivot fields.
USDA Crop Sequence Boundaries (CSB, via the FIBOA GeoParquet read by
``download_csb_fields.py``) delineate fields at the quarter-section level: in
pivot country one CSB polygon is the ~160 ac square that holds the pivot
circle plus its dry corners. CSB does not draw the circle itself.

This script keeps CSB fields that look like quarter-section pivot fields:

* centroid inside the requested county (geoadmin L2 county polygon),
* CSB crop label in ``--crops`` (default Corn, Soybeans),
* area within ``--min-acres``..``--max-acres`` (default 120-170 ac),
* square-ish: fills >= ``--min-rect-fill`` of its minimum rotated rectangle and
  that rectangle's long/short side ratio <= ``--max-aspect``.

It then draws a deterministic sample (``--seed``) and writes the same canonical
farm boundary GeoJSON + field inventory CSV that ``bootstrap_farm_from_county``
writes, so ``run_farm_pipeline.py`` treats the farm exactly like an OSM farm.
The pivot claim is a *filter*, not proof: confirm it with the satellite basemap
and per-field NDVI (green circle, drier corners) after the pipeline runs.

Usage
-----
    python scripts/ingest/select_csb_pivot_fields.py \\
        --state-fips 31 --county-name Hamilton --count 10 --seed 42 \\
        --grower-slug nebraska-grower --farm-slug nebraska-grower-nebraska \\
        --farm-name "Nebraska Farm" --run-pipeline
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(_SCRIPTS_DIR / "ingest"))

from bootstrap_runtime import ensure_runtime_environment

ensure_runtime_environment()

import fsspec  # noqa: E402
import geopandas as gpd  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
from pyproj import Transformer  # noqa: E402
from shapely import wkb  # noqa: E402

from bootstrap_farm_from_county import (  # noqa: E402
    _load_target_county,
    _run_farm_pipeline,
    _runtime_path,
    _runtime_relative,
    _write_inventory,
)
from download_csb_fields import LEAF, URL, file_crs  # noqa: E402
from paths import farm_boundary_path, farm_manifest_dir  # noqa: E402

ACRES_PER_M2 = 1 / 4046.8564224


def _read_csb_window(county_geom_4326) -> tuple[gpd.GeoDataFrame, dict]:
    """Read CSB polygons whose bbox intersects the county, pruning row groups."""
    fo = fsspec.open(URL, block_size=2**26).open()
    pf = pq.ParquetFile(fo)
    crs = file_crs(pf)
    w, s, e, n = county_geom_4326.bounds
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    xs, ys = zip(*(tr.transform(x, y) for x, y in ((w, s), (e, s), (w, n), (e, n))))
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)

    groups = []
    for i in range(pf.metadata.num_row_groups):
        st = pf.metadata.row_group(i)
        c = [st.column(LEAF[k]).statistics for k in LEAF]
        if any(not (x and x.has_min_max) for x in c):
            groups.append(i)
        elif c[2].max > xmin and c[0].min < xmax and c[3].max > ymin and c[1].min < ymax:
            groups.append(i)

    rows = []
    for i in groups:
        d = pf.read_row_group(
            i, columns=["id", "crop:name", "administrative_area_level_2", "geometry"]
        ).to_pydict()
        for fid, crop, county, geom in zip(
            d["id"], d["crop:name"], d["administrative_area_level_2"], d["geometry"]
        ):
            if geom is None:
                continue
            poly = wkb.loads(geom)
            b = poly.bounds
            if b[2] > xmin and b[0] < xmax and b[3] > ymin and b[1] < ymax:
                rows.append({"csb_id": str(fid), "csb_crop": crop, "csb_county": county, "geometry": poly})
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)
    meta = {"source_url": URL, "row_groups_read": len(groups), "file_crs": crs.to_string()}
    return gdf, meta


def _shape_metrics(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Area plus quarter-section squareness, computed in the file's equal-area CRS."""
    out = gdf.copy()
    out["area_acres"] = out.geometry.area * ACRES_PER_M2
    mrr = out.geometry.minimum_rotated_rectangle()
    out["rect_fill"] = out.geometry.area / mrr.area

    def _aspect(rect) -> float:
        xs, ys = rect.exterior.coords.xy
        sides = sorted(
            ((xs[k + 1] - xs[k]) ** 2 + (ys[k + 1] - ys[k]) ** 2) ** 0.5 for k in range(2)
        )
        return sides[1] / sides[0] if sides[0] > 0 else float("inf")

    out["aspect_ratio"] = mrr.map(_aspect)
    return out


def _write_preview(cands: gpd.GeoDataFrame, out_png: Path) -> None:
    """Contact sheet of candidates on Esri World Imagery for visual pivot checks."""
    import math

    import contextily as cx
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    cands = cands.sort_values("csb_id").to_crs("EPSG:3857")
    cols = 5
    rows = max(1, math.ceil(len(cands) / cols))
    fig, axs = plt.subplots(rows, cols, figsize=(4 * cols, 4.2 * rows), squeeze=False)
    for ax in axs.flat:
        ax.set_axis_off()
    for ax, (_, r) in zip(axs.flat, cands.iterrows()):
        g = gpd.GeoSeries([r.geometry], crs="EPSG:3857")
        b = g.buffer(120).total_bounds
        ax.set_xlim(b[0], b[2])
        ax.set_ylim(b[1], b[3])
        cx.add_basemap(ax, source=cx.providers.Esri.WorldImagery, zoom=16, attribution=False)
        g.boundary.plot(ax=ax, color="yellow", lw=1.5)
        ax.set_title(f"{r.csb_id}\n{r.csb_crop}  {r.area_acres:.0f} ac", fontsize=9)
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=80)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state-fips", required=True)
    ap.add_argument("--county-name", required=True)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--crops", default="Corn,Soybeans", help="Comma-separated CSB crop names")
    ap.add_argument("--min-acres", type=float, default=120.0)
    ap.add_argument("--max-acres", type=float, default=170.0)
    ap.add_argument("--min-rect-fill", type=float, default=0.90)
    ap.add_argument("--max-aspect", type=float, default=1.25)
    ap.add_argument("--grower-slug", required=True)
    ap.add_argument("--farm-slug", required=True)
    ap.add_argument("--farm-name", required=True)
    ap.add_argument(
        "--include-ids",
        default="",
        help=(
            "Comma-separated CSB ids to use instead of a random sample (e.g. fields "
            "visually confirmed as center pivots). Each id must pass the filters."
        ),
    )
    ap.add_argument(
        "--preview-png",
        default="",
        help="Write a satellite contact sheet of --preview-count unselected candidates and exit",
    )
    ap.add_argument("--preview-count", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true", help="Report candidates; write nothing")
    ap.add_argument("--run-pipeline", action="store_true")
    ap.add_argument("--force", action="store_true", help="Pass --force to run_farm_pipeline")
    args = ap.parse_args()

    county = _load_target_county(args.state_fips, args.county_name)
    county_geom = county.geometry.iloc[0]
    county_name = str(county["county_name"].iloc[0])
    county_fips = str(county["county_fips"].iloc[0]).zfill(3)

    raw, meta = _read_csb_window(county_geom)
    county_proj = county.to_crs(raw.crs).geometry.iloc[0]
    in_county = raw[raw.geometry.centroid.within(county_proj)]
    scored = _shape_metrics(in_county)

    crops = {c.strip() for c in args.crops.split(",") if c.strip()}
    keep = scored[
        scored["csb_crop"].isin(crops)
        & scored["area_acres"].between(args.min_acres, args.max_acres)
        & (scored["rect_fill"] >= args.min_rect_fill)
        & (scored["aspect_ratio"] <= args.max_aspect)
    ].sort_values("csb_id")

    funnel = {
        "csb_polygons_in_window": int(len(raw)),
        "centroid_in_county": int(len(in_county)),
        "crop_match": int(scored["csb_crop"].isin(crops).sum()),
        "plus_area_window": int(
            (scored["csb_crop"].isin(crops) & scored["area_acres"].between(args.min_acres, args.max_acres)).sum()
        ),
        "plus_square_shape": int(len(keep)),
    }
    print(json.dumps({"county": county_name, "county_fips": county_fips, "funnel": funnel}, indent=2))
    include_ids = [x.strip() for x in args.include_ids.split(",") if x.strip()]
    if args.preview_png:
        pool = keep[~keep["csb_id"].isin(include_ids)]
        n = min(args.preview_count, len(pool))
        _write_preview(pool.sample(n=n, random_state=args.seed), Path(args.preview_png))
        print(f"Wrote preview of {n} candidates to {args.preview_png}")
        return

    if include_ids:
        missing = sorted(set(include_ids) - set(keep["csb_id"]))
        if missing:
            raise SystemExit(f"--include-ids not in filtered candidate pool: {missing}")
        sampled = keep[keep["csb_id"].isin(include_ids)].sort_values("csb_id")
        funnel["selection"] = f"visually confirmed list ({len(sampled)} ids)"
    else:
        if len(keep) < args.count:
            raise SystemExit(f"Only {len(keep)} candidates; relax filters or pick another county")
        sampled = keep.sample(n=args.count, random_state=args.seed).sort_values("csb_id")
        funnel["selection"] = f"random sample n={args.count} seed={args.seed}"
    out = sampled.to_crs("EPSG:4326").reset_index(drop=True)
    out["field_id"] = "csb-" + out["csb_id"]
    out["source"] = "USDA CSB (FIBOA us-usda-cropland)"
    out["crop_name"] = out["csb_crop"]
    out["state_fips"] = args.state_fips.zfill(2)
    out["county_fips"] = county_fips
    out["county_name"] = county_name
    for col in ("area_acres", "rect_fill", "aspect_ratio"):
        out[col] = out[col].round(3)
    out = out[
        ["field_id", "source", "crop_name", "state_fips", "county_fips", "county_name",
         "area_acres", "rect_fill", "aspect_ratio", "csb_id", "geometry"]
    ]
    print(out.drop(columns="geometry").to_string(index=False))
    if args.dry_run:
        return

    boundary_out = farm_boundary_path(args.grower_slug, args.farm_slug)
    boundary_out.parent.mkdir(parents=True, exist_ok=True)
    out.to_file(boundary_out, driver="GeoJSON")
    inventory_path = _runtime_path(farm_manifest_dir(args.grower_slug, args.farm_slug) / "field-inventory.csv")
    _write_inventory(inventory_path, out)

    provenance = {
        "script": "ingest/select_csb_pivot_fields.py",
        "accessed_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        **meta,
        "state_fips": args.state_fips.zfill(2),
        "county_name": county_name,
        "county_fips": county_fips,
        "filters": {
            "crops": sorted(crops),
            "acres": [args.min_acres, args.max_acres],
            "min_rect_fill": args.min_rect_fill,
            "max_aspect": args.max_aspect,
        },
        "funnel": funnel,
        "seed": args.seed,
        "selected_field_ids": out["field_id"].tolist(),
        "boundary_path": _runtime_relative(boundary_out),
        "inventory_csv": _runtime_relative(inventory_path),
    }
    prov_path = boundary_out.parent / "csb_pivot_selection_provenance.json"
    prov_path.write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    print(f"Wrote {boundary_out}\nWrote {inventory_path}\nWrote {prov_path}")

    if args.run_pipeline:
        _run_farm_pipeline(args, boundary_out, inventory_path)


if __name__ == "__main__":
    main()
