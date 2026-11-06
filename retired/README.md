# Retired (pre-refactor) code and configuration — safe to delete after review

Everything here was superseded by the config-driven workflow (README.md, docs/workflow.md).
Nothing in `src/` or `tests/` reads these files; only the one-off `scripts/migrate_config.py` reads `retired/config/`.

| Retired | Replaced by | Evidence |
| --- | --- | --- |
| `config/*.json` (6 acquisition specifications) | `config/pipeline.yml` + `data/selections/conus_2026_11/` + `config/sources/*.yml`, rebuilt by `scripts/plan.py` into `locks/<source>.json` | every lock is byte-identical to its retired file (`tests/test_requests.py`) |
| `src/sm_drydown/homogenization/` (wide per-station bundles) | `src/sm_drydown/features/`, `assemble.py`, `scripts/homogenize.py` (long feature tables) | `scripts/check_equivalence.py`: ARM, 35 stations / 1,411 sensor columns / 3.95 M sensor-days identical |
| `src/sm_drydown/storage/catalog.py` | `scripts/publish.py`, `src/sm_drydown/manifest.py` | — |
| `src/sm_drydown/formats/soil.py` | `features/gridded.snap_texture` (tolerance from `gldas.texture_snap_tolerance`) | `tests/test_grids.py` |
| `scripts/pull_*.py` | `scripts/plan.py` + `scripts/acquire.py` (lockfile instead of `--approved-config-sha256`) | same provider modules, now in `src/sm_drydown/providers/` |
| `docs/workflow_pre_refactor.md` | `docs/workflow.md` (rewritten for the new layout) | description of the pre-refactor code as of `bb86058` |
| `scripts/homogenize_data.py`, `inspect_data.py`, `restore_from_kopah.py` | `scripts/homogenize.py`, `inspect_tables.py`, `restore.py` | — |

The policy that produced the published v1 `daily/` tables is kept in `records/legacy/`.
To remove: `git rm -r retired/` (and `rm -r retired/` for files that were never committed).
