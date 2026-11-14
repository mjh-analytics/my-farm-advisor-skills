#!/usr/bin/env python3
"""Apply field-boundary QC markups (from the QC viewer) to a farm's field boundaries.

Deterministic: same original boundaries + same markups CSV + same parameters -> same output.
It always starts from ``boundary/field_boundaries.original.geojson`` (the untouched source
outlines), never from a previously edited file, so re-running is idempotent.

Markup handling (CSV exported by the viewer's "Export pins & lines CSV"):
  line / edge     LINESTRING the user traced. If its ends are within --snap-m it is closed
                  and REPLACES the field outline. Otherwise it is reported, not applied.
  line / exclude  POLYGON the user outlined; it is CUT OUT of the field.
  pin / *         Reported only (pins carry no shape).

Straightening (--square-deg > 0): the drawn outline is simplified (--simplify-m), rotated
to the field's dominant axis, every edge within --square-deg of that axis (or its
perpendicular) is snapped onto it, and corners are rebuilt as edge intersections. If
squaring moves the outline by more than --max-square-shift-m it falls back to the
simplified outline. Fields without markups keep their original outline, unless listed
in --auto-clean (morphological closing + small outward grow for pixel-edged CSB shapes).

Outputs (per farm):
  boundary/field_boundaries.geojson         edited outlines + provenance properties
  boundary/csb_original_outlines.geojson    originals of edited fields (viewer overlay)
  boundary/boundary_edits_manifest.json     params, input hashes, per-field metrics
  manifests/field-inventory.csv             unchanged ids (field_id, field_slug)
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from pathlib import Path

import geopandas as gpd
from shapely import affinity, wkt
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

ACRE_M2 = 4046.8564224


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def dominant_angle(poly: Polygon) -> float:
    """Bearing (deg) of the longest side of the minimum rotated rectangle."""
    c = list(poly.minimum_rotated_rectangle.exterior.coords)
    sides = [(c[i], c[i + 1]) for i in range(4)]
    (x0, y0), (x1, y1) = max(sides, key=lambda s: math.dist(*s))
    return math.degrees(math.atan2(y1 - y0, x1 - x0))


def square_polygon(poly: Polygon, tol_deg: float, max_edge_move_m: float) -> Polygon:
    """Snap near-axis edges to the dominant axis and rebuild corners by intersection.

    An edge is snapped only if it is within ``tol_deg`` of the axis (or its perpendicular) AND
    rotating it onto the axis moves its ends by at most ``max_edge_move_m`` (L/2 * sin(dev)).
    Long edges that are genuinely a few degrees off square are therefore kept as drawn.
    """
    theta = dominant_angle(poly)
    origin = poly.centroid
    rp = affinity.rotate(poly, -theta, origin=origin)
    pts = list(rp.exterior.coords)[:-1]
    n = len(pts)
    edges = []  # (kind, value, p, q, length); kind in {"h","v","f"}
    for i in range(n):
        p, q = pts[i], pts[(i + 1) % n]
        ang = math.degrees(math.atan2(q[1] - p[1], q[0] - p[0])) % 180
        length = math.dist(p, q)
        dev_h, dev_v = min(ang, 180 - ang), abs(ang - 90)
        move = lambda dev: length / 2 * math.sin(math.radians(dev))
        if dev_h <= tol_deg and move(dev_h) <= max_edge_move_m:
            edges.append(["h", (p[1] + q[1]) / 2, p, q, length])
        elif dev_v <= tol_deg and move(dev_v) <= max_edge_move_m:
            edges.append(["v", (p[0] + q[0]) / 2, p, q, length])
        else:
            edges.append(["f", None, p, q, length])
    # merge consecutive edges snapped to the same axis (length-weighted offset)
    merged = True
    while merged and len(edges) > 3:
        merged = False
        for i in range(len(edges)):
            a, b = edges[i], edges[(i + 1) % len(edges)]
            if a[0] == b[0] != "f":
                w = a[4] + b[4]
                a[1] = (a[1] * a[4] + b[1] * b[4]) / w
                a[3], a[4] = b[3], w
                edges.pop((i + 1) % len(edges))
                merged = True
                break

    def line(e):
        if e[0] == "h":
            return (0.0, 1.0, e[1])  # y = c  ->  0*x + 1*y = c
        if e[0] == "v":
            return (1.0, 0.0, e[1])
        (x0, y0), (x1, y1) = e[2], e[3]
        a, b = y1 - y0, x0 - x1
        return (a, b, a * x0 + b * y0)

    verts = []
    for i in range(len(edges)):
        e0, e1 = edges[i - 1], edges[i]
        a1, b1, c1 = line(e0)
        a2, b2, c2 = line(e1)
        det = a1 * b2 - a2 * b1
        if abs(det) < 1e-9:
            verts.append(e1[2])
        else:
            verts.append(((c1 * b2 - c2 * b1) / det, (a1 * c2 - a2 * c1) / det))
    out = Polygon(verts)
    return affinity.rotate(out, theta, origin=origin)


def auto_clean(poly: Polygon, close_m: float, grow_m: float, simplify_m: float) -> Polygon:
    return poly.buffer(close_m, join_style=2).buffer(-close_m, join_style=2).buffer(grow_m, join_style=2).simplify(simplify_m)


def largest(geom):
    if geom.geom_type == "MultiPolygon":
        return max(geom.geoms, key=lambda g: g.area)
    return geom


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--farm-dir", required=True, type=Path, help=".../growers/<grower>/farms/<farm>")
    ap.add_argument("--markups", required=True, type=Path, help="field_qc_markups.csv from the viewer")
    ap.add_argument("--snap-m", type=float, default=15.0, help="max start/end gap closed automatically")
    ap.add_argument("--simplify-m", type=float, default=2.0, help="Douglas-Peucker tolerance for drawn outlines")
    ap.add_argument("--square-deg", type=float, default=8.0, help="snap edges within this angle of the field axis (0 = off)")
    ap.add_argument("--max-edge-move-m", type=float, default=3.0, help="snap an edge only if its ends move at most this much")
    ap.add_argument("--max-square-shift-m", type=float, default=6.0, help="reject squaring that moves the outline more than this")
    ap.add_argument("--min-iou", type=float, default=0.5, help="flag edits overlapping the original less than this")
    ap.add_argument("--min-zoom", type=float, default=16.0, help="warn for outlines digitized below this map zoom")
    ap.add_argument("--auto-clean", default="", help="comma-separated field_ids with no markups to auto-clean")
    ap.add_argument("--clean-close-m", type=float, default=20.0)
    ap.add_argument("--clean-grow-m", type=float, default=10.0)
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    args = ap.parse_args()

    bdir = args.farm_dir / "boundary"
    orig_path = bdir / "field_boundaries.original.geojson"
    if not orig_path.exists():
        sys.exit(f"missing {orig_path}: save the untouched source outlines there first")
    orig = gpd.read_file(orig_path).sort_values("field_id").reset_index(drop=True)
    utm = orig.estimate_utm_crs()
    orig_utm = orig.to_crs(utm)
    ids = set(orig["field_id"])

    with open(args.markups, newline="") as fh:
        marks = [r for r in csv.DictReader(fh) if r["field_id"] in ids]
    marks.sort(key=lambda r: (r["field_id"], r["kind"], r["type"], r["wkt"]))  # order-independent
    to_utm = lambda g: gpd.GeoSeries([g], crs="EPSG:4326").to_crs(utm).iloc[0]

    auto = {s.strip() for s in args.auto_clean.split(",") if s.strip()}
    out_geoms, report = [], []
    for _, row in orig_utm.iterrows():
        fid, base = row["field_id"], row.geometry
        fm = [m for m in marks if m["field_id"] == fid]
        rec = {"field_id": fid, "original_acres": round(base.area / ACRE_M2, 2), "actions": [], "warnings": []}
        geom, edited = base, False

        edges = [m for m in fm if m["kind"] == "line" and m["type"] == "edge"]
        replacements = []
        for m in edges:
            z = (m.get("zoom") or "").strip()
            if z and float(z) < args.min_zoom:
                rec["warnings"].append(f"outline drawn at zoom {z} (< {args.min_zoom:g}); clicks may be off by "
                                       f"~{156543.03 * math.cos(math.radians(40)) / 2 ** float(z):.0f} m per pixel")
            ls = to_utm(wkt.loads(m["wkt"]))
            coords = list(ls.coords)
            gap = math.dist(coords[0], coords[-1])
            rec.setdefault("closure_gaps_m", []).append(round(gap, 2))
            if len(coords) < 3:
                rec["warnings"].append("edge line has <3 points; skipped")
                continue
            if gap > args.snap_m:
                rec["warnings"].append(f"edge line not closed (gap {gap:.1f} m > {args.snap_m} m); not applied")
                continue
            ring = coords[:-1] + [coords[0]] if gap > 0 else coords
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = largest(poly.buffer(0))
                rec["warnings"].append("drawn outline self-intersected; repaired with buffer(0)")
            replacements.append(poly)
        if replacements:
            drawn = largest(unary_union(replacements))
            if len(replacements) > 1:
                rec["warnings"].append(f"{len(replacements)} outlines drawn; merged (union)")
            simp = drawn.simplify(args.simplify_m)
            final = simp
            if args.square_deg > 0:
                sq = square_polygon(simp, args.square_deg, args.max_edge_move_m)
                shift = sq.hausdorff_distance(simp) if sq.is_valid else float("inf")
                rec["square_shift_m"] = round(shift, 2)
                if sq.is_valid and shift <= args.max_square_shift_m:
                    final = sq
                    rec["actions"].append(f"squared (max shift {shift:.1f} m)")
                else:
                    rec["warnings"].append(f"squaring rejected (shift {shift:.1f} m); kept simplified outline")
            rec["vertices_drawn"] = len(drawn.exterior.coords) - 1
            rec["vertices_final"] = len(final.exterior.coords) - 1
            rec["actions"].insert(0, "replaced with user-drawn outline")
            geom, edited = final, True
        elif fid in auto:
            geom = auto_clean(base, args.clean_close_m, args.clean_grow_m, args.simplify_m)
            rec["actions"].append(f"auto-clean (close {args.clean_close_m:g} m, grow {args.clean_grow_m:g} m)")
            edited = True

        for m in fm:
            if m["kind"] == "line" and m["type"] == "exclude":
                cut = to_utm(wkt.loads(m["wkt"]))
                geom = largest(geom.difference(cut))
                rec["actions"].append(f"cut out {cut.area / ACRE_M2:.2f} ac")
                edited = True
            elif m["kind"] == "pin" or m["type"] == "note":
                rec["warnings"].append(f"{m['kind']} '{m['type']}' noted, not applied: {m['note'][:60]}")

        iou = geom.intersection(base).area / geom.union(base).area
        rec.update(final_acres=round(geom.area / ACRE_M2, 2), iou_vs_original=round(iou, 3),
                   area_change_pct=round(100 * (geom.area - base.area) / base.area, 1), edited=edited)
        if iou < args.min_iou:
            rec["warnings"].append(f"low overlap with original (IoU {iou:.2f}) - check it is the same field")
        out_geoms.append(geom)
        report.append(rec)

    # neighbour overlap check
    for i in range(len(out_geoms)):
        for j in range(i + 1, len(out_geoms)):
            ov = out_geoms[i].intersection(out_geoms[j]).area / ACRE_M2
            if ov > 0.05:
                for k, other in ((i, j), (j, i)):
                    report[k]["warnings"].append(f"overlaps {report[other]['field_id']} by {ov:.2f} ac")

    params = {k: v for k, v in vars(args).items() if k not in ("farm_dir", "markups", "dry_run")}
    for r in report:
        flag = "EDIT" if r["edited"] else "keep"
        print(f"{flag} {r['field_id']}: {r['original_acres']} -> {r['final_acres']} ac "
              f"(IoU {r['iou_vs_original']}) {'; '.join(r['actions'])}")
        for w in r["warnings"]:
            print(f"     ! {w}")
    if args.dry_run:
        return 0

    out = orig.copy()
    out_utm = gpd.GeoSeries(out_geoms, crs=utm)
    out["geometry"] = out_utm.to_crs(orig.crs).values
    out["area_acres"] = [round(g.area / ACRE_M2, 3) for g in out_geoms]
    out["boundary_edit"] = ["; ".join(r["actions"]) for r in report]
    out["boundary_source"] = [("user-edited from " if r["edited"] else "") + str(s) for r, s in zip(report, orig["source"])]
    out.to_file(bdir / "field_boundaries.geojson", driver="GeoJSON")
    orig[[r["edited"] for r in report]].to_file(bdir / "csb_original_outlines.geojson", driver="GeoJSON")
    manifest = {
        "tool": "field-boundary-review/apply_boundary_edits.py",
        "params": params,
        "inputs": {"original": {"path": orig_path.name, "sha256": sha256(orig_path)},
                   "markups": {"path": args.markups.name, "sha256": sha256(args.markups)}},
        "analysis_crs": str(utm),
        "fields": report,
    }
    (bdir / "boundary_edits_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote {bdir/'field_boundaries.geojson'} and boundary_edits_manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
