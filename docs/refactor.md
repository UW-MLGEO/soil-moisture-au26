# The config-driven refactor: what changed and why

This note compares the `refactor/config-driven-workflow` branch with `main` (merge base `bb86058`). It is
for anyone who knows the `main` workflow and needs to review, use or extend the new one. How to use the
pipeline is in the [README](../README.md#workflow), the design rules are in [workflow.md](workflow.md),
and every setting is listed in [decisions.md](decisions.md).

## Summary

The science is unchanged. Only the way it is written down changed.

- **Every choice that can change a value is in one file.** That covers changing a value, deciding whether
  a value is kept, and labelling it. The file is [config/pipeline.yml](../config/pipeline.yml), and each
  of its 101 settings has a unit and a reason.
- **Provider requests are rebuilt from config and frozen as committed locks.** With the baseline config
  the locks are byte-identical to the request specifications the published corpus was acquired with, so
  every hash and Kopah key stays valid.
- **The output format changed.** Instead of 1,388 wide per-station folders, the pipeline writes one long
  table per feature, joined into a single `sensor_day` table.
- **The outputs are the same.** On the ARM network, the new `sensor_day` tables are identical to the old
  code's output: every value, count, missing reason and QC word. Inspection results match too.

## Problems with the old workflow

The old pipeline was careful. It hash-verified every file, kept raw data immutable on Kopah, never filled
values and recorded why each value was missing. But a scientist could not easily answer *"what was
assumed, and where is it set?"*

1. **Choices were spread over four places:**
   - `config/daily_homogenization_policy.json` held the QC bits and bands;
   - prose strings sat inside the 0.2–2.5 MB acquisition JSONs (`"processing": "No QC filtering…"`,
     `sensor_depth_assignment`);
   - Python literals: the ISMN `G`-flag rule, the 1e-4 GLDAS texture snap in `formats/soil.py`, GLDAS
     tie-breaking, and a hard check that temperature uses `G` only;
   - command-line defaults: `--flatline-min-days 3`, `--chirps-positive-quantile .999`,
     `--gldas-sum-tolerance .01`.

   Changing a threshold meant finding it first. Reporting the thresholds meant reading code.
2. **Config files mixed three jobs:** scientific choices, frozen machine-made inventories and provenance
   prose. The 1,388-station list was copied into five files.
3. **No script made the station selection.** It was built outside the repository from an ISMN inventory
   download and a Census boundary. The rules existed only as prose (`spatial_rule`, `selection_note`).
4. **Acquisition approval was manual.** There were five `pull_*.py` scripts, each with about ten argparse
   defaults (paths, Kopah endpoint and profile repeated). Each needed a SHA-256 pasted by hand
   (`--approved-config-sha256 APPROVED_HASH`), and code was imported through `sys.path.insert`.
5. **The outputs were hard to scan and to version:**
   - each soil-moisture and temperature column carried seven companion columns (counts, JSON flag
     histograms, missing reason) and a very large `description.json`;
   - any code edit created a sibling "signature" folder for every station, which blocked upload and
     inspection until the old folder was resolved.
6. **Small hazards:** merge-conflict markers in `.gitignore`, an `osx-arm64`-only lockfile, and
   mismatched default output folders between scripts.

## What changed and why

### 1. One file for every scientific choice

[config/pipeline.yml](../config/pipeline.yml) has one section per source or stage: `study`, `selection`,
`subset`, `ismn`, `chirps`, `gldas`, `modis_lai`, `modis_land_cover`, `features`, `inspection`,
`export_drydown_q` and `runtime`. Each key's comment gives its unit in brackets, what it means, and the
options the code implements:

```yaml
ismn:
  flags_kept: [G]              # [-] ISMN quality flags accepted for soil moisture; other flags are masked, not dropped
  daily_statistic: mean        # [-] statistic of accepted values within a UTC day; options: mean | median
  min_obs_per_day: 1           # [obs] a daily value needs at least this many accepted observations (1 = no minimum)
```

**Why:** a scientist can review, report or change every assumption without reading code. The pattern
comes from drydown-q's `config/default.yml`, which the project already found readable.

How it is enforced:

- [src/sm_drydown/config.py](../src/sm_drydown/config.py) loads the file into frozen dataclasses. It
  rejects unknown or missing keys and options outside the allowed values. A typo fails loudly instead of
  silently using a default.
- **Run files** in [config/runs/](../config/runs/) (`baseline`, `pilot_arm`, `global`, `smoke`) override
  only the keys they list. Every run saves its fully resolved settings and logs each one that differs
  from `pipeline.yml`. Exploring a variant never means editing the reference file.
- **The rule is tested.** [tests/test_guards.py](../tests/test_guards.py) fails if processing code
  contains a numeric literal with scientific meaning. The only exceptions are structural values such as
  indices, hash-prefix lengths, and the unit conversions 100 and 273.15. It also fails if a
  `pipeline.yml` key lacks a unit comment.
- **The docs cannot drift.** [docs/decisions.md](decisions.md) is generated from `pipeline.yml` by
  `scripts/document_config.py`, and a test fails if it is out of date.

**Choices, inventories and provenance are now separate files:**

| Kind | Where | Edited by |
| --- | --- | --- |
| Scientific choices | `config/pipeline.yml`, `config/runs/` | you |
| Provider facts and wording (fill codes, scale factors, grids, coverage, citations, request text) | `config/sources/<source>.yml` | rarely |
| Frozen station, sensor and granule lists (CSV, diffable) | `data/selections/<name>/` | `scripts/select_stations.py` |
| Fixed lookup tables (IGBP classes, FAO texture classes) | `data/tables/` | rarely |
| Published catalog, rights and citation evidence, spatial partitions | `records/` (moved unchanged from `config/`) | by hand |
| Local paths, Kopah locations and transfer limits | `config/paths.yml` (from `paths.example.yml`), `config/storage.yml` | you |

### 2. Station selection is specified in config and generated by a script

The `selection:` section holds the rules:
- the region: a named boundary from [config/regions.yml](../config/regions.yml), a bounding box, a polygon
  file, or global, plus a buffer;
- the CRS assumed for station coordinates;
- which networks to include;
- the minimum overlap with the study window;
- the allowed depth range.

[scripts/select_stations.py](../scripts/select_stations.py) downloads the ISMN inventory and the boundary
(cached and hashed), applies the rules, and writes `stations.csv`, `sm_sensors.csv` and
`excluded_stations.csv`. Each excluded station is listed with the rule that removed it.

**Why:**
- The selection is the first scientific decision, and it was the one step with no code.
- Changing the region is now a run file, not a code change.
  [config/runs/global.yml](../config/runs/global.yml) sets `selection.region.kind: global`. A station
  outside a source's declared coverage (CHIRPS covers 50°S–50°N) gets the missing reason
  `outside_source_coverage`, never a silent NaN.
- `plan ismn` refuses to plan a network until it has a citation record in `records/`, as the ISMN terms
  require.

### 3. Plan locks replace pasted hashes

`pixi run plan <source>` builds the exact provider request from three inputs: `pipeline.yml`, the
selection CSVs and `config/sources/<source>.yml`. It writes the request to `locks/<source>.json`, plus a
short `.summary.yml` (stations, assets, estimated bytes). The SHA-256 of the lock is the acquisition's
identity: it appears in Kopah keys, provenance records and every extract.

`pixi run acquire <source>` runs only if two things hold:
- the request rebuilt from the current config equals the lock byte for byte;
- the lock is committed in git (`--allow-uncommitted-lock` relaxes this for pilots).

**Why:**
- Approval becomes a reviewed commit, not a hash copied between terminals.
- An unnoticed config change can no longer fetch a different dataset under the same name.
- The locks are byte-identical to the old `config/*_conus.json` specifications: git records the moves as
  100 % renames. So the published request hashes and every existing Kopah scratch object still apply,
  and nothing has to be downloaded again.

### 4. A content-addressed stage cache replaces per-station signatures

Every stage writes `output/cache/<stage>/<key>/<table>/<network>.parquet`.
- The key is the SHA-256 of three things: the stage's config sections, its inputs (lock hashes or
  upstream keys) and its own code.
