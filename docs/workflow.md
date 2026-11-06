# Data workflow

How the pipeline is organised and why. The stage table, commands and output tables are in the
[README](../README.md#workflow); every scientific setting, with its unit and reason, is in
[config/pipeline.yml](../config/pipeline.yml) (as a table: [decisions.md](decisions.md)). This document
covers what neither can: the design rules, what each stage guarantees, provider quirks, the Kopah layout,
and known gaps.

## 1. Scope

A daily, per-sensor feature table for the 1,388 ISMN soil-moisture stations selected in CONUS
(`data/selections/conus_2026_11/`), 2002-07-04 through 2025-12-31 inclusive (8,582 UTC days), plus an
unchanged copy of the published Morgan et al. (2025) drydown events and fitted *q* labels.

The pipeline does **not** detect drydowns or fit loss functions (drydown-q does that from the
`export-drydown-q` tables), does not add PET yet (GLEAM is downloaded by the standalone
`scripts/download_gleam.sh` but not used), does not apply the spatial partitions in
[records/spatial_partitions.json](../records/spatial_partitions.json), and never fills, interpolates or
carries a measurement.

## 2. Design rules

1. **One place for choices.** Anything that can change a value, decide whether a value is kept, or change
   how a value is labelled is a key in `pipeline.yml`. Code implements the options; `tests/test_guards.py`
   fails on a numeric literal with scientific meaning in the processing code and on an undocumented key.
   Integrity checks that can only raise an error (hashes match, intervals do not overlap) stay in code.
2. **Choices, inventories and provenance are separate files.** `pipeline.yml` (choices), `data/selections/`
   (frozen station and sensor lists, as CSV), `config/sources/` (provider facts and wording),
   `records/` (published catalog, rights and citation evidence).
3. **Requests are derived, then frozen.** `plan.py` builds each provider request from those files and writes
   it to `locks/<source>.json`. Its SHA-256 is the acquisition identity used in Kopah keys, provenance
   records and every extract. `acquire.py` runs only if the request rebuilt from the current config equals
   the committed lock byte for byte, so the approval is the reviewed commit, not a pasted hash. With the
   baseline config the locks are byte-identical to the specifications the published corpus was acquired with.
4. **Raw stays raw.** Provider bytes are uploaded unchanged; station-cell extracts keep encoded values.
   Decoding, QC and units happen only in stage 2, under `pipeline.yml` rules.
5. **Content-addressed outputs.** Every stage writes `output/cache/<stage>/<key>/<table>/<network>.parquet`.
   The key covers the stage's config sections, inputs and own code; a per-network completion marker records
   input hashes (e.g. the network's ISMN archive) and file hashes. Changing an inspection threshold reruns
   only inspection; adding a feature reruns only that feature group and the join.
6. **Missing stays missing, with a reason.** Every value column has a `missing_reason`
   ([features/common.py](../src/sm_drydown/features/common.py) lists the vocabulary).
7. **Scripts read as methods.** `scripts/homogenize.py` has one numbered block per source; each logs the
   rule and the config values it used, then the counts kept, masked and missing. Each run writes
   `output/runs/<run>/{config.resolved.yml, homogenize.log, manifest.json}`.

## 3. Stage notes

### −1. Select (`scripts/select_stations.py`)

Rules (`selection:`): region (named boundary, bbox, polygon file or global) buffered by `buffer_deg`, with
station lon/lat interpreted in `station_crs`; networks; at least one soil-moisture time series overlapping the
study window by `min_overlap_days`, with both depths in `depth_range_m`. Excluded in-region stations are listed
with the rule that removed them. The inventory snapshot is stored by SHA-256; pin it with
`selection.inventory_snapshot` to rebuild a selection exactly. Run with today's inventory (2026-11-05), the
baseline rules reproduce the committed 1,388 stations and 10,825 sensors in the same order; only the
exclusion list is fuller (28 stations with reasons, against 7 in the legacy record).
Networks without a citation record in `records/` are reported, and `plan ismn` refuses until they are cited
(ISMN terms).

### 0–1. Plan, acquire, restore

`plan.py` prints a summary (stations, sensors, assets, estimated bytes) and writes the lock pair. Sources
declare their coverage in `config/sources/<source>.yml` (CHIRPS 50°S–50°N); stations outside it are left out
of that request and get `outside_source_coverage` in stage 2. For a new selection, MODIS land-cover tiles
are resolved from NASA CMR with `pixi run python scripts/plan.py modis_land_cover --discover-granules`.

Provider behaviour the clients in `src/sm_drydown/providers/` handle:

- **ISMN** exports are asynchronous, one per network. Submissions are spaced ≥ 61 s apart because ISMN
  timestamps requests to the minute and that timestamp identifies each export. A POST is never retried: an
  interrupted `submitting` checkpoint stops the run for a manual check of the ISMN requests page. `collect`
  exits 2 while exports are pending. Export options keep everything (`format: own`, `fill_nan: false`,
  `g_flag_only: false`). Credentials files must be mode 600.
- **Earth Engine** (CHIRPS, MODIS LAI): image metadata and `getRegion` responses are cached as decoded JSON,
  in sequential batches below `max_values_per_request`; each response must contain exactly the expected
  images and native pixel centres. Provider nulls and absent rows are stored as two separate masks. LAI
  composites are `[start, start + 4 d)`, cut at 1 January.
- **LP DAAC** (land cover): HDF4 tiles behind Earthdata Login; credentials are sent only to the login host on
  redirects. Tile grid boundaries are checked against the MODIS sinusoidal lattice. A year with no valid
  `LC_Type1` value at all is refused.
- **GLDAS** (HTTPS NetCDF4): only the HDF5 chunks containing station cells are read; values stay encoded. The
  NetCDF default fill is applied in stage 2 only when the file's creation properties prove it
  (`gldas.default_fill: verified_only`).
- **Morgan et al. (2025)**: 19 files pinned to `ecohydro/ismn-drydowns@ece5bbc5d741`, each SHA-256 checked;
  stored in processed (not scratch) storage; never parsed.

`restore.py` rebuilds the local caches from Kopah scratch instead (ISMN ZIPs with their checksum sidecars
rebuilt from the uploaded provenance records, and every station extract), verifying each object's SHA-256.

### 2–3. Features and assembly (`scripts/homogenize.py`)

Per-source rules are documented next to their keys in `pipeline.yml`; the code is in
[features/ismn.py](../src/sm_drydown/features/ismn.py) and [features/gridded.py](../src/sm_drydown/features/gridded.py).
Soil moisture rows cover each sensor's observed span (first to last day with any record;
`ismn.sensor_day_span`); days inside the span with no record are kept as `no_observations`. Each archive
member is its own sensor (`sm_<16 hex of the member path>`); sensors, depths and replicates are never averaged.
Assembly joins station-day features on (station_id, date) and static features on station_id, and refuses any
join that would add or drop sensor-days.

### 4. Inspection (`scripts/inspect_tables.py`)

Candidate problems only (never edits): exact-equality flatlines (`inspection.flatline_min_days`), CHIRPS
negatives and extremes above a station's positive-day quantile, season totals with coverage, GLDAS range and
sand+silt+clay closure, and distributions weighted by station and by unique native cell. On the ARM pilot the
results equal the pre-refactor inspection apart from quantile key names and last-digit float differences.

### 5–6. Publish and export

`publish.py` uploads every file in a run manifest, re-hashing each first, to
`s3://gaia/ml4geo_sm_drydown/v2/<stage>/<key>/…` and the manifest to `v2/manifests/<sha256>.json`; uploads are
private and immutable (identical objects skipped, different objects at the same key refused).
`export_drydown_q.py` writes drydown-q's `sensors` and `station_day` (one row per sensor per day): PET, tree
cover and dPET cells are NaN until those sources exist, texture comes from GLDAS rather than ISMN metadata, and
LAI is MCD15A3H; the export manifest records all of this. The ARM export passes drydown-q's `assert_schema`.

## 4. Kopah layout

```
s3://gaia/scratch/ml4geo_sm_drydown/               raw acquisitions (unchanged by the refactor)
  ismn/<collection_id>/{config,raw,provenance/<request-sha>,manifests,extracted}/
  <dataset>/<collection_id>/{config,raw,extracts,provenance,manifests}/
s3://gaia/ml4geo_sm_drydown/                        processed
  daily/<network>/<station_id>/<signature>/         v1 wide station tables (published 2026-11; records/ai_ready_dataset.json)
  v2/<stage>/<key>/<table>/<network>.parquet        long feature and assembled tables (publish.py)
  v2/manifests/<sha256>.json                        run manifests
  published_drydowns/morgan_2025/<commit>/          pinned published labels
```

Collection IDs and request hashes are those of the locks, so the existing scratch objects are what
`restore.py` reads. Nothing on Kopah is overwritten or deleted.

## 5. Gaps and caveats

1. **Selection inputs.** The 2026-11-01 inventory snapshot used for the legacy selection (sha256 `75c86b…`)
   is not in the repository or on Kopah as far as we know; a fresh inventory reproduces the same stations
   (see §3), but its file hash differs because the provider regenerates it daily.
2. **PET** is not yet a feature; tree cover (MOD44B) is not acquired.
3. **ISMN precipitation** is downloaded but deliberately not used (`ismn.daily_variables`).
4. **Spatial partitions** are a frozen record for this selection; they are not regenerated for new selections.
5. **linux-64** is in the lockfile but the test suite has only been run on macOS ARM64.
6. `scripts/upload_to_kopah.sh` needs `s5cmd`; `scripts/download_gleam.sh` writes to a hard-coded path and
   does not follow the cache/provenance conventions.
7. `retired/` holds the pre-refactor code and JSON specifications until they are removed.
