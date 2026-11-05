# soil-moisture-au26

## environment

Clone the repository and set up the [Pixi](https://pixi.sh) environment:

```sh
git clone --recurse-submodules https://github.com/UW-MLGEO/soil-moisture-au26.git
cd soil-moisture-au26
git submodule update --init --recursive
pixi install
pixi shell
```

The locked environment targets macOS ARM64. The upstream `drydowns` and `ecflux`
submodules remain pinned for future work; the current workflows acquire and
homogenize data without running drydown detection or fitting.

Run the offline suite and environment smoke test from the repository root:

```sh
pixi run test
pixi run smoke
```

The suite groups checks into downloads/storage, native grids, Earth Engine,
homogenization and inspection, using synthetic fixtures and mocked providers
without credentials or downloads. It checks cache repair and immutable uploads,
raw-value preservation, missing/QC handling, daily means and source intervals,
diagnostic boundaries and spatial station assignments; the environment smoke
check also runs during test discovery.

## uploading data to kopah

Clone [skypilot-hyak](https://github.com/gaia-hazlab/skypilot-hyak) separately and follow its setup.

Kopah Project storage is under `s3://gaia/ml4geo_sm_drydown/` and `s3://gaia/scratch/ml4geo_sm_drydown/`. Follow the [Kopah setup instructions](https://github.com/gaia-hazlab/skypilot-hyak/blob/main/docs/kopah.md) to configure access to these locations.

[scripts/upload_to_kopah.sh](scripts/upload_to_kopah.sh) is a minimal example for fetching HTTPS files into Kopah through Hyak. 
If you need to configure provider-specific authentication, use provider API calls or want a more robust pipeline (e.g. checksum validation, resuming partial downloads), you will probably want to write your own Python script instead.

```sh
cd /path/to/skypilot-hyak
ssh klone-login
pixi run ssh klone-login \
  'bash -s -- "https://provider.example/file.nc" "s3://gaia/scratch/ml4geo_sm_drydown/dataset/version/file.nc" "/tmp/provider-cache/file.nc"' \
  < /path/to/soil-moisture-au26/scripts/upload_to_kopah.sh
```

## AI-ready dataset

[config/ai_ready_dataset.json](config/ai_ready_dataset.json) describes two separate
components which are stored in a Kopah bucket at `s3://gaia/ml4geo_sm_drydown/`:

- `daily/`: daily station feature tables built by `scripts/homogenize_data.py`.
- `published_drydowns/morgan_2025/`: the unchanged published Morgan event table
  and fitted labels.

The data corpus lives in Kopah bucket `gaia`, with raw acquisitions under `s3://gaia/scratch/ml4geo_sm_drydown/` and AI-ready tables and published Morgan labels under `s3://gaia/ml4geo_sm_drydown/`.

| Source and citation | Version / retrieval | License or rights | Provider access |
| --- | --- | --- | --- |
| ISMN SM, temperature and precipitation; [Dorigo et al. (2021)](https://doi.org/10.5194/hess-25-5749-2021), [network references](config/ismn_network_provenance.json) | Exported 2026-11-04 | [ISMN terms](https://ismn.earth/terms-and-conditions): registered-user/organisation scientific use; no onward transfer of original data; cite contributing networks. | Free ISMN account |
| CHIRPS daily precipitation; [Funk et al. (2015)](https://doi.org/10.1038/sdata.2015.66) | v2.0 | [Provider public-domain waiver](https://www.chc.ucsb.edu/data/chirps) | Earth Engine authentication and project |
| GLDAS static soil properties; [Rodell et al. (2004)](https://doi.org/10.1175/BAMS-85-3-381), [soil-map lineage](https://ldas.gsfc.nasa.gov/gldas/soils) | p4/p5, 0.25°; retrieved 2026-11-04 | No file-level license declared; [NASA policy](https://science.data.nasa.gov/about/license) and upstream source rights apply; CC0 is not assumed. | Public HTTPS |
| MODIS land cover; [Friedl and Sulla-Menashe (2022)](https://doi.org/10.5067/MODIS/MCD12Q1.061) | MCD12Q1 v6.1 | [NASA mission-data CC0 default](https://science.data.nasa.gov/about/license); no additional restriction found. | Earthdata Login for LP DAAC |
| MODIS LAI; [Myneni et al. (2021)](https://doi.org/10.5067/MODIS/MCD15A3H.061) | MCD15A3H v6.1, four-day composites | NASA mission-data CC0 default; no additional restriction found. | Earth Engine authentication and project |
| Published drydowns and fitted labels; [Morgan et al. (2025)](https://doi.org/10.1038/s41559-025-02810-8) | [Pinned commit `ece5bbc5d741`](https://github.com/ecohydro/ismn-drydowns/tree/ece5bbc5d741526e7440aef21a903d94d7ffc2e8) | Repository GPLv3 coverage of the CSV is inferred; original-provider restrictions remain applicable. | Public GitHub |

This project adds no synthetic observations or augmentation.
[Source provenance](config/source_provenance.json) records retrievals, rights evidence and full citations, including the contributing ISMN networks.


## AI readiness

The [AI-ready dataset report](docs/ai_ready_dataset.md) documents our preprocessing, data quality and distribution analyses, and proposed spatial partitions; see also our [reflection](docs/ai_readiness_reflection.md) and [rubric critique](docs/ai_readiness_critique.md). The following table summarizes the corpus’s current AI readiness against the [project rubric](https://geo-smart.github.io/mlgeo-book/final-project-assignement/).

| Criterion | Score / 2 | Evidence and remaining work |
| --- | ---: | --- |
| A1 Provenance | 2 | The [source registry](config/source_provenance.json) and network bibliography record origins, versions, retrievals, citations and declared rights with auditable scope evidence. |
| A2 Homogenization | 2 | The [daily workflow](docs/ai_ready_dataset.md#preprocessing-and-inspection-by-dataset) standardizes units, coordinates and UTC sampling while preserving sensor identities. |
| A3 Quality and gaps | 1 | Quality flags, masking percentages, explicit gaps and candidate rainfall, soil and flatline anomalies are documented. Resolve remaining anomalies and document detection limits and gap causes where available. |
| A4 Structure and metadata | 2 | Parquet/JSON station bundles and the [catalog](config/ai_ready_dataset.json) describe shapes, dtypes, units, hashes and missingness. |
| B1 Diversity | 2 | Catalog and regional tables distinguish station IDs, networks, years and sensor streams. |
| B2 Distribution | 2 | [Inspection summaries](output/inspection/summary.json) and regional tables report input distributions, rare classes, tails and geographic composition shifts. |
| B3 Noise characterization | 0 | No quantified instrumental-noise estimate separated from natural variability. Add a defensible noise or SNR estimate with calibration evidence and assumptions. |
| B4 Augmentation potential | 2 | Training-only masking preserves reconstruction targets, whereas independently shuffling precipitation and SM breaks forcing-response alignment. Apply and validate augmentation only if needed for modeling. |
| B5 Labels | 1 | [Event-level fitted q labels](docs/ai_ready_dataset.md#dimensionality-and-labels) quantify soil-moisture loss nonlinearity and are supplied by the pinned Morgan dataset; we can also fit our daily station SM for comparison. Link labels to ancillary variables and assess fit quality, uncertainty and agreement between sources. |
| B6 Dimensionality | 2 | The [defined dimensionality](docs/ai_ready_dataset.md#dimensionality-and-labels) is six variables per sensor-day: daily sensor SM plus precipitation, PET, soil texture, land cover and LAI. |
| B7 Partitioning | 2 | [Station-ID membership](docs/ai_ready_dataset.md#spatial-regions) defines four spatial folds and a southern holdout with verification that station IDs and GLDAS are non-overlapping. |
| **Total** | **18 / 22** | |

## AI use disclosure

We used Codex (GPT 6.1) to draft and revise data-acquisition, homogenization and inspection code, tests, documentation and rubric assessments. Claude (Opus 5.5) helped review acquisition and homogenization code to identify bugs. We provided datasets and processing policies, reviewed outputs and specified corrections for identified bugs.

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