- A `_complete/<network>.json` marker records each network's input hashes and output file hashes. A
  network is reused only when its marker still matches.
- `output/runs/<run>/manifest.json` names the keys that make up the run, along with the locks, the git
  commit and package versions.

**Why:**
- A change reruns only what depends on it. Changing an inspection threshold reruns inspection alone;
  adding a feature rebuilds only that feature group and the join.
- An interrupted run resumes from the last completed network.
- Old outputs no longer block anything: a new key is simply a new folder.
- `pixi run clean-cache --delete` removes key folders that no run record names. Without `--delete`, it
  only lists them.

### 5. Long feature tables, joined into `sensor_day`

Stage 2 writes one long table per feature:
- per sensor-day: `theta_m3m3`, `soil_temp_degc`;
- per station-day: `precip_chirps_mm_day`, `lai_m2m2`, `igbp_class`;
- per station: `gldas_static`, with texture class, sand, silt and clay fractions, and porosity.

Each value column carries a `missing_reason` from a fixed vocabulary
([features/common.py](../src/sm_drydown/features/common.py)). Daily flag histograms and raw QC words go to
separate audit columns and tables, out of the main tables.

Stage 3 joins the features into `sensor_day`: one row per soil-moisture sensor per day, with features in
the order listed under `features:` in `pipeline.yml`. The join refuses to add or drop sensor-days.

