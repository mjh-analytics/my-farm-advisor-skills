---
name: field-level-eda
description: Static, deterministic field-level EDA for one grower and a chosen set of their fields - boundaries, headland share and terrain; USDA CDL rotation, next-season projection and edge purity; crop-aware NASA POWER weather (GDD, critical-month rain and heat, water balance); field scorecard and map. Writes PNG figures, CSV tables, findings.json and a SHA-256 manifest.
version: 0.2.0
author: Max Hoeffel
tags: [eda, field-boundaries, cdl, weather, gdd, evapotranspiration, headlands, static-figures]
---

# Workflow: field-level-eda

## Description

Exploratory analysis of the three layers every My Farm Advisor farm has after the pipeline runs -
field boundaries, CDL crop history and daily weather - for **one grower at a time**, written for a
grower or for an analyst preparing a report for that grower. You pick the grower and the fields;
the skill compares the fields with each other, each field across years, and each field's edge with
its interior. All outputs are plain files (PNG, CSV, JSON); there is no dashboard in this skill.

The same code runs every time, so the same inputs give identical outputs. Nothing is hard-coded:
grower, farm and fields come from the command line and the runtime folders.

> "Run the EDA skill for illinois-grower, all 10 fields except IL-07" is
> `field_level_eda.py --grower illinois-grower --exclude-fields IL-07`.

## Where it sits

```
field-boundary-review -> run_farm_pipeline.py -> headlands-ring -> field-level-eda -> report
 (QC the outlines)        (weather, CDL, DEM)    (edge/interior)   (PNG/CSV/JSON)    (HTML/PDF)
```

| Uses | From |
|---|---|
| `boundary/field_boundaries.geojson`, `farm.json` | pipeline / `../../field-management/field-boundary-review/` |
| `derived/tables/*_weather_*.csv`, `*_<year>_cdl.csv`, `*_dem_summary.csv` | `../../data-pipeline/` |
| `*_headlands_<w>m_summary.csv`, `*_cdl_by_zone_<w>m.csv`, `field_headlands_<w>m.geojson` | `../../field-management/headlands-ring/` (`headlands_farm.py`) |
| State/county outlines for the map (optional) | `data-pipeline/shared/geoadmin/` in the runtime |

Field labels (e.g. `IA-07`) follow the same rule as the review viewer, so a field has the same name
in the viewer, the figures and the tables. Labels do not change when fields are excluded.

## Run

```bash
source pipeline-env.sh                       # sets DATA_PIPELINE_DATA_ROOT
python my-farm-advisor/eda/field-level-eda/src/field_level_eda.py --list      # growers, farms, field labels
# prerequisite, once per farm
python my-farm-advisor/field-management/headlands-ring/src/headlands_farm.py \
    --farm-dir <growers>/<grower>/farms/<farm> --widths 30
# analysis
python my-farm-advisor/eda/field-level-eda/src/field_level_eda.py --grower illinois-grower
python my-farm-advisor/eda/field-level-eda/src/field_level_eda.py --grower illinois-grower --exclude-fields IL-07
python my-farm-advisor/eda/field-level-eda/src/field_level_eda.py --grower iowa-grower --fields IA-03 IA-08 IA-16
python my-farm-advisor/eda/field-level-eda/src/field_level_eda.py --grower illinois-grower --verify
```

| Option | Default | Meaning |
|---|---|---|
| `--grower` | the only grower, else required | grower folder name |
| `--farm` | all farms of the grower | restrict to one farm |
| `--fields` / `--exclude-fields` | all fields | labels (`IL-03`), field ids, trailing id digits or the bare number. An unknown or ambiguous name stops the run. |
| `--runtime-root` | `$DATA_PIPELINE_DATA_ROOT` or `~/my-farm-advisor-runtime` | runtime to analyse |
| `--out` | `<root>/data-pipeline/shared/eda/field-level-eda/<grower>` | output folder |
| `--headlands-width` | 30 | ring width in m (one CDL pixel) |
| `--season` | `05-01 09-30` | season window (planting dates are not known) |
| `--corn-month` / `--soy-month` | 7 / 8 | critical month for corn (pollination) / soybeans (pod fill) |
| `--diversity-min-pct` | 1 | a CDL class counts toward crop diversity if it covers at least this % of the field-year |
| `--heat-f` | 90 | heat-stress threshold, deg F |

