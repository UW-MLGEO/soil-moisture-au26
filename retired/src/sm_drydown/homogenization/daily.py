"""Homogenize declared source observations without imputing measurements."""

from dataclasses import dataclass
import hashlib
import json

import numpy as np
import pandas as pd


@dataclass
class Sensor:
    """One instrument; observations have value, ismn_flag and provider_flag columns."""

    identity: str
    variable: str
    observations: pd.DataFrame
    units: str
    missing_codes: list
    metadata: dict


def daily_index(start, end):
    """Inclusive UTC calendar dates represented by timezone-naive midnights."""
    bounds = [pd.Timestamp(start), pd.Timestamp(end)]
    if any(t.tz is not None or t != t.normalize() for t in bounds) or bounds[1] < bounds[0]:
        raise ValueError('start/end must be ordered timezone-naive calendar dates interpreted as UTC')
    return pd.date_range(*bounds, freq='D', name='date')


def decode_values(values, *, missing_codes, scale_factor=1, add_offset=0, valid_range=None):
    """Mask declared codes/ranges before scaling; never replace missing data by defaults."""
    values = pd.Series(values, copy=True).astype(float)
    invalid = values.isna()
    for code in missing_codes:
        invalid |= values.isna() if pd.isna(code) else values.eq(float(code))
    if valid_range is not None:
        invalid |= ~values.between(*valid_range)
    if np.isinf(values).any():
        raise ValueError('Infinite source value requires an explicit source decoding rule')
    return values.mask(invalid) * scale_factor + add_offset, int(invalid.sum())


def _flag_histograms(series, day):
    """Count every original flag per day without invoking pandas once per group."""
    if not len(series):
        return pd.Series(index=pd.DatetimeIndex([]), dtype='str')
    words = series.astype('string').fillna('<missing>').to_numpy()
    counts = pd.crosstab(day, words)
    labels = list(counts.columns)
    encoded = [json.dumps({str(label): int(count) for label, count in zip(labels, row) if count},
                          sort_keys=True) for row in counts.itertuples(index=False, name=None)]
    return pd.Series(encoded, index=pd.DatetimeIndex(counts.index))


def sensor_daily(sensor, dates, *, temperature_quality):
    """Average valid observations per UTC day; preserve sensor identity and daily QC counts."""
    if sensor.variable not in ('soil_moisture', 'soil_temperature'):
        raise ValueError('Only soil moisture and soil temperature belong in the daily table')
    if temperature_quality not in ('G_only', 'retain_all_flags'):
        raise ValueError('Explicit temperature_quality must be G_only or retain_all_flags')
    frame = sensor.observations.copy()
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.hasnans:
        raise ValueError('Sensor observations require nonmissing DatetimeIndex timestamps')
    frame.index = (frame.index.tz_localize('UTC') if frame.index.tz is None
                   else frame.index.tz_convert('UTC'))
    if frame.index.has_duplicates:
        raise ValueError('Duplicate timestamps require a resolution decision per sensor')
    required = {'value', 'ismn_flag', 'provider_flag'}
    if not required.issubset(frame.columns):
        raise ValueError(f'Sensor observations require columns {sorted(required)}')
    outside = ~((frame.index >= dates[0].tz_localize('UTC')) &
                (frame.index < (dates[-1] + pd.Timedelta(days=1)).tz_localize('UTC')))
    frame = frame.loc[~outside].copy()
    values, missing = decode_values(frame.value, missing_codes=sensor.missing_codes)
    if sensor.variable == 'soil_moisture':
        if sensor.units != 'm3/m3':
            raise ValueError('SM units must be explicitly converted to m3/m3')
        units = 'm3/m3'
        good_only = True
    else:
        if sensor.units not in ('degC', 'K'):
            raise ValueError('Temperature units must be degC or K')
        if sensor.units == 'K':
            values = values - 273.15
        units = 'degC'
        good_only = temperature_quality == 'G_only'
    good = frame.ismn_flag.eq('G').fillna(False)
    masked = values.where(good) if good_only else values
    frame['decoded'] = values
    frame['accepted'] = masked
    frame['good'] = good
    day = frame.index.floor('D').tz_localize(None)
    groups = frame.groupby(day)
    short = hashlib.sha256(sensor.identity.encode()).hexdigest()[:16]
    column = ('sm_' if sensor.variable == 'soil_moisture' else 'soil_temperature_') + short
    output = pd.DataFrame(index=dates)
    output[column] = groups.accepted.mean().reindex(dates)
    for suffix, series in [('observation_count', groups.size()),
                           ('good_flag_count', groups.good.sum()),
                           ('valid_count', groups.accepted.count())]:
        output[column + '__' + suffix] = series.reindex(dates, fill_value=0).astype('int64')
    for flag in ('ismn_flag', 'provider_flag'):
        output[column + '__' + flag + '_counts'] = _flag_histograms(frame[flag], day).reindex(dates)
    reason = pd.Series('available', index=dates)
    reason.loc[output[column].isna()] = 'missing_values'
    if good_only:
        valid_raw = groups.decoded.count().reindex(dates, fill_value=0)
        reason.loc[output[column].isna() & valid_raw.gt(0)] = 'quality_masked'
    reason.loc[output[column + '__observation_count'].eq(0)] = 'no_observations'
    output[column + '__missing_reason'] = reason
    metadata = {**sensor.metadata, 'identity': sensor.identity, 'variable': sensor.variable,
                'units': units, 'source_units': sensor.units, 'quality': 'G_only' if good_only else 'retain_all_flags',
                'missing_codes': sensor.missing_codes, 'observations_in_window': len(frame),
                'observations_outside_window': int(outside.sum()), 'decoded_missing_values': missing,
                'values_masked_by_quality': int((values.notna() & ~good).sum()) if good_only else 0,
                'days_without_valid_values': int(output[column].isna().sum()),
                'aggregation': 'available-observation UTC daily mean; no minimum count', 'values_filled': 0}
    return output, {column: metadata}


