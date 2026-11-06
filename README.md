# soil-moisture-au26

A reproducible workflow that turns in situ ISMN soil moisture and gridded
ecohydrological products (CHIRPS precipitation, GLDAS soil properties, MODIS
LAI and land cover) into one daily, per-sensor, ML-ready table. Raw data and
outputs live in Kopah S3; every acquisition and table is identified by SHA-256.

**Every scientific choice and threshold is in [config/pipeline.yml](config/pipeline.yml)**,
one section per source or stage, with a unit and a reason on every key
(rendered as a table in [docs/decisions.md](docs/decisions.md)). The scripts in
`scripts/` read as the methods: each numbered block states the rule it applies
and logs the values used. [docs/workflow.md](docs/workflow.md) explains the
design, provider quirks and the Kopah layout.

## quick start

```sh
git clone --recurse-submodules https://github.com/UW-MLGEO/soil-moisture-au26.git
cd soil-moisture-au26
pixi install
pixi run test        # unit tests (synthetic data, offline)
pixi run smoke       # whole pipeline on a synthetic 2-station selection (< 1 min)
```

To rebuild the published tables from the raw data already on Kopah (needs only the
`kopah` AWS profile; set up per the [Kopah instructions](https://github.com/gaia-hazlab/skypilot-hyak/blob/main/docs/kopah.md)):

```sh
pixi run restore                    # ~3.2 GB of ISMN archives and station extracts into output/
pixi run homogenize                 # feature tables + sensor_day (offline; roughly 30 min, extrapolated from the ARM pilot)
pixi run inspect                    # candidate problems, distributions, report figures
pixi run export-drydown-q           # optional: drydown-q's sensors/station_day tables
```

For a pilot, use the ARM network only: `pixi run restore pilot_arm`, `pixi run homogenize pilot_arm`, and so on.
The locked environment covers `osx-arm64` and `linux-64` (Hyak/klone). On klone, run homogenization in a Slurm job, not on a login node.

## workflow

Each stage's cache key is the SHA-256 of its config sections, its inputs and its
code, so a stage reruns exactly when one of those changes and is reused otherwise.

| # | Stage | Command | Reads | Config | Writes | Kopah | Network |
| --- | --- | --- | --- | --- | --- | --- | --- |
| −1 | Select | `pixi run select` | ISMN station inventory; region boundary (cached, hashed) | `study`, `selection`, [regions.yml](config/regions.yml) | `data/selections/<name>/` CSVs (commit) | — | ISMN, Census |
| 0 | Plan | `pixi run plan <source>` | selection, `config/sources/<source>.yml` | `study`, `<source>` | `locks/<source>.json` + `.summary.yml` (commit = approval) | — | none |
| 1 | Acquire | `pixi run acquire <source>` | committed lock; provider | `<source>`, [storage.yml](config/storage.yml) | `output/{ismn,ancillary}/<collection_id>/` | scratch (raw + extracts) | provider, Kopah |
| 1′ | Restore | `pixi run restore` | Kopah scratch | storage.yml | same as stage 1 | read only | Kopah |
| 2 | Features | `pixi run homogenize` (steps 1–5) | stage 1 caches | one section per source | `output/cache/features/<group>/<key>/` | — | none |
| 3 | Assemble | `pixi run homogenize` (step 6) | stage 2 tables | `features` | `output/cache/assemble/<key>/`, `output/runs/<run>/manifest.json` | — | none |
| 4 | Inspect | `pixi run inspect` | stage 2–3 tables | `inspection` | `output/cache/inspect/<key>/`, `docs/figures/` | — | none |
| 5 | Publish | `pixi run publish` | run manifest | storage.yml | — | processed `v2/<stage>/<key>/`, `v2/manifests/` | Kopah |
| 6 | Export | `pixi run export-drydown-q` | stage 3 tables | `export_drydown_q` | `output/drydown_q/<run>/` | — | none |

`<source>` is one of `ismn`, `chirps`, `gldas`, `modis_lai`, `modis_land_cover`, `morgan_2025`.
ISMN exports are asynchronous: `pixi run python scripts/acquire.py ismn --action submit`, then
`--action collect --wait`. Acquisition credentials are listed in [config/storage.yml](config/storage.yml).

### where things are

| Path | Holds | Edited by |
| --- | --- | --- |
| [config/pipeline.yml](config/pipeline.yml) | every scientific choice and threshold | you (prefer a run file) |
| [config/runs/](config/runs/) | named runs that override a few keys (`baseline`, `pilot_arm`, `global`, `smoke`) | you |
| [config/sources/](config/sources/) | provider facts, provenance, request wording | rarely |
| [config/storage.yml](config/storage.yml), `config/paths.yml` | Kopah locations, transfer limits; local paths (copy [paths.example.yml](config/paths.example.yml)) | you |
| [data/selections/](data/selections/) | frozen station/sensor lists (CSV) | `select_stations.py` |
| [locks/](locks/) | the exact provider requests (SHA-256 = acquisition identity) | `plan.py` |
| [records/](records/) | published catalog, rights and citation evidence, spatial partitions | by hand, rarely |
| `output/` (git-ignored) | caches, stage outputs, run manifests | the scripts |

### output tables

All dates are UTC calendar days; units are in column names; schemas are checked on write ([schemas.py](src/sm_drydown/schemas.py)).

- `sensor_day` (stage 3): one row per soil-moisture sensor per day within the sensor's observed span —
  `sensor_id, station_id, network, date`, then each feature in `pipeline.yml features` order followed by
  `<feature>__missing_reason`, then `n_obs, n_good, n_valid` (ISMN records, kept-flag records, accepted values).
- `stations`, `sensors` (stage 3): coordinates, native cells used per gridded source; depth, instrument and per-sensor masking counts.
- Feature tables (stage 2): `theta_m3m3`, `soil_temp_degc`, `precip_chirps_mm_day`, `gldas_static`,
  `lai_m2m2`, `igbp_class`, plus `ismn_qc_audit` (daily flag histograms) and raw `qc_*` words.

Nothing is filled or interpolated. Every missing value has a reason
(`no_observations`, `quality_masked`, `provider_null`, `missing_qc`, `outside_source_coverage`, …; see
[features/common.py](src/sm_drydown/features/common.py)).

### adding a feature

1. Provenance and provider facts in `config/sources/<source>.yml`; a `plan`/`acquire` path if it is a new download.
2. Its rules in a new `pipeline.yml` section, and its name in `features:`.
3. A `build(cfg, inputs, stations, dates)` function in its own module under `src/sm_drydown/features/`, and entries in `GROUPS` and `FEATURES` ([features/__init__.py](src/sm_drydown/features/__init__.py)).
4. A schema in [schemas.py](src/sm_drydown/schemas.py) and one step block in [scripts/homogenize.py](scripts/homogenize.py).

Existing feature caches are reused; `tests/test_pipeline.py` shows a minimal example.

### verification

`pixi run test` covers config validation, request rebuilding (the committed locks must equal the request rebuilt
from config, byte for byte), selection rules, every QC/missing-value rule, stage caching, assembly, inspection
checks, provider clients (mocked) and the guards that keep numbers out of the code and every setting documented.
`pixi run smoke` runs the real scripts end to end on synthetic data.
[scripts/check_equivalence.py](scripts/check_equivalence.py) compares new tables with tables built by the
pre-refactor code (done for the ARM network: identical).

## uploading data to kopah from Hyak

Clone [skypilot-hyak](https://github.com/gaia-hazlab/skypilot-hyak) separately and follow its setup.
[scripts/upload_to_kopah.sh](scripts/upload_to_kopah.sh) is a minimal example for fetching HTTPS files into Kopah through Hyak
(needs `s5cmd`, not a Pixi dependency):

```sh
cd /path/to/skypilot-hyak
pixi run ssh klone-login \
  'bash -s -- "https://provider.example/file.nc" "s3://gaia/scratch/ml4geo_sm_drydown/dataset/version/file.nc" "/tmp/provider-cache/file.nc"' \
  < /path/to/soil-moisture-au26/scripts/upload_to_kopah.sh
```

## AI-ready dataset

[records/ai_ready_dataset.json](records/ai_ready_dataset.json) describes two separate
components which are stored in a Kopah bucket at `s3://gaia/ml4geo_sm_drydown/`:

- `daily/`: daily station feature tables built by the pre-refactor `homogenize_data.py` (v1; the refactored
  pipeline reproduces them, see Verification, and publishes long tables under `v2/`).
- `published_drydowns/morgan_2025/`: the unchanged published Morgan event table
  and fitted labels.

The data corpus lives in Kopah bucket `gaia`, with raw acquisitions under `s3://gaia/scratch/ml4geo_sm_drydown/` and AI-ready tables and published Morgan labels under `s3://gaia/ml4geo_sm_drydown/`.

| Source and citation | Version / retrieval | License or rights | Provider access |
| --- | --- | --- | --- |
| ISMN SM, temperature and precipitation; [Dorigo et al. (2021)](https://doi.org/10.5194/hess-25-5749-2021), [network references](records/ismn_network_provenance.json) | Exported 2026-11-04 | [ISMN terms](https://ismn.earth/terms-and-conditions): registered-user/organisation scientific use; no onward transfer of original data; cite contributing networks. | Free ISMN account |
| CHIRPS daily precipitation; [Funk et al. (2015)](https://doi.org/10.1038/sdata.2015.66) | v2.0 | [Provider public-domain waiver](https://www.chc.ucsb.edu/data/chirps) | Earth Engine authentication and project |
| GLDAS static soil properties; [Rodell et al. (2004)](https://doi.org/10.1175/BAMS-85-3-381), [soil-map lineage](https://ldas.gsfc.nasa.gov/gldas/soils) | p4/p5, 0.25°; retrieved 2026-11-04 | No file-level license declared; [NASA policy](https://science.data.nasa.gov/about/license) and upstream source rights apply; CC0 is not assumed. | Public HTTPS |
| MODIS land cover; [Friedl and Sulla-Menashe (2022)](https://doi.org/10.5067/MODIS/MCD12Q1.061) | MCD12Q1 v6.1 | [NASA mission-data CC0 default](https://science.data.nasa.gov/about/license); no additional restriction found. | Earthdata Login for LP DAAC |
| MODIS LAI; [Myneni et al. (2021)](https://doi.org/10.5067/MODIS/MCD15A3H.061) | MCD15A3H v6.1, four-day composites | NASA mission-data CC0 default; no additional restriction found. | Earth Engine authentication and project |
| Published drydowns and fitted labels; [Morgan et al. (2025)](https://doi.org/10.1038/s41559-025-02810-8) | [Pinned commit `ece5bbc5d741`](https://github.com/ecohydro/ismn-drydowns/tree/ece5bbc5d741526e7440aef21a903d94d7ffc2e8) | Repository GPLv3 coverage of the CSV is inferred; original-provider restrictions remain applicable. | Public GitHub |

This project adds no synthetic observations or augmentation.
[Source provenance](records/source_provenance.json) records retrievals, rights evidence and full citations, including the contributing ISMN networks.


## AI readiness

The [AI-ready dataset report](docs/ai_ready_dataset.md) documents our preprocessing, data quality and distribution analyses, and proposed spatial partitions; see also our [reflection](docs/ai_readiness_reflection.md) and [rubric critique](docs/ai_readiness_critique.md). The following table summarizes the corpus’s current AI readiness against the [project rubric](https://geo-smart.github.io/mlgeo-book/final-project-assignement/).

| Criterion | Score / 2 | Evidence and remaining work |
| --- | ---: | --- |
| A1 Provenance | 2 | The [source registry](records/source_provenance.json) and network bibliography record origins, versions, retrievals, citations and declared rights with auditable scope evidence. |
| A2 Homogenization | 2 | The [daily workflow](docs/ai_ready_dataset.md#preprocessing-and-inspection-by-dataset) standardizes units, coordinates and UTC sampling while preserving sensor identities. |
| A3 Quality and gaps | 1 | Quality flags, masking percentages, explicit gaps and candidate rainfall, soil and flatline anomalies are documented. Resolve remaining anomalies and document detection limits and gap causes where available. |
| A4 Structure and metadata | 2 | Parquet/JSON station bundles and the [catalog](records/ai_ready_dataset.json) describe shapes, dtypes, units, hashes and missingness. |
| B1 Diversity | 2 | Catalog and regional tables distinguish station IDs, networks, years and sensor streams. |
| B2 Distribution | 2 | [Inspection summaries](output/inspection/summary.json) and regional tables report input distributions, rare classes, tails and geographic composition shifts. |
| B3 Noise characterization | 0 | No quantified instrumental-noise estimate separated from natural variability. Add a defensible noise or SNR estimate with calibration evidence and assumptions. |
| B4 Augmentation potential | 2 | Training-only masking preserves reconstruction targets, whereas independently shuffling precipitation and SM breaks forcing-response alignment. Apply and validate augmentation only if needed for modeling. |
| B5 Labels | 1 | [Event-level fitted q labels](docs/ai_ready_dataset.md#dimensionality-and-labels) quantify soil-moisture loss nonlinearity and are supplied by the pinned Morgan dataset; we can also fit our daily station SM for comparison. Link labels to ancillary variables and assess fit quality, uncertainty and agreement between sources. |
| B6 Dimensionality | 2 | The [defined dimensionality](docs/ai_ready_dataset.md#dimensionality-and-labels) is six variables per sensor-day: daily sensor SM plus precipitation, PET, soil texture, land cover and LAI. |
| B7 Partitioning | 2 | [Station-ID membership](docs/ai_ready_dataset.md#spatial-regions) defines four spatial folds and a southern holdout with verification that station IDs and GLDAS are non-overlapping. |
| **Total** | **18 / 22** | |

## AI use disclosure

We used Codex (GPT 6.1) to draft and revise data-acquisition, homogenization and inspection code, tests, documentation and rubric assessments. Claude (Opus 5.5) helped review acquisition and homogenization code to identify bugs. We provided datasets and processing policies, reviewed outputs and specified corrections for identified bugs. Claude (Opus 5.5) drafted the config-driven refactor (pipeline.yml, plan locks, feature tables, scripts, tests and documentation).

## earth science topics

Megan
- Satellite remote sensing of soil moisture.
- Spatiotemporal fusion of satellite and in situ obervations.
- Uncertainty propagation and calibration in environmental models.

Ben
- The role of plants and land-atmosphere feedbacks in terrestrial hydroclimate.
- Characterizing uncertainty in Earth system models.
- Global carbon cycle dynamics.

## license

This repository is licenced under the [MIT License](LICENSE) to make this code easy to reuse, modify and share.

## literature review

- Yu et al. (2025), [“Spatial Soil Moisture Prediction From In Situ Data Upscaled to Landsat Footprint: Assessing Area of Applicability of Machine Learning Models”](https://doi.org/10.1109/TGRS.2025.3565818), *IEEE TGRS*, 63. The study combines machine learning and spatiotemporal fusion to upscale in situ soil moisture to the Landsat footprint, showing that predictions within the models’ area of applicability have lower uncertainty.
- Lesinger & Tian (2025), [“Skillful subseasonal soil moisture drought forecasts with deep learning-dynamic models”](https://doi.org/10.1038/s41467-025-62761-3), *Nature Communications*, 16(1), 7461. This study develops a hybrid model combining a deep learning model with subseasonal forecasts from dynamical models to predict root-zone soil moisture. Compared to ECMWF and GEFS models, the hybrid model generally shows greater skill in predicting flash droughts at greater than two week lead times.
