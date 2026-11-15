"""Split every field of a pipeline farm into a headlands ring and an interior.

Runs after ``run_farm_pipeline.py`` (or any time a farm has
``boundary/field_boundaries.geojson``). For each width it writes:

* ``boundary/field_headlands_<w>m.geojson``  - one feature per field x zone
  (``zone`` = ``headlands`` | ``interior``), WGS84
* ``derived/tables/<farm>_headlands_<w>m_summary.csv`` - areas, perimeter, shares
* ``derived/tables/<farm>_cdl_by_zone_<w>m.csv`` - area-weighted CDL crop shares
  per field, year and zone (``field`` | ``interior`` | ``headlands``), computed
  from the locally cached state CDL rasters (no download)
* ``derived/reports/<farm>_headlands_<w>m.png`` - static map of all fields
* ``boundary/headlands_manifest.json`` - parameters + SHA-256 of inputs

Deterministic: no randomness, fields sorted by id, coordinates rounded to 1e-7 deg.
Never modifies ``field_boundaries.geojson``.

Usage::

    python src/headlands_farm.py --farm-dir <growers>/<grower>/farms/<farm> --widths 9 30
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
from headlands_ring import ACRES_PER_SQM, split_headlands_and_interior  # noqa: E402

# CDL class names come from the cdl-cropland skill (official USDA NASS legend), so there is
# one legend in the repo. Unknown codes are kept as "CDL code N" (never dropped), so zone
# shares always sum to 100 %.
_CDL_SRC = Path(__file__).resolve().parents[3] / "soil" / "cdl-cropland" / "src"
sys.path.insert(0, str(_CDL_SRC))
from cdl_reporting import CDL_CODES as CDL_NAMES  # noqa: E402

CDL_NODATA = 0


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def round_geom(geom, nd=7):
    return shapely.set_precision(geom, 10 ** -nd) if not geom.is_empty else geom


def split_fields(fields_utm: gpd.GeoDataFrame, width_m: float) -> gpd.GeoDataFrame:
    """Per-field headlands/interior using the headlands_ring library (keeps field_id)."""
    rows = []
    for _, f in fields_utm.iterrows():
        one = gpd.GeoDataFrame([f], geometry="geometry", crs=fields_utm.crs)
        ring, interior = split_headlands_and_interior(one, width_m=width_m)
        ring_g = ring.geometry.iloc[0] if not ring.empty else shapely.Polygon()
        int_g = interior.geometry.iloc[0] if not interior.empty else shapely.Polygon()
        for zone, g in (("headlands", ring_g), ("interior", int_g)):
            rows.append({"field_id": f["field_id"], "zone": zone, "width_m": width_m,
                         "geometry": g})
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=fields_utm.crs)


def summarize(fields_utm, zones, width_m):
    out = []
    for _, f in fields_utm.iterrows():
        z = zones[zones.field_id == f.field_id].set_index("zone").geometry
        a = f.geometry.area
        ring_a, int_a = z["headlands"].area, z["interior"].area
        perim = f.geometry.length
        out.append({
            "field_id": f.field_id, "width_m": width_m,
            "field_acres": round(a * ACRES_PER_SQM, 3),
            "headlands_acres": round(ring_a * ACRES_PER_SQM, 3),
            "interior_acres": round(int_a * ACRES_PER_SQM, 3),
            "headlands_pct": round(100 * ring_a / a, 2) if a else None,
            "perimeter_m": round(perim, 1),
            "perimeter_to_area_m_per_ha": round(perim / (a / 1e4), 1) if a else None,
            # 1.0 = circle; long or ragged outlines are lower and lose more area to edges
            "compactness": round(4 * math.pi * a / perim ** 2, 3) if perim else None,
            "area_check_m2": round(a - ring_a - int_a, 3),
        })
    return pd.DataFrame(out)


def zone_cdl_shares(raster_path: Path, geoms: dict[str, shapely.Geometry]):
    """Exact area-weighted class shares: every 30 m pixel is weighted by the area of the
    pixel square that lies inside the zone polygon (geometries in the raster CRS)."""
    import rasterio
    from rasterio.windows import from_bounds

    res = {}
    with rasterio.open(raster_path) as src:
        union = shapely.union_all([g for g in geoms.values() if not g.is_empty])
        win = from_bounds(*union.bounds, transform=src.transform).round_offsets().round_lengths()
        win = rasterio.windows.Window(win.col_off - 1, win.row_off - 1, win.width + 3, win.height + 3)
        arr = src.read(1, window=win, boundless=True, fill_value=CDL_NODATA)
        t = src.window_transform(win)
        rows, cols = np.indices(arr.shape)
        x0 = t.c + cols * t.a
        y0 = t.f + rows * t.e
        boxes = shapely.box(x0, y0 + t.e, x0 + t.a, y0)
        codes = arr.ravel()
        boxes = boxes.ravel()
        for zone, g in geoms.items():
            if g.is_empty:
                res[zone] = {}
                continue
            hit = shapely.intersects(boxes, g)
            areas = shapely.area(shapely.intersection(boxes[hit], g))
            s = pd.Series(areas).groupby(codes[hit]).sum()
            res[zone] = {int(k): float(v) for k, v in s.items() if int(k) != CDL_NODATA and v > 0}
    return res


def cdl_by_zone(fields_utm, zones, width_m, cdl_dir: Path, years):
    rows, missing = [], []
    cdl_crs = "EPSG:5070"  # CDL Albers
    f5070 = fields_utm.to_crs(cdl_crs)
    z5070 = zones.to_crs(cdl_crs)
    for _, f in f5070.iterrows():
        sf = str(f.get("state_fips") or "").zfill(2)
        zg = z5070[z5070.field_id == f.field_id].set_index("zone").geometry
        geoms = {"field": f.geometry, "interior": zg["interior"], "headlands": zg["headlands"]}
        for y in years:
            rp = cdl_dir / f"CDL_{y}_{sf}.tif"
            if not rp.exists():
                missing.append(rp.name)
                continue
            for zone, shares in zone_cdl_shares(rp, geoms).items():
                tot = sum(shares.values())
                for code, a in sorted(shares.items(), key=lambda kv: (-kv[1], kv[0])):
                    rows.append({
                        "field_id": f.field_id, "year": y, "width_m": width_m, "zone": zone,
                        "crop_code": code, "crop_name": CDL_NAMES.get(code, f"CDL code {code}"),
                        "area_acres": round(a * ACRES_PER_SQM, 4),
                        "pct": round(100 * a / tot, 2) if tot else 0.0,
                    })
    return pd.DataFrame(rows), sorted(set(missing))


def plot_farm(fields_utm, zones, width_m, title, out_png: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n = len(fields_utm)
    ncol = 5 if n > 4 else n
    nrow = math.ceil(n / ncol)
    fig, axes = plt.subplots(nrow, ncol, figsize=(3.2 * ncol, 3.2 * nrow + 0.6), squeeze=False)
    summ = summarize(fields_utm, zones, width_m).set_index("field_id")
    for ax, (_, f) in zip(axes.flat, fields_utm.iterrows()):
        z = zones[zones.field_id == f.field_id]
        z[z.zone == "interior"].plot(ax=ax, color="#bbe5a0", edgecolor="none")
        z[z.zone == "headlands"].plot(ax=ax, color="#f4a259", edgecolor="none")
        gpd.GeoSeries([f.geometry], crs=fields_utm.crs).boundary.plot(ax=ax, color="#1b4332", lw=1)
        s = summ.loc[f.field_id]
        lab = f.get("label") or f.field_id[-7:]
        ax.set_title(f"{lab}  {s.field_acres:.0f} ac\nheadlands {s.headlands_pct:.1f} %", fontsize=9)
        ax.set_axis_off()
        ax.set_aspect("equal")
    for ax in list(axes.flat)[n:]:
        ax.set_axis_off()
    fig.suptitle(f"{title} - {width_m:g} m headlands ring (orange) vs interior (green)",
                 fontsize=12, fontweight="bold")
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=150, bbox_inches="tight", metadata={"Software": None})
    plt.close(fig)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--farm-dir", required=True, type=Path)
    ap.add_argument("--widths", type=float, nargs="+", default=[9.0],
                    help="ring widths in metres (e.g. 9 = one 30 ft equipment pass, 30 = one CDL/Landsat pixel)")
    ap.add_argument("--boundaries", type=Path, help="default: <farm>/boundary/field_boundaries.geojson")
    root = os.environ.get("DATA_PIPELINE_DATA_ROOT", str(Path.home() / "my-farm-advisor-runtime"))
    ap.add_argument("--cdl-dir", type=Path, default=Path(root) / "data-pipeline/shared/cdl/rasters")
    ap.add_argument("--years", type=int, nargs="+", default=[2021, 2022, 2023, 2024, 2025])
    ap.add_argument("--no-cdl", action="store_true", help="geometry and summary only")
    ap.add_argument("--no-plot", action="store_true")
    a = ap.parse_args(argv)

    farm = a.farm_dir.resolve()
    bpath = a.boundaries or farm / "boundary/field_boundaries.geojson"
    fields = gpd.read_file(bpath).sort_values("field_id").reset_index(drop=True)
    if fields.crs is None:
        fields = fields.set_crs("EPSG:4326")
    utm = fields.estimate_utm_crs()
    fields_utm = fields.to_crs(utm)
    fields_utm["geometry"] = fields_utm.geometry.make_valid()
    slug = farm.name.replace("-", "_")
    tables = farm / "derived/tables"
    tables.mkdir(parents=True, exist_ok=True)
    manifest = {
        "tool": "headlands-ring/src/headlands_farm.py",
        "boundaries": str(bpath.relative_to(farm)) if bpath.is_relative_to(farm) else str(bpath),
        "boundaries_sha256": sha256(bpath),
        "analysis_crs": utm.to_string(),
        "widths_m": a.widths, "years": a.years, "fields": len(fields),
        "cdl_weighting": "exact pixel-square area inside zone polygon (EPSG:5070)",
        "outputs": {},
    }
    for w in a.widths:
        tag = f"{w:g}m"
        zones = split_fields(fields_utm, w)
        summ = summarize(fields_utm, zones, w)
        bad = summ[summ.area_check_m2.abs() > 1.0]
        if not bad.empty:
            raise SystemExit(f"area check failed for {list(bad.field_id)}")
        empty = summ[summ.interior_acres == 0].field_id.tolist()

        out = zones.copy()
        out = out.merge(summ[["field_id", "field_acres"]], on="field_id")
        out["acres"] = (out.geometry.area * ACRES_PER_SQM).round(3)
        out = out.to_crs("EPSG:4326")
        out["geometry"] = out.geometry.apply(round_geom)
        gj = farm / f"boundary/field_headlands_{tag}.geojson"
        fc = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": mapping(r.geometry),
             "properties": {k: r[k] for k in ("field_id", "zone", "width_m", "acres", "field_acres")}}
            for _, r in out.iterrows()]}
        gj.write_text(json.dumps(fc, separators=(",", ":")))
        sp = tables / f"{slug}_headlands_{tag}_summary.csv"
        summ.drop(columns="area_check_m2").to_csv(sp, index=False)
        outs = {"geojson": str(gj.relative_to(farm)), "summary": str(sp.relative_to(farm)),
                "fields_without_interior": empty}

        if not a.no_cdl:
            cdl, missing = cdl_by_zone(fields_utm, zones, w, a.cdl_dir, a.years)
            cp = tables / f"{slug}_cdl_by_zone_{tag}.csv"
            cdl.to_csv(cp, index=False)
            outs.update({"cdl_by_zone": str(cp.relative_to(farm)), "missing_cdl_rasters": missing})
            if missing:
                print(f"  warning: missing CDL rasters {missing}")
        if not a.no_plot:
            pp = farm / f"derived/reports/{slug}_headlands_{tag}.png"
            try:
                meta = json.loads((farm / "farm.json").read_text())
            except (OSError, ValueError):
                meta = {}
            title = meta.get("display_name") or farm.name.replace("-", " ").title()
            if meta.get("state"):
                title += f" ({meta['state']})"
            plot_farm(fields_utm, zones, w, title, pp)
            outs["map"] = str(pp.relative_to(farm))
        manifest["outputs"][tag] = outs
        print(f"{farm.name} {tag}: headlands {summ.headlands_pct.min():.1f}-"
              f"{summ.headlands_pct.max():.1f} % of field area"
              + (f"; no interior: {empty}" if empty else ""))
    (farm / "boundary/headlands_manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
