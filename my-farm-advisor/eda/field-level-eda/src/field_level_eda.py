"""Field-level EDA for ONE grower of a My Farm Advisor runtime (static PNG + CSV).

Run it for a grower and (optionally) a subset of their fields; the same inputs always give the
same files. Compares the grower's fields with each other, each field across years, and each
field's edge with its interior - the questions a grower or an analyst preparing a grower report
asks about their own ground.

* Field boundaries - size and headland (edge) share per field, terrain, field size vs crop diversity.
* CDL crop history - rotation grid, corn/soy acres by year with next-season projection, edge vs
  interior crop purity, non-crop area inside each boundary.
* Weather - cumulative growing degree days by year, weather during each field's crop-critical
  month, how much of the weather variation is year-to-year vs between fields, season water balance.
* Map - fields by latest crop and by headland share.
* Field scorecard - one row per field combining all of the above.

Inputs come from the runtime (``growers/<grower>/farms/<farm>``); nothing is hard-coded.
Requires the pipeline tables (weather, CDL, DEM) and headlands-ring outputs for the farm.

Outputs (``--out``): ``figures/*.png``, ``tables/*.csv``, ``findings.json`` (what each output
shows, with the numbers), ``manifest.json`` (parameters, selected fields, SHA-256 of every input
and output). Deterministic: no randomness, sorted rows, rounded numbers, fixed fonts, no PNG
timestamps. ``--verify`` re-runs into a temporary folder and compares output hashes.

Examples
  field_level_eda.py --list
  field_level_eda.py --grower illinois-grower
  field_level_eda.py --grower illinois-grower --exclude-fields IL-03 IL-07
  field_level_eda.py --grower iowa-grower --fields IA-01 IA-02 IA-05
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import tempfile
from pathlib import Path

import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from scipy import stats  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agro  # noqa: E402
from discover import (ACRES_PER_M2, InputTracker, discover, field_geometry_table,  # noqa: E402
                      list_growers, read_csv, select_fields)

VERSION = "0.2.0"
ACCENT = "#0072B2"
CROP_GROUPS = [  # (name, colour, CDL codes)
    ("Corn", "#E3B505", {1, 12, 13}),
    ("Soybeans", "#3A8D3A", {5, 26, 239, 240, 241, 254}),
    ("Other crops", "#B07AA1", None),  # any other cultivated class
    ("Hay / grass / pasture", "#A8C686", {36, 37, 58, 59, 60, 62, 176}),
    ("Fallow / idle", "#D9C8A9", {61}),
    ("Developed / roads", "#8C8C8C", {82, 121, 122, 123, 124}),
    ("Forest / shrub", "#4E6E58", {63, 64, 141, 142, 143, 152}),
    ("Water / wetland", "#5DA5DA", {83, 87, 92, 111, 112, 190, 195}),
    ("Barren / other", "#C9B8A0", {0, 65, 81, 88, 131}),
]
CROPLAND = ("Corn", "Soybeans", "Other crops")


def crop_group(code: int) -> str:
    for name, _, codes in CROP_GROUPS:
        if codes is not None and int(code) in codes:
            return name
    return "Other crops"


GROUP_COLOR = {n: c for n, c, _ in CROP_GROUPS}


def style():
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 9, "axes.titlesize": 11,
        "axes.titleweight": "bold", "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.color": "#e5e5e5", "grid.linewidth": 0.6,
        "axes.axisbelow": True, "figure.dpi": 150, "savefig.dpi": 150,
        "svg.hashsalt": "field-level-eda", "legend.frameon": False,
    })


class Run:
    def __init__(self, out: Path):
        self.out = out
        (out / "figures").mkdir(parents=True, exist_ok=True)
        (out / "tables").mkdir(parents=True, exist_ok=True)
        self.items: list[dict] = []

    def fig(self, fig, name: str) -> str:
        rel = f"figures/{name}.png"
        fig.savefig(self.out / rel, bbox_inches="tight", metadata={"Software": None})
        plt.close(fig)
        return rel

    def table(self, df: pd.DataFrame, name: str) -> str:
        rel = f"tables/{name}.csv"
        df.to_csv(self.out / rel, index=False, float_format="%.4g", lineterminator="\n")
        return rel

    def add(self, **item):
        self.items.append(item)


class Ctx:
    """Everything an analysis needs: the selected farms/fields and helpers bound to them."""

    def __init__(self, farms, geo, tr, a):
        self.farms, self.geo, self.tr, self.a = farms, geo, tr, a
        self.grower = farms[0].grower
        self.keep = set(geo.field_id)
        self.n = len(geo)
        self.order = geo.sort_values("label").field_id.tolist()
        self.label = geo.set_index("field_id").label.to_dict()
        self.acres = geo.set_index("field_id").acres.to_dict()
        self.width = a.headlands_width
        self.tag = f"{a.headlands_width:g}m"

    def rd(self, farm, key: str, **kw) -> pd.DataFrame:
        d = read_csv(farm.tables[key], self.tr, **kw)
        return d[d.field_id.astype(str).isin(self.keep)].copy() if "field_id" in d else d


def fmt_p(p: float) -> str:
    return "p < 0.001" if p < 0.001 else f"p = {p:.3f}"


def strength(rho: float) -> str:
    a = abs(rho)
    return "very weak" if a < 0.2 else "weak" if a < 0.4 else "moderate" if a < 0.6 else "strong"


def sign(x: float) -> str:
    return "negative" if x < 0 else "positive"


def lab_list(c: Ctx, fids, k: int = 3) -> str:
    return ", ".join(c.label[f] for f in list(fids)[:k])


# ----------------------------------------------------------------------------- boundaries
def boundaries(run: Run, c: Ctx):
    geo, tag, width = c.geo, c.tag, c.width
    rows = []
    for f in c.farms:
        if f"headlands_{tag}" not in f.tables:
            raise SystemExit(
                f"{f.farm}: missing headlands summary for {tag}. Run:\n  python "
                "my-farm-advisor/field-management/headlands-ring/src/headlands_farm.py "
                f"--farm-dir {f.path} --widths {width:g}")
        rows.append(c.rd(f, f"headlands_{tag}").assign(tag=tag))
    hs = pd.concat(rows)
    wide = hs.pivot_table(index="field_id", columns="tag",
                          values=["headlands_pct", "headlands_acres", "interior_acres"])
    wide.columns = [f"{a}_{b}" for a, b in wide.columns]
    dem = []
    for f in c.farms:
        if "dem" in f.tables:
            dm = c.rd(f, "dem")
            dem.append(pd.DataFrame({
                "field_id": dm.field_id.astype(str),
                "relief_m": (dm.elevation_max_m - dm.elevation_min_m).round(1),
                "slope_pct": dm.slope_percent_p50.round(2),
                "elev_m": ((dm.elevation_max_m + dm.elevation_min_m) / 2).round(0)}))
    d = geo.merge(wide.reset_index(), on="field_id")
    if dem:
        d = d.merge(pd.concat(dem), on="field_id", how="left")
    else:
        d[["relief_m", "slope_pct", "elev_m"]] = np.nan
    d = d.sort_values("label").reset_index(drop=True)
    hp = f"headlands_pct_{tag}"
    keep = ["label", "field_id", "county", "acres", "perimeter_ft", "compactness",
            f"interior_acres_{tag}", f"headlands_acres_{tag}", hp, "relief_m", "slope_pct", "elev_m"]
    t_all = run.table(d[keep], "boundaries_fields")

    # B1 interior vs headland acres per field
    fig, ax = plt.subplots(figsize=(7.5, 0.34 * len(d) + 1.4))
    y = np.arange(len(d))[::-1]
    ax.barh(y, d[f"interior_acres_{tag}"], color=ACCENT, label="Interior")
    ax.barh(y, d[f"headlands_acres_{tag}"], left=d[f"interior_acres_{tag}"], color="#F4A259",
            label=f"Headland ring ({width:g} m)")
    for yi, (_, r) in zip(y, d.iterrows()):
        ax.text(r.acres + d.acres.max() * 0.01, yi, f"{r.acres:.0f} ac · {r[hp]:.1f}% edge", va="center", fontsize=7.5)
    ax.set_yticks(y, d.label)
    ax.set_xlim(0, d.acres.max() * 1.28)
    ax.set_xlabel("Acres")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower right", fontsize=8)
    ax.set_title(f"Field size: interior vs {width:g} m headland ring")
    img = run.fig(fig, "boundaries_acres_interior_vs_headland")
    tot_hl = d[f"headlands_acres_{tag}"].sum()
    hi = d.loc[d[hp].idxmax()]
    lo = d.loc[d[hp].idxmin()]
    run.add(id="B1", category="Field boundaries", kind="chart", level="across fields",
            title="Field size: interior vs headland ring", image=img, table=t_all,
            shows=(f"Acres of each of the {len(d)} fields, split into the interior and a {width:g} m ring along "
                   f"the boundary. Fields range {d.acres.min():.0f}-{d.acres.max():.0f} ac "
                   f"({d.acres.sum():.0f} ac in total)."),
            takeaway=(f"{tot_hl:.0f} of {d.acres.sum():.0f} ac ({100 * tot_hl / d.acres.sum():.1f} %) lie within "
                      f"{width:g} m of an edge - the area most affected by turning, compaction, input overlap and "
                      f"mixed pixels in satellite data. {hi.label} has the most edge ({hi[hp]:.1f} %), "
                      f"{lo.label} the least ({lo[hp]:.1f} %)."),
            limits="Acres are computed from the reviewed boundary polygons (UTM), not from deeds or FSA records.")

    # B2 terrain
    if d.slope_pct.notna().any():
        dd = d.sort_values("slope_pct", ascending=False)
        fig, ax = plt.subplots(figsize=(7.5, 0.3 * len(dd) + 1.3))
        y = np.arange(len(dd))[::-1]
        ax.barh(y, dd.slope_pct, color="#8C6D46")
        for yi, (_, r) in zip(y, dd.iterrows()):
            ax.text(r.slope_pct + 0.03, yi, f"{r.slope_pct:.1f}%  ·  relief {r.relief_m:.1f} m  ·  ~{r.elev_m:.0f} m asl",
                    va="center", fontsize=7.5)
        ax.set_yticks(y, dd.label)
        ax.set_xlim(0, dd.slope_pct.max() * 1.9)
        ax.grid(axis="y", visible=False)
        ax.set_xlabel("Median slope (%)")
        ax.set_title("Terrain: median slope and relief of each field")
        img = run.fig(fig, "boundaries_terrain")
        steep = dd.iloc[0]
        flat = dd.iloc[-1]
        run.add(id="B2", category="Field boundaries", kind="chart", level="across fields",
                title="Terrain by field", image=img, table=t_all,
                shows=(f"Median slope and elevation range (max - min) inside each field, from the DEM. "
                       f"Slopes run {dd.slope_pct.min():.1f}-{dd.slope_pct.max():.1f} %."),
                takeaway=(f"{steep.label} is the steepest ({steep.slope_pct:.1f} % median slope, "
                          f"{steep.relief_m:.1f} m relief) and {flat.label} the flattest ({flat.slope_pct:.1f} %). "
                          "Steeper fields have more erosion and runoff risk and more uneven soil moisture; the flattest "
                          "are the easiest to irrigate evenly."),
                limits="Terrain from the pipeline DEM (coarse resolution); slope is a field median, so local "
                       "swales and ridges are averaged out.")
    return d


# ----------------------------------------------------------------------------- CDL
def cdl(run: Run, c: Ctx, bd: pd.DataFrame):
    geo, width = c.geo, c.width
    frames = []
    for f in c.farms:
        for k in sorted(k for k in f.tables if k.startswith("cdl_") and k[4:].isdigit()):
            frames.append(c.rd(f, k))
    if not frames:
        raise SystemExit("no <farm>_<year>_cdl.csv tables found; run the pipeline CDL step first")
    cd = pd.concat(frames)
    cd["field_id"] = cd.field_id.astype(str)
    cd["group"] = cd.crop_code.map(crop_group)
    years = sorted(int(y) for y in cd.year.unique())
    last = years[-1]
    order = c.order
    labels = [c.label[f] for f in order]

    # C1 rotation grid
    dom = (cd.sort_values(["field_id", "year", "pct", "crop_code"], ascending=[True, True, False, True])
           .groupby(["field_id", "year"]).first().reset_index())
    grid = dom.pivot(index="field_id", columns="year", values="group").reindex(order)
    pct = dom.pivot(index="field_id", columns="year", values="pct").reindex(order)
    fig, ax = plt.subplots(figsize=(1.0 * len(years) + 2.8, 0.3 * len(order) + 1.3))
    for i, fid in enumerate(order):
        for j, yr in enumerate(years):
            g = grid.loc[fid, yr]
            if pd.isna(g):
                continue
            ax.add_patch(plt.Rectangle((j, len(order) - 1 - i), 0.96, 0.92, color=GROUP_COLOR[g]))
            ax.text(j + 0.48, len(order) - 1 - i + 0.46, f"{pct.loc[fid, yr]:.0f}", ha="center", va="center",
                    fontsize=6.5, color="white" if g != "Fallow / idle" else "#333")
    ax.set_xlim(0, len(years))
    ax.set_ylim(0, len(order))
    ax.set_xticks(np.arange(len(years)) + 0.48, years)
    ax.set_yticks(np.arange(len(order)) + 0.46, labels[::-1])
    ax.grid(False)
    for s in ax.spines.values():
        s.set_visible(False)
    used = [n for n, _, _ in CROP_GROUPS if n in set(dom.group)]
    ax.legend(handles=[Patch(color=GROUP_COLOR[n], label=n) for n in used], loc="upper left",
              bbox_to_anchor=(1.01, 1), fontsize=8)
    ax.set_title("Dominant crop by field and year (number = % of field)")
    img = run.fig(fig, "cdl_rotation_grid")
    rot = []
    for fid, row in grid.iterrows():
        seq = [x for x in row.tolist() if isinstance(x, str)]
        alt = sum(1 for a, b in zip(seq, seq[1:]) if {a, b} == {"Corn", "Soybeans"})
        cc = sum(1 for a, b in zip(seq, seq[1:]) if a == b == "Corn")
        ss = sum(1 for a, b in zip(seq, seq[1:]) if a == b == "Soybeans")
        pattern = ("corn-soy rotation" if alt == len(seq) - 1 else
                   "continuous corn" if seq.count("Corn") == len(seq) else
                   "includes corn-on-corn" if cc else
                   "includes soy-on-soy" if ss else "mixed / other")
        nxt = ("Soybeans" if seq[-1] == "Corn" else "Corn" if seq[-1] == "Soybeans" else seq[-1]) \
            if pattern == "corn-soy rotation" else seq[-1]
        rot.append({"field_id": fid, "label": c.label[fid], "acres": c.acres[fid],
                    "sequence": " > ".join(s[0] if s in ("Corn", "Soybeans") else "O" for s in seq),
                    "corn_years": seq.count("Corn"), "soy_years": seq.count("Soybeans"),
                    "corn_soy_alternations": alt, "corn_after_corn": cc, "pattern": pattern,
                    f"crop_{last}": seq[-1], f"projected_{last + 1}": nxt,
                    "projection_basis": "rotation continues" if pattern == "corn-soy rotation"
                    else "no clean rotation: last crop repeated"})
    rt = pd.DataFrame(rot)
    t = run.table(rt, "cdl_rotation_by_field")
    pat = rt.pattern.value_counts()
    odd = rt[rt.pattern != "corn-soy rotation"]
    run.add(id="C1", category="CDL crop history", kind="chart", level="within field over time, across fields",
            title="Crop rotation by field and year", image=img, table=t,
            shows=(f"The dominant CDL crop in each field for {years[0]}-{years[-1]}; the number is the share of "
                   "the field in that crop."),
            takeaway=(f"{pat.get('corn-soy rotation', 0)} of {len(rt)} fields follow a strict corn-soy rotation"
                      + (f"; {lab_list(c, odd.field_id, 4)} {'does' if len(odd) == 1 else 'do'} not ("
                         + ", ".join(sorted(set(odd.pattern))) + "). Corn after corn typically yields less and needs "
                           "more nitrogen and rootworm management, so those fields deserve attention."
                         if len(odd) else ".")),
            limits="CDL is a satellite classification (~90-95 % accurate for corn and soy in the Corn Belt); "
                   "dominant crop is the class covering the largest share of the field.")

    # C2 corn / soy acres by year + projection
    cd["acres_in"] = cd.field_id.map(c.acres) * cd.pct / 100
    ay = cd.groupby(["year", "group"]).acres_in.sum().unstack(fill_value=0)
    proj = rt.groupby(f"projected_{last + 1}").acres.sum()
    ay.loc[last + 1] = 0.0
    for gname, ac in proj.items():
        ay.loc[last + 1, gname] = ac
    cols = [n for n, _, _ in CROP_GROUPS if n in ay.columns and ay[n].sum() > 0]
    ay = ay[cols]
    fig, ax = plt.subplots(figsize=(7.5, 3.8))
    bottom = np.zeros(len(ay))
    xl = [str(y) if y <= last else f"{y}\n(rotation\nprojection)" for y in ay.index]
    for col in cols:
        ax.bar(xl, ay[col], bottom=bottom, color=GROUP_COLOR[col], label=col,
               hatch=None)
        for xi, (v, b0) in enumerate(zip(ay[col], bottom)):
            if v >= ay.sum(axis=1).max() * 0.06:
                ax.text(xi, b0 + v / 2, f"{v:.0f}", ha="center", va="center", fontsize=7.5,
                        color="white" if col != "Fallow / idle" else "#333")
        bottom += ay[col].to_numpy()
    ax.axvline(len(ay) - 1.5, color="#999", ls=":", lw=1)
    ax.grid(axis="x", visible=False)
    ax.set_ylabel("Acres")
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=8)
    ax.set_title("Acres by crop each year and where the rotation points next")
    img = run.fig(fig, "cdl_acres_by_crop_year")
    at = ay.round(1).reset_index().rename(columns={"index": "year", "year": "year"})
    at["basis"] = ["CDL" if y <= last else "rotation projection" for y in ay.index]
    t = run.table(at, "cdl_acres_by_crop_year")
    corn_l, soy_l = ay.loc[last].get("Corn", 0), ay.loc[last].get("Soybeans", 0)
    corn_n, soy_n = ay.loc[last + 1].get("Corn", 0), ay.loc[last + 1].get("Soybeans", 0)
    run.add(id="C2", category="CDL crop history", kind="chart", level="across fields, over time",
            title="Corn and soybean acres by year, with next-season projection", image=img, table=t,
            shows=(f"Acres in each crop class per year, field acres x CDL share, {years[0]}-{last}. The last bar "
                   f"projects {last + 1}: fields in a clean corn-soy rotation switch crop, the others repeat "
                   f"their {last} crop."),
            takeaway=(f"In {last} the grower had {corn_l:.0f} ac corn and {soy_l:.0f} ac soybeans. The rotation "
                      f"implies about {corn_n:.0f} ac corn and {soy_n:.0f} ac soybeans in {last + 1}"
                      + (" - a lopsided year for input, storage and marketing planning." if abs(corn_n - soy_n) > 0.2 * (corn_n + soy_n)
                         else " - a balanced year.")),
            limits="The projection is a rule (switch corn/soy), not a forecast of the grower's plans or the market; "
                   "CDL acres use the field's boundary area times the class share.")

    # C3 edge vs interior purity (paired)
    zf = []
    for f in c.farms:
        if f"cdl_zone_{c.tag}" in f.tables:
            zf.append(c.rd(f, f"cdl_zone_{c.tag}"))
    if not zf:
        raise SystemExit(f"no *_cdl_by_zone_{c.tag}.csv; run headlands_farm.py with --widths {width:g}")
    z = pd.concat(zf)
    z["cs"] = z.crop_code.isin([1, 5, 12, 13])
    a = z.groupby(["field_id", "zone", "cs"]).area_acres.sum().unstack(fill_value=0)
    pur = (100 * a[True] / a.sum(axis=1)).unstack().reindex(order)[["interior", "headlands"]].dropna()
    pur["diff"] = pur.interior - pur.headlands
    try:
        w = stats.wilcoxon(pur.interior, pur.headlands)
        wp, wst = float(w.pvalue), float(w.statistic)
    except ValueError:
        wp, wst = float("nan"), float("nan")
    pt = pur.round(2).reset_index()
    pt["label"] = pt.field_id.map(c.label)
    pt = pt[["label", "field_id", "interior", "headlands", "diff"]].rename(
        columns={"interior": "interior_corn_soy_pct", "headlands": "headland_corn_soy_pct",
                 "diff": "difference_pct_points"})
    t = run.table(pt, "cdl_edge_vs_interior_purity")
    fig, ax = plt.subplots(figsize=(7.5, 0.3 * len(pt) + 1.6))
    ps = pt.sort_values("difference_pct_points")
    y = np.arange(len(ps))
    ax.hlines(y, ps.headland_corn_soy_pct, ps.interior_corn_soy_pct, color="#bbb", lw=2)
    ax.scatter(ps.interior_corn_soy_pct, y, color=ACCENT, s=36, zorder=3, label="Interior")
    ax.scatter(ps.headland_corn_soy_pct, y, color="#F4A259", s=36, zorder=3, label=f"{width:g} m edge ring")
    ax.set_yticks(y, ps.label)
    ax.set_xlabel("Corn + soybean share of CDL area, all years (%)")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower left", fontsize=8)
    ax.set_title(f"Crop purity by field: {width:g} m edge ring vs interior")
    img = run.fig(fig, "cdl_edge_vs_interior_purity")
    pst = (f"Wilcoxon signed-rank, n = {len(pur)}, {fmt_p(wp)}" if not np.isnan(wp) else f"n = {len(pur)}")
    worst = pt.sort_values("difference_pct_points", ascending=False).iloc[0]
    run.add(id="C3", category="CDL crop history", kind="comparison", level="within field (edge vs interior)",
            title="Edge vs interior crop purity (paired Wilcoxon test)", image=img, table=t,
            shows=(f"For each field, the corn + soybean share of CDL area in the {width:g} m ring and in the "
                   "interior (all years, area-weighted by exact pixel overlap). Each line is one field."),
            takeaway=(f"The interior is a median {pur['diff'].median():.1f} percentage points purer than the edge "
                      f"(interior median {pur.interior.median():.1f} % vs ring {pur.headlands.median():.1f} %; {pst}). "
                      f"The largest gap is {worst.label} ({worst.difference_pct_points:.1f} points). Field averages that "
                      "include the edge understate how uniformly the field is cropped; excluding the ring gives cleaner "
                      "crop, NDVI and yield statistics."),
            stats={"wilcoxon_statistic": wst, "p_value": wp, "n": int(len(pur))},
            limits="Edge pixels mix field with road and grass by construction (30 m pixels), so part of the "
                   "difference is a pixel effect rather than different crops at the edge.")

    # C4 non-crop area inside the boundary
    nc = cd[~cd.group.isin(CROPLAND)].groupby(["field_id", "group"]).acres_in.sum().unstack(fill_value=0) / len(years)
    nc = nc.reindex(order).fillna(0)
    nc["total"] = nc.sum(axis=1)
    nc = nc.sort_values("total", ascending=False)
    ncg = [g for g in [n for n, _, _ in CROP_GROUPS] if g in nc.columns and g != "total" and nc[g].sum() > 0]
    fig, ax = plt.subplots(figsize=(7.5, 0.3 * len(nc) + 1.5))
    y = np.arange(len(nc))[::-1]
    left = np.zeros(len(nc))
    for g in ncg:
        ax.barh(y, nc[g], left=left, color=GROUP_COLOR[g], label=g)
        left += nc[g].to_numpy()
    for yi, (fid, r) in zip(y, nc.iterrows()):
        ax.text(r.total + nc.total.max() * 0.01, yi, f"{r.total:.1f} ac ({100 * r.total / c.acres[fid]:.1f}%)",
                va="center", fontsize=7.5)
    ax.set_yticks(y, [c.label[f] for f in nc.index])
    ax.set_xlim(0, nc.total.max() * 1.3)
    ax.set_xlabel(f"Average acres per year not in a crop class ({years[0]}-{last})")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower right", fontsize=8)
    ax.set_title("Non-crop area inside each field boundary")
    img = run.fig(fig, "cdl_noncrop_area")
    nt = nc.round(2).reset_index()
    nt["label"] = nt.field_id.map(c.label)
    nt["pct_of_field"] = (100 * nt.total / nt.field_id.map(c.acres)).round(2)
    nt = nt[["label", "field_id"] + ncg + ["total", "pct_of_field"]]
    t = run.table(nt, "cdl_noncrop_area")
    top = nc.index[0]
    top_cls = nc.loc[top, ncg].idxmax() if ncg else "non-crop"
    run.add(id="C4", category="CDL crop history", kind="chart", level="across fields",
            title="Non-crop area inside the boundaries", image=img, table=t,
            shows="Average yearly acres inside each boundary classified as something other than a crop, by class.",
            takeaway=(f"{nc.total.sum():.0f} ac in total sit inside the boundaries as non-crop "
                      f"({100 * nc.total.sum() / sum(c.acres.values()):.1f} % of the area); {c.label[top]} has the most "
                      f"({nc.loc[top, 'total']:.1f} ac, mostly {top_cls.lower()}). Check these on imagery: roads, "
                      "farmsteads and tree lines inside a boundary inflate planted and input acres."),
            limits="30 m CDL pixels along edges can be mixed, so small amounts of non-crop area are partly an "
                   "artefact of resolution.")

    # B3 (boundaries x CDL): field size vs crop diversity
    mp = c.a.diversity_min_pct
    cdv = cd[cd.pct >= mp].groupby(["field_id", "year"]).crop_code.nunique().rename("classes").reset_index()
    dv = cdv.groupby("field_id").classes.agg(mean_classes="mean", min_classes="min", max_classes="max").reset_index()
    dv["label"] = dv.field_id.map(c.label)
    dv["acres"] = dv.field_id.map(c.acres)
    dv = dv.sort_values("acres").reset_index(drop=True)
    cdv["acres"] = cdv.field_id.map(c.acres)

    def sp(x, y):
        if len(x) < 3 or x.nunique() < 2 or y.nunique() < 2:
            return float("nan"), float("nan")
        r_, p_ = stats.spearmanr(x, y)
        return float(r_), float(p_)

    rf, pf = sp(dv.acres, dv.mean_classes)
    ry, py_ = sp(cdv.acres, cdv.classes)
    st = pd.DataFrame([
        {"level": "field (mean over years)", "n": len(dv), "spearman_rho": round(rf, 3), "p_value": round(pf, 4)},
        {"level": "field-year (pooled)", "n": len(cdv), "spearman_rho": round(ry, 3), "p_value": round(py_, 4)}])
    t_sum = run.table(st, "boundaries_size_vs_diversity_correlation")
    dvt = dv[["label", "field_id", "acres", "mean_classes", "min_classes", "max_classes"]].round(2)
    t = run.table(dvt, "boundaries_size_vs_diversity")
    fig, ax = plt.subplots(figsize=(7.5, 4.0))
    ax.scatter(dv.acres, dv.mean_classes, color=ACCENT, s=40, zorder=3)
    for _, r in dv.iterrows():
        ax.annotate(r.label.split("-")[-1], (r.acres, r.mean_classes), xytext=(4, 3), textcoords="offset points", fontsize=7)
    ax.set_xlabel("Field size (acres)")
    ax.set_ylabel(f"Distinct CDL classes per year (>= {mp:g} % of field), mean")
    ax.set_ylim(bottom=max(0, dv.mean_classes.min() - 0.5))
    if not np.isnan(rf):
        ax.text(0.03, 0.95, f"Spearman rho = {rf:.2f} ({fmt_p(pf)}), n = {len(dv)} fields", transform=ax.transAxes,
                va="top", fontsize=9)
    ax.set_title("Field size vs crop diversity")
    img = run.fig(fig, "boundaries_size_vs_diversity")
    if np.isnan(rf):
        take = ("Crop diversity does not vary enough between fields (or there are too few fields) to compute a "
                "correlation.")
    elif pf < 0.05:
        take = (f"Larger fields contain {'more' if rf > 0 else 'fewer'} distinct CDL classes: a {strength(rf)} "
                f"{sign(rf)} rank correlation across fields (rho = {rf:.2f}, {fmt_p(pf)}, n = {len(dv)}); the pooled "
                f"field-year value is rho = {ry:.2f}. Fields range from {dv.mean_classes.min():.1f} to "
                f"{dv.mean_classes.max():.1f} classes per year. "
                + ("More classes usually means more roads, grass, trees or buffer strips inside the boundary, not more "
                   "crops - see the non-crop chart (C4)." if rf > 0 else
                   "Smaller fields carry proportionally more edge, where non-crop classes sit."))
    else:
        take = (f"No clear relationship between field size and crop diversity: rho = {rf:.2f} ({strength(rf)} "
                f"{sign(rf)}, {fmt_p(pf)}, n = {len(dv)}; pooled field-year rho = {ry:.2f}). Fields range from "
                f"{dv.mean_classes.min():.1f} to {dv.mean_classes.max():.1f} distinct classes per year, so how many "
                "classes a boundary catches is not explained by its size here - look at the non-crop chart (C4) and "
                "the edge/interior comparison (C3) for what is inside the outline.")
    run.add(id="B3", category="Field boundaries", kind="comparison", level="across fields (boundary x CDL)",
            title="Field size vs crop diversity (Spearman correlation)", image=img, table=t, summary=t_sum,
            shows=(f"For each field, the average number of distinct CDL classes covering at least {mp:g} % of it per "
                   f"year ({years[0]}-{last}), against field size."),
            takeaway=take,
            stats={"spearman_rho_field": rf, "p_value_field": pf, "spearman_rho_field_year": ry,
                   "p_value_field_year": py_, "min_class_share_pct": mp},
            limits=(f"Only {len(dv)} fields, so the test has limited power. Field-year values are not independent "
                    "(same field across years), so the field-level row is the primary result. Larger fields have more "
                    "30 m pixels and so more chances to catch a rare class; classes below the threshold are ignored."))
    return dom, rt


# ----------------------------------------------------------------------------- weather
def eta2(df: pd.DataFrame, value: str, by: str) -> float:
    tot = ((df[value] - df[value].mean()) ** 2).sum()
    if tot == 0 or df[by].nunique() < 2:
        return 0.0
    ss = df.groupby(by)[value].agg(["mean", "size"])
    return float((ss["size"] * (ss["mean"] - df[value].mean()) ** 2).sum() / tot)


def weather(run: Run, c: Ctx, dom: pd.DataFrame):
    a = c.a
    season = tuple(tuple(int(x) for x in s.split("-")) for s in a.season)
    corn_m, soy_m, heat_f = a.corn_month, a.soy_month, a.heat_f
    frames, cellrows, fcell = [], [], {}
    for f in c.farms:
        if "weather" not in f.tables:
            raise SystemExit(f"{f.farm}: no weather table; run the pipeline weather step")
        w = c.rd(f, "weather")
        elev = {}
        if "dem" in f.tables:
            dm = c.rd(f, "dem")
            if {"elevation_min_m", "elevation_max_m"} <= set(dm.columns):
                elev = dict(zip(dm.field_id.astype(str), (dm.elevation_min_m + dm.elevation_max_m) / 2))
        w["field_id"] = w.field_id.astype(str)
        sid = {fid: agro.series_id(g) for fid, g in w.groupby("field_id")}
        rep = {}
        for fid in sorted(sid):
            rep.setdefault(sid[fid], fid)
        cell_name = {s: f"cell-{i}" for i, s in enumerate(
            sorted(rep, key=lambda s: (-sum(1 for v in sid.values() if v == s), rep[s])), 1)}
        for fid, s in sid.items():
            fcell[fid] = cell_name[s]
        for s, fid in rep.items():
            cellrows.append({"cell": cell_name[s], "fields": sum(1 for v in sid.values() if v == s),
                             "lat": round(float(w[w.field_id == fid].lat.iloc[0]), 3),
                             "lon": round(float(w[w.field_id == fid].lon.iloc[0]), 3)})
        wr = w[w.field_id.isin(rep.values())]
        frames.append(agro.daily_metrics(wr, elev).assign(cell=wr.field_id.map(fcell)))
    cells = pd.DataFrame(cellrows).sort_values("cell").reset_index(drop=True)
    run.table(cells, "weather_cells")
    wt = cells.set_index("cell").fields
    fc = pd.DataFrame({"field_id": c.order, "label": [c.label[f] for f in c.order],
                       "weather_cell": [fcell[f] for f in c.order]})
    run.table(fc, "weather_field_cells")
    d = pd.concat(frames)
    d["in_season"] = agro.in_season(d.date, season[0], season[1])
    d["month"] = d.date.dt.month
    d["heat"] = (d.tmax_f > heat_f).astype(float)
    years = sorted(int(y) for y in d.year.unique())
    s_lab = f"{season[0][0]:02d}/{season[0][1]:02d}-{season[1][0]:02d}/{season[1][1]:02d}"
    ncell = len(cells)
    cmap = plt.get_cmap("viridis")(np.linspace(0.05, 0.9, len(years)))

    def wavg(df, cols, by):
        """Field-weighted mean of per-cell values (each cell counted by how many fields it holds)."""
        x = df.assign(_w=df.cell.map(wt))
        return x.groupby(by).apply(lambda g: pd.Series({k: np.average(g[k], weights=g._w) for k in cols}),
                                   include_groups=False)

    # season totals per cell-year
    se = d[d.in_season].groupby(["cell", "year"]).agg(
        gdd=("gdd_f", "sum"), rain=("rain_in", "sum"), et0=("et0_in", "sum")).reset_index()
    se["deficit"] = se.et0 - se.rain
    cm = d.groupby(["cell", "year", "month"]).agg(rain=("rain_in", "sum"), heat_days=("heat", "sum")).reset_index()

    # W1 cumulative GDD, field-weighted over cells
    sd = d[d.in_season].copy()
    day = wavg(sd, ["gdd_f"], ["year", "date"]).reset_index().sort_values(["year", "date"])
    day["cum"] = day.groupby("year").gdd_f.cumsum()
    day["dos"] = day.groupby("year").cumcount() + 1
    fig, ax = plt.subplots(figsize=(7.5, 3.8))
    for yi, yr in enumerate(years):
        s = day[day.year == yr]
        ax.plot(s.dos, s.cum, color=cmap[yi], lw=1.5, label=str(yr))
    avg = day.groupby("dos").cum.mean()
    ax.plot(avg.index, avg.values, color="black", lw=1.6, ls="--", label=f"{len(years)}-yr mean")
    ax.set_xlabel(f"Days since {season[0][0]}/{season[0][1]}")
    ax.set_ylabel("Cumulative GDD (deg F, corn method)")
    ax.legend(fontsize=8, ncol=2, loc="upper left")
    ax.set_title(f"Growing degree days accumulated over the season ({s_lab})")
    img = run.fig(fig, "weather_gdd_cumulative")
    tot = day.groupby("year").gdd_f.sum()
    gt = pd.DataFrame({"year": tot.index, "season_gdd": tot.round(0).values})
    gt["vs_mean"] = (gt.season_gdd - gt.season_gdd.mean()).round(0)
    t = run.table(gt, "weather_season_gdd")
    swing = float(tot.max() - tot.min())
    run.add(id="W1", category="Weather", kind="chart", level="across years (within the farm)",
            title="Season growing degree days", image=img, table=t,
            shows=(f"Corn-method GDD (base 50 F, capped at 86 F) accumulated from {s_lab}, one line per year; the "
                   f"dashed line is the {len(years)}-year mean. Season totals range {tot.min():.0f}-{tot.max():.0f}."),
            takeaway=(f"The warmest season ({int(tot.idxmax())}) had {swing:.0f} more GDD than the coolest "
                      f"({int(tot.idxmin())}). At the common 20-25 GDD per day of hybrid relative maturity, that is "
                      f"roughly {swing / 25:.0f}-{swing / 20:.0f} RM days, so hybrid choice and harvest timing should be "
                      "judged against this range, not a single year."),
            limits=(f"Planting dates are unknown, so a fixed {s_lab} window is used. NASA POWER is a ~50 km grid: this "
                    f"farm's fields fall in {ncell} distinct weather cell{'s' if ncell != 1 else ''} "
                    f"(weighted by field count)."))

    # W2 weather in each field's crop-critical month
    dm_ = dom[["field_id", "year", "group"]].copy()
    dm_["month"] = dm_.group.map({"Corn": corn_m, "Soybeans": soy_m})
    dm_["cell"] = dm_.field_id.map(fcell)
    cw = dm_.merge(cm, on=["cell", "year", "month"], how="left")
    cw["label"] = cw.field_id.map(c.label)
    cw["crop"] = cw.group.map(lambda g: {"Corn": "C", "Soybeans": "S"}.get(g, "-"))
    order = c.order
    rain_g = cw.pivot(index="field_id", columns="year", values="rain").reindex(order)
    heat_g = cw.pivot(index="field_id", columns="year", values="heat_days").reindex(order)
    crop_g = cw.pivot(index="field_id", columns="year", values="crop").reindex(order)
    mn = lambda m: pd.Timestamp(2000, m, 1).strftime("%B")
    fig, axes = plt.subplots(1, 2, figsize=(1.15 * len(years) * 2 + 1.5, 0.3 * len(order) + 1.8))
    for ax, grid, cm_, ttl, fmt in ((axes[0], rain_g, "YlGnBu", f"Rain in the critical month (in)", "{:.1f}"),
                                    (axes[1], heat_g, "OrRd", f"Days above {heat_f:g} F in the critical month", "{:.0f}")):
        v = grid.to_numpy(dtype=float)
        im = ax.imshow(np.ma.masked_invalid(v), cmap=cm_, aspect="auto")
        for i in range(v.shape[0]):
            for j in range(v.shape[1]):
                if not np.isnan(v[i, j]):
                    ax.text(j, i, f"{crop_g.iloc[i, j]} " + fmt.format(v[i, j]), ha="center", va="center", fontsize=6.5,
                            color="white" if (v[i, j] - np.nanmin(v)) / (np.nanmax(v) - np.nanmin(v) + 1e-9) > 0.6 else "#222")
        ax.set_xticks(range(len(years)), years)
        ax.set_yticks(range(len(order)), [c.label[f] for f in order])
        ax.set_title(ttl, fontsize=10)
        ax.grid(False)
        for s in ax.spines.values():
            s.set_visible(False)
    fig.suptitle(f"Weather when it matters for each year's crop (C = corn in {mn(corn_m)}, S = soybeans in {mn(soy_m)})",
                 fontweight="bold", y=1.02)
    img = run.fig(fig, "weather_critical_window")
    ct = cw[["label", "field_id", "year", "group", "month", "cell", "rain", "heat_days"]].rename(
        columns={"group": "crop", "month": "critical_month", "rain": "rain_in", "heat_days": "days_above_threshold"})
    ct = ct.sort_values(["label", "year"]).round(2)
    t = run.table(ct, "weather_critical_window")
    cc = cw[cw.crop == "C"]
    ss = cw[cw.crop == "S"]
    dry = cc.loc[cc.rain.idxmin()] if len(cc) else None
    hot = cw.loc[cw.heat_days.idxmax()] if cw.heat_days.notna().any() else None
    run.add(id="W2", category="Weather", kind="chart", level="field-year (crop-aware)",
            title="Weather in each field's critical month", image=img, table=t,
            shows=(f"Each cell is one field-year. If the field grew corn that year the value is for {mn(corn_m)} "
                   f"(pollination), if soybeans for {mn(soy_m)} (pod fill); left: rainfall, right: days above "
                   f"{heat_f:g} F. Fields in the same weather cell share values, so differences within a year come from "
                   "the crop grown and, if there are several cells, from location."),
            takeaway=((f"The driest corn pollination month was {c.label[dry.field_id]} {int(dry.year)} "
                       f"({dry.rain:.1f} in); " if dry is not None else "")
                      + (f"the hottest critical month was {c.label[hot.field_id]} {int(hot.year)} "
                         f"({hot.heat_days:.0f} days > {heat_f:g} F, {hot.group.lower()}). " if hot is not None else "")
                      + "Dry, hot weather in the critical month is when yield is most at risk and irrigation pays most."),
            limits="NASA POWER rainfall is a modelled grid average and misses local thunderstorms; treat single values "
                   "as indicative. The crop is the CDL dominant class, and the critical months are fixed rules of thumb.")

    # W3 comparison: year-to-year vs between-field(cell) variation
    jm = cm[cm.month == corn_m].groupby(["cell", "year"]).rain.sum().reset_index().rename(columns={"rain": "corn_month_rain"})
    v = se.merge(jm, on=["cell", "year"])
    rows = []
    for col, lab in (("rain", "season rain"), ("gdd", "season GDD"), ("deficit", "season water deficit"),
                     ("corn_month_rain", f"{mn(corn_m)} rain")):
        rows.append({"variable": lab, "share_explained_by_year_pct": round(100 * eta2(v, col, "year"), 1),
                     "share_explained_by_weather_cell_pct": round(100 * eta2(v, col, "cell"), 1),
                     "n_cell_years": len(v)})
    et = pd.DataFrame(rows)
    kw = None
    if ncell > 1 and v.groupby("year").size().min() >= 2:
        kw = stats.kruskal(*[g.rain.to_numpy() for _, g in v.groupby("year")])
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    y = np.arange(len(et))[::-1]
    ax.barh(y + 0.19, et.share_explained_by_year_pct, 0.36, color=ACCENT, label="between years")
    ax.barh(y - 0.19, et.share_explained_by_weather_cell_pct, 0.36, color="#F4A259", label="between weather cells (fields)")
    for yi, (_, r) in zip(y, et.iterrows()):
        ax.text(r.share_explained_by_year_pct + 1, yi + 0.19, f"{r.share_explained_by_year_pct:.0f}%", va="center", fontsize=7.5)
        ax.text(r.share_explained_by_weather_cell_pct + 1, yi - 0.19, f"{r.share_explained_by_weather_cell_pct:.0f}%", va="center", fontsize=7.5)
    ax.set_yticks(y, et.variable)
    ax.set_xlim(0, 105)
    ax.set_xlabel("Share of variation explained (eta-squared, %)")
    ax.grid(axis="y", visible=False)
    ax.legend(fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=2)
    ax.set_title("Is weather different between years or between this farm's fields?")
    img = run.fig(fig, "weather_year_vs_field_variance")
    t = run.table(et, "weather_variance_decomposition")
    yr_r = et.iloc[0]
    run.add(id="W3", category="Weather", kind="comparison", level="across years vs across fields",
            title="Year-to-year vs field-to-field weather variation", image=img, table=t,
            shows=(f"For {len(v)} cell-years ({ncell} weather cell{'s' if ncell != 1 else ''} x {len(years)} years), how "
                   "much of the variation in each weather measure is explained by the year and how much by which "
                   "weather cell (i.e. which of the farm's fields) it is."),
            takeaway=(f"Year explains {yr_r.share_explained_by_year_pct:.0f} % of season rainfall variation, location "
                      f"within the farm {yr_r.share_explained_by_weather_cell_pct:.0f} %"
                      + (f" (Kruskal-Wallis of season rain across years {fmt_p(float(kw.pvalue))})" if kw else "")
                      + ". When fields of this farm differ in a given year, weather is rarely the reason - "
                        "management, soil, hybrid or crop are more likely; compare fields on those instead."
                      if yr_r.share_explained_by_year_pct > yr_r.share_explained_by_weather_cell_pct else
                      f"Year explains {yr_r.share_explained_by_year_pct:.0f} % of season rainfall variation but location "
                      f"within the farm {yr_r.share_explained_by_weather_cell_pct:.0f} %: this farm's fields do see "
                      "different rain, so weather can be part of between-field differences."),
            stats={"kruskal_H": float(kw.statistic), "p_value": float(kw.pvalue)} if kw else {},
            limits=(f"Only {len(years)} years and {ncell} weather cell{'s' if ncell != 1 else ''} (NASA POWER ~50 km "
                    "grid), so shares are descriptive, not tested; with one cell no between-field variation can show."))

    # W4 season water balance
    sw = se.assign(w=se.cell.map(wt)).groupby("year").apply(
        lambda g: pd.Series({"rain_in": np.average(g.rain, weights=g.w), "et0_in": np.average(g.et0, weights=g.w),
                             "deficit_in": np.average(g.deficit, weights=g.w), "deficit_min_in": g.deficit.min(),
                             "deficit_max_in": g.deficit.max()}), include_groups=False).reset_index()
    fig, ax = plt.subplots(figsize=(7.5, 3.6))
    x = np.arange(len(sw))
    ax.bar(x - 0.2, sw.rain_in, 0.38, color="#5DA5DA", label="Rain")
    ax.bar(x + 0.2, sw.et0_in, 0.38, color="#E69F00", label="Crop-water demand (reference ET)")
    for xi, (_, r) in zip(x, sw.iterrows()):
        ax.text(xi, max(r.rain_in, r.et0_in) + 0.4, f"deficit {r.deficit_in:+.1f}", ha="center", fontsize=8)
    ax.set_xticks(x, sw.year.astype(int))
    ax.set_ylabel("Inches over the season")
    ax.grid(axis="x", visible=False)
    ax.legend(fontsize=8, loc="lower right")
    ax.set_title(f"Season water balance ({s_lab}): demand vs rainfall")
    img = run.fig(fig, "weather_season_water_balance")
    t = run.table(sw.round(2), "weather_season_water_balance")
    wet, dry_y = sw.loc[sw.deficit_in.idxmin()], sw.loc[sw.deficit_in.idxmax()]
    run.add(id="W4", category="Weather", kind="chart", level="across years (within the farm)",
            title="Season water balance", image=img, table=t,
            shows=("Season rainfall against reference evapotranspiration (FAO-56 Penman-Monteith from NASA POWER "
                   "temperature, radiation, humidity and wind). The label is demand minus rain."),
            takeaway=(f"Demand exceeded rain in {int((sw.deficit_in > 0).sum())} of {len(sw)} seasons (mean "
                      f"{sw.deficit_in.mean():+.1f} in); {int(dry_y.year)} was the driest ({dry_y.deficit_in:+.1f} in) and "
                      f"{int(wet.year)} the wettest ({wet.deficit_in:+.1f} in). A persistent deficit is the agronomic "
                      "case for irrigation and its year-to-year range shows how variable water needs are."),
            limits=("Reference ET is for a grass surface; corn at full canopy uses ~1.1-1.2x this in July-August and "
                    "less early in the season. Rainfall is not all effective (runoff, deep drainage); only "
                    f"{len(years)} years."))
    return se, cm, fcell


# ----------------------------------------------------------------------------- map
def spatial_map(run: Run, c: Ctx, dom, bd, admin_dir: Path | None):
    geo, tag, width = c.geo, c.tag, c.width
    latest = int(dom.year.max())
    crop_now = dom[dom.year == latest].set_index("field_id").group
    n = len(c.farms)
    hp = f"headlands_pct_{tag}"
    hpct = bd.set_index("field_id")[hp]
    vmin, vmax = float(hpct.min()), float(hpct.max())
    fig = plt.figure(figsize=(3.2 + 4.6 * 2 * n, 4.6))
    gs = fig.add_gridspec(1, 1 + 2 * n, width_ratios=[0.9] + [1] * (2 * n))
    ax0 = fig.add_subplot(gs[0, 0])
    gp = geo.groupby("grower")
    pts = gpd.GeoDataFrame({"grower": list(gp.groups)}, geometry=gpd.points_from_xy(gp.lon.mean(), gp.lat.mean()),
                           crs="EPSG:4326")
    states = None
    if admin_dir is not None:
        sp = admin_dir / "l1_states" / "states_usa.parquet"
        if sp.exists():
            states = gpd.read_parquet(c.tr.add(sp))
    if states is not None:
        crs = "EPSG:5070"
        pts_p = pts.to_crs(crs)
        b = pts_p.total_bounds
        pad = max(b[2] - b[0], b[3] - b[1]) * 0.35 + 250_000
        st = states.to_crs(crs).cx[b[0] - pad:b[2] + pad, b[1] - pad:b[3] + pad]
        st.plot(ax=ax0, color="#f3f3f3", edgecolor="#9a9a9a", lw=0.6)
        for _, r in st.iterrows():
            q = r.geometry.representative_point()
            if b[0] - pad < q.x < b[2] + pad and b[1] - pad < q.y < b[3] + pad:
                ax0.text(q.x, q.y, r.state_code, fontsize=7, color="#888", ha="center")
        ax0.set_xlim(b[0] - pad, b[2] + pad)
        ax0.set_ylim(b[1] - pad, b[3] + pad)
    else:
        pts_p = pts
    ax0.scatter(pts_p.geometry.x, pts_p.geometry.y, s=70, color=ACCENT, edgecolor="black", zorder=3)
    ax0.set_axis_off()
    ax0.set_title("Farm location")
    counties = None
    if admin_dir is not None:
        cp = admin_dir / "l2_counties" / "counties_usa.parquet"
        if cp.exists() and geo.county_fips.str.len().eq(5).any():
            counties = gpd.read_parquet(c.tr.add(cp))
    sm = None
    for i, f in enumerate(c.farms):
        utm = f.fields.estimate_utm_crs()
        fl = f.fields.to_crs(utm)
        fl["grp"] = fl.field_id.map(crop_now).fillna("Barren / other")
        fl["hp"] = fl.field_id.map(hpct)
        x0, y0, x1, y1 = fl.total_bounds
        span = max(x1 - x0, y1 - y0, 3000) * 1.25
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        for k in range(2):
            ax = fig.add_subplot(gs[0, 1 + 2 * i + k])
            if counties is not None:
                fips = set(geo[geo.farm == f.farm].county_fips)
                cz = counties.to_crs(utm).cx[cx - span:cx + span, cy - span:cy + span]
                cz.boundary.plot(ax=ax, color="#b5b5b5", lw=0.7, zorder=1)
                for _, r in cz.iterrows():
                    if r.fips in fips:
                        gpd.GeoSeries([r.geometry], crs=utm).plot(ax=ax, color="#f6f6f2", edgecolor="#8a8a8a", lw=0.9, zorder=0)
            hk = f"headlands_geom_{tag}"
            cen = fl.geometry.representative_point()
            if k == 0:
                fl.plot(ax=ax, color=[GROUP_COLOR[g] for g in fl.grp], edgecolor="#1b1b1b", lw=0.4, zorder=2)
                col = [GROUP_COLOR[g] for g in fl.grp]
                ax.set_title(f"Dominant crop {latest}", fontsize=10)
            else:
                cm_ = plt.get_cmap("YlOrRd")
                col = [cm_((h - vmin) / (vmax - vmin + 1e-9)) for h in fl.hp]
                fl.plot(ax=ax, color=col, edgecolor="#1b1b1b", lw=0.4, zorder=2)
                sm = plt.cm.ScalarMappable(cmap=cm_, norm=plt.Normalize(vmin, vmax))
                ax.set_title(f"Edge share ({width:g} m ring, %)", fontsize=10)
            if hk in f.tables and k == 0:
                hz = gpd.read_file(c.tr.add(f.tables[hk])).to_crs(utm)
                hz = hz[hz.field_id.astype(str).isin(c.keep)]
                hz[hz.zone == "headlands"].plot(ax=ax, facecolor="none", edgecolor="white", lw=0.2, hatch="////", zorder=3)
            ax.scatter(cen.x, cen.y, s=80, c=col, edgecolors="#1b1b1b", linewidths=0.7, marker="s", zorder=4)
            for (_, r), q in zip(fl.iterrows(), cen):
                ax.annotate(r.label.split("-")[-1], (q.x, q.y), xytext=(6, 4), textcoords="offset points", fontsize=7,
                            zorder=5, bbox=dict(boxstyle="round,pad=0.12", fc="white", ec="none", alpha=0.8))
            ax.set_xlim(cx - span / 2, cx + span / 2)
            ax.set_ylim(cy - span / 2, cy + span / 2)
            bar = 10 ** np.floor(np.log10(span / 4))
            bar = bar * (2 if span / 4 >= 2 * bar else 1)
            bx, by = cx - span / 2 + span * 0.05, cy - span / 2 + span * 0.05
            ax.plot([bx, bx + bar], [by, by], color="black", lw=2)
            ax.text(bx + bar / 2, by + span * 0.015, f"{bar / 1000:g} km" if bar >= 1000 else f"{bar:g} m", ha="center", fontsize=7)
            ax.set_axis_off()
            if k == 1 and sm is not None:
                cb = fig.colorbar(sm, ax=ax, fraction=0.04, pad=0.01, shrink=0.7)
                cb.ax.tick_params(labelsize=7)
    used = [nm for nm, _, _ in CROP_GROUPS if nm in set(crop_now)]
    hand = [Patch(color=GROUP_COLOR[nm], label=f"{nm} ({latest})") for nm in used]
    if counties is not None:
        hand.append(Patch(facecolor="#f6f6f2", edgecolor="#8a8a8a", label="Study county"))
    fig.legend(handles=hand, loc="lower center", ncol=len(hand), fontsize=8, bbox_to_anchor=(0.5, -0.04))
    cn = sorted(set(geo.county) - {""})
    fig.suptitle(f"{c.grower}: {len(geo)} fields" + (f" in {', '.join(cn)} Co." if cn else ""), fontweight="bold")
    img = run.fig(fig, "map_fields")

    # vector layers for interactive viewers
    (run.out / "map").mkdir(exist_ok=True)
    props = bd.set_index("field_id")
    feats, rings = [], []
    for f in c.farms:
        hk = f"headlands_geom_{tag}"
        hz = gpd.read_file(f.tables[hk]) if hk in f.tables else None
        for _, r in f.fields.iterrows():
            p = props.loc[r.field_id]
            feats.append({"type": "Feature",
                          "geometry": json.loads(gpd.GeoSeries([r.geometry]).set_precision(1e-6).to_json())["features"][0]["geometry"],
                          "properties": {"field_id": r.field_id, "label": r.label, "grower": c.grower, "county": p.county,
                                         "acres": float(p.acres), "crop": str(crop_now.get(r.field_id, "")),
                                         "crop_year": latest, "color": GROUP_COLOR.get(crop_now.get(r.field_id, ""), "#999999")}})
            if hz is not None:
                ring = hz[(hz.field_id.astype(str) == r.field_id) & (hz.zone == "headlands")]
                if len(ring):
                    g = json.loads(gpd.GeoSeries([ring.geometry.iloc[0]]).set_precision(1e-6).to_json())
                    rings.append({"type": "Feature", "geometry": g["features"][0]["geometry"],
                                  "properties": {"field_id": r.field_id, "width_m": width}})
    for name, fs in (("fields.geojson", feats), (f"headlands_{tag}.geojson", rings)):
        (run.out / "map" / name).write_text(json.dumps({"type": "FeatureCollection", "features": fs},
                                                       separators=(",", ":"), sort_keys=True) + "\n")
    pts_ea = gpd.GeoDataFrame(geo, geometry=gpd.points_from_xy(geo.lon, geo.lat), crs="EPSG:4326").to_crs("EPSG:6933")
    xy = np.c_[pts_ea.geometry.x, pts_ea.geometry.y]
    spread = np.sqrt(((xy[:, None] - xy[None]) ** 2).sum(-1)).max() / 1000 if len(xy) > 1 else 0
    run.add(id="M1", category="Map", kind="map", level="field, farm",
            title="Where the fields are", image=img,
            shows=(f"Left: the farm's location. Middle: each field coloured by its {latest} dominant CDL crop with the "
                   f"{width:g} m headland ring hatched. Right: the same fields coloured by edge share. Numbers are "
                   "field labels as in the review viewer; true outlines are drawn under each marker."),
            takeaway=(f"The {len(geo)} fields lie within {spread:.0f} km of each other. Fields with the darkest "
                      "colour on the right lose the most area to their edges."),
            limits="Plain outlines (no imagery) so the static map is fully reproducible; the HTML report adds an "
                   "interactive satellite view of the exact boundaries.")


# ----------------------------------------------------------------------------- scorecard
def scorecard(run: Run, c: Ctx, bd, rot, se, cm, fcell):
    last = max(int(y) for y in se.year.unique())
    hp = f"headlands_pct_{c.tag}"
    s = bd[["label", "field_id", "acres", hp, "compactness", "slope_pct", "relief_m"]].copy()
    s = s.merge(rot[["field_id", "pattern", f"crop_{last}", f"projected_{last + 1}"]], on="field_id", how="left")
    nc = pd.read_csv(run.out / "tables" / "cdl_noncrop_area.csv")[["field_id", "total", "pct_of_field"]]
    pu = pd.read_csv(run.out / "tables" / "cdl_edge_vs_interior_purity.csv")[["field_id", "difference_pct_points"]]
    s = s.merge(nc.rename(columns={"total": "noncrop_acres", "pct_of_field": "noncrop_pct"}), on="field_id", how="left")
    s = s.merge(pu.rename(columns={"difference_pct_points": "edge_purity_gap_pts"}), on="field_id", how="left")
    seas = se.groupby("cell").agg(season_rain_in=("rain", "mean"), season_gdd=("gdd", "mean"),
                                  season_deficit_in=("deficit", "mean"))
    s["weather_cell"] = s.field_id.map(fcell)
    s = s.merge(seas, left_on="weather_cell", right_index=True, how="left")
    hi = s[hp].quantile(0.75)
    flags = []
    for _, r in s.iterrows():
        fl = []
        if r.pattern != "corn-soy rotation":
            fl.append(r.pattern)
        if r[hp] >= hi and len(s) >= 4:
            fl.append("high edge share")
        if r.noncrop_pct >= 3:
            fl.append("non-crop area >= 3 %")
        if r.slope_pct >= s.slope_pct.quantile(0.75) and len(s) >= 4:
            fl.append("steeper")
        flags.append("; ".join(fl) if fl else "-")
    s["attention"] = flags
    s = s.drop(columns=["field_id"]).round(2).rename(columns={
        hp: f"edge_share_{c.tag}_pct", "pattern": "rotation_pattern", "slope_pct": "median_slope_pct",
        "season_rain_in": "mean_season_rain_in", "season_gdd": "mean_season_gdd",
        "season_deficit_in": "mean_season_deficit_in"})
    run.table(s, "field_scorecard_full")        # every column, for analysts
    s["rotation_pattern"] = s.rotation_pattern.str.replace(" rotation", "", regex=False)
    s = s[["label", "acres", f"edge_share_{c.tag}_pct", "median_slope_pct", "rotation_pattern", f"crop_{last}",
           f"projected_{last + 1}", "noncrop_pct", "edge_purity_gap_pts", "mean_season_rain_in", "attention"]]
    s = s.rename(columns={f"edge_share_{c.tag}_pct": "edge_%", "median_slope_pct": "slope_%",
                          "rotation_pattern": "rotation", "noncrop_pct": "noncrop_%",
                          "edge_purity_gap_pts": "purity_gap_pts", "mean_season_rain_in": "season_rain_in"})
    t = run.table(s, "field_scorecard")
    nflag = int((s.attention != "-").sum())
    run.add(id="S1", category="Field scorecard", kind="table", level="across fields",
            title="Field scorecard", table=None, summary=t,
            shows=("One row per field combining size, edge share, terrain, rotation pattern, the crop in "
                   f"{last} and the rotation projection for {last + 1}, non-crop area, edge/interior purity gap and "
                   "the field's season weather. Compactness, relief, weather cell, GDD and deficit are in "
                   "field_scorecard_full.csv."),
            takeaway=(f"{nflag} of {len(s)} fields have at least one flag in the 'attention' column (outlier edge "
                      "share or slope - top quartile, rotation that is not clean corn-soy, 3 % or more non-crop area); "
                      "start the field review with those."),
            limits="Flags are simple screening rules for prioritising a look at the field, not agronomic findings; "
                   "weather is shared by fields in the same ~50 km NASA POWER cell.")


# ----------------------------------------------------------------------------- main
def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run_all(a, out: Path) -> dict:
    style()
    root = a.runtime_root.expanduser().resolve()
    growers = (a.growers_dir or root / "data-pipeline" / "growers").expanduser().resolve()
    avail = list_growers(growers)
    if not avail:
        raise SystemExit(f"no growers with boundary/field_boundaries.geojson under {growers}")
    if a.grower is None:
        if len(avail) > 1:
            raise SystemExit("choose a grower with --grower; available: " + ", ".join(avail))
        a.grower = avail[0]
    if a.grower not in avail:
        raise SystemExit(f"unknown grower '{a.grower}'; available: {', '.join(avail)}")
    tr = InputTracker(root)
    farms = discover(growers, tr, a.grower)
    if a.farm:
        farms = [f for f in farms if f.farm == a.farm]
        if not farms:
            raise SystemExit(f"grower {a.grower} has no farm '{a.farm}'")
    all_labels = sorted(l for f in farms for l in f.fields.label)
    farms = select_fields(farms, a.fields, a.exclude_fields)
    geo = field_geometry_table(farms)
    c = Ctx(farms, geo, tr, a)
    run = Run(out)
    bd = boundaries(run, c)
    dom, rot = cdl(run, c, bd)
    se, cm, fcell = weather(run, c, dom)
    admin = root / "data-pipeline" / "shared" / "geoadmin"
    spatial_map(run, c, dom, bd, admin if admin.exists() else None)
    scorecard(run, c, bd, rot, se, cm, fcell)
    run.items.sort(key=lambda i: ({"Field scorecard": 0, "Map": 1, "Field boundaries": 2, "CDL crop history": 3,
                                   "Weather": 4}[i["category"]], i["id"]))

    summ = (geo.groupby(["grower", "farm"])
            .agg(state=("state", "first"), counties=("county", lambda s: ", ".join(sorted(set(s) - {""}))),
                 fields=("field_id", "size"), total_acres=("acres", "sum"), median_acres=("acres", "median"),
                 lat=("lat", "mean"), lon=("lon", "mean")).round(3).reset_index())
    run.table(summ, "dataset_summary")
    run.table(geo, "fields")
    excluded = sorted(set(all_labels) - set(geo.label))
    findings = {
        "skill": "field-level-eda", "version": VERSION,
        "dataset": {"grower": a.grower, "farms": len(farms), "fields": int(len(geo)),
                    "field_labels": sorted(geo.label), "excluded_labels": excluded,
                    "years": sorted(int(y) for y in dom.year.unique()),
                    "summary_table": "tables/dataset_summary.csv"},
        "parameters": {"grower": a.grower, "farm": a.farm, "fields": a.fields or [],
                       "exclude_fields": a.exclude_fields or [], "headlands_width_m": a.headlands_width,
                       "season": a.season, "corn_critical_month": a.corn_month,
                       "soy_critical_month": a.soy_month, "diversity_min_pct": a.diversity_min_pct, "heat_threshold_f": a.heat_f},
        "items": run.items,
    }
    (out / "findings.json").write_text(json.dumps(findings, indent=2, sort_keys=True) + "\n")
    outputs = {p.relative_to(out).as_posix(): sha(p) for p in sorted(out.rglob("*"))
               if p.is_file() and p.name != "manifest.json"}
    manifest = {
        "skill": "field-level-eda", "version": VERSION,
        "inputs_root": "<runtime-root>", "inputs": dict(sorted(tr.files.items())),
        "parameters": findings["parameters"], "selected_field_ids": sorted(geo.field_id),
        "outputs": outputs,
        "environment": {"python": platform.python_version(), "pandas": pd.__version__,
                        "numpy": np.__version__, "matplotlib": matplotlib.__version__,
                        "geopandas": gpd.__version__, "scipy": __import__("scipy").__version__},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    root = os.environ.get("DATA_PIPELINE_DATA_ROOT", str(Path.home() / "my-farm-advisor-runtime"))
    ap.add_argument("--runtime-root", type=Path, default=Path(root))
    ap.add_argument("--growers-dir", type=Path, help="default: <runtime-root>/data-pipeline/growers")
    ap.add_argument("--grower", help="grower folder name, e.g. illinois-grower (required if several exist)")
    ap.add_argument("--farm", help="only this farm folder of the grower (default: all)")
    ap.add_argument("--fields", nargs="+", metavar="FIELD",
                    help="only these fields: labels (IL-03), field ids or trailing id digits (default: all)")
    ap.add_argument("--exclude-fields", nargs="+", metavar="FIELD", help="drop these fields from the analysis")
    ap.add_argument("--out", type=Path,
                    help="default: <runtime-root>/data-pipeline/shared/eda/field-level-eda/<grower>")
    ap.add_argument("--list", action="store_true", help="list growers, farms and field labels, then exit")
    ap.add_argument("--headlands-width", type=float, default=30.0, help="ring width used for analysis (m)")
    ap.add_argument("--season", nargs=2, default=["05-01", "09-30"], metavar=("MM-DD", "MM-DD"))
    ap.add_argument("--corn-month", type=int, default=7, help="critical month for corn (pollination)")
    ap.add_argument("--soy-month", type=int, default=8, help="critical month for soybeans (pod fill)")
    ap.add_argument("--diversity-min-pct", type=float, default=1.0,
                    help="a CDL class counts toward crop diversity if it covers at least this %% of the field-year")
    ap.add_argument("--heat-f", type=float, default=90.0, help="heat-stress threshold, deg F")
    ap.add_argument("--verify", action="store_true",
                    help="re-run into a temp folder and compare output hashes with <out>/manifest.json")
    a = ap.parse_args(argv)
    growers = (a.growers_dir or a.runtime_root / "data-pipeline" / "growers").expanduser().resolve()
    if a.list:
        tr = InputTracker(a.runtime_root.expanduser().resolve())
        for f in discover(growers, tr):
            print(f"{f.grower}  /  {f.farm}  ({f.state}, {len(f.fields)} fields)")
            print("   " + ", ".join(f"{r.label}={r.field_id[-7:]}" for r in f.fields.itertuples()))
        return
    if a.grower is None:
        avail = list_growers(growers)
        if len(avail) == 1:
            a.grower = avail[0]
    out = (a.out or a.runtime_root / "data-pipeline" / "shared" / "eda" / "field-level-eda" / str(a.grower)).expanduser().resolve()
    if a.verify:
        old = json.loads((out / "manifest.json").read_text())
        with tempfile.TemporaryDirectory() as td:
            new = run_all(a, Path(td))
        bad = sorted(k for k in set(old["outputs"]) | set(new["outputs"])
                     if old["outputs"].get(k) != new["outputs"].get(k))
        changed_in = sorted(k for k in set(old["inputs"]) | set(new["inputs"])
                            if old["inputs"].get(k) != new["inputs"].get(k))
        if changed_in:
            print("inputs changed since the manifest:", *changed_in, sep="\n  ")
        if bad:
            print("outputs differ:", *bad, sep="\n  ")
            raise SystemExit(1)
        print(f"verified: {len(new['outputs'])} outputs identical")
        return
    m = run_all(a, out)
    print(f"wrote {len(m['outputs'])} outputs for {a.grower} ({len(m['selected_field_ids'])} fields) to {out}")


if __name__ == "__main__":
    main()
