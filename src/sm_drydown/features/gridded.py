"""Gridded sources at station cells: CHIRPS, GLDAS and MODIS extracts -> station features.

Acquisition already sampled each source at the native cell containing (CHIRPS,
MODIS) or nearest to (GLDAS) each station and stored the *encoded* values. Here
they are decoded with the provider's declared attributes and the rules in
config/pipeline.yml. A station absent from an extract (outside the source's
coverage) gets ``missing_reason = outside_source_coverage``.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

from sm_drydown.features.common import align_daily, decode_values, interval_days
from sm_drydown.formats.cf import decode_cf_values
from sm_drydown.storage.files import sha256

PERCENT_UNITS = ('%', 'percent', 'percentage')
FRACTION_UNITS = ('1', '-', 'fraction', 'm3/m3', 'm^3/m^3', 'cm3/cm3', 'cm^3/cm^3')
GLDAS_FIELDS = {'soil_texture': 'gldas_texture_class', 'soil_texture_class': 'gldas_texture_class',
                'sand_fraction': 'gldas_sand_frac', 'silt_fraction': 'gldas_silt_frac',
                'clay_fraction': 'gldas_clay_frac', 'porosity': 'gldas_porosity'}


class ExtractCatalog:
    """Hash-verified station-cell extracts of one acquisition (locks/<source>.json)."""

    def __init__(self, request_path, cache_dir):
        self.request_path = Path(request_path)
        self.request = json.loads(self.request_path.read_text())
        self.assets, self.inputs = [], {}
        for asset in self.request['assets']:
            file = Path(cache_dir) / self.request['collection_id'] / 'extracts' / f"{asset['asset_id']}.npz"
            if not file.exists() or not file.with_suffix('.json').exists():
                raise FileNotFoundError(f'Missing acquisition extract {file}: run `pixi run acquire` or `pixi run restore`')
            meta = json.loads(file.with_suffix('.json').read_text())
            if sha256(file) != meta['sha256']:
                raise ValueError(f'Corrupt acquisition extract: {file}')
            with np.load(file, allow_pickle=False) as content:
                arrays = {key: content[key] for key in content.files}
            index = {int(sid): j for j, sid in enumerate(arrays['station_ids'])}
            if len(index) != len(arrays['station_ids']):
                raise ValueError(f'Duplicate stations in {file}')
            self.assets.append((asset, arrays, meta, index))
            self.inputs[file.name] = meta['sha256']
            self.inputs[file.with_suffix('.json').name] = sha256(file.with_suffix('.json'))

    def covers(self, station_id):
        return all(station_id in index for *_, index in self.assets)

    def cell(self, station_id):
        """Native cell coordinates of the station (first asset), or None outside coverage."""
        if not self.covers(station_id):
            return None
        _, arrays, _, index = self.assets[0]
        j = index[station_id]
        return {k: (arrays[k][j].item() if hasattr(arrays[k][j], 'item') else arrays[k][j])
                for k in ('native_latitudes', 'native_longitudes', 'latitude_indices', 'longitude_indices')
                if k in arrays and len(arrays[k])}


def outside_coverage(dates, columns):
    frame = pd.DataFrame(index=dates)
    for name, dtype in columns.items():
        frame[name] = pd.Series(pd.NA if dtype in ('Int64', 'boolean') else np.nan, index=dates, dtype=dtype)
    frame['missing_reason'] = 'outside_source_coverage'
    return frame


def chirps_source(catalog, station_id):
    """Provider rows for one station: value plus disjoint provider-null and absent-row masks."""
    pieces = []
    for _, arrays, _, index in catalog.assets:
        j = index[station_id]
        values = arrays['values'][:, j].astype(float)
        null, absent = arrays['null_mask'][:, j], arrays['absent_row_mask'][:, j]
        if null.dtype != bool or absent.dtype != bool or np.any(null & absent):
            raise ValueError('CHIRPS null/absent masks must be disjoint boolean arrays')
        if not np.array_equal(np.isnan(values), null | absent):
            raise ValueError('CHIRPS values and explicit missingness masks disagree')
        pieces.append(pd.DataFrame({'value': values, 'source_present': ~absent, 'provider_null': null,
                                    'provider_absent_row': absent}, index=pd.DatetimeIndex(arrays['dates'])))
    return pd.concat(pieces).sort_index()


def chirps_daily(catalog, station_id, dates, cfg_chirps):
    """Daily precipitation; provider nulls and absent rows stay distinct and are never zero rain."""
    if not catalog.covers(station_id):
        return outside_coverage(dates, {'value': 'float64', 'provider_null': 'boolean',
                                        'provider_absent_row': 'boolean'})
    source = chirps_source(catalog, station_id)
    return align_daily(source.value, dates, source_present=source.source_present,
                       source_null=source.provider_null, source_absent=source.provider_absent_row,
                       null_is_zero=cfg_chirps.null_is_zero_rain, absent_is_zero=cfg_chirps.absent_row_is_zero_rain)


def snap_texture(value, tolerance):
    """(FAO class or None, whether float32 noise was removed); error if not within tolerance of an integer."""
    if value is None or pd.isna(value):
        return None, False
    if not np.isfinite(value):
        raise ValueError(f'Non-finite GLDAS texture class: {value!r}')
    nearest = round(float(value))
    if abs(value - nearest) > tolerance:
        raise ValueError(f'GLDAS texture class {value!r} is not within {tolerance} of an integer')
    return int(nearest), value != nearest


def gldas_static(catalog, station_id, cfg_gldas):
    """One row of static soil properties, with a missing reason and audit per field."""
    row = {'station_id': station_id}
    if not catalog.covers(station_id):
        for column in dict.fromkeys(GLDAS_FIELDS.values()):
            row[column], row[column + '__missing_reason'] = None, 'outside_source_coverage'
        return row
    low, high = cfg_gldas.fraction_bounds
    for asset, arrays, meta, index in catalog.assets:
        j = index[station_id]
        column = GLDAS_FIELDS[asset['field']]
        if column in row:
            raise ValueError('Multiple GLDAS assets map to one static field')
        attrs = meta['source_attributes']
        decoded, _ = decode_cf_values(arrays['values'][:, j], attrs,
                                      creation_properties=meta.get('source_creation_properties'))
        if len(decoded) != 1:
            raise ValueError('Static GLDAS extract requires exactly one value per station and field')
        value = float(decoded[0])
        if column == 'gldas_texture_class':
            raw = None if np.isnan(value) else value
            value, snapped = snap_texture(raw, cfg_gldas.texture_snap_tolerance)
            row['gldas_texture_source_value'], row['gldas_texture_float_noise_removed'] = raw, snapped
        else:
            units = str(attrs.get('units', '')).strip().lower()
            if units in PERCENT_UNITS and cfg_gldas.percent_to_fraction:
                value /= 100
            elif units not in FRACTION_UNITS:
                raise ValueError(f'Unresolved GLDAS units for {column}: {units!r}')
            if np.isfinite(value) and not low <= value <= high:
                raise ValueError(f'GLDAS {column} = {value} outside gldas.fraction_bounds')
            value = None if np.isnan(value) else value
        row[column] = value
        row[column + '__missing_reason'] = 'missing_static_value' if value is None else 'available'
        row[column + '__cell'] = f"{arrays['latitude_indices'][j]}:{arrays['longitude_indices'][j]}"
    return row


def modis_intervals(catalog, station_id, band, qc_bands):
    """Decoded composites [start, end) for one band plus raw QC words (qc_<band>)."""
    pieces = []
    for _, arrays, meta, index in catalog.assets:
        j = index[station_id]
        bands = list(arrays['bands'].astype(str))
        frame = pd.DataFrame({'start': arrays['interval_start'], 'end': arrays['interval_end']})
        for source_band, target in [(band, 'value')] + [(b, 'qc_' + b) for b in qc_bands]:
            b = bands.index(source_band)
            raw = arrays['values'][:, j, b].copy().astype(float)
            raw[arrays['null_mask'][:, j, b] | arrays['absent_row_mask'][:, j]] = np.nan
            attrs = meta['source_attributes'][source_band]
            codes = np.atleast_1d(attrs.get('_FillValue', [])).tolist()
            if 'missing_value' in attrs:
                codes += np.atleast_1d(attrs['missing_value']).tolist()
            bounds = attrs.get('valid_range')
            if bounds is None and 'valid_min' in attrs and 'valid_max' in attrs:
                bounds = [attrs['valid_min'], attrs['valid_max']]
            decoded, _ = decode_values(raw, missing_codes=codes, scale_factor=attrs.get('scale_factor', 1),
                                       add_offset=attrs.get('add_offset', 0), valid_range=bounds)
            frame[target] = decoded.to_numpy()
        pieces.append(frame)
    return pd.concat(pieces, ignore_index=True)


def modis_daily(catalog, station_id, dates, section, *, band, categorical):
    """Daily values of one MODIS band, each confined to its own composite interval."""
    if not catalog.covers(station_id):
        return outside_coverage(dates, {'value': 'Int64' if categorical else 'float64'})
    frame = modis_intervals(catalog, station_id, band, section.qc_bands)
    rules = getattr(section, 'qc_rules', [])
    return interval_days(frame, dates, qc_mode=section.qc_mode, rules=rules,
                         missing_qc_is_good=getattr(section, 'missing_qc_is_good', False), categorical=categorical)


def _station_days(stations, dates, column, daily_of):
    parts = []
    for s in stations:
        daily = daily_of(s['station_id']).rename(columns={'value': column}).rename_axis('date').reset_index()
        daily.insert(0, 'station_id', s['station_id'])
        parts.append(daily)
    return pd.concat(parts, ignore_index=True)


def build_chirps(cfg, inputs, stations, dates):
    return {'precip_chirps_mm_day': _station_days(stations, dates, 'precip_chirps_mm_day',
                                                  lambda sid: chirps_daily(inputs.catalog, sid, dates, cfg.chirps))}


def build_gldas(cfg, inputs, stations, dates):
    frame = pd.DataFrame([gldas_static(inputs.catalog, s['station_id'], cfg.gldas) for s in stations])
    frame['gldas_texture_class'] = frame['gldas_texture_class'].astype('Int64')
    for column in ('gldas_sand_frac', 'gldas_silt_frac', 'gldas_clay_frac', 'gldas_porosity',
                   'gldas_texture_source_value'):
        frame[column] = frame[column].astype('float64')
    frame['gldas_texture_float_noise_removed'] = frame['gldas_texture_float_noise_removed'].astype(bool)
    return {'gldas_static': frame}


def build_lai(cfg, inputs, stations, dates):
    lai = cfg.modis_lai
    return {'lai_m2m2': _station_days(stations, dates, 'lai_m2m2', lambda sid: modis_daily(
        inputs.catalog, sid, dates, lai, band=lai.band, categorical=False))}


def build_land_cover(cfg, inputs, stations, dates):
    lc = cfg.modis_land_cover
    return {'igbp_class': _station_days(stations, dates, 'igbp_class', lambda sid: modis_daily(
        inputs.catalog, sid, dates, lc, band=lc.scheme, categorical=True))}
