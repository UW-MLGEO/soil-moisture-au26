"""Write drydown-q's ``sensors`` and ``station_day`` tables from our assembled features.

drydown-q (github.com/bbucho/drydown-q layout) detects drydowns itself, so it
takes daily per-sensor series. Mapping (config/pipeline.yml ``export_drydown_q``):

- ``station_id`` = ``"<network>/<station_id>"``; ``sensor_id`` = our sensor_id;
- ``theta_m3m3`` and ``n_valid_hours`` (= accepted ISMN observations that day);
- ``precip_mm`` = CHIRPS; ``precip_60d_mm`` = trailing sum over ``ai_window_days``
  ending on the date, NaN unless ``ai_window_min_valid_days`` are finite;
- ``lai_m2m2`` = MODIS MCD15A3H (drydown-q's default is MOD15A2H; recorded in
  the export manifest); ``igbp_class`` = IGBP abbreviation ('' when missing);
- soil texture: GLDAS sand/silt/clay fractions and FAO-16 class names;
- not available here, written as NaN/False: ``pet_mm``, ``pet_60d_mm``,
  ``tree_cover_pct``, ``dpet_offset_km``, ``dpet_cell_is_land``.
"""

import numpy as np
import pandas as pd

from sm_drydown.config import REPO_ROOT

TABLES = REPO_ROOT / 'data' / 'tables'
MISSING_FEATURES = ('pet_mm', 'pet_60d_mm', 'tree_cover_pct')


def haversine_km(lat1, lon1, lat2, lon2, radius_km):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2
    return 2 * radius_km * np.arcsin(np.sqrt(a))


def trailing_sum(values, window, min_valid):
    """Sum over the ``window`` days ending on each day; NaN with fewer than ``min_valid`` finite days."""
    series = pd.Series(values)
    return series.rolling(window, min_periods=min_valid).sum().to_numpy()


def export_network(cfg, sensor_day, stations, sensors, precip):
    """drydown-q tables for one network (precip: our full CHIRPS station-day table)."""
    ex = cfg.export_drydown_q
    igbp = pd.read_csv(TABLES / 'igbp_lc_type1.csv').set_index('code')['abbreviation']
    texture = pd.read_csv(TABLES / 'gldas_fao16_texture.csv').set_index('code')['texture_class']
    sm = sensors[sensors.variable == 'soil_moisture']
    if ex.max_sensor_depth_m is not None:
        sm = sm[sm.depth_to_m <= ex.max_sensor_depth_m]
    st = stations.set_index('station_id')
    first = sensor_day.drop_duplicates('sensor_id').set_index('sensor_id')
    out_sensors = pd.DataFrame({
        'sensor_id': sm.sensor_id.to_numpy(),
        'station_id': [f'{n}/{s}' for n, s in zip(sm.network, sm.station_id)],
        'network': sm.network.to_numpy(),
        'lat_deg': st.loc[sm.station_id, 'lat_deg'].to_numpy(), 'lon_deg': st.loc[sm.station_id, 'lon_deg'].to_numpy(),
        'depth_top_m': sm.depth_from_m.to_numpy(dtype=float), 'depth_bottom_m': sm.depth_to_m.to_numpy(dtype=float)})
    for column in ('sand', 'silt', 'clay'):
        values = first.reindex(sm.sensor_id)[f'gldas_{column}_frac']
        out_sensors[f'{column}_frac'] = values.to_numpy(dtype=float, na_value=np.nan)
    codes = first.reindex(sm.sensor_id)['gldas_texture_class']
    out_sensors['texture_class'] = [texture.get(int(c), '') if pd.notna(c) else '' for c in codes]
    out_sensors['chirps_offset_km'] = haversine_km(out_sensors.lat_deg, out_sensors.lon_deg,
                                                   st.loc[sm.station_id, 'chirps_cell_lat_deg'].to_numpy(),
                                                   st.loc[sm.station_id, 'chirps_cell_lon_deg'].to_numpy(),
                                                   ex.earth_radius_km)
    has_rain = precip.groupby('station_id').precip_chirps_mm_day.apply(lambda x: x.notna().any())
    out_sensors['chirps_cell_is_land'] = has_rain.reindex(sm.station_id).fillna(False).to_numpy(dtype=bool)
    out_sensors['dpet_offset_km'] = np.nan
    out_sensors['dpet_cell_is_land'] = False

    windows = []
    for sid, group in precip.groupby('station_id', sort=False):
        group = group.sort_values('date')
        windows.append(pd.DataFrame({'station_id': sid, 'date': group.date.to_numpy(),
                                     'precip_60d_mm': trailing_sum(group.precip_chirps_mm_day.to_numpy(dtype=float),
                                                                   ex.ai_window_days, ex.ai_window_min_valid_days)}))
    days = sensor_day[sensor_day.sensor_id.isin(out_sensors.sensor_id)]
    days = days.merge(pd.concat(windows), on=['station_id', 'date'], how='left', validate='many_to_one')
    out_days = pd.DataFrame({
        'sensor_id': days.sensor_id.to_numpy(), 'date': days.date.to_numpy(),
        'theta_m3m3': days.theta_m3m3.to_numpy(dtype=float, na_value=np.nan),
        'n_valid_hours': days.n_valid.to_numpy(dtype='int64'),
        'precip_mm': days.precip_chirps_mm_day.to_numpy(dtype=float, na_value=np.nan),
        'pet_mm': np.nan, 'precip_60d_mm': days.precip_60d_mm.to_numpy(dtype=float), 'pet_60d_mm': np.nan,
        'lai_m2m2': days.lai_m2m2.to_numpy(dtype=float, na_value=np.nan),
        'igbp_class': [igbp.get(int(c), '') if pd.notna(c) else '' for c in days.igbp_class],
        'tree_cover_pct': np.nan})
    return out_sensors, out_days
