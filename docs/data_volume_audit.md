# Dataset data volume estimate

- **Scope:** 2002-07-04–2025-12-31; 8,582 days, 24 annual files, 1,081 LAI composites. Audited 2026-11-01.
- **Units:** Decimal GB. Full = native files; stations = remote subsets/source files plus compact extracts. ISMN is counted once; backups and temporary copies are excluded.
- **Notation:** Paired values = global / CONUS. **N = 2,920 / 1,395 stations**; **C = 2,541 / 1,395 CHIRPS-covered stations**. B = bytes.

| Dataset | Product | Full-data basis | Station-data basis | Full global GB | Full CONUS GB | Stations global GB | Stations CONUS GB |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| [ISMN](https://ismn.earth/en/networks/) | All networks; all soil-moisture depths, available temperature and precipitation | 66,775,026 / 45,249,631 sensor-days × 24 records/day × 32 B/record | Same observations | 51.283 | 34.752 | 51.283 | 34.752 |
| [GLDAS soils](https://ldas.gsfc.nasa.gov/gldas/soils) | NASA 0.25°; five static soil fields | 203,409 B texture + 1,808,905 B fractions + 239,237 B porosity | Source files + N stations × 5 values × 4 B/value | 0.002252 | 0.002252 | 0.002310 | 0.002279 |
| [CHIRPS](https://data.chc.ucsb.edu/products/CHIRPS/v2.0/global_daily/netcdf/p05/) | UCSB v2; daily, 0.05° | 24 annual files × 1.181863 GB/file | C stations × 8,582 days × 576 B/response + N stations × 8,582 days × 4 B/value | 28.365 | 28.365 | 12.661 | 6.944 |
| [dPET](https://data.bris.ac.uk/datasets/qb8ujazzda0s2aykkv0oq0ctp/) | Bristol; daily, 0.1° | 24 annual files × 2.353160 GB/file | Same annual files + N stations × 8,582 days × 4 B/value | 56.476 | 56.476 | 56.576 | 56.524 |
| [MODIS land cover](https://doi.org/10.5067/MODIS/MCD12Q1.061) | MCD12Q1 v6.1; annual, 500 m, 13 layers | 24 maps × 1.547878 / 0.194296 GB/map | N stations × 24 years × (1,587 B/response + 13 values × 1 B/value) | 37.149 | 4.663 | 0.112 | 0.054 |
| [MODIS LAI](https://doi.org/10.5067/MODIS/MCD15A2H.061) | MCD15A2H v6.1; eight-day, 500 m, six layers | 1,081 composites × 1.190749 / 0.154370 GB/composite | N stations × 1,081 composites × (755 B/response + 6 values × 1 B/value) | 1,287.200 | 166.874 | 2.402 | 1.148 |
| **Total** | | | | **1,460.475** | **291.132** | **123.037** | **99.423** |

- **Inventory:** 3,347 / 1,416 stations across 89 / 15 networks. ISMN uses each sensor's available overlap; predictor extracts span the full window.
- **Remote extraction:** CHIRPS uses [ClimateSERV](https://github.com/SERVIR/ClimateSERV/blob/master/docs/api.rst); MODIS uses [ORNL](https://modis.ornl.gov/data/modis_webservice.html). Sizes extrapolate measured single-station/date responses, including all MODIS layers. Batching may reduce overhead. dPET retains annual files in both studies.
- **Availability:** ORNL land cover was verified through 2024. Remote 2025 access via [AppEEARS](https://appeears.earthdatacloud.nasa.gov/) remains unverified; native-file fallback adds storage.
- **Coverage:** CHIRPS excludes 377 overlapping soil-moisture stations outside 50°S–50°N; their extract slots remain missing. GLDAS supplies top-layer soils; field capacity needs derivation.
- **Spatial basis:** CONUS = 48 states + DC. Native MODIS uses 125°W–66°W, 24°N–50°N: 22 tiles versus 315 global land-cover tiles. LAI sizes average four seasonal 2025 samples.
- **Uncertainty:** ISMN assumes hourly records, temperature streams matching soil-moisture streams, and one precipitation stream per reporting station. Gaps and compression are unverified. Record-width and LAI-seasonality sensitivity: full-data totals ≈1,270–1,640 / 247–336 GB.
- **Evidence:** [Metadata, sizes and provenance](data_volume_inputs.json); [network inventory](../data/ismn_network_inventory.csv). Totals use unrounded inputs; transfer headers, retries and filesystem overhead are excluded.
- **Reproduce:** `pixi install`, then `pixi run python scripts/estimate_data_volume.py` (macOS arm64).
- **Refresh:** `pixi run python scripts/estimate_data_volume.py --collect --cache-dir output/data_volume/cache --output-dir output/data_volume`. Requires internet and curl; reuses validated cache. Use a new cache directory for fresh metadata. Window and export-size samples stay fixed.
