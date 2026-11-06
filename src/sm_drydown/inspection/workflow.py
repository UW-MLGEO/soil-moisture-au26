"""Inspect assembled feature tables: candidate problems and distributions, never edits.

Reads the stage outputs of one homogenize run (its manifest) and writes CSVs, a
summary.json and figures. Thresholds come from config/pipeline.yml `inspection:`.
"""

from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from sm_drydown.config import as_dict
from sm_drydown.inspection.checks import (Samples, flatlines, gldas_checks, precipitation_extremes,
                                          seasonal_precipitation)
from sm_drydown.inspection.plots import distribution_plot
from sm_drydown.storage.files import utc_now

IDENTITY = ['network', 'station_id', 'station_name']
COLUMNS = {
    'ismn_masked_counts': IDENTITY + ['sensor_id', 'variable', 'depth_from_m', 'depth_to_m', 'source_observations',
                                      'source_missing_values', 'non_kept_flag_values_masked',
                                      'kept_flag_values_accepted', 'member'],
    'ismn_flatlines': IDENTITY + ['sensor_id', 'depth_from_m', 'depth_to_m', 'member', 'start', 'end',
                                  'consecutive_days', 'value'],
    'chirps_negative': IDENTITY + ['date', 'precip_mm_day'],
    'chirps_extremes': IDENTITY + ['date', 'precip_mm_day', 'station_positive_quantile_threshold_mm_day',
                                   'positive_days_for_threshold'],
    'chirps_seasonal_totals': IDENTITY + ['season_year', 'season', 'season_start', 'season_end_exclusive',
                                          'calendar_days', 'days_in_selection', 'observed_days',
                                          'missing_days_in_selection', 'coverage_fraction', 'negative_days',
                                          'nonfinite_days', 'observed_sum_mm', 'complete_season_total_mm',
                                          'complete_season'],
    'station_checks': IDENTITY + ['sm_flatline_runs', 'chirps_negative_days', 'chirps_extreme_days',
                                  'chirps_positive_days', 'chirps_positive_quantile_threshold_mm_day',
                                  'chirps_missing_days'],
}
DISTRIBUTIONS = ['ismn_sm_daily_sensor_days', 'chirps_station_days', 'chirps_native_cell_days',
                 'lai_station_composites', 'lai_native_cell_composites', 'land_cover_station_years',
                 'land_cover_native_cell_years']


def inspect_network(cfg, tables, stations, rows, samples, seen_cells):
    """Append candidate rows for one network's stations; add values to the distribution samples."""
    ins = cfg.inspection
    identity = {s['station_id']: {'network': s['network'], 'station_id': s['station_id'],
                                  'station_name': s['station_name']} for s in stations}
    cells = tables['stations'].set_index('station_id')
    sensors = tables['ismn_sensors'].set_index('sensor_id')
    for sid, sensor in sensors.iterrows():
        rows['ismn_masked_counts'].append({
            **identity[sensor.station_id], 'sensor_id': sid, 'variable': sensor.variable,
            'depth_from_m': sensor.depth_from_m, 'depth_to_m': sensor.depth_to_m,
            'source_observations': sensor.observations_in_window,
            'source_missing_values': sensor.decoded_missing_values,
            'non_kept_flag_values_masked': sensor.values_masked_by_quality,
            'kept_flag_values_accepted': (sensor.observations_in_window - sensor.decoded_missing_values
                                          - sensor.values_masked_by_quality), 'member': sensor.member})
    flat = Counter()
    theta = tables['theta_m3m3']
    if theta is not None:
        samples['ismn_sm_daily_sensor_days'].add(theta.theta_m3m3)
        for sid, group in theta.groupby('sensor_id', sort=True):
            sensor = sensors.loc[sid]
            for run in flatlines(group.theta_m3m3, group.date, ins.flatline_min_days):
                rows['ismn_flatlines'].append({**identity[sensor.station_id], 'sensor_id': sid,
                                               'depth_from_m': sensor.depth_from_m, 'depth_to_m': sensor.depth_to_m,
                                               'member': sensor.member, **run})
                flat[sensor.station_id] += 1
    precip = tables['precip_chirps_mm_day']
    for sid, group in precip.groupby('station_id', sort=False):
        x, dates = group.precip_chirps_mm_day.to_numpy(dtype=float, na_value=np.nan), pd.DatetimeIndex(group.date)
        samples['chirps_station_days'].add(x)
        cell = (cells.loc[sid, 'chirps_cell_lat_deg'], cells.loc[sid, 'chirps_cell_lon_deg'])
        if np.isfinite(cell).all() and cell not in seen_cells['chirps']:
            seen_cells['chirps'].add(cell)
            samples['chirps_native_cell_days'].add(x)
        negative = np.flatnonzero(np.isfinite(x) & (x < 0))
        rows['chirps_negative'] += [{**identity[sid], 'date': dates[j], 'precip_mm_day': x[j]} for j in negative]
        flags, threshold, positive = precipitation_extremes(x, ins.chirps_extreme_quantile)
        rows['chirps_extremes'] += [{**identity[sid], 'date': dates[j], 'precip_mm_day': x[j],
                                     'station_positive_quantile_threshold_mm_day': threshold,
                                     'positive_days_for_threshold': positive} for j in np.flatnonzero(flags)]
        rows['chirps_seasonal_totals'] += [{**identity[sid], **season} for season in
                                           seasonal_precipitation(x, dates, ins.seasons).to_dict('records')]
        rows['station_checks'].append({**identity[sid], 'sm_flatline_runs': flat[sid],
                                       'chirps_negative_days': len(negative), 'chirps_extreme_days': int(flags.sum()),
                                       'chirps_positive_days': positive,
                                       'chirps_positive_quantile_threshold_mm_day': threshold,
                                       'chirps_missing_days': int(np.isnan(x).sum())})
    for table, column, source, name in (('lai_m2m2', 'lai_m2m2', 'modis_lai', 'lai'),
                                        ('igbp_class', 'igbp_class', 'modis_land_cover', 'land_cover')):
        frame = tables[table]
        composites = frame[frame.source_start.notna()].drop_duplicates(['station_id', 'source_start'])
        unit = 'composites' if name == 'lai' else 'years'
        for sid, group in composites.groupby('station_id', sort=False):
            values = group[column].to_numpy(dtype=float, na_value=np.nan)
            samples[f'{name}_station_{unit}'].add(values)
            cell = (cells.loc[sid, f'{source}_cell_lat_deg'], cells.loc[sid, f'{source}_cell_lon_deg'])
            if np.isfinite(cell).all() and cell not in seen_cells[source]:
                seen_cells[source].add(cell)
                samples[f'{name}_native_cell_{unit}'].add(values)


