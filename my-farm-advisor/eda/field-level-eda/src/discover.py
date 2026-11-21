"""Find growers, farms and fields under a My Farm Advisor runtime and load their tables.

Nothing here is specific to a state, county or field: every grower directory with
``farms/<farm>/boundary/field_boundaries.geojson`` is picked up, in sorted order.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import pandas as pd

ACRES_PER_M2 = 0.0002471053814671653

FIPS_ABBR = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT",
    "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL",
    "18": "IN", "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD",
    "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT", "31": "NE",
    "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA", "54": "WV",
    "55": "WI", "56": "WY",
}


class InputTracker:
    """Records every file read, with its SHA-256, for the run manifest."""

    def __init__(self, root: Path):
        self.root = root
        self.files: dict[str, str] = {}

    def add(self, path: Path) -> Path:
        h = hashlib.sha256(path.read_bytes()).hexdigest()
        key = path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else path.as_posix()
        self.files[key] = h
        return path


@dataclass
class Farm:
    grower: str
    farm: str
    path: Path
    state: str
    display_name: str
    fields: gpd.GeoDataFrame            # WGS84, one row per field, sorted by field_id
    tables: dict[str, Path] = field(default_factory=dict)


def _first(tables: Path, pattern: str) -> Path | None:
    hits = sorted(tables.glob(pattern))
    return hits[0] if hits else None


def field_labels(farm_dir: Path, field_ids: list[str], prefix: str,
                 pool_glob: str = "*candidates*.geojson") -> dict[str, str]:
    """Same rule as field-boundary-review: number fields within the largest candidate pool
    (ties -> file-name order) so labels match the review viewer; else number the farm's own
    fields. Labels look like ``IA-07``."""
    pools = []
    for p in sorted((farm_dir / "boundary").glob(pool_glob)):
        try:
            pools.append([str(f["properties"]["field_id"]) for f in json.loads(p.read_text())["features"]])
        except (ValueError, KeyError):
            continue
    pool = max(pools, key=len) if pools else []
    if not set(field_ids) <= set(pool):
        pool = field_ids
    return {fid: f"{prefix}-{i:02d}" for i, fid in enumerate(sorted(pool), 1) if fid in set(field_ids)}


def list_growers(growers_dir: Path) -> list[str]:
    """Grower folders that contain at least one farm with a boundary file."""
    return sorted(g.name for g in growers_dir.iterdir()
                  if (g / "farms").is_dir()
                  and any((f / "boundary" / "field_boundaries.geojson").exists()
                          for f in (g / "farms").iterdir() if f.is_dir()))


def discover(growers_dir: Path, tracker: InputTracker, grower: str | None = None) -> list[Farm]:
    farms: list[Farm] = []
    for gdir in sorted(d for d in growers_dir.iterdir()
                       if (d / "farms").is_dir() and (grower is None or d.name == grower)):
        for fdir in sorted(d for d in (gdir / "farms").iterdir() if d.is_dir()):
            bpath = fdir / "boundary" / "field_boundaries.geojson"
            if not bpath.exists():
                continue
            gdf = gpd.read_file(tracker.add(bpath))
            if gdf.crs is None:
                gdf = gdf.set_crs("EPSG:4326")
            gdf = gdf.to_crs("EPSG:4326")
            gdf["field_id"] = gdf["field_id"].astype(str)
            gdf = gdf.sort_values("field_id").reset_index(drop=True)
            fips = [str(x).zfill(2) for x in gdf.get("state_fips", pd.Series(dtype=str)).dropna()]
            state = FIPS_ABBR.get(max(set(fips), key=fips.count), "") if fips else ""
            meta = {}
            if (fdir / "farm.json").exists():
                meta = json.loads(tracker.add(fdir / "farm.json").read_text())
            state = state or str(meta.get("state") or "")
            prefix = state or gdir.name[:2].upper()
            labels = field_labels(fdir, gdf.field_id.tolist(), prefix)
            gdf["label"] = gdf.field_id.map(labels)
            gdf["grower"] = gdir.name
            gdf["farm"] = fdir.name
            gdf["state"] = state
            if "county_name" not in gdf:
                gdf["county_name"] = ""
            tdir = fdir / "derived" / "tables"
            tables = {
                "weather": _first(tdir, "*_weather_*.csv"),
                "rotation": _first(tdir, "*_crop_rotation.csv"),
                "dem": _first(tdir, "*_dem_summary.csv"),
            }
            cdl_years = sorted(tdir.glob("*_[12][0-9][0-9][0-9]_cdl.csv"))
            tables.update({f"cdl_{p.stem.split('_')[-2]}": p for p in cdl_years})
            for p in sorted(tdir.glob("*_headlands_*m_summary.csv")):
                tables[f"headlands_{p.stem.split('_')[-2]}"] = p
            for p in sorted(tdir.glob("*_cdl_by_zone_*m.csv")):
                tables[f"cdl_zone_{p.stem.split('_')[-1]}"] = p
            for p in sorted((fdir / "boundary").glob("field_headlands_*m.geojson")):
                tables[f"headlands_geom_{p.stem.split('_')[-1]}"] = p
            farms.append(Farm(gdir.name, fdir.name, fdir, state,
                              str(meta.get("display_name") or fdir.name), gdf,
                              {k: v for k, v in tables.items() if v is not None}))
    return farms


def grower_labels(farms: list[Farm]) -> dict[str, str]:
    """Short grower names for axes: the state when every grower has its own state,
    otherwise the grower slug."""
    by_grower: dict[str, set[str]] = {}
    for f in farms:
        by_grower.setdefault(f.grower, set()).add(f.state)
    states = [next(iter(s)) if len(s) == 1 else "" for s in by_grower.values()]
    if all(states) and len(set(states)) == len(states):
        return {g: next(iter(s)) for g, s in by_grower.items()}
    return {g: g for g in by_grower}


def read_csv(path: Path, tracker: InputTracker, **kw) -> pd.DataFrame:
    return pd.read_csv(tracker.add(path), **kw)


def field_geometry_table(farms: list[Farm]) -> pd.DataFrame:
    """Acres, perimeter and compactness measured in each farm's UTM zone."""
    rows = []
    for f in farms:
        utm = f.fields.to_crs(f.fields.estimate_utm_crs())
        for (_, r), (_, u) in zip(f.fields.iterrows(), utm.iterrows()):
            a, p = u.geometry.area, u.geometry.length
            c = r.geometry.centroid
            rows.append({
                "grower": f.grower, "farm": f.farm, "state": f.state, "field_id": r.field_id,
                "label": r.label, "county": r.get("county_name") or "",
                "county_fips": (str(r.get("state_fips") or "").zfill(2) + str(r.get("county_fips") or "").zfill(3))
                if r.get("county_fips") else "",
                "acres": round(a * ACRES_PER_M2, 2), "perimeter_ft": round(p * 3.28084, 0),
                "compactness": round(4 * math.pi * a / p ** 2, 3) if p else None,
                "lon": round(c.x, 6), "lat": round(c.y, 6),
            })
    return pd.DataFrame(rows)


