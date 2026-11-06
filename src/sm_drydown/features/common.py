"""Rules shared by every feature: the daily grid, decoding, QC words and source intervals.

Nothing here fills, interpolates or carries values. Every way a value can be
missing gets its own ``missing_reason`` label:

- ``available``: a value is present;
- ``no_observations`` / ``no_source_record`` / ``no_source_interval``: the source has nothing for that day;
- ``missing_values``/``missing_value``: the source row exists but its value is a missing code or NaN;
- ``quality_masked``: values exist but failed the configured QC rule;
- ``missing_qc``: the QC word needed to judge the value is missing (never assumed good);
- ``below_min_obs_per_day``: fewer accepted observations than ismn.min_obs_per_day;
- ``provider_null`` / ``provider_absent_row``: CHIRPS service null or absent row;
- ``missing_static_value``: a static property is missing at the station's cell;
- ``outside_source_coverage``: the station lies outside the source's declared coverage.
"""

import json

import numpy as np
import pandas as pd


def daily_index(start, end):
    """Inclusive UTC calendar dates represented by timezone-naive midnights."""
    bounds = [pd.Timestamp(start), pd.Timestamp(end)]
    if any(t.tz is not None or t != t.normalize() for t in bounds) or bounds[1] < bounds[0]:
        raise ValueError('start/end must be ordered timezone-naive calendar dates interpreted as UTC')
    return pd.date_range(*bounds, freq='D', name='date')


def decode_values(values, *, missing_codes, scale_factor=1, add_offset=0, valid_range=None):
    """Mask declared codes and out-of-range values *before* scaling; return (decoded, n_masked)."""
    values = pd.Series(values, copy=True).astype(float)
    invalid = values.isna()
    for code in missing_codes:
        invalid |= values.isna() if pd.isna(code) else values.eq(float(code))
    if valid_range is not None:
        invalid |= ~values.between(*valid_range)
    if np.isinf(values).any():
        raise ValueError('Infinite source value requires an explicit source decoding rule')
    return values.mask(invalid) * scale_factor + add_offset, int(invalid.sum())


def flag_histograms(series, day):
    """JSON histogram of every original flag word per day (missing words count as '<missing>')."""
    if not len(series):
        return pd.Series(index=pd.DatetimeIndex([]), dtype='str')
    words = series.astype('string').fillna('<missing>').to_numpy()
    counts = pd.crosstab(day, words)
    labels = list(counts.columns)
    encoded = [json.dumps({str(label): int(count) for label, count in zip(labels, row) if count}, sort_keys=True)
               for row in counts.itertuples(index=False, name=None)]
    return pd.Series(encoded, index=pd.DatetimeIndex(counts.index))


def quality_acceptance(frame, *, qc_mode, rules, missing_qc_is_good):
    """True where a source record passes every configured QC bit rule.

    Each rule extracts ``(word >> offset) & mask`` from column ``column`` and
    accepts values in ``allowed``. A missing QC word passes only if
    ``missing_qc_is_good``.
    """
    if qc_mode == 'retain_flagged':
        return pd.Series(True, index=frame.index)
    if qc_mode != 'bit_rules' or not rules:
        raise ValueError('QC mode must be retain_flagged or bit_rules with at least one rule')
    good = pd.Series(True, index=frame.index)
    for rule in rules:
        source = frame[rule.column]
        present = source.notna()
        if ((source.loc[present] % 1) != 0).any() or (source.loc[present] < 0).any():
            raise ValueError('QC words must be nonnegative integers')
        word = source.where(present, 0).astype('int64').to_numpy()  # placeholder 0 is masked by `present`
        decoded = pd.Series((word >> rule.offset) & rule.mask, index=frame.index)
        good &= (present & decoded.isin(rule.allowed)) | (~present & missing_qc_is_good)
    return good