def inspect(cfg, read, networks, stations_by_network, output_dir, figure_dir):
    """Write all inspection outputs. ``read(table, network)`` returns one network's table."""
    ins = cfg.inspection
    output, figures = Path(output_dir), Path(figure_dir)
    output.mkdir(parents=True, exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)
    rows = {name: [] for name in COLUMNS}
    samples = {name: Samples() for name in DISTRIBUTIONS}
    seen_cells, gldas = defaultdict(set), []
    for network in networks:
        names = ('theta_m3m3', 'ismn_sensors', 'precip_chirps_mm_day', 'lai_m2m2', 'igbp_class', 'stations',
                 'gldas_static')
        tables = {name: read(name, network) for name in names}
        inspect_network(cfg, tables, stations_by_network[network], rows, samples, seen_cells)
        gldas.append(tables['gldas_static'])
    for name, columns in COLUMNS.items():
        pd.DataFrame.from_records(rows[name], columns=columns).to_csv(output / f'{name}.csv', index=False)
    checks = gldas_checks(pd.concat(gldas, ignore_index=True), sum_tolerance=ins.gldas_fraction_sum_tolerance,
                          texture_codes=ins.gldas_texture_codes, texture_tolerance=cfg.gldas.texture_snap_tolerance,
                          bounds=cfg.gldas.fraction_bounds)
    checks.to_csv(output / 'gldas_checks.csv', index=False)
    masking = defaultdict(Counter)
    by_network = defaultdict(Counter)
    for row in rows['ismn_masked_counts']:
        for key in ('source_observations', 'source_missing_values', 'non_kept_flag_values_masked',
                    'kept_flag_values_accepted'):
            masking[row['variable']][key] += int(row[key])
        by_network[row['network']][row['variable']] += int(row['non_kept_flag_values_masked'])
    distributions = {name: s.summary(ins.summary_quantiles, categorical=name.startswith('land_cover'))
                     for name, s in samples.items()}
    distribution_plot({'Daily sensor means (kept ISMN flags)': samples['ismn_sm_daily_sensor_days'].plot_sample()},
                      figures / 'ismn_sm_distribution.png', 'ISMN soil moisture (daily means of kept-flag values)',
                      'SM (m³/m³)', ins.histogram_bins)
    distribution_plot({key: samples[key].plot_sample() for key in ('chirps_station_days', 'chirps_native_cell_days')},
                      figures / 'chirps_precip_distribution.png', 'CHIRPS precipitation (zero and tails retained)',
                      'Precipitation (mm/day)', ins.histogram_bins)
    return {'created_at': utc_now(), 'stations': sum(len(v) for v in stations_by_network.values()),
            'start': cfg.study.start, 'end_inclusive': cfg.study.end, 'timezone': 'UTC calendar days',
            'rules': {**as_dict(ins), 'flatline_comparison': 'exact equality on consecutive dates; gaps break runs',
                      'chirps_extreme_comparison': 'strictly greater than the station positive-day quantile',
                      'gldas_fraction_bounds': cfg.gldas.fraction_bounds},
            'flag_masking_by_variable': {k: dict(v) for k, v in masking.items()},
            'flag_masking_by_network': {k: dict(v) for k, v in by_network.items()},
            'flatline_candidate_runs': len(rows['ismn_flatlines']),
            'chirps_negative_station_days': len(rows['chirps_negative']),
            'chirps_extreme_station_days': len(rows['chirps_extremes']),
            'gldas': {'physical_range_failure_stations': int(checks.any_physical_range_failure.sum()),
                      'closure_failure_stations': int(checks.closure_outside_tolerance.sum()),
                      'closure_unevaluable_stations': int((~checks.closure_evaluable).sum())},
            'distributions': distributions,
            'sampling_note': ('SM: sensor-days within each sensor observed span; CHIRPS: station-days and unique '
                              'native-cell days; MODIS: one value per source composite/year, by station and by '
                              'unique native cell. No independence or population claim.'),
            'source_data_modified': False, 'values_filled': 0, 'network_access': False}