**Why:**
- One table is easy to scan, load and train on.
- The project plans to add ecohydrological predictors of *q*. A new feature needs only four things: a
  `pipeline.yml` section, a build function, a registry entry and a schema. Nothing in the cache,
  manifest, join or publish code changes, and existing feature caches are reused.
  [tests/test_pipeline.py](../tests/test_pipeline.py) adds a toy feature this way.

### 6. Export to drydown-q

[scripts/export_drydown_q.py](../scripts/export_drydown_q.py) writes drydown-q's `sensors` and
`station_day` tables. drydown-q's `station_day` has one row per sensor per day, because drydown-q detects
the drydown events itself. The export passes drydown-q's own `assert_schema`, so this repository can
replace drydown-q's `prepare` step.

Columns without a source yet are written as NaN and listed in the export manifest: PET, the 60-day PET
sum, tree cover and the dPET cell. Texture comes from GLDAS, not ISMN metadata. LAI is MCD15A3H, not
drydown-q's default MOD15A2H.

### 7. A new processed layout on Kopah; raw objects are unchanged

The raw acquisitions under `s3://gaia/scratch/ml4geo_sm_drydown/` are untouched. Their collection IDs and
request hashes come from the locks, so `pixi run restore` reads them as before.

New processed tables go to `s3://gaia/ml4geo_sm_drydown/v2/<stage>/<key>/…`, and run manifests to
`v2/manifests/<sha256>.json`, through `scripts/publish.py`. That script re-hashes each file before
upload, skips identical objects and refuses to overwrite a different one. The published v1 `daily/`
tables stay where they are. Nothing on Kopah is overwritten or deleted.

### 8. Scripts that read as methods

There is one script per stage. It runs as a `pixi run` task, takes the same `--run` and `--paths`
options, and has a docstring giving its purpose, invocation, inputs and outputs:

| Stage | Script | Task |
| --- | --- | --- |
| Select | `select_stations.py` | `pixi run select` |
| Plan | `plan.py` | `pixi run plan <source>` |
| Acquire | `acquire.py` | `pixi run acquire <source>` |
| Restore | `restore.py` | `pixi run restore` |
| Features and assembly | `homogenize.py` | `pixi run homogenize` |
| Inspect | `inspect_tables.py` | `pixi run inspect` |
| Publish | `publish.py` | `pixi run publish` |
| Export | `export_drydown_q.py` | `pixi run export-drydown-q` |
| Housekeeping | `clean_cache.py` | `pixi run clean-cache` |

