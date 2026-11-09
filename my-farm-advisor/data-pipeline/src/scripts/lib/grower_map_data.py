"""Discover grower-level field data for the interactive web-map subskills.

The module is deliberately renderer-agnostic: both the Leaflet generator and
the D3 comparison generator consume the same payload so their maps show
identical data. It only reads canonical runtime artifacts (field boundaries,
CDL rotation tables, crop composition, and SSURGO summaries) and never writes
anything.

Runtime layout it expects, per grower::

    growers/<grower>/farms/<farm>/farm.json
    growers/<grower>/farms/<farm>/manifests/field-inventory.csv
    growers/<grower>/farms/<farm>/fields/<field>/boundary/field_boundary.geojson
    growers/<grower>/farms/<farm>/derived/tables/<farm>_crop_rotation.csv
    growers/<grower>/farms/<farm>/derived/tables/<farm>_cdl_<start>_<end>_full_composition.csv
    growers/<grower>/farms/<farm>/derived/tables/<farm>_ssurgo_summary.csv
"""

from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from paths import (
    farm_cdl_preferred_full_composition_path,
    farm_cdl_rotation_path,
    farm_manifest_dir,
    farm_ssurgo_summary_path,
    field_boundary_path,
    grower_dir,
)

SCHEMA_VERSION = 1
DEFAULT_WINDOW = (2021, 2025)
FARM_COLORS = (
    "#2E7D32",
    "#1565C0",
    "#6A1B9A",
    "#EF6C00",
    "#00838F",
    "#AD1457",
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    parsed = _as_float(value)
    return int(parsed) if parsed is not None else None


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _title_from_slug(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part)


def _exterior_ring(geometry: dict[str, Any]) -> list[list[float]]:
    """Return the largest exterior ring as ``[[lon, lat], ...]``."""
    coordinates = geometry.get("coordinates") or []
    if geometry.get("type") == "Polygon":
        rings = coordinates[:1]
    elif geometry.get("type") == "MultiPolygon":
        rings = [polygon[0] for polygon in coordinates if polygon]
    else:
        rings = []

    best: list[list[float]] = []
    best_area = -1.0
    for ring in rings:
        if not isinstance(ring, list) or len(ring) < 3:
            continue
        area = abs(_shoelace_area(ring))
        if area > best_area:
            best = ring
            best_area = area
    return best


def _shoelace_area(ring: Iterable[Iterable[float]]) -> float:
    points = [(float(p[0]), float(p[1])) for p in ring if p and len(p) >= 2]
    if len(points) < 3:
        return 0.0
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _centroid(ring: list[list[float]]) -> tuple[float, float] | None:
    """Area-weighted centroid of a lon/lat ring; returns ``(lat, lon)``."""
    points = [(float(p[0]), float(p[1])) for p in ring if p and len(p) >= 2]
    if len(points) < 3:
        return None
    twice_area = 0.0
    cx = 0.0
    cy = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        cross = x1 * y2 - x2 * y1
        twice_area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(twice_area) < 1e-12:
        lon = sum(p[0] for p in points) / len(points)
        lat = sum(p[1] for p in points) / len(points)
    else:
        lon = cx / (3.0 * twice_area)
        lat = cy / (3.0 * twice_area)
    return (round(lat, 7), round(lon, 7))


def _rotation_lookup(grower_slug: str, farm_slug: str) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in _read_csv_rows(farm_cdl_rotation_path(grower_slug, farm_slug)):
        field_id = str(row.get("field_id", "")).strip()
        if not field_id:
            continue
        lookup[field_id] = {
            "sequence": row.get("rotation_sequence") or "",
            "years": _as_int(row.get("history_years")) or 0,
            "start_year": _as_int(row.get("history_start_year")),
            "end_year": _as_int(row.get("history_end_year")),
            "diversity": _as_int(row.get("crop_diversity")),
            "corn_years": _as_int(row.get("corn_years")),
            "soybean_years": _as_int(row.get("soybean_years")),
            "predicted_next_crop": row.get("predicted_next_crop") or "",
            "predicted_following_crop": row.get("predicted_following_crop") or "",
            "confidence": row.get("rotation_confidence") or "",
            "pattern_summary": row.get("rotation_patterns") or "",
        }
    return lookup


def _soil_lookup(grower_slug: str, farm_slug: str) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in _read_csv_rows(farm_ssurgo_summary_path(grower_slug, farm_slug)):
        field_id = str(row.get("field_id", "")).strip()
        if not field_id:
            continue
        lookup[field_id] = {
            "dominant_soil": row.get("dominant_soil") or "",
            "drainage_class": row.get("drainage_class") or "",
            "avg_organic_matter_pct": _round(_as_float(row.get("avg_om_pct")), 2),
            "avg_ph": _round(_as_float(row.get("avg_ph")), 2),
            "aws_inches": _round(_as_float(row.get("total_aws_inches")), 2),
            "avg_cec": _round(_as_float(row.get("avg_cec")), 1),
            "avg_clay_pct": _round(_as_float(row.get("avg_clay_pct")), 1),
            "avg_sand_pct": _round(_as_float(row.get("avg_sand_pct")), 1),
            "ph_constraint": row.get("ph_constraint") or "",
            "erosion_risk": row.get("erosion_risk") or "",
            "source": row.get("soil_source") or "",
        }
    return lookup


def _crop_year_lookup(grower_slug: str, farm_slug: str) -> dict[str, dict[str, str]]:
    """Return ``{field_id: {year: dominant crop name}}`` from CDL composition."""
    path = farm_cdl_preferred_full_composition_path(grower_slug, farm_slug)
    lookup: dict[str, dict[str, float]] = {}
    names: dict[tuple[str, str], str] = {}
    for row in _read_csv_rows(path):
        field_id = str(row.get("field_id", "")).strip()
        year = str(_as_int(row.get("year")) or "")
        crop_name = row.get("crop_name") or ""
        if not field_id or not year or not crop_name:
            continue
        pct = _as_float(row.get("pct")) or 0.0
        key = (field_id, year)
        if pct >= lookup.get(field_id, {}).get(year, -1.0):
            lookup.setdefault(field_id, {})[year] = pct
            names[key] = crop_name

    return {
        field_id: {year: names[(field_id, year)] for year in sorted(years)}
        for field_id, years in lookup.items()
    }


def _inventory_field_ids(inventory_path: Path) -> list[str]:
    rows = _read_csv_rows(inventory_path)
    ordered: list[str] = []
    for row in rows:
        field_id = str(row.get("field_id", "")).strip()
        if field_id and field_id not in ordered:
            ordered.append(field_id)
    return ordered


def _farm_directories(grower_slug: str) -> list[Path]:
    farms_root = grower_dir(grower_slug) / "farms"
    if not farms_root.exists():
        return []
    return sorted(path for path in farms_root.iterdir() if path.is_dir())


def _field_record(
    *,
    grower_slug: str,
    farm_slug: str,
    farm_name: str,
    farm_state: str,
    field_id: str,
    rotation: dict[str, Any],
    soil: dict[str, Any],
    crops: dict[str, str],
) -> dict[str, Any] | None:
    boundary_path = field_boundary_path(grower_slug, farm_slug, field_id)
    payload = _read_json(boundary_path)
    features = payload.get("features") or []
    if not features:
        return None
    feature = features[0]
    geometry = feature.get("geometry") or {}
    properties = feature.get("properties") or {}
    ring = _exterior_ring(geometry)
    center = _centroid(ring) if ring else None
    if center is None:
        return None
    area = _as_float(properties.get("area_acres"))
    if area is None and ring:
        # Approximate acres from degree-space area at this latitude.
        lat = center[0]
        km_per_deg_lat = 110.574
        km_per_deg_lon = 111.320 * max(0.01, abs(math.cos(math.radians(lat))))
        sq_km = abs(_shoelace_area(ring)) * km_per_deg_lat * km_per_deg_lon
        area = sq_km * 247.105

    return {
        "field_id": field_id,
        "field_slug": str(properties.get("field_slug") or field_id),
        "farm_slug": farm_slug,
        "farm_name": farm_name,
        "farm_state": farm_state,
        "area_acres": _round(area, 1),
        "center": [center[0], center[1]],
        "geometry": {"type": geometry.get("type"), "coordinates": geometry.get("coordinates")},
        "county_name": properties.get("county_name") or "",
        "rotation": rotation,
        "soil": soil,
        "crops": crops,
    }


def build_grower_map_payload(
    grower_slug: str,
    window: tuple[int, int] = DEFAULT_WINDOW,
) -> dict[str, Any]:
    """Build the renderer-neutral payload for one grower."""
    farms: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []

    for index, farm_dir_path in enumerate(_farm_directories(grower_slug)):
        farm_slug = farm_dir_path.name
        farm_meta = _read_json(farm_dir_path / "farm.json")
        farm_name = str(farm_meta.get("display_name") or _title_from_slug(farm_slug))
        farm_state = str(farm_meta.get("state") or "")
        farm_country = str(farm_meta.get("country") or "")

        inventory_path = farm_manifest_dir(grower_slug, farm_slug) / "field-inventory.csv"
        field_ids = _inventory_field_ids(inventory_path)
        if not field_ids:
            field_ids = sorted(
                path.name
                for path in (farm_dir_path / "fields").glob("*")
                if path.is_dir()
            )

        rotation_lookup = _rotation_lookup(grower_slug, farm_slug)
        soil_lookup = _soil_lookup(grower_slug, farm_slug)
        crop_lookup = _crop_year_lookup(grower_slug, farm_slug)

        farm_fields: list[dict[str, Any]] = []
        for field_id in field_ids:
            record = _field_record(
                grower_slug=grower_slug,
                farm_slug=farm_slug,
                farm_name=farm_name,
                farm_state=farm_state,
                field_id=field_id,
                rotation=rotation_lookup.get(field_id, {}),
                soil=soil_lookup.get(field_id, {}),
                crops=crop_lookup.get(field_id, {}),
            )
            if record is None:
                continue
            farm_fields.append(record)
            fields.append(record)

        bounds = _bounds_for(farm_fields)
        farms.append(
            {
                "slug": farm_slug,
                "display_name": farm_name,
                "state": farm_state,
                "country": farm_country,
                "field_count": len(farm_fields),
                "acres": _round(sum(f["area_acres"] or 0.0 for f in farm_fields), 1),
                "bounds": bounds,
                "color": FARM_COLORS[index % len(FARM_COLORS)],
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "window": {"start_year": window[0], "end_year": window[1]},
        "grower": {
            "slug": grower_slug,
            "display_name": _title_from_slug(grower_slug),
            "farm_count": len(farms),
            "field_count": len(fields),
            "acres": _round(sum(f["area_acres"] or 0.0 for f in fields), 1),
        },
        "farms": farms,
        "fields": fields,
        "bounds": _bounds_for(fields),
    }


def _bounds_for(fields: list[dict[str, Any]]) -> list[list[float]] | None:
    points: list[tuple[float, float]] = []
    for field in fields:
        for ring_point in _iter_coordinates(field["geometry"]):
            points.append((ring_point[1], ring_point[0]))
    if not points:
        return None
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return [[min(lats), min(lons)], [max(lats), max(lons)]]


def _iter_coordinates(geometry: dict[str, Any]) -> Iterable[list[float]]:
    coordinates = geometry.get("coordinates")
    if not coordinates:
        return []

    def walk(node: Any) -> Iterable[list[float]]:
        if (
            isinstance(node, list)
            and len(node) >= 2
            and all(isinstance(value, (int, float)) for value in node[:2])
        ):
            yield [float(node[0]), float(node[1])]
            return
        if isinstance(node, list):
            for child in node:
                yield from walk(child)

    return list(walk(coordinates))


def grower_report_path(grower_slug: str, filename: str) -> Path:
    """Return a path under ``growers/<grower>/derived/reports``."""
    return grower_dir(grower_slug) / "derived" / "reports" / filename