def align_daily(values, dates, *, name, source_present=None, source_null=None, source_absent=None):
    """Align native daily values without averaging, filling or converting missing rainfall to zero."""
    values = values.copy()
    if not isinstance(values.index, pd.DatetimeIndex) or values.index.hasnans:
        raise ValueError('Daily inputs require a nonmissing DatetimeIndex')
    if values.index.tz is not None:
        values.index = values.index.tz_convert('UTC').tz_localize(None)
    if values.index.has_duplicates or not values.index.equals(values.index.normalize()):
        raise ValueError('Daily source dates must be unique UTC midnights')
    result = pd.DataFrame({name: values.reindex(dates)}, index=dates)
    present = pd.Series(True, index=values.index) if source_present is None else source_present.reindex(values.index)
    reason = pd.Series('available', index=dates)
    reason.loc[result[name].isna()] = 'missing_value'
    reason.loc[~present.reindex(dates, fill_value=False).fillna(False)] = 'no_source_record'
    if source_null is not None:
        null = source_null.reindex(dates).astype('boolean')
        reason.loc[null.fillna(False)] = 'provider_null'
        result[name + '__provider_null'] = null
    if source_absent is not None:
        absent = source_absent.reindex(dates).astype('boolean')
        reason.loc[absent.fillna(False)] = 'provider_absent_row'
        result[name + '__provider_absent_row'] = absent
    result[name + '__missing_reason'] = reason
    return result


def quality_acceptance(frame, policy):
    """Evaluate explicit QC bit rules; missing required QC is never assumed good."""
    if policy.get('mode') == 'retain_flagged':
        return pd.Series(True, index=frame.index)
    if policy.get('mode') != 'bit_rules' or not policy.get('rules'):
        raise ValueError('QC policy must explicitly retain_flagged or specify bit_rules')
    good = pd.Series(True, index=frame.index)
    for rule in policy['rules']:
        source = frame[rule['column']]
        present = source.notna()
        if ((source.loc[present] % 1) != 0).any() or (source.loc[present] < 0).any():
            raise ValueError('QC words must be nonnegative integers')
        # Zero below is solely an implementation placeholder; present masks it out.
        word = source.fillna(0).astype('int64').to_numpy()
        decoded = pd.Series((word >> rule['offset']) & rule['mask'], index=frame.index)
        good &= present & decoded.isin(rule['allowed'])
    return good