def interval_days(frame, dates, *, qc_mode, rules, missing_qc_is_good, categorical=False):
    """Assign each composite's value to the days of its own ``[start, end)`` interval only.

    Returns a frame indexed by date with ``value``, ``missing_reason``,
    ``source_start``, ``source_end_exclusive`` and every ``qc_*`` column.
    Days outside every interval are ``no_source_interval``; nothing is carried.
    """
    frame = frame.copy()
    for key in ('start', 'end'):
        frame[key] = pd.to_datetime(frame[key], utc=True)
        if frame[key].isna().any() or not frame[key].equals(frame[key].dt.normalize()):
            raise ValueError('Source intervals require valid UTC calendar boundaries')
    frame = frame.sort_values('start').reset_index(drop=True)
    if (frame.end <= frame.start).any() or (frame.start.iloc[1:].to_numpy() < frame.end.iloc[:-1].to_numpy()).any():
        raise ValueError('Source intervals must be positive and nonoverlapping')
    if categorical and frame.value.dropna().map(lambda x: float(x) % 1 != 0).any():
        raise ValueError('Categorical source values must be integer codes')
    good = quality_acceptance(frame, qc_mode=qc_mode, rules=rules, missing_qc_is_good=missing_qc_is_good)
    intervals = pd.IntervalIndex.from_arrays(pd.DatetimeIndex(frame.start.dt.tz_localize(None)).as_unit('ns'),
                                             pd.DatetimeIndex(frame.end.dt.tz_localize(None)).as_unit('ns'),
                                             closed='left')
    positions = intervals.get_indexer(dates.as_unit('ns'))
    matched = positions >= 0
    rows = frame.iloc[positions[matched]].copy()
    rows.index = dates[matched]
    accepted = pd.Series(good.iloc[positions[matched]].to_numpy(), index=rows.index)
    output = pd.DataFrame(index=dates)
    output['value'] = rows['value'].where(accepted).reindex(dates).astype('Int64' if categorical else 'float64')
    reason = pd.Series('available', index=rows.index)
    reason.loc[~accepted] = 'quality_masked'
    required = [rule.column for rule in rules] if qc_mode == 'bit_rules' else []
    if required and not missing_qc_is_good:
        reason.loc[rows[required].isna().any(axis=1)] = 'missing_qc'
    reason.loc[rows.value.isna()] = 'missing_value'
    output['missing_reason'] = reason.reindex(dates, fill_value='no_source_interval')
    output['source_start'] = rows.start.dt.tz_localize(None).reindex(dates)
    output['source_end_exclusive'] = rows.end.dt.tz_localize(None).reindex(dates)
    for qc in [c for c in frame if c.startswith('qc_')]:
        output[qc] = rows[qc].reindex(dates).astype('Int64')
    return output


def align_daily(values, dates, *, source_present=None, source_null=None, source_absent=None,
                null_is_zero=False, absent_is_zero=False):
    """Align native daily values one-to-one with the daily grid; never average or fill.

    Returns a frame indexed by date with ``value``, ``missing_reason`` and, when
    given, the boolean ``provider_null`` / ``provider_absent_row`` masks.
    """
    values = values.copy()
    if not isinstance(values.index, pd.DatetimeIndex) or values.index.hasnans:
        raise ValueError('Daily inputs require a nonmissing DatetimeIndex')
    if values.index.tz is not None:
        values.index = values.index.tz_convert('UTC').tz_localize(None)
    if values.index.has_duplicates or not values.index.equals(values.index.normalize()):
        raise ValueError('Daily source dates must be unique UTC midnights')
    result = pd.DataFrame({'value': values.reindex(dates)}, index=dates)
    present = (pd.Series(True, index=values.index) if source_present is None
               else source_present.reindex(values.index))
    reason = pd.Series('available', index=dates)
    reason.loc[result['value'].isna()] = 'missing_value'
    reason.loc[~present.reindex(dates, fill_value=False).fillna(False)] = 'no_source_record'
    for mask, label, as_zero in ((source_null, 'provider_null', null_is_zero),
                                 (source_absent, 'provider_absent_row', absent_is_zero)):
        if mask is None:
            continue
        flag = mask.reindex(dates).astype('boolean')
        reason.loc[flag.fillna(False)] = label + ('_set_to_zero' if as_zero else '')
        if as_zero:
            result.loc[flag.fillna(False).to_numpy(), 'value'] = 0.0
        result[label] = flag
    result['missing_reason'] = reason
    return result
