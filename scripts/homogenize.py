"""Stages 2-3: acquisition caches -> per-feature tables -> the ML-ready sensor_day table.

    pixi run homogenize                       # baseline run
    pixi run python scripts/homogenize.py --run pilot_arm
    pixi run python scripts/homogenize.py --run smoke --paths config/paths.smoke.yml

This file is the methods section. Each numbered block states the rule it applies
(values logged from the resolved config), calls one library function per ISMN
network, and logs what was kept, masked or missing. Reads only local caches
(paths.ismn_cache, paths.ancillary_cache; fill them with `pixi run restore` or
`pixi run acquire`); no network access. Every stage output is content-addressed
under <outputs_dir>/cache/ and reused when its config, inputs and code are
unchanged. Writes <outputs_dir>/runs/<run>/{manifest.json, config.resolved.yml, homogenize.log}.
"""

import argparse
import logging
import time
from pathlib import Path

from sm_drydown.assemble import sensor_day, stations_table
from sm_drydown.cache import Stage, stage_key
from sm_drydown.config import LOCKS_DIR, code_hash, differences_from_default, load_config, write_resolved
from sm_drydown.features import FEATURES, GROUPS
from sm_drydown.features.inputs import Inputs
from sm_drydown.features.common import daily_index
from sm_drydown.manifest import write_manifest
from sm_drydown.selections import load_selection

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--run', default='baseline', help='run file config/runs/<run>.yml')
parser.add_argument('--paths', default=None, help='paths file (default config/paths.yml, else paths.example.yml)')
parser.add_argument('--locks-dir', type=Path, default=LOCKS_DIR)
args = parser.parse_args()
started = time.time()

# ── Step 0. Configuration, selection and inputs ───────────────────────────────
cfg = load_config(args.run, args.paths)
run_dir = Path(cfg.paths.outputs_dir) / 'runs' / cfg.run_name
run_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=cfg.runtime.log_level, format='%(asctime)s %(message)s', datefmt='%H:%M:%S', force=True,
                    handlers=[logging.StreamHandler(), logging.FileHandler(run_dir / 'homogenize.log', mode='w')])
log = logging.getLogger('homogenize')
write_resolved(cfg, run_dir / 'config.resolved.yml')
for key, (default, value) in differences_from_default(cfg).items():
    log.info('setting differs from pipeline.yml: %s = %r (default %r)', key, value, default)
selection = load_selection(cfg.selection.name)
stations = selection.subset(cfg.subset.networks, cfg.subset.station_ids)
networks = sorted({s['network'] for s in stations})
dates = daily_index(cfg.study.start, cfg.study.end)
unknown = [f for f in cfg.features if f not in FEATURES]
if unknown:
    raise SystemExit(f'pipeline.yml features without a builder: {unknown}')
log.info('step 0: selection %s: %d stations in %d networks; %s to %s (%d UTC days)', selection.name,
         len(stations), len(networks), cfg.study.start, cfg.study.end, len(dates))


def run_group(name):
    """Build (or reuse) one feature group, network by network."""
    group = GROUPS[name]
    inputs = Inputs(cfg, group, args.locks_dir)
    sections = [getattr(cfg, s) for s in group.sections]
    subset = cfg.subset.station_ids if cfg.subset.station_ids != 'all' else None
    stage = Stage(cfg.paths.outputs_dir, f'features/{name}', stage_key(
        name, *sections, inputs.hashes, subset, code_hash(*group.code)))
    for network in networks:
        partition_inputs = inputs.network_hashes(network)
        if stage.done(network, partition_inputs):
            log.info('    %s/%s: reused %s', name, network, stage.key[:12])
            continue
        members = [s for s in stations if s['network'] == network]
        stage.write(network, group.build(cfg, inputs, members, dates), partition_inputs)
        log.info('    %s/%s: built %d stations', name, network, len(members))
    return stage, inputs


def counts(stage, table, reason='missing_reason'):
    frame = stage.read(table, networks, columns=[reason])
    return frame[reason].value_counts().to_dict() if frame is not None else {}


stages, catalogs, locks = {}, {}, {}

# ── Step 1. ISMN soil moisture and soil temperature ───────────────────────────
# Records outside the study window are dropped; NaN and ismn.missing_codes are
# missing; only ISMN flags in ismn.flags_kept are accepted; the daily value is the
# ismn.daily_statistic of accepted values on each UTC day with at least
# ismn.min_obs_per_day of them. Temperature in K is converted to degC.
i = cfg.ismn
log.info('step 1: ISMN %s: flags kept %s (temperature %s); daily %s of accepted values, >= %d per day; rows: %s',
         i.daily_variables, i.flags_kept, i.temperature_flags_kept, i.daily_statistic, i.min_obs_per_day,
         i.sensor_day_span)
stages['ismn'], inputs = run_group('ismn')
locks['ismn'] = inputs.request_path
log.info('    theta_m3m3 sensor-days by missing_reason: %s', counts(stages['ismn'], 'theta_m3m3'))

