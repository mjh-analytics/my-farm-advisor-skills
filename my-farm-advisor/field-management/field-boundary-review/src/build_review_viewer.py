#!/usr/bin/env python3
"""Build a single-file HTML field QC viewer for every grower/farm in a pipeline runtime.

Reads (per farm): boundary/field_boundaries.geojson (master list; works before any download),
optional per-field outputs (weather, satellite, soil, rotation, report PNGs) and optional
boundary/csb_original_outlines.geojson (drawn as dashed overlay for edited fields).

The page lets a reviewer mark each field Keep/Drop with a comment, drop pins, draw lines or
outlines, and export two CSVs: field_qc_decisions.csv and field_qc_markups.csv (the input of
apply_boundary_edits.py). Reviewer input lives in the browser (localStorage) until exported.

Output is deterministic for the same runtime contents: fields are sorted by grower, farm,
field_id; labels (e.g. IL-07) are numbered from a candidate-pool file when one exists so they
stay stable while the selection shrinks.

Usage:
    python build_review_viewer.py --growers-dir ~/my-farm-advisor-runtime/data-pipeline/growers
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from shapely.geometry import shape

FIPS_ABBR = {
    "01": "AL", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE", "12": "FL",
    "13": "GA", "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY", "22": "LA",
    "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT",
    "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD", "47": "TN",
    "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI", "56": "WY",
}
TEMPLATE = Path(__file__).with_name("review_viewer_template.html")


def read_csv(p: Path) -> dict:
    if not p.exists():
        return {}
    with open(p) as fh:
        return {r["field_id"]: r for r in csv.DictReader(fh)}


def count_rows(p: Path) -> int:
    if not p.exists():
        return 0
    with open(p) as fh:
        return max(sum(1 for _ in fh) - 1, 0)


def rnd(c):
    return [rnd(x) for x in c] if isinstance(c[0], list) else [round(c[0], 6), round(c[1], 6)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--growers-dir", required=True, type=Path)
    ap.add_argument("--out", type=Path, help="default: <growers-dir>/qc_field_viewer.html (keep it there so chart links resolve)")
    ap.add_argument("--large-acres", type=float, default=250.0)
    ap.add_argument("--small-acres", type=float, default=20.0)
    ap.add_argument("--min-fill", type=float, default=0.75, help="flag shapes filling less of their min. rotated rectangle")
    ap.add_argument("--min-cdl-share", type=float, default=0.7)
    ap.add_argument("--weather-days", type=int, default=1826, help="expected daily weather rows per field")
    ap.add_argument("--label-pool-glob", default="*candidates*.geojson", help="boundary file used to number labels")
    args = ap.parse_args()

    G = args.growers_dir.expanduser()
    out_path = (args.out or G / "qc_field_viewer.html").expanduser()
    features, farm_issues = [], []
    for gdir in sorted(d for d in G.iterdir() if d.is_dir() and (d / "farms").is_dir()):
        for fdir in sorted(d for d in (gdir / "farms").iterdir() if d.is_dir()):
            farm_bound = fdir / "boundary" / "field_boundaries.geojson"
            if not farm_bound.exists():
                continue
            farm_feats = json.loads(farm_bound.read_text())["features"]
            fips = [str(f["properties"].get("state_fips") or "") for f in farm_feats]
            fips = [x for x in fips if x]
            state = FIPS_ABBR.get(max(set(fips), key=fips.count)) if fips else ""
            farm = json.loads((fdir / "farm.json").read_text()) if (fdir / "farm.json").exists() else {}
            if farm and state and farm.get("state") != state:
                farm_issues.append(f"{fdir.name}/farm.json has state='{farm.get('state')}', fields are in {state}")
            tables = fdir / "derived" / "tables"
            rot = read_csv(next(tables.glob("*crop_rotation.csv"), Path("_")))
            soil = read_csv(next(tables.glob("*ssurgo_summary.csv"), Path("_")))
            prefix = state or gdir.name[:2].upper()
            # largest candidate pool wins (ties -> name order), so labels match what the reviewer saw
            pools = [json.loads(p.read_text())["features"] for p in sorted((fdir / "boundary").glob(args.label_pool_glob))]
            pool_feats = max(pools, key=len) if pools else farm_feats
            pool_ids = sorted(str(f["properties"]["field_id"]) for f in pool_feats)
            labels = {fid: f"{prefix}-{i:02d}" for i, fid in enumerate(pool_ids, 1)}
            orig_file = fdir / "boundary" / "csb_original_outlines.geojson"
            orig_geoms = ({f["properties"]["field_id"]: f["geometry"] for f in json.loads(orig_file.read_text())["features"]}
                          if orig_file.exists() else {})

            for feat in sorted(farm_feats, key=lambda f: str(f["properties"]["field_id"])):
                props = feat["properties"]
                fid = str(props["field_id"])
                fld = fdir / "fields" / fid
                downloaded = (fld / "boundary" / "field_boundary.geojson").exists()
                geom = shape(feat["geometry"])
                mrr = geom.minimum_rotated_rectangle
                compact = geom.area / mrr.area if mrr.area else 0
                r, s = rot.get(fid, {}), soil.get(fid, {})
                ls_n = sum(1 for _ in (fld / "satellite" / "landsat").glob("*/*"))
                s2_n = sum(1 for _ in (fld / "satellite" / "sentinel").glob("*/*"))
                wx = count_rows(fld / "weather" / "daily_weather.csv")
                acres = float(props.get("area_acres") or 0)
                cdl_share = props.get("cdl_crop_share")
                flags = []
                if acres > args.large_acres:
                    flags.append(f"large (>{args.large_acres:g} ac) – may be several fields")
                if acres < args.small_acres:
                    flags.append(f"small (<{args.small_acres:g} ac)")
                if compact < args.min_fill:
                    flags.append(f"irregular shape (fill {compact:.2f})")
                if cdl_share is not None and float(cdl_share) < args.min_cdl_share:
                    flags.append(f"low CDL crop share ({float(cdl_share):.2f})")
                if downloaded:
                    if wx < args.weather_days:
                        flags.append(f"weather rows {wx} < {args.weather_days}")
                    if ls_n + s2_n == 0:
                        flags.append("no satellite scenes")
                    if not r:
                        flags.append("missing crop rotation")
                    if not s:
                        flags.append("missing SSURGO summary")
                rel = fld.relative_to(out_path.parent).as_posix() if fld.is_relative_to(out_path.parent) else fld.as_posix()
                imgs = {
                    "Field report": "derived/reports/field_report.png",
                    "Cumulative NDVI": "derived/features/ndvi_current_season_cumulative.png",
                    "Soil components": "derived/features/soil_component_map.png",
                    "Soil properties": "derived/features/soil_weighted_property_map.png",
                }
                imgs = {k: f"{rel}/{v}" for k, v in imgs.items() if (fld / v).exists()}
                g = json.loads(json.dumps(feat["geometry"]))
                g["coordinates"] = rnd(g["coordinates"])
                features.append({"type": "Feature", "geometry": g, "properties": {
                    "grower": gdir.name, "farm": fdir.name, "field": fid, "label": labels.get(fid, fid),
                    "county": props.get("county_name"), "state_fips": props.get("state_fips"),
                    "acres": round(acres, 1), "compact": round(compact, 2),
                    "cdl_share": None if cdl_share is None else round(float(cdl_share), 2),
                    "rotation": r.get("rotation_sequence", ""), "corn_years": r.get("corn_years"),
                    "soy_years": r.get("soybean_years"), "dominant_soil": s.get("dominant_soil", ""),
                    "drainage": s.get("drainage_class", ""), "om": s.get("avg_om_pct", ""),
                    "weather_days": wx, "landsat": ls_n, "sentinel": s2_n,
                    "flags": flags, "imgs": imgs, "downloaded": downloaded,
                    "source": props.get("boundary_source") or props.get("source", ""),
                    "csb_crop": props.get("crop_name", ""),
                    "edit": props.get("boundary_edit") or "", "orig": orig_geoms.get(fid),
                }})

    data = json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":"))
    html = TEMPLATE.read_text().replace("__DATA__", data).replace("__FARM_ISSUES__", json.dumps(farm_issues))
    out_path.write_text(html)
    print(f"Wrote {out_path} ({out_path.stat().st_size / 1024:.0f} KB), {len(features)} fields")
    for f in features:
        p = f["properties"]
        if p["flags"]:
            print(f"  {p['label']:7} {p['field']:22} {'; '.join(p['flags'])}")
    for i in farm_issues:
        print("  FARM:", i)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
