# soil-moisture-au26

## environment

Install the Pixi environment:

```sh
pixi install
```

Activate it with `pixi shell`, or run Python directly with `pixi run python`.

## literature review

- Yu et al. (2025), [“Spatial Soil Moisture Prediction From In Situ Data Upscaled to Landsat Footprint: Assessing Area of Applicability of Machine Learning Models”](https://doi.org/10.1109/TGRS.2025.3565818), *IEEE TGRS*, 63. The study combines machine learning and spatiotemporal fusion to upscale in situ soil moisture to the Landsat footprint, showing that predictions within the models’ area of applicability have lower uncertainty.
- Lesinger & Tian (2025), [“Skillful subseasonal soil moisture drought forecasts with deep learning-dynamic models”](https://doi.org/10.1038/s41467-025-62761-3), *Nature Communications*, 16(1), 7461. This study develops a hybrid model combining a deep learning model with subseasonal forecasts from dynamical models to predict root-zone soil moisture. Compared to ECMWF and GEFS models, the hybrid model generally shows greater skill in predicting flash droughts at greater than two week lead times.