def interval_daily(frame, dates, *, name, policy, categorical=False):
    """Assign source values within explicit [start,end) intervals, never across missing composites/years."""
    frame = frame.copy()
    for key in ('start', 'end'):
        frame[key] = pd.to_datetime(frame[key], utc=True)
        if frame[key].isna().any() or not frame[key].equals(frame[key].dt.normalize()):
            raise ValueError('Source intervals require valid UTC calendar boundaries')
    frame = frame.sort_values('start').reset_index(drop=True)
    if (frame.end <= frame.start).any() or (frame.start.iloc[1:].to_numpy() < frame.end.iloc[:-1].to_numpy()).any():
        raise ValueError('Source intervals must be positive and nonoverlapping')
    if categorical and frame.value.dropna().map(lambda x: float(x) % 1 != 0).any():
        raise ValueError('Land-cover categories must be integer codes')
    good = quality_acceptance(frame, policy)
    # Index each daily record into a containing [start,end) source interval.
    # Selecting columns directly avoids iterrows coercing all-missing numeric
    # values/QC words to NaT in rows that also contain timestamps (pandas 3).
    intervals = pd.IntervalIndex.from_arrays(pd.DatetimeIndex(frame.start.dt.tz_localize(None)).as_unit('ns'),
        pd.DatetimeIndex(frame.end.dt.tz_localize(None)).as_unit('ns'), closed='left')
    positions = intervals.get_indexer(dates.as_unit('ns'))
    matched = positions >= 0
    rows = frame.iloc[positions[matched]].copy()
    rows.index = dates[matched]
    accepted = pd.Series(good.iloc[positions[matched]].to_numpy(), index=rows.index)
    values = rows['value'].where(accepted)
    output = pd.DataFrame(index=dates)
    output[name] = values.reindex(dates).astype('Int64' if categorical else 'float64')
    reason = pd.Series('available', index=rows.index)
    reason.loc[~accepted] = 'quality_masked'
    required = [rule['column'] for rule in policy.get('rules', [])]
    if required:
        reason.loc[rows[required].isna().any(axis=1)] = 'missing_qc'
    reason.loc[rows.value.isna()] = 'missing_value'
    output[name + '__missing_reason'] = reason.reindex(dates, fill_value='no_source_interval')
    output[name + '__source_start'] = rows.start.dt.tz_localize(None).reindex(dates)
    output[name + '__source_end_exclusive'] = rows.end.dt.tz_localize(None).reindex(dates)
    qc_columns = list(dict.fromkeys(rule['column'] for rule in policy.get('rules', [])))
    qc_columns += [c for c in frame if c.startswith('qc_') and c not in qc_columns]
    for qc in qc_columns:
        output[name + '__' + qc] = rows[qc].reindex(dates).astype('Int64')
    return output


def station_daily(station, start, end, sensors, chirps, static_fields, land_cover, lai, *, policies):
    """Build one wide daily table per station, preserving per-sensor identities and source support."""
    dates = daily_index(start, end)
    result = pd.DataFrame(index=dates)
    for key in ('network', 'station_id', 'station_name', 'latitude', 'longitude'):
        result[key] = station[key]
    sensor_metadata = {}
    parts = [result]
    seen = set(result)
    for sensor in sensors:
        values, metadata = sensor_daily(sensor, dates, temperature_quality=policies['temperature_quality'])
        if set(values).intersection(seen):
            raise ValueError('Repeated sensor identity would overwrite a daily column')
        seen.update(values)
        parts.append(values)
        sensor_metadata.update(metadata)
    if isinstance(chirps, pd.DataFrame):
        parts.append(align_daily(chirps.value, dates, name='chirps_precip_mm_day',
            source_present=chirps.source_present, source_null=chirps.provider_null,
            source_absent=chirps.provider_absent_row))
    else:
        parts.append(align_daily(chirps, dates, name='chirps_precip_mm_day'))
    expected = {'soil_texture_class', 'sand_fraction', 'silt_fraction', 'clay_fraction', 'porosity'}
    if set(static_fields) != expected:
        raise ValueError('GLDAS must explicitly supply all five fields, including nulls for unavailable values')
    static = {}
    for field, value in static_fields.items():
        name = 'gldas_' + field
        if field == 'soil_texture_class':
            if pd.notna(value) and float(value) % 1:
                raise ValueError('GLDAS texture must be categorical integer')
            static[name] = pd.Series(value, index=dates, dtype='Int64')
        else:
            static[name] = pd.Series(value, index=dates, dtype='float64')
        static[name + '__missing_reason'] = pd.Series('missing_static_value' if pd.isna(value) else 'available', index=dates)
    parts.append(pd.DataFrame(static, index=dates))
    if not policies.get('land_cover_scheme'):
        raise ValueError('Explicit MODIS land_cover_scheme is required')
    parts.append(interval_daily(land_cover, dates, name='modis_land_cover',
                                policy=policies['land_cover_quality'], categorical=True))
    parts.append(interval_daily(lai, dates, name='modis_lai_m2_m2', policy=policies['lai_quality']))
    result = pd.concat(parts, axis=1).copy()
    metadata = {'station': station, 'start': start, 'end_inclusive': end,
                'timezone': 'UTC calendar days', 'coordinate_reference': 'EPSG:4326',
                'sensors': sensor_metadata, 'policies': policies, 'values_filled': 0,
                'rows': len(result), 'units': {'chirps_precip_mm_day': 'mm/day', 'modis_lai_m2_m2': 'm2/m2',
                    **{'gldas_' + f: '1' for f in expected if f != 'soil_texture_class'},
                    **{c: m['units'] for c, m in sensor_metadata.items()}},
                'missing_counts': {c: int(result[c].isna().sum()) for c in result}}
    return result.reset_index(), metadata
