# Local Instructions

## Purpose

This folder owns the grower-level interactive field map subskill. It renders one
self-contained HTML map per grower from existing data-pipeline runtime outputs:
all farms, all fields, Esri World Imagery, a field list, click-to-zoom, and a
rotation plus soil panel.

## Safe edit scope

Edits should stay in this folder, the two map source files it documents, and the
data-pipeline README section unless the user explicitly asks for a broader
change. The source files are:

- `../src/scripts/lib/grower_map_data.py` — renderer-neutral payload builder.
- `../src/scripts/reporting/generate_grower_web_map.py` — Leaflet renderer.

Do not change parent `SKILL.md`, sibling workflows, or root policy from a
subskill task unless explicitly requested.

## Read nearby docs first

Read [GUIDE.md](GUIDE.md) for the runbook and inputs, then
[COMPARISON.md](COMPARISON.md) before changing renderers or dependencies. Read
[../README.md](../README.md) for runtime installation and shared-data setup, and
[../AGENTS.md](../AGENTS.md) for the parent data-pipeline contract.

## Runtime contract delta

- The generator runs from the runtime source copy, not from this checkout:
  `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src/scripts/reporting/generate_grower_web_map.py`.
- Refresh the runtime with `./scripts/install.sh --force-refresh --no-install-deps`
  after editing either map source file. The installer copies `src/` wholesale.
- Output goes to
  `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/derived/reports/<grower>_field_map.html`.
  Never commit generated HTML, screenshots, or browser profiles.
- The map reads `farm.json`, `field-inventory.csv`, field boundaries, the farm
  rotation table, the 2021–2025 CDL composition table, and the farm SSURGO
  summary. It must not trigger downloads or rebuild steps.
- Keep the Esri World Imagery basemap. Do not add an OpenStreetMap toggle; that
  is a deliberate product choice for this subskill.

## Command runbook

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd my-farm-advisor/data-pipeline
./scripts/install.sh --data-root "${DATA_PIPELINE_DATA_ROOT}" --non-interactive --force-refresh --no-install-deps

cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/reporting/generate_grower_web_map.py --grower-slug illinois-grower
```

Repeat with the other grower slug, or omit `--grower-slug` and export
`AG_GROWER_SLUG`.

## Local validation

- Run `./scripts/validate.sh` from the repository root after documentation or
  structure changes.
- Confirm the generator reports the expected field count for the grower, the
  displayed window is 2021–2025, and the output stays under 300 KB.
- Open the HTML in a browser or headless Chromium and confirm tiles load, the
  farm legend matches the polygons, and a field click fills the rotation and
  soil panel.
- The D3 prototype and its generator live outside the repository and must never
  appear in `git status`.

## Local-delta-only reminder

This nested AGENTS.md only records instructions that differ from
[../AGENTS.md](../AGENTS.md) and [../../../AGENTS.md](../../../AGENTS.md). Do not
duplicate root-wide asset, vendor, or validation policy here.