## Outputs

| ID | Category | Output | Level |
|---|---|---|---|
| S1 | Scorecard | one row per field: size, edge share, terrain, rotation, crop and projection, non-crop area, purity gap, weather, attention flags | across fields |
| M1 | Map | farm location; fields by latest crop; fields by edge share | field, farm |
| B1 | Boundaries | interior vs headland acres per field | across fields |
| B2 | Boundaries | median slope, relief and elevation per field (DEM) | across fields |
| B3 | Boundaries | **comparison**: Spearman correlation of field size with crop diversity (distinct CDL classes per year, each >= 1 % of the field) | across fields (boundary x CDL) |
| C1 | CDL | dominant crop by field x year with rotation classification | within field over time |
| C2 | CDL | corn / soy / other acres by year plus next-season projection from the rotation | across fields, over time |
| C3 | CDL | **comparison**: edge ring vs interior corn+soy purity per field, paired Wilcoxon signed-rank | within field |
| C4 | CDL | non-crop area (roads, grass, trees, water) inside each boundary, by class | across fields |
| W1 | Weather | cumulative corn GDD (base 50 F, cap 86 F) per year with the multi-year mean | across years |
| W2 | Weather | rain and heat days in each field-year's critical month for the crop actually grown (corn: July, soy: August) | field-year |
| W3 | Weather | **comparison**: share of weather variation explained by year vs by weather cell (eta-squared; Kruskal-Wallis across years when there are several cells) | across years vs fields |
| W4 | Weather | season rain vs reference ET (FAO-56 Penman-Monteith), per year | across years |

Also `tables/*.csv` (every number behind every chart), `map/*.geojson`, `findings.json` (per output:
what it shows, farmer takeaway, limits, statistics - text generated from the numbers) and
`manifest.json` (parameters, selected field ids, SHA-256 of every input and output, library versions).

## Methods notes

- **Weather cells.** NASA POWER is a ~0.5 deg grid; fields in the same cell have identical series.
  Series are fingerprinted and one field per cell is kept (`tables/weather_cells.csv`,
  `weather_field_cells.csv`). Farm-level weather averages the cells weighted by field count.
  Fields can differ in weather only if they sit in different cells, which W3 quantifies.
- **Crop-aware weather (W2).** Each field-year uses the CDL dominant crop to choose the critical
  month; other crops are left blank.
- **Rotation projection (C2).** Fields in a clean corn-soy alternation switch crop; any other field
  repeats its last crop. This is a rule, not a forecast.
- **ET0** uses daily FAO-56 Penman-Monteith with POWER radiation, humidity and 10 m wind reduced to
  2 m; elevation from the DEM summary when available, else 300 m.
- **CDL purity** uses the area-weighted zone tables from headlands-ring (exact pixel overlap).
- **Statistics** are rank-based or descriptive because there are only ~10 fields and ~5 years; the
  text reports n and p and names low-power tests.
- **No yield data.** The skill describes structure, rotation and weather exposure, not productivity.
  With yield or as-applied data, field-year yield against W2 exposure would be the natural next output.

## Determinism

No randomness; inputs discovered in sorted order; CSV rows sorted and numbers rounded; fixed font
(DejaVu Sans), Agg backend, no PNG timestamps. On one machine `--verify` reproduces every output
byte for byte. Across machines tables match; figures can differ at the byte level if
Matplotlib/FreeType versions differ, so `manifest.json` records them. Outputs contain no absolute paths.

## Report (outside this skill)

The skill stops at figures and tables. `reports/build_eda_report.py` (repository root) turns one
output folder into a single-page HTML report with an **Export PDF** button and an on-screen
satellite view, and optionally prints a PDF with headless Chrome:

```bash
python reports/build_eda_report.py --eda-dir <out> --out report.html --pdf report.pdf \
    --title "..." --intro intro.md
```

## Limitations

- No yield data. CDL is a classification (~90-95 % accurate for corn/soy); edge pixels mix classes.
- Fixed season window and critical months; GDD and water balance are not tied to planting dates.
- Reference ET is for grass; crop ET differs by stage. Rainfall is not all effective.
- Weather is ~50 km resolution: fields of one farm often share a series.