# ── Step 2. CHIRPS precipitation ──────────────────────────────────────────────
# Native 0.05 deg pixel containing the station; one value per UTC day, no
# averaging. Provider nulls and absent rows stay missing (never zero rain).
log.info('step 2: CHIRPS %s band %s; null is zero rain: %s; absent row is zero rain: %s', cfg.chirps.collection,
         cfg.chirps.band, cfg.chirps.null_is_zero_rain, cfg.chirps.absent_row_is_zero_rain)
stages['chirps'], inputs = run_group('chirps')
catalogs['chirps'], locks['chirps'] = inputs.catalog, inputs.request_path
log.info('    station-days by missing_reason: %s', counts(stages['chirps'], 'precip_chirps_mm_day'))

# ── Step 3. GLDAS static soil properties ──────────────────────────────────────
# Nearest 0.25 deg cell (ties to the larger coordinate). CF missing codes decoded;
# the NetCDF default fill only when verified. Percent -> fraction; values outside
# gldas.fraction_bounds are an error; texture codes are snapped to the nearest
# FAO class within gldas.texture_snap_tolerance.
g = cfg.gldas
log.info('step 3: GLDAS %s; percent to fraction %s; bounds %s; texture snap tolerance %g', g.depth_assignment,
         g.percent_to_fraction, g.fraction_bounds, g.texture_snap_tolerance)
stages['gldas'], inputs = run_group('gldas')
catalogs['gldas'], locks['gldas'] = inputs.catalog, inputs.request_path

# ── Step 4. MODIS land cover ──────────────────────────────────────────────────
# Annual IGBP class (modis_land_cover.scheme) at the containing 500 m pixel,
# valid only within its own calendar year; fill and out-of-range codes missing;
# QC recorded (qc_* columns) but not used to mask.
lc = cfg.modis_land_cover
log.info('step 4: land cover %s %s; QC mode %s (bands %s)', lc.collection, lc.scheme, lc.qc_mode, lc.qc_bands)
stages['modis_land_cover'], inputs = run_group('modis_land_cover')
catalogs['modis_land_cover'], locks['modis_land_cover'] = inputs.catalog, inputs.request_path
log.info('    station-days by missing_reason: %s', counts(stages['modis_land_cover'], 'igbp_class'))

# ── Step 5. MODIS LAI ─────────────────────────────────────────────────────────
# 4-day composites (cut at 1 January), each value valid only within its own
# [start, end) interval. Fill/valid-range masked before scale_factor. QC rules on
# modis_lai.qc_rules; missing QC is never assumed good.
lai = cfg.modis_lai
log.info('step 5: LAI %s band %s; QC %s %s; missing QC is good: %s', lai.collection, lai.band, lai.qc_mode,
         [(r.column, r.offset, r.mask, r.allowed) for r in lai.qc_rules], lai.missing_qc_is_good)
stages['modis_lai'], inputs = run_group('modis_lai')
catalogs['modis_lai'], locks['modis_lai'] = inputs.catalog, inputs.request_path
log.info('    station-days by missing_reason: %s', counts(stages['modis_lai'], 'lai_m2m2'))

# ── Step 6. Assemble sensor_day, stations and sensors ─────────────────────────
# One row per soil-moisture sensor-day; station features joined on (station_id,
# date), static features on station_id, in the order of pipeline.yml features.
log.info('step 6: assemble %s', cfg.features)
assemble = Stage(cfg.paths.outputs_dir, 'assemble', stage_key(
    'assemble', cfg.features, {n: s.key for n, s in stages.items()}, cfg.subset, code_hash('assemble.py')))
for network in networks:
    if assemble.done(network):
        continue
    tables = {t: stages[g].read(t, [network]) for g in stages for t in GROUPS[g].tables}
    members = [s for s in stations if s['network'] == network]
    sensors = tables['ismn_sensors']
    assemble.write(network, {'sensor_day': sensor_day(cfg, tables, network),
                             'stations': stations_table(members, catalogs),
                             'sensors': sensors})
sd = assemble.read('sensor_day', networks, columns=['sensor_id', 'theta_m3m3__missing_reason'])
log.info('    sensor_day: %d rows, %d sensors; theta available on %d sensor-days', len(sd), sd.sensor_id.nunique(),
         int((sd.theta_m3m3__missing_reason == 'available').sum()))
stages['assemble'] = assemble

# ── Step 7. Run manifest ──────────────────────────────────────────────────────
write_manifest(run_dir / 'manifest.json', cfg, locks=locks, stages=stages,
               extra={'stations': len(stations), 'networks': networks, 'values_filled': 0,
                      'wall_time_s': round(time.time() - started, 1)})
log.info('step 7: manifest %s; %.0f s; no measurements filled', run_dir / 'manifest.json', time.time() - started)