[scripts/homogenize.py](../scripts/homogenize.py) has one numbered block per source. Each block states
its rule in a comment, logs the config values it used, and logs how many values were kept, masked and
missing. The run's `homogenize.log` therefore reads like a methods paragraph with numbers.

`sm_drydown` is now installed as an editable package through `pyproject.toml`, so no script uses
`sys.path.insert`. The lockfile covers `osx-arm64` and `linux-64` (Hyak/klone).

## Old → new

| On `main` | On the branch |
| --- | --- |
| `config/daily_homogenization_policy.json`, prose in the acquisition JSONs, Python literals, CLI defaults | `config/pipeline.yml` (rendered in `docs/decisions.md`); the old policy file is kept in `records/legacy/` |
| `config/ismn_conus_sm_temperature.json`, `chirps_earthengine_conus.json`, `gldas_soils_conus.json`, `modis_lai_conus.json`, `modis_land_cover_conus.json`, `published_drydowns_morgan_2025.json` | `locks/<source>.json`: identical bytes, now generated by `plan.py` from `pipeline.yml` + `data/selections/` + `config/sources/` |
| Station list copied into five JSONs; selection made outside the repo | `data/selections/conus_2026_11/*.csv`, generated by `select_stations.py` |
| `config/source_provenance.json`, `ismn_network_*`, `spatial_partitions.json`, `ai_ready_dataset.json` | `records/` (unchanged files) |
| `scripts/pull_{ismn,chirps,gldas,modis,published_drydowns}.py` with `--approved-config-sha256` | `scripts/acquire.py <source>`, gated by the committed lock |
| `scripts/homogenize_data.py`, `src/sm_drydown/homogenization/` | `scripts/homogenize.py`, `src/sm_drydown/features/`, `assemble.py` |
| `scripts/inspect_data.py`, CLI thresholds | `scripts/inspect_tables.py`; thresholds in `pipeline.yml` `inspection:` |
| `src/sm_drydown/acquisition/` | `src/sm_drydown/providers/` (moved; imports updated; `config.py` renamed `ancillary_spec.py`) |
| `src/sm_drydown/storage/catalog.py`, per-station signature folders, `description.json` | `cache.py` stage cache, `manifest.py` run manifest, `schemas.py` |
| `formats/soil.py` (hard-coded texture snap) | `gldas.texture_snap_tolerance` and `data/tables/gldas_fao16_texture.csv` |
| Restore script (uncommitted on `main`) | `scripts/restore.py` |
| — | `scripts/plan.py`, `publish.py`, `export_drydown_q.py`, `check_equivalence.py`, `clean_cache.py`, `document_config.py`, `migrate_config.py` |

Every file superseded on `main` is in [retired/](../retired/README.md) until it is removed with
`git rm -r retired/`. That includes the pre-refactor workflow notes, which were never committed to
`main`: they are in `retired/docs/workflow_pre_refactor.md`.

## What stayed the same

- **The processing rules.** Every default in `pipeline.yml` reproduces the old behaviour:
  - only ISMN flag `G` is accepted;
  - the daily value is the mean of accepted values per UTC day;
  - CHIRPS nulls and absent rows stay missing, never zero rain;
  - LAI QC bits 5–7 must be 0 or 1, and missing QC is never assumed good;
  - land-cover QC is recorded but not used to mask;
  - GLDAS uses the nearest 0.25° cell.
- **Nothing is filled.** No value is filled, interpolated or carried forward. Every missing value has a
  reason.
- **The provider clients.** ISMN asynchronous exports, Earth Engine batching, LP DAAC HDF4 and GLDAS
  chunk reads were moved, not rewritten. They encode provider quirks that took effort to find.
- **The data-safety code.** `storage/files.py` keeps verified downloads with SHA-256 sidecars and
  immutable, private, verified Kopah uploads.
- **The study.** 1,388 CONUS stations, 2002-07-04 to 2025-12-31 inclusive (8,582 UTC days).

## Evidence of equivalence

All checks below were run locally on 2026-11-05 (macOS arm64).

