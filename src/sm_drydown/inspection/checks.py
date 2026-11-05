"""Diagnostic candidates, not additional data-cleaning or exclusion rules."""
import numpy as np
import pandas as pd


def flatlines(values, dates, minimum_days=3):
    """Find maximal exact-repeat runs on consecutive daily dates; missing breaks runs."""
    if minimum_days < 2:
        raise ValueError('Flatline minimum must be at least two daily records')
    x = np.asarray(values, dtype=float)
    dates = pd.DatetimeIndex(dates)
    if len(x) != len(dates) or dates.has_duplicates or not dates.is_monotonic_increasing:
        raise ValueError('Flatline values need unique, increasing daily dates')
    if dates.hasnans or not dates.equals(dates.normalize()):
        raise ValueError('Flatline dates must be calendar-day boundaries')
    if not len(x):
        return []
    consecutive = np.diff(dates.to_numpy(dtype='datetime64[D]')) == np.timedelta64(1, 'D')
    same = np.r_[False, consecutive & np.isfinite(x[:-1]) & np.isfinite(x[1:]) & (x[1:] == x[:-1])]
    starts = np.r_[0, np.flatnonzero(~same[1:]) + 1]
    stops = np.r_[starts[1:], len(x)]
    return [{'start': dates[first], 'end': dates[stop-1], 'consecutive_days': int(stop-first),
             'value': float(x[first])}
            for first, stop in zip(starts, stops) if stop-first >= minimum_days and np.isfinite(x[first])]


def precipitation_extremes(values, quantile=.999):
    """Strictly exceed the station's empirical positive-value percentile; keep zero separate."""
    if not 0 < quantile <= 1:
        raise ValueError('Precipitation quantile must be in (0, 1]')
    x = np.asarray(values, dtype=float)
    positive = x[np.isfinite(x) & (x > 0)]
    threshold = float(np.quantile(positive, quantile, method='linear')) if len(positive) else np.nan
    return np.isfinite(x) & (x > threshold), threshold, int(len(positive))


def seasonal_precipitation(values, dates):
    """Observed meteorological-season sums with coverage and complete-only totals.

    DJF is labeled by January's year. Partial edge seasons and missing days are
    never extrapolated. Negative values, if any, remain in observed sums and are
    counted explicitly; this function does not silently clean precipitation.
    """
    x = np.asarray(values, dtype=float)
    dates = pd.DatetimeIndex(dates)
    if len(x) != len(dates) or dates.has_duplicates or dates.hasnans or not dates.equals(dates.normalize()):
        raise ValueError('Seasonal precipitation requires unique daily dates and matching values')
    frame = pd.DataFrame({'date': dates, 'value': x})
    month = frame.date.dt.month
    frame['season'] = np.select([month.isin([12, 1, 2]), month.isin([3, 4, 5]),
                                month.isin([6, 7, 8])], ['DJF', 'MAM', 'JJA'], default='SON')
    frame['season_year'] = frame.date.dt.year + month.eq(12).astype(int)
    frame['finite'] = np.isfinite(frame.value)
    frame['negative'] = frame.finite & frame.value.lt(0)
    frame['nonfinite'] = ~frame.finite & frame.value.notna()
    frame['observed'] = frame.value.where(frame.finite)
    groups = frame.groupby(['season_year', 'season'], sort=True)
    sums = groups['observed'].sum(min_count=1)
    sizes, counts = groups.size(), groups['finite'].sum()
    negatives, nonfinite = groups['negative'].sum(), groups['nonfinite'].sum()
    rows = []
    for (year, season), total in sums.items():
        start_month = {'DJF': 12, 'MAM': 3, 'JJA': 6, 'SON': 9}[season]
        start = pd.Timestamp(int(year)-(season == 'DJF'), start_month, 1)
        end = start + pd.DateOffset(months=3)
        expected = (end-start).days
        observed = int(counts.loc[year, season])
        complete = observed == expected
        rows.append({'season_year': int(year), 'season': season, 'season_start': start,
            'season_end_exclusive': end, 'calendar_days': expected,
            'days_in_selection': int(sizes.loc[year, season]), 'observed_days': observed,
            'missing_days_in_selection': int(sizes.loc[year, season])-observed,
            'coverage_fraction': observed/expected, 'negative_days': int(negatives.loc[year, season]),
            'nonfinite_days': int(nonfinite.loc[year, season]), 'observed_sum_mm': total,
            'complete_season_total_mm': total if complete else np.nan,
            'complete_season': bool(complete)})
    return pd.DataFrame(rows)


