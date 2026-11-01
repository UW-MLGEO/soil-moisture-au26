# Learning Plant Water-Use Strategies from Soil Moisture Drydowns

[Guidelines and Rubric | mlgeo-book](https://geo-smart.github.io/mlgeo-book/project-proposal-rubric/)

[Slides](https://docs.google.com/presentation/d/1hSPKWMSFsy3hVtsn3tS-oL-jPmyd6lix-e1eU74HTsY/edit?slide=id.p#slide=id.p)

## P1 Scientific Question

- **Research question:** Can vegetation water-use strategy, measured as the drydown nonlinearity q, be predicted from ecohydrological conditions at sites without soil moisture measurements?
- **Why it matters:** Transpiration is 60-80% of the total water flux from land and is a crucial part of the global water cycle. Plants regulate transpiration using different water-use strategies, which are challenging to quantify and represent a large uncertainty in ecosystem functioning.
- **Inputs:** static soil properties, precipitation, PET, land cover and leaf area index for one drydown event.
- **Target:** drydown nonlinearity estimates from observed soil moisture drydown trajectories
- **Success:** Improve drydown trajectory predictions for held-out regions relative to exponential and nonlinear baselines.

## P2 Literature Review

### A. Theoretical foundation: the staged loss function

The ecohydrological theory that defines the loss function and allowed nonlinear plant responses.

**1.** Laio, F., A. Porporato, L. Ridolfi, and I. Rodriguez-Iturbe, 2001: Plants in water-controlled ecosystems: Active role in hydrologic processes and response to water stress. II. Probabilistic soil moisture dynamics. *Adv. Water Resour.*, **24**, 707–723, https://doi.org/10.1016/S0309-1708(01)00005-7.

- Defines the staged soil moisture loss function used by every later drydown study: drainage above field capacity, maximum-rate ET above θ\*, a linear decline between θ_w and θ\*, and slow evaporation below θ_w. Built for stochastic rainfall in water-limited ecosystems.
- *Relevance:* the theory behind the q = 1 assumption that Araki et al. and Morgan et al. relax.

**2.** Porporato, A., F. Laio, L. Ridolfi, K. K. Caylor, and I. Rodriguez-Iturbe, 2003: Soil moisture and plant stress dynamics along the Kalahari precipitation gradient. *J. Geophys. Res.*, **108**, 4127, https://doi.org/10.1029/2002JD002448.

- Applies the stochastic ecohydrological framework along a strong rainfall gradient and describes vegetation water stress as a power function of available soil moisture.
- *Relevance:* nonlinear plant responses to soil moisture were in the theory two decades ago.

### B. Observed drydowns: loss functions from soil moisture data, and their scale limits

Studies that estimate drydown behavior from observations, under the linear assumption or without a functional form, and show that the result depends on sensing scale.

**3.** Shellito, P. J., et al., 2016: SMAP soil moisture drying more rapid than observed in situ following rainfall events. *Geophys. Res. Lett.*, **43**, 8068–8075, https://doi.org/10.1002/2016GL069946.

- Across 63 drydowns, SMAP's median drying timescale was 44% shorter and its drying magnitude 35% larger than upscaled in situ probes, attributed mainly to SMAP's shallower sensing depth.
- *Relevance:* first evidence that drydown parameters depend on scale and sensing depth, the main confound when comparing satellite and in situ q.

**4.** McColl, K. A., et al., 2017: Global characterization of surface soil moisture drydowns. *Geophys. Res. Lett.*, **44**, 3682–3690, https://doi.org/10.1002/2017GL072819.

- First global characterization of SMAP drydowns, fitting an exponential (q = 1) model to estimate drydown timescales and lower soil moisture bounds.
- *Relevance:* the τ-based linear model against which nonlinear fits are compared; τ from q ≠ 1 drydowns partly reflects q.

**5.** Akbar, R., D. J. Short Gianotti, K. A. McColl, E. Haghighi, G. D. Salvucci, and D. Entekhabi, 2018: Estimation of landscape soil water losses from satellite observations of soil moisture. *J. Hydrometeor.*, **19**, 871–889, https://doi.org/10.1175/JHM-D-17-0200.1.

- Estimates landscape-scale loss functions L(θ) from SMAP drydowns without assuming a functional form, and identifies regime transitions such as θ\*.
- *Relevance:* curvature in L(θ) was already visible non-parametrically; q compresses that shape into one mappable scalar.

### C. Plant water-use strategy: thresholds and isohydricity

Studies that characterize strategy through a soil moisture threshold or vegetation water dynamics, rather than the shape of the loss function.

**6.** Konings, A. G., and P. Gentine, 2017: Global variations in ecosystem-scale isohydricity. *Global Change Biol.*, **23**, 891–905, https://doi.org/10.1111/gcb.13389.

- Estimates ecosystem-scale isohydricity from microwave vegetation optical depth and finds that vegetation type alone predicts it poorly.
- *Relevance:* q targets the same strategy axis from soil moisture rather than vegetation water content, and this paper sets the expectation that strategy is not a simple function of vegetation class.

**7.** Bassiouni, M., S. P. Good, C. J. Still, and C. W. Higgins, 2020: Plant water uptake thresholds inferred from satellite soil moisture. *Geophys. Res. Lett.*, **47**, e2020GL087077, https://doi.org/10.1029/2020GL087077.

- Infers ecosystem soil moisture thresholds for plant water uptake from satellite drydowns; herbaceous vegetation keeps taking up water to drier soil than woody vegetation.
- *Relevance:* the threshold-based (θ\*) approach to water-use strategy that q extends.

### D. Nonlinear loss function as water-use strategy (nearest neighbors)

The two studies that introduce q and measure it at satellite and point scales.

**8.** Araki, R., B. E. Morgan, H. K. McMillan, and K. K. Caylor, 2025: Nonlinear soil moisture loss function reveals vegetation responses to water availability. *Geophys. Res. Lett.*, **52**, e2024GL111403, https://doi.org/10.1029/2024GL111403.

- Adds the nonlinearity parameter q and fits it to every SMAP drydown, 2015–2022. Median q = 1.76 (convex, conservative dominates); q differs significantly from 1 in fewer than 10% of events; q is nearly uncorrelated with long-term aridity (ρ = 0.04) but varies by vegetation type. The q map has no values under dense vegetation.
- *Relevance:* closest neighbor at satellite scale, and the source of the scale and identifiability questions tested here.

**9.** Morgan, B. E., R. Araki, A. T. Trugman, and K. K. Caylor, 2025: Ecological and hydroclimatic determinants of vegetation water-use strategies. *Nat. Ecol. Evol.*, https://doi.org/10.1038/s41559-025-02810-8.

- Fits q to 26,747 in situ drydowns at 1,268 ISMN stations. Grasslands are most aggressive (median q = 0.85) and woodlands most conservative (1.08); antecedent aridity and LAI explain up to 77% of the variance in binned median q, in sample.
- *Relevance:* closest neighbor at point scale, and the source of the baseline regression and label-generation code.

## P3 Nearest Neighbors

| | Morgan et al. (2025) | Araki et al. (2025) | This Project |
| --- | --- | --- | --- |
| **Data** | ISMN | SMAP L3 | ISMN in situ (USCRN, SCAN), with MODIS, CHIRPS, dPET, and GLDAS soils |
| **Scale** | Point (station) | 36km footprint, top ~5cm | Point (station) |
| **Region, period** | Global stations, 2001-2022 | Global, 2015-2022 | Global, TBD (depends on data quality) |
| **Method** | Per-event fit, then linear regression of binned medians | Per-event fit, then rank correlations | Per-event fitted q compared to Morgan et al. (2025) baseline |
| **Evaluation** | R2>0.7 for q fit; R2 on binned medians for regression | R2 and Akaike Information Criterion (AIC) | Spatial split, held-out stations |
| **Question** | What explains q? | Is the loss function nonlinear? How does q vary? | Is q predictable at sites with unmeasured SM, and where is that prediction reliable? |

Unlike Morgan et al. (2025) and Araki et al. (2025), we learn q as a predictive function of ecohydrological conditions from fitted event-level q.

## P4 Data Corpus and AI Readiness

| Dataset | Provider | Version or Product | License | One sample | Coverage |
| --- | --- | --- | --- | --- | --- |
| **ISMN** | Contributing networks, hosted by ICWRGC | [Contributing networks](https://ismn.earth/en/networks/), e.g. [USCRN Hourly02](https://www.ncei.noaa.gov/pub/data/uscrn/products/hourly02/), [AWDB hourly](https://wcc.sc.egov.usda.gov/reportGenerator/) | Registration required, no redistribution of original data. | Varies by network, e.g., USCRN reports hourly soil moisture and soil temperature at 5, 10, 20, 50 and 100 cm, precipitation, air temperature, relative humidity, solar radiation and surface temperature | Varies by network, e.g., USCRN has 139 stations in the continental US with hourly archives from 2000–present. Soil-moisture collection began in 2009 |
| **GLDAS ancillary soil properties** | NASA GSFC LDAS | Source layers 0–30 and 30–100 cm | Public domain | One soil-property grid cell provides texture and estimates field capacity | Static global coverage |
| **CHIRPS rainfall** | Climate Hazards Center, UC Santa Barbara | [CHIRPS v2.0](https://data.chc.ucsb.edu/products/CHIRPS/v2.0/global_daily/) | Public domain | Daily precipitation at 0.05° resolution | 50°S–50°N globally from 1981 to December 2026 |
| **dPET** | University of Bristol | Daily product from [linked archive](https://data.bris.ac.uk/datasets/qb8ujazzda0s2aykkv0oq0ctp/) | CC BY 4.0 | Daily potential evapotranspiration (mm/day) at 0.1° resolution | Globally from 1981–2025 |
| **MODIS land cover** | NASA MODIS | [MCD12Q1 v6.1](https://www.earthdata.nasa.gov/data/catalog/lpcloud-mcd12q1-061) | Public domain | Annual land-cover classes and quality flags at 500 m resolution | Globally from 2001–present |
| **MODIS LAI** | NASA MODIS | [MCD15A2H v6.1](https://www.earthdata.nasa.gov/data/catalog/lpcloud-mcd15a2h-061) | Public domain | LAI (m²/m²), FPAR and quality flags at 500 m resolution every 8 days | Global land from from 2002–present |

Supervised training labels are q estimates from observed soil moisture drydown trajectories, obtained by fitting a nonlinear soil moisture loss function (Morgan et al.). Input samples are static soil properties, precipitation, PET, land cover and leaf area index for one drydown event.

We will reserve one geographic region as the test set and divide the remaining regions into spatial folds for training and validation. All events, depths and years from each station belong to the same fold. The out-of-domain test set targets the spatiotemporal axis through geographic separation.

### AI readiness

| Criterion | Score | Evidence and remaining gap |
| --- | --- | --- |
| **A1 Provenance** | **1** | Providers and licenses are listed, some exact products are yet to be decided. |
| **A2 Homogenization** | **1** | Depths, resolutions and sampling intervals are reported, spatiotemporal alignment needs specification. |
| **A3 Quality and gaps** | **1** | Quality flags are identified, filtering rules and missing-value codes need specification. |
| **A4 Structure and metadata** | **0** | Data formats and schemas are not yet standardized across the corpus. |
| **B1 Diversity** | **2** | Multiple networks and decades offer broad coverage. |
| **B2 Distribution** | **0** | Data not yet studied. |
| **B3 Noise** | **0** | Data not yet studied. |
| **B4 Augmentation** | **0** | Data transformations not yet applied. |
| **B5 Labels and targets** | **1** | Labels are specified but need to be derived. |
| **B6 Dimensionality** | **2** | Each sample represents one station drydown, with a dimension per predictor (~5 planned predictors). |
| **B7 Partitioning** | **2** | Plan specifies station grouping, a regional test set and spatial validation folds. |

We plan to address A4 by combining source products into a tabular dataset with one row per drydown event. The schema will define column names, units and missing-value conventions. We plan to address B2 by examining the distribution of environmental predictors and drydown events across regions. This will help balance the data splits and identify poorly represented predictors or regions.

## P5 Reproducibility

[GitHub repo](https://github.com/UW-MLGEO/soil-moisture-au26), [AGENTS.md](https://github.com/UW-MLGEO/soil-moisture-au26/blob/main/AGENTS.md)

## P6 Plan and Roles

**Baseline:** nonlinear least squares fit replicating Morgan et al. (2025)

**Machine learning approach:** random forest or gradient-boosted trees to predict each station’s fitted q

**Metric:** RMSE of q; or RMSE of SM(t) on held-out drydowns using the predicted q to integrate the drydown

**Fallback:** keep the pipeline and choose a simpler, more robust target (e.g., classify drydowns as linear/nonlinear, estimate drydown e-folding timescale)

**Timeline:**

| Week | Deliverables |
| --- | --- |
| 1, Nov 2-8 | Download and quality-control data; extract drydowns; assemble predictors |
| 2, Nov 9-15 | Replicate Morgan et al. (2025) baseline fits of q |
| 3, Nov 16-22 | Baseline regression; ML approach |
| 4, Nov 23-29 | Out-of-domain tests, comparison to baseline |
| 5, Nov 30-Dec 6 | Write the final report |
| 6 Dec 7- | Develop and practice presentation |

**Roles:**

| Member | Lead |
| --- | --- |
| Ben | Conceptualization; Methodology (drydown model); Formal analysis; Writing |
| Megan | Data preparation and QC; ML modeling; Reproducibility; Writing |

## P8 AI Disclosure and Reflection

*Ben:* Guided by my knowledge of ecohydrology and soil moisture dynamics, I used Claude to iterate and brainstorm the research question and feasibility of the ML approach. I would read papers, then provide them to Claude and ask questions to improve my understanding. Once the research question was chosen and I selected the core papers motivating this question, Claude was used to generate an annotated bibliography for the literature review. I read each paper to ensure the key points and interpretations were correct.

*Megan:* I used ChatGPT to perform a broad search for literature related to SM drydown to understand the space of related work, and I asked it to summarize key takeaways from each work. For our two nearest neighbors, I read over the papers while prompting ChatGPT to help me understand the science and methods. For the data corpus, I prompted Codex to identify dataset sources and metadata from Morgan et al., which I verified by navigating to the data provider pages it surfaced. To assess AI readiness, I pointed Codex to the finalized data corpus and AI-ready data rubric and asked it to produce a score and one sentence covering evidence and the remaining gap. I adjusted the evidence where I felt it was too vague and kept the scores in most cases.