| Check | Result |
| --- | --- |
| Locks vs the old request JSONs | Byte-identical for all six sources (git: 100 % renames). `tests/test_requests.py` rebuilds each lock from config and requires equality. The ISMN lock's SHA-256 (`15b45472…`) matches the published record. |
| Station selection | `select_stations.py`, run on the live ISMN inventory plus the Census boundary (whose SHA-256 matches the recorded one), reproduces all 1,388 stations and 10,825 sensors in the original order. The exclusion list is fuller: 28 stations with reasons, against 7 in the old record. |
| ARM network, old vs new code (`scripts/check_equivalence.py`) | 35 stations, 1,411 sensor columns and 3,951,494 sensor-days identical: values, counts, missing reasons, masks, intervals and QC words. The checker was shown to catch a change of 1e-9 and a single altered missing reason. |
| ARM inspection | Matches the old inspection: 3,619 flatline runs, 71 CHIRPS extremes, the same season totals, station checks and GLDAS checks. Differences: quantile key names and last-digit floating-point rounding. |
| drydown-q export | Passes drydown-q's `assert_schema` for `sensors` and `station_day`. |
| `pixi run test` | 117 passed (synthetic, offline). |
| `pixi run smoke` | "Smoke test passed: 43 sensor-days from 2 stations; missing reasons {'available': 38, 'no_observations': 3, 'missing_values': 1, 'quality_masked': 1}; stages reused on rerun; inspection and drydown-q export written; stale cache removed." |

## Deviations from the original plan

- **The code grew.** Python in `src/` grew from 3,236 to 4,566 lines, and scripts from 381 to 1,019. The
  plan had expected `src/` to shrink. The growth is mostly new, single-purpose modules (config loader,
  request builders, selector, cache, manifest, schemas, feature registry) and the scripts' explanatory
  step blocks.
- **The provider clients were moved nearly unchanged** rather than slimmed. Their timeouts and retry
  counts stay as named constants in `providers/`, which the numeric-literal guard exempts.
- **Raw caches keep their old local paths,** `output/ismn/` and `output/ancillary/`.
- **Feature caches are keyed per source group,** not per individual feature. Adding a group still leaves
  the other groups' caches untouched.
- **Superseded code is parked in `retired/`,** not deleted.
- **Two scripts got different names:** `select_stations.py` and `inspect_tables.py`, because `select.py`
  and `inspect.py` would shadow Python standard-library modules.

## Not yet verified, and known gaps

- **Live services:** acquire, publish and restore have not been run against the live providers or Kopah.
  Acquisition is covered only by mocked provider tests.
- **Full run:** a full 14-network `homogenize` has not been run. About 30 minutes is an extrapolation
  from the ARM pilot. Before anything is published under `v2/`, the full-run counts should match
  `records/ai_ready_dataset.json`: 1,388 stations, 10,825 soil-moisture and 8,632 temperature streams,
  and 11,911,816 station-days.
- **Linux:** `linux-64` is in the lockfile, but the tests have only been run on macOS arm64.
- **Selection inventory:** the original 2026-11-01 ISMN inventory snapshot is not in the repository. A
  fresh inventory reproduces the same stations, but its file hash differs because ISMN regenerates the
  file daily.
- **Missing features:** PET (GLEAM) and tree cover (MOD44B) are not features yet. Spatial partitions are
  a frozen record for this selection and are not regenerated for new selections.

## How to review or migrate

Run the offline checks (no credentials or downloads):

```sh
pixi install
pixi run test
pixi run smoke
```

Then read [config/pipeline.yml](../config/pipeline.yml) or [docs/decisions.md](decisions.md). Every
setting is there with its unit and reason.

To rebuild and compare the ARM pilot from the existing Kopah data:

```sh
pixi run restore pilot_arm
pixi run homogenize pilot_arm
pixi run inspect pilot_arm
```

To change a rule, write a run file in `config/runs/` and run it with `--run <name>`. If the rule changes
what is requested from a provider, run `pixi run plan <source>` and commit the new lock before
acquiring. `pixi run acquire` refuses otherwise.
