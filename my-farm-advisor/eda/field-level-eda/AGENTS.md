# Local Instructions

## Purpose

This folder owns the field-level EDA workflow: static figures, tables and findings for field
boundaries, CDL crop history and weather for ONE grower and a chosen set of its fields
(`--grower`, `--fields`, `--exclude-fields`).

## Safe edit scope

Edits should stay in this folder and its children unless the user explicitly asks for a broader
skill change. Never write runtime data or generated outputs into the repo.

## Read nearby docs first

Read `GUIDE.md` first. For routing context read `../INDEX.md` and `../../SKILL.md`.

## Rules for agents

- The skill analyses one grower per run. Do not add cross-grower comparisons; a different
  grower is a different run with its own output folder.
- Keep the analysis agnostic: no grower, field, county or state names in code. Text in
  `findings.json` must be computed from the data.
- Keep report generation out of this skill; it produces figures, tables and findings only.
- After any change, run the skill and then `--verify`; both must pass before committing.
- Do not add randomness; if sampling is ever needed, take a seed argument and record it in
  the manifest.

## Local validation

Run `./scripts/validate.sh` from the repository root after structural changes, then run
`src/field_level_eda.py` and `src/field_level_eda.py --verify` against a runtime.

## Local-delta-only reminder

This nested AGENTS.md only records instructions that differ from the parent or root files. See
`../../../AGENTS.md` for repository-wide policy.
