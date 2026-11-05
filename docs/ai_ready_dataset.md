# AI-ready data report

[data catalog](../config/ai_ready_dataset.json)

## Data Sources

Daily CONUS data covers 2002-07-04 to 2025-12-31; the [source registry](../config/source_provenance.json) records versions, retrievals, citations, hashes and rights evidence, including the [14-network reference index](../config/ismn_network_provenance.json) and [bibliography](../config/ismn_network_references.bib).

| Source/citation | Product/use | Rights/declaration status |
| --- | --- | --- |
| ISMN networks; [Dorigo et al. (2021)](https://doi.org/10.5194/hess-25-5749-2021) | SM (m³/m³), soil temperature (°C); daily means use observations flagged “good” (`G`) by ISMN, with no minimum count. | [ISMN terms](https://ismn.earth/terms-and-conditions): registered-user/associated-organisation scientific use; original-data onward transfer prohibited; cite ISMN and every contributing network ([references](../config/ismn_network_provenance.json)). |
| CHC/USGS; [Funk et al. (2015)](https://doi.org/10.1038/sdata.2015.66) | CHIRPS v2, daily 0.05°, mm/day, native Earth Engine cells. | [Provider public-domain waiver](https://www.chc.ucsb.edu/data/chirps); product/version and extract dates are recorded in the source registry. |
| NASA GSFC; [Rodell et al. (2004)](https://doi.org/10.1175/BAMS-85-3-381), [soil-map lineage](https://ldas.gsfc.nasa.gov/gldas/soils) | Native 0.25° p4/p5 static texture, sand/silt/clay fractions and porosity; file histories and Reynolds/STATSGO/FAO/Catchment ancestry recorded. | [NASA science-data policy](https://science.data.nasa.gov/about/license): no separate license in the parameter files; upstream rights remain relevant and the mission-data CC0 default is not assigned to these non-mission maps. |
| NASA LP DAAC; [Friedl and Sulla-Menashe (2022)](https://doi.org/10.5067/MODIS/MCD12Q1.061) | Annual 500 m MCD12Q1 v6.1 IGBP `LC_Type1`; fill/range decoding, QC retained. | [NASA-led-mission CC0 default](https://science.data.nasa.gov/about/license), absent a restrictive notice; no additional restriction found in checked CMR metadata. |
| NASA/Earth Engine; [Myneni et al. (2021)](https://doi.org/10.5067/MODIS/MCD15A3H.061) | Four-day 500 m MCD15A3H v6.1 LAI (m²/m²); scale 0.1, `SCF_QC` 0/1, QC retained. | Same NASA-led-mission CC0 default; DOI authors and collection policy reference are recorded. |
| [Morgan et al. (2025)](https://doi.org/10.1038/s41559-025-02810-8); [pinned dataset](https://github.com/ecohydro/ismn-drydowns/tree/ece5bbc5d741526e7440aef21a903d94d7ffc2e8) | Separate published events, fitted `q_q` labels and metadata. | Repository GPLv3 default for the contributed CSV is inferred from the pre-existing license and [2024 GitHub terms](https://raw.githubusercontent.com/github/site-policy/673c01e2bed86d4e1f62f10f1d4d00a4c4cdd867/Policies/github-terms/github-terms-of-service.md); original-provider restrictions remain separate. |

## Dimensionality and labels

The intended dimensionality is six variables per drydown event: daily sensor SM plus precipitation, PET (acquisition deferred), soil texture, land cover and LAI.

The parameter *q* is the exponent describing how soil-moisture loss varies with soil moisture.
Labels are event-level fitted *q* values supplied either by [Morgan et al.'s published dataset](../config/published_drydowns_morgan_2025.json) or fitted ourselves against our daily station SM.

## Preprocessing and inspection by dataset

### ISMN soil moisture and temperature

Daily UTC means use only observations marked “good” by ISMN's quality-control flag, keeping sensors/depths/replicates separate and masking 34.8% of raw SM and 0.2% of temperature readings.
Gaps are retained will fill values, totalling 84.1% of the full SM sensor-day grid, including inactive monitoring periods. 
The [daily SM distribution](figures/ismn_sm_distribution.png) spans 0–0.6 m³/m³ with a median of 0.19 m³/m³.
Three percent of valid sensor-days belong to constant-reading periods lasting at least three days; these periods require further inspection and possible masking.

### CHIRPS precipitation

Native daily CHIRPS precipitation is aligned without substitution or filling, retaining 0.3% missing station-days. [Rainfall is strongly right-skewed](figures/chirps_precip_distribution.png), with 75% of observed days having no rainfall and 99% at or below 29.6 mm/day. A single >600mm observation day is potentially erroneous, as preceeding and proceeding days have no rainfall. 

### GLDAS static soil properties

Native static soil properties repeat daily without normalization or filling, retaining missing texture at 0.4% of stations and missing ancillary fractions/porosity at 0.5%. Native cell compositions of sand, silt and clay do not add to 1 at 5.7% of stations.

### MODIS land cover and LAI

Annual IGBP land cover and quality-screened four-day LAI are decoded/scaled and assigned do daily records within source intervals, retaining 18.3% missing daily LAI values and <1% missing land-cover values. LAI has a median of 0.6 m²/m² and the leading CONUS land-cover classes are grassland (46.5%), savanna (16.4%), woody savanna (12.3%) and cropland (11.8%) of station-years.

## Spatial regions

The proposed split reserves southern CONUS (latitude <35.25°N) for final evaluation and divides the remainder into four spatial folds balanced by station ID: northwest (F1), southwest (F2), northeast (F3) and southeast (F4). Each cross-validation round trains on three folds and tests on the fourth, keeping all sensors, depths and dates from each station together; no separation buffer has been applied.

Station counts represent unique ISMN station IDs, with each ID weighted equally in split assignment; [config/spatial_partitions.json](../config/spatial_partitions.json) lists every region's station IDs and geographic boundaries.

| Region | Stations |
| --- | ---: |
| Southern CONUS (evaluation holdout) | 277 |
| Northwest (F1) | 275 |
| Southwest (F2) | 280 |
| Northeast (F3) | 283 |
| Southeast (F4) | 273 |
| **Total** | **1,388** |

For each region, land-cover shares are the percentages of station-year MODIS classifications (2002–2025) assigned to each class, weighted by each station's number of soil-moisture sensor streams; rows are sorted by overall share across all regions.

| IGBP land cover | South (eval) | NW (F1) | SW (F2) | NE (F3) | SE (F4) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Grassland | 32.9% | 46.3% | 45.8% | 38.9% | 39.2% |
| Savanna | 11.2% | 20.3% | 27.9% | 16.1% | 12.8% |
| Cropland | 23.3% | 3.0% | 3.5% | 21.6% | 25.4% |
| Woody savanna | 17.2% | 14.0% | 12.0% | 17.8% | 15.4% |
| Evergreen needleleaf forest | 1.2% | 14.0% | 3.6% | 0.6% | 1.4% |
| Open shrubland | 7.2% | 0.0% | 4.7% | 0.0% | 0.5% |
| Deciduous broadleaf forest | 0.8% | 0.7% | 0.0% | 3.1% | 3.1% |
| Barren | 4.3% | 0.1% | 1.6% | 0.0% | 0.0% |
| Cropland/natural mosaic | 0.8% | 0.0% | 0.0% | 1.1% | 0.8% |
| Urban/built-up | 0.6% | 0.1% | 0.5% | 0.1% | 1.3% |
| Mixed forest | 0.3% | 1.1% | 0.0% | 0.6% | 0.0% |
| Permanent wetland | 0.0% | 0.2% | 0.2% | 0.0% | 0.0% |
| Evergreen broadleaf forest | 0.0% | 0.3% | 0.0% | 0.0% | 0.0% |
| Closed shrubland | 0.1% | 0.0% | 0.1% | 0.0% | 0.0% |

Network shares report each network's percentage of SM streams within each region, rounded to the nearest tenth of a percent and sorted by overall share across all regions.

| Sensor network | South (eval) | NW (F1) | SW (F2) | NE (F3) | SE (F4) |
| --- | ---: | ---: | ---: | ---: | ---: |
| SNOTEL | 5.5% | 83.4% | 47.1% | 51.2% | 42.3% |
| SCAN | 68.4% | 10.8% | 18.0% | 21.6% | 19.8% |
| ARM | 0.8% | 0.0% | 0.0% | 0.1% | 24.6% |
| SOILSCAPE | 8.6% | 0.0% | 21.5% | 1.5% | 3.6% |
| USCRN | 10.1% | 2.7% | 1.5% | 8.0% | 4.0% |
| AWDN | 0.0% | 0.0% | 0.0% | 10.4% | 0.0% |
| ICN | 0.0% | 0.0% | 0.0% | 4.2% | 3.6% |
| CW3E | 1.0% | 1.1% | 4.6% | 1.4% | 0.0% |
| PBO_H2O | 2.0% | 1.7% | 2.1% | 0.6% | 0.6% |
| FLUXNET-AMERIFLUX | 0.0% | 0.0% | 5.0% | 0.0% | 0.0% |
| COSMOS | 1.1% | 0.2% | 0.2% | 1.0% | 0.6% |
| TxSON | 2.1% | 0.0% | 0.0% | 0.0% | 0.0% |
| iRON | 0.0% | 0.0% | 0.0% | 0.0% | 0.9% |
| USDA-ARS | 0.3% | 0.1% | 0.0% | 0.0% | 0.0% |

## Rubric score

The [AI-readiness table](../README.md#ai-readiness) in the README summarizes the current scores and remaining work.

For B2, water is the rarest observed land-cover class: **0.036% of station-years** (0.0046% with SM-stream weights), so it disappears from the rounded regional table; the [inspection summary](../output/inspection/summary.json) retains the unrounded class counts.
For B4, artificial training masks model observation outages while preserving the physical series and original missingness; precipitation/SM shuffling changes the forcing-response pairing and is therefore invalid.

For B7, existing decoding, provider-QC masks and per-day averaging are fixed/per-record operations, with no learned cross-station preprocessing statistics; inspection percentiles only flag diagnostics and do not alter measurements.
The proposed spatial split is reproducible locally, but the [published catalog](../config/ai_ready_dataset.json) currently contains no partition index.
