"""ISMN soil moisture and soil temperature: native records -> daily per-sensor features.

Rules (config/pipeline.yml ``ismn:``): observations outside the study window are
dropped; declared missing codes and NaN are decoded to missing; only records
whose ISMN flag is in ``flags_kept`` (``temperature_flags_kept``) are accepted;
the daily value is the ``daily_statistic`` of accepted values within a UTC day
when at least ``min_obs_per_day`` are accepted; Kelvin is converted to degC.
Each sensor (archive member) keeps its own identity; nothing is averaged across
sensors, depths or replicates.
"""

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass

import numpy as np
import pandas as pd

from sm_drydown.features.common import decode_values, flag_histograms
from sm_drydown.formats.ismn import parse_header, station_key

KELVIN_OFFSET = 273.15  # unit conversion K -> degC
MEMBER_VARIABLE = re.compile(r'_(sm|ts)_-?\d+(?:\.\d+)?_-?\d+(?:\.\d+)?_')
VARIABLES = {'sm': ('soil_moisture', 'm3/m3'), 'ts': ('soil_temperature', 'degC')}
PREFIX = {'soil_moisture': 'sm_', 'soil_temperature': 'soil_temperature_'}
FEATURE = {'soil_moisture': 'theta_m3m3', 'soil_temperature': 'soil_temp_degc'}


@dataclass
class Sensor:
    """One instrument record (one archive member); observations have value/ismn_flag/provider_flag."""
    member: str
    variable: str
    observations: pd.DataFrame
    units: str
    metadata: dict

    @property
    def sensor_id(self):
        """Stable ID: variable prefix + first 16 hex digits of the SHA-256 of the archive member path."""
        return PREFIX[self.variable] + hashlib.sha256(self.member.encode()).hexdigest()[:16]


def read_station_sensors(archive, station, *, variables):
    """Read every native member of ``station`` for the requested variables, keeping all flags."""
    sensors = []
    prefix = station['network'] + '/' + station_key(station['station_name']) + '/'
    with zipfile.ZipFile(archive) as source:
        for member in sorted(source.namelist()):
            if not member.startswith(prefix) or not member.endswith('.stm'):
                continue
            match = MEMBER_VARIABLE.search(member)
            if not match or VARIABLES[match[1]][0] not in variables:
                continue  # e.g. ISMN precipitation: requested from the provider but not a daily feature
            variable, units = VARIABLES[match[1]]
            payload = source.read(member)
            stream = io.StringIO(payload.decode('utf-8'))
            header = stream.readline().strip()
            metadata = parse_header(member, header)
            try:
                rows = pd.read_csv(stream, sep=r'\s+', header=None, keep_default_na=False,
                                   dtype={0: str, 1: str, 3: str, 4: str})  # not-science: ISMN header+values columns
            except pd.errors.EmptyDataError:
                rows = pd.DataFrame(columns=range(5))  # not-science: ISMN header+values columns
            if len(rows.columns) != 5:  # not-science: ISMN header+values columns
                raise ValueError('Native records require date, time, value, ISMN flag, provider flag')
            stamps = pd.to_datetime(rows[0] + ' ' + rows[1], format='%Y/%m/%d %H:%M', utc=True)  # not-science: ISMN header+values columns
            observations = pd.DataFrame({'value': np.array([float(v) for v in rows[2]], dtype=float),  # not-science: ISMN header+values columns
                                         'ismn_flag': rows[3].to_numpy(), 'provider_flag': rows[4].to_numpy()},  # not-science: ISMN header+values columns
                                        index=pd.DatetimeIndex(stamps))
            metadata.update(units=units, variable=variable, member=member,
                            member_sha256=hashlib.sha256(payload).hexdigest(), native_header=header)
            sensors.append(Sensor(member, variable, observations, units, metadata))
    if not sensors:
        raise ValueError(f"No native members for station {station['station_id']} in {archive}")
    return sensors