def _match(spec: str, row) -> bool:
    t = spec.strip().lower()
    fid, lab = str(row.field_id).lower(), str(row.label).lower()
    if t in (fid, lab):
        return True
    if len(t) >= 4 and fid.endswith(t):          # csb id suffix, e.g. 4849132
        return True
    return t.isdigit() and lab.rsplit("-", 1)[-1].lstrip("0") == t.lstrip("0")   # "7" or "07"


def select_fields(farms: list[Farm], include: list[str] | None, exclude: list[str] | None) -> list[Farm]:
    """Keep only the requested fields (``include``) minus ``exclude``. A spec is a field label
    (``IL-07``), a full or trailing part of the field id, or the bare label number. Unknown or
    ambiguous specs stop the run, so a typo never silently changes the analysis."""
    for kind, specs in (("--fields", include), ("--exclude-fields", exclude)):
        for sp in specs or []:
            hits = [(f.farm, r.field_id) for f in farms for r in f.fields.itertuples() if _match(sp, r)]
            if len(hits) != 1:
                known = ", ".join(r.label for f in farms for r in f.fields.itertuples())
                raise SystemExit(f"{kind} '{sp}' matches {len(hits)} fields (known labels: {known})")
    out = []
    for f in farms:
        keep = [(not include or any(_match(s, r) for s in include))
                and not any(_match(s, r) for s in (exclude or []))
                for r in f.fields.itertuples()]
        sub = f.fields[keep].reset_index(drop=True)
        if len(sub):
            f.fields = sub
            out.append(f)
    if not out:
        raise SystemExit("no fields left after --fields / --exclude-fields")
    return out
