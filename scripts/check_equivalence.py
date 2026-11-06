"""Check that the new feature tables reproduce legacy per-station daily bundles exactly.

    pixi run python scripts/check_equivalence.py --legacy-dir output/legacy_daily_arm --run pilot_arm

The legacy bundles (one wide daily.parquet + description.json per station) come
from the pre-refactor code (git tag `pre-refactor`, homogenization/workflow.py).
For every station and day this compares: each SM and soil-temperature sensor's
daily value, observation/good/valid counts, missing reason and flag histograms;
CHIRPS values, missing reasons and null/absent masks; the five GLDAS fields; and
MODIS land cover and LAI values, missing reasons, source intervals and QC words.
Days outside a sensor's observed span (not stored in the new long tables) must be
`no_observations` with zero counts in the legacy table. Exits non-zero on any
difference. Reads local files only.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from sm_drydown.cache import Stage
from sm_drydown.config import load_config

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--legacy-dir', type=Path, required=True)
parser.add_argument('--run', default='baseline')
parser.add_argument('--paths', default=None)
args = parser.parse_args()
cfg = load_config(args.run, args.paths)
manifest = json.loads((Path(cfg.paths.outputs_dir) / 'runs' / cfg.run_name / 'manifest.json').read_text())
stage = {name: Stage(cfg.paths.outputs_dir, s['stage'], s['key'])
         for name, s in manifest['stages'].items()}
problems = []


def same(label, old, new, *, exact_object=False):
    old, new = pd.Series(old).reset_index(drop=True), pd.Series(new).reset_index(drop=True)
    if len(old) != len(new):
        problems.append(f'{label}: length {len(old)} != {len(new)}')
        return
    if exact_object or old.dtype == object or new.dtype == object or str(old.dtype) in ('string', 'str'):
        o, n = old.astype(object).where(old.notna(), None), new.astype(object).where(new.notna(), None)
        bad = [(i, a, b) for i, (a, b) in enumerate(zip(o, n)) if a != b]
    else:
        a, b = old.to_numpy(dtype=float, na_value=np.nan), new.to_numpy(dtype=float, na_value=np.nan)
        bad = np.flatnonzero(~((a == b) | (np.isnan(a) & np.isnan(b))))
        bad = [(i, a[i], b[i]) for i in bad]
    if bad:
        problems.append(f'{label}: {len(bad)} differences, first {bad[:3]}')  # not-science: console output length


compared = {'stations': 0, 'sensor_columns': 0, 'sensor_days': 0}
for description_path in sorted(args.legacy_dir.glob('*/*/*/description.json')):
    meta = json.loads(description_path.read_text())
    old = pd.read_parquet(description_path.with_name('daily.parquet'))
    sid, network = meta['station']['station_id'], meta['station']['network']
    dates = pd.DatetimeIndex(old.date)
    for column, sensor in meta['sensors'].items():
        feature = 'theta_m3m3' if sensor['variable'] == 'soil_moisture' else 'soil_temp_degc'
        new = stage['ismn'].read(feature, [network])
        new = new[new.sensor_id == column].set_index('date').reindex(dates)
        inside = new.missing_reason.notna().to_numpy()
        for legacy, current in (('', feature), ('__observation_count', 'n_obs'), ('__good_flag_count', 'n_good'),
                                ('__valid_count', 'n_valid'), ('__missing_reason', 'missing_reason')):
            same(f'{sid}/{column}{legacy}', old[column + legacy][inside], new[current][inside])
        outside = old.loc[~inside]
        if not (outside[column + '__missing_reason'].eq('no_observations').all()
                and outside[column + '__observation_count'].eq(0).all()):
            problems.append(f'{sid}/{column}: legacy has observations outside the new observed span')
        audit = stage['ismn'].read('ismn_qc_audit', [network])
        audit = audit[audit.sensor_id == column].set_index('date').reindex(dates)
        for flag in ('ismn_flag_counts', 'provider_flag_counts'):
            same(f'{sid}/{column}__{flag}', old[f'{column}__{flag}'][inside], audit[flag][inside])
        sensors = stage['ismn'].read('ismn_sensors', [network]).set_index('sensor_id').loc[column]
        for key in ('values_masked_by_quality', 'decoded_missing_values', 'observations_in_window',
                    'observations_outside_window'):
            if sensors[key] != sensor[key]:
                problems.append(f'{sid}/{column}.{key}: {sensor[key]} != {sensors[key]}')
        compared['sensor_columns'] += 1
        compared['sensor_days'] += int(inside.sum())
    precip = stage['chirps'].read('precip_chirps_mm_day', [network])
    precip = precip[precip.station_id == sid].set_index('date').reindex(dates)
    same(f'{sid}/chirps', old.chirps_precip_mm_day, precip.precip_chirps_mm_day)
    same(f'{sid}/chirps reason', old.chirps_precip_mm_day__missing_reason, precip.missing_reason)
    for mask in ('provider_null', 'provider_absent_row'):
        same(f'{sid}/chirps {mask}', old[f'chirps_precip_mm_day__{mask}'], precip[mask], exact_object=True)
    gldas = stage['gldas'].read('gldas_static', [network]).set_index('station_id').loc[sid]
    for legacy, current in (('soil_texture_class', 'gldas_texture_class'), ('sand_fraction', 'gldas_sand_frac'),
                            ('silt_fraction', 'gldas_silt_frac'), ('clay_fraction', 'gldas_clay_frac'),
                            ('porosity', 'gldas_porosity')):
        same(f'{sid}/{current}', old[f'gldas_{legacy}'], pd.Series([gldas[current]] * len(old)))
        same(f'{sid}/{current} reason', old[f'gldas_{legacy}__missing_reason'],
             pd.Series([gldas[current + '__missing_reason']] * len(old)))
    for legacy, group, table in (('modis_land_cover', 'modis_land_cover', 'igbp_class'),
                                 ('modis_lai_m2_m2', 'modis_lai', 'lai_m2m2')):
        new = stage[group].read(table, [network])
        new = new[new.station_id == sid].set_index('date').reindex(dates)
        same(f'{sid}/{table}', old[legacy], new[table])
        for suffix, current in (('__missing_reason', 'missing_reason'), ('__source_start', 'source_start'),
                                ('__source_end_exclusive', 'source_end_exclusive')):
            same(f'{sid}/{table}{suffix}', old[legacy + suffix], new[current], exact_object=True)
        for qc in [c for c in old if c.startswith(legacy + '__qc_')]:
            same(f'{sid}/{qc}', old[qc], new[qc.split('__', 1)[1]])
    compared['stations'] += 1

print(f"compared {compared['stations']} stations, {compared['sensor_columns']} sensor columns, "
      f"{compared['sensor_days']} sensor-days")
if problems:
    print(f'{len(problems)} differences:')
    print('\n'.join(problems[:40]))  # not-science: console output length
    raise SystemExit(1)
print('equivalent: every compared value, count, missing reason, mask, interval and QC word is identical')