def sensor_daily(sensor, dates, cfg_ismn):
    """Daily feature rows, per-day flag histograms and a per-sensor audit for one sensor."""
    frame = sensor.observations.copy()
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.hasnans:
        raise ValueError('Sensor observations require nonmissing DatetimeIndex timestamps')
    frame.index = frame.index.tz_localize('UTC') if frame.index.tz is None else frame.index.tz_convert('UTC')
    if frame.index.has_duplicates:  # cfg_ismn.duplicate_timestamps == 'error' (the only option)
        raise ValueError(f'Duplicate timestamps in {sensor.member}')
    inside = ((frame.index >= dates[0].tz_localize('UTC')) &
              (frame.index < (dates[-1] + pd.Timedelta(days=1)).tz_localize('UTC')))
    outside = int((~inside).sum())
    frame = frame.loc[inside].copy()
    values, n_missing = decode_values(frame.value, missing_codes=cfg_ismn.missing_codes[sensor.variable])
    if sensor.variable == 'soil_moisture':
        if sensor.units != cfg_ismn.soil_moisture_units:
            raise ValueError(f'{sensor.member}: soil moisture must be {cfg_ismn.soil_moisture_units}')
        kept_flags = cfg_ismn.flags_kept
    else:
        if sensor.units == 'K':
            values = values - KELVIN_OFFSET
        elif sensor.units != cfg_ismn.temperature_units:
            raise ValueError(f'{sensor.member}: temperature units must be K or {cfg_ismn.temperature_units}')
        kept_flags = cfg_ismn.temperature_flags_kept
    good = frame.ismn_flag.isin(kept_flags).fillna(False)
    frame['decoded'], frame['accepted'], frame['good'] = values, values.where(good), good
    day = frame.index.floor('D').tz_localize(None)
    groups = frame.groupby(day)
    statistic = groups.accepted.mean() if cfg_ismn.daily_statistic == 'mean' else groups.accepted.median()
    out = pd.DataFrame(index=dates)
    out['n_obs'] = groups.size().reindex(dates, fill_value=0).astype('int64')
    out['n_good'] = groups.good.sum().reindex(dates, fill_value=0).astype('int64')
    out['n_valid'] = groups.accepted.count().reindex(dates, fill_value=0).astype('int64')
    out['value'] = statistic.reindex(dates).where(out.n_valid >= cfg_ismn.min_obs_per_day)
    reason = pd.Series('available', index=dates)
    reason.loc[out.value.isna()] = 'missing_values'
    reason.loc[out.value.isna() & groups.decoded.count().reindex(dates, fill_value=0).gt(0)] = 'quality_masked'
    reason.loc[out.value.isna() & out.n_valid.gt(0)] = 'below_min_obs_per_day'
    reason.loc[out.n_obs.eq(0)] = 'no_observations'
    out['missing_reason'] = reason
    audit = pd.DataFrame({'ismn_flag_counts': flag_histograms(frame.ismn_flag, day),
                          'provider_flag_counts': flag_histograms(frame.provider_flag, day)})
    summary = {'observations_in_window': len(frame), 'observations_outside_window': outside,
               'decoded_missing_values': n_missing,
               'values_masked_by_quality': int((values.notna() & ~good).sum()),
               'days_with_value': int(out.value.notna().sum())}
    return out, audit, summary


def observed_span(daily):
    """Rows from the first to the last day with at least one observation (empty if none)."""
    observed = np.flatnonzero(daily.n_obs.to_numpy() > 0)
    return daily.iloc[observed[0]:observed[-1] + 1] if len(observed) else daily.iloc[:0]


def station_tables(cfg, archive, station, dates):
    """Feature, audit and sensor rows for one station (all its SM and temperature sensors)."""
    ismn = cfg.ismn
    features = {name: [] for name in FEATURE.values()}
    audits, sensors = [], []
    for sensor in read_station_sensors(archive, station, variables=ismn.daily_variables):
        daily, audit, summary = sensor_daily(sensor, dates, ismn)
        rows = observed_span(daily) if ismn.sensor_day_span == 'observed' else daily
        if len(rows):
            table = rows.rename(columns={'value': FEATURE[sensor.variable]}).rename_axis('date').reset_index()
            table.insert(0, 'station_id', station['station_id'])
            table.insert(0, 'sensor_id', sensor.sensor_id)
            features[FEATURE[sensor.variable]].append(table)
            kept = audit.reindex(rows.index).dropna(how='all').rename_axis('date').reset_index()
            kept.insert(0, 'sensor_id', sensor.sensor_id)
            audits.append(kept)
        meta = sensor.metadata
        sensors.append({'sensor_id': sensor.sensor_id, 'station_id': station['station_id'],
                        'network': station['network'], 'variable': sensor.variable,
                        'depth_from_m': meta['depth_from'], 'depth_to_m': meta['depth_to'],
                        'instrument': meta['instrument'], 'source_units': sensor.units,
                        'member': sensor.member, 'member_sha256': meta['member_sha256'],
                        'native_latitude': meta['latitude'], 'native_longitude': meta['longitude'],
                        'elevation_m': meta['elevation_m'], 'rows': len(rows), **summary})
    return features, audits, sensors


def build(cfg, inputs, stations, dates):
    """ISMN feature tables for the stations of one network."""
    archive, _ = inputs.archive(stations[0]['network'])
    tables = {'theta_m3m3': [], 'soil_temp_degc': [], 'ismn_qc_audit': [], 'ismn_sensors': []}
    for station in stations:
        features, audits, sensors = station_tables(cfg, archive, station, dates)
        for name, parts in features.items():
            tables[name] += parts
        tables['ismn_qc_audit'] += audits
        tables['ismn_sensors'].append(pd.DataFrame(sensors))
    return {name: pd.concat(parts, ignore_index=True) if parts else None for name, parts in tables.items()}
