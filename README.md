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

To run notebooks in `notebooks/`, point the Jupyter kernel to `.pixi/envs/default/bin/python`. 

Run an environment smoke test from the repository root:

```sh
pixi run smoke
```

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