def gldas_checks(fields, sum_tolerance=.01):
    """Check fractions/porosity in [0,1], FAO 1–16 codes and sand+silt+clay closure."""
    if not 0 <= sum_tolerance < 1:
        raise ValueError('GLDAS closure tolerance must be in [0, 1)')
    result = fields.copy()
    continuous = ['sand_fraction', 'silt_fraction', 'clay_fraction', 'porosity']
    for field in continuous:
        x = result[field].to_numpy(dtype=float)
        result[field+'_outside_physical_range'] = ~np.isnan(x) & (~np.isfinite(x) | (x < 0) | (x > 1))
    x = result['soil_texture_class'].to_numpy(dtype=float)
    finite = np.isfinite(x)
    nearest = np.rint(x)
    result['invalid_texture_code'] = ~np.isnan(x) & (~finite | (np.abs(x-nearest) > 1e-4)
                                                   | (nearest < 1) | (nearest > 16))
    fractions = result[['sand_fraction', 'silt_fraction', 'clay_fraction']].to_numpy(dtype=float)
    available = np.isfinite(fractions).all(axis=1)
    total = np.full(len(result), np.nan)
    total[available] = fractions[available].sum(axis=1)
    deviation = np.abs(total-1)
    result['sand_silt_clay_sum'] = total
    result['closure_absolute_error'] = deviation
    result['closure_evaluable'] = available
    # Only guard float64 arithmetic at the exact decision boundary, not source
    # precision/uncertainty; raw values and deviations remain unchanged.
    result['closure_outside_tolerance'] = available & (deviation > sum_tolerance) & ~np.isclose(
        deviation, sum_tolerance, atol=1e-12, rtol=0)
    result['any_physical_range_failure'] = result[[f+'_outside_physical_range' for f in continuous]
        + ['invalid_texture_code']].any(axis=1)
    return result


def numeric_summary(values):
    """Describe finite displayed values; report missing/nonfinite separately, with no filling."""
    x = np.asarray(values, dtype=float)
    valid = x[np.isfinite(x)]
    result = {'records': int(x.size), 'finite': int(valid.size), 'missing': int(np.isnan(x).sum()),
              'nonfinite_nonmissing': int(np.isinf(x).sum()), 'std_ddof': 0}
    if not valid.size:
        return {**result, **dict.fromkeys(['min', 'p01', 'q25', 'median', 'q75', 'p99', 'max', 'mean', 'std'])}
    quantiles = np.quantile(valid, [.01, .25, .5, .75, .99])
    return {**result, 'min': float(valid.min()), 'max': float(valid.max()),
        **{key: float(v) for key, v in zip(['p01', 'q25', 'median', 'q75', 'p99'], quantiles)},
        'mean': float(valid.mean()), 'std': float(valid.std(ddof=0))}


class Samples:
    """Keep finite values for exact distributions without storing repeated missing days."""
    def __init__(self):
        self.parts = []
        self.records = self.missing = self.nonfinite = 0
        self._values = None

    def add(self, values):
        if self._values is not None:
            raise ValueError('Distribution was already finalized')
        x = np.asarray(values, dtype=float)
        self.records += x.size
        self.missing += int(np.isnan(x).sum())
        self.nonfinite += int(np.isinf(x).sum())
        finite = x[np.isfinite(x)]
        if finite.size:
            self.parts.append(finite)

    def values(self):
        if self._values is None:
            self._values = np.concatenate(self.parts) if self.parts else np.array([], dtype=float)
            self.parts.clear()
        return self._values

    def summary(self, categorical=False):
        values = self.values()
        if categorical:
            codes, counts = np.unique(values, return_counts=True)
            result = {'finite': int(values.size), 'class_counts': {str(int(c)): int(n) for c,n in zip(codes, counts)}}
        else:
            result = numeric_summary(values)
        return {**result, 'records': self.records, 'missing': self.missing,
                'nonfinite_nonmissing': self.nonfinite}

    def plot_sample(self):
        return {'values': self.values(), 'missing': self.missing}
