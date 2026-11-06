"""Join feature tables into the ML-ready ``sensor_day`` table (one row per SM sensor per day).

Rows come from the soil-moisture feature (``theta_m3m3``); station-day features
are joined on (station_id, date) and static features on station_id. Columns follow
the order of ``pipeline.yml features:``; every feature is followed by its
``<feature>__missing_reason``. Joining never creates or drops sensor-days.
"""

import pandas as pd

from sm_drydown.features import FEATURES


def sensor_day(cfg, tables, network):
    """Assemble one network. ``tables`` maps feature table name -> DataFrame for this network."""
    theta = tables['theta_m3m3']
    if theta is None or not len(theta):
        return None
    base = theta.rename(columns={'missing_reason': 'theta_m3m3__missing_reason'})
    base.insert(2, 'network', network)
    rows = len(base)
    ordered = ['sensor_id', 'station_id', 'network', 'date']
    for name in cfg.features:
        feature = FEATURES[name]
        if feature.level == 'sensor':
            ordered += [name, f'{name}__missing_reason']
        elif feature.level == 'station_day':
            table = tables[feature.table][['station_id', 'date', name, 'missing_reason']]
            base = base.merge(table.rename(columns={'missing_reason': f'{name}__missing_reason'}),
                              on=['station_id', 'date'], how='left', validate='many_to_one')
            ordered += [name, f'{name}__missing_reason']
        elif feature.level == 'static':
            table = tables[feature.table][['station_id', name, f'{name}__missing_reason']]
            base = base.merge(table, on='station_id', how='left', validate='many_to_one')
            ordered += [name, f'{name}__missing_reason']
    if len(base) != rows:
        raise ValueError('Joining features changed the number of sensor-days')
    for column in [c for c in base if c.endswith('__missing_reason')]:
        base[column] = base[column].fillna('no_source_record')
    return base[ordered + ['n_obs', 'n_good', 'n_valid']]


def stations_table(stations, catalogs):
    """Selected stations with the native cell each gridded source used."""
    frame = pd.DataFrame([{'station_id': s['station_id'], 'network': s['network'], 'station_name': s['station_name'],
                           'lat_deg': s['latitude'], 'lon_deg': s['longitude']} for s in stations])
    for source, catalog in catalogs.items():
        cells = [catalog.cell(s['station_id']) or {} for s in stations]
        frame[f'{source}_cell_lat_deg'] = [c.get('native_latitudes') for c in cells]
        frame[f'{source}_cell_lon_deg'] = [c.get('native_longitudes') for c in cells]
        frame[f'{source}_cell_lat_deg'] = frame[f'{source}_cell_lat_deg'].astype('float64')
        frame[f'{source}_cell_lon_deg'] = frame[f'{source}_cell_lon_deg'].astype('float64')
    return frame
