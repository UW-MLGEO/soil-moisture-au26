"""Extract native station cells without masking, interpolation or gap filling.

Decoded arrays are derived extracts, stored separately from native HTTP bodies.
"""

import hashlib
import json
from pathlib import Path

import h5py
import numpy as np
import pandas as pd
from xarray.coding.times import decode_cf_datetime

from sm_drydown.storage.files import sha256, utc_now, write_json


def native_indices(axis, values):
    """Nearest native grid centers; exact midpoint ties use greater coordinate.

    Round float32 geographic centers to six decimals to remove representation
    noise before deterministic tie handling. Returned coordinates remain native.
    """
    axis = np.asarray(axis)
    if axis.ndim != 1 or len(axis) < 2 or not np.isfinite(axis).all():
        raise ValueError('Expected a finite one-dimensional native coordinate axis')
    order = np.argsort(axis)
    sorted_axis = np.round(axis[order].astype(float), 6)
    if np.any(np.diff(sorted_axis) <= 0):
        raise ValueError('Native coordinates must be unique')
    values = np.asarray(values, dtype=float)
    edges = (sorted_axis[:-1] + sorted_axis[1:]) / 2
    outer_low = sorted_axis[0] - (sorted_axis[1] - sorted_axis[0]) / 2
    outer_high = sorted_axis[-1] + (sorted_axis[-1] - sorted_axis[-2]) / 2
    if not np.isfinite(values).all() or np.any(values < outer_low) or np.any(values >= outer_high):
        raise ValueError('Requested station falls outside native grid-cell bounds')
    return order[np.searchsorted(edges, values, side='right')]


def _plain(value):
    if isinstance(value, bytes):
        return value.decode()
    if isinstance(value, np.ndarray):
        return [_plain(item) for item in value.tolist()] if value.ndim else _plain(value.item())
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, np.generic):
        return _plain(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return str(value)
    return value


def config_fingerprint(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, allow_nan=False).encode()).hexdigest()


def creation_properties(source, values):
    """Retain HDF5 creation fill metadata separately from native CF attributes."""
    properties = values.id.get_create_plist()
    statuses = {h5py.h5d.FILL_VALUE_UNDEFINED: 'undefined',
                h5py.h5d.FILL_VALUE_DEFAULT: 'default',
                h5py.h5d.FILL_VALUE_USER_DEFINED: 'user_defined'}
    times = {h5py.h5d.FILL_TIME_ALLOC: 'allocation', h5py.h5d.FILL_TIME_NEVER: 'never',
             h5py.h5d.FILL_TIME_IFSET: 'if_set'}
    status = statuses[properties.fill_value_defined()]
    return {'dtype': values.dtype.str, 'fill_value_status': status,
            'fill_time': times[properties.get_fill_time()],
            'fill_value': _plain(values.fillvalue) if status != 'undefined' else None,
            'netcdf_file_attributes': {key: _plain(source.attrs[key])
                                       for key in ('_NCProperties', '_nc3_strict') if key in source.attrs}}


def extract_native_cells(file, config, output_path, *, asset=None):
    """Read configured variable only at station cells; return extraction metadata.

    HDF5 chunks may contain surrounding cells; they are needed for decompression.
    Each spatial chunk is decoded once per source time chunk. Raw encoded numeric
    fill values, scale/offset attributes and missing values are retained unchanged.
    """
    variables = (asset or {}).get('variables', config['variables'])
    stations = config['stations']
    with h5py.File(file, 'r') as source:
        lat = source[variables['latitude']][:]
        lon = source[variables['longitude']][:]
        iy = native_indices(lat, [s['latitude'] for s in stations])
        ix = native_indices(lon, [s['longitude'] for s in stations])
        values = source[variables['value']]
        if values.ndim != 3 or values.shape[1:] != (len(lat), len(lon)):
            raise ValueError('Expected source variable with dimensions time, latitude, longitude')
        attrs = {key: _plain(value) for key, value in values.attrs.items()
                 if key in ('units', '_FillValue', 'missing_value', 'scale_factor', 'add_offset', 'long_name')}
        creation = creation_properties(source, values)
        time = source[variables['time']]
        units = _plain(time.attrs['units'])
        calendar = _plain(time.attrs.get('calendar', 'standard'))
        dates = pd.DatetimeIndex(decode_cf_datetime(time[:], units, calendar, use_cftime=False))
        if dates.has_duplicates or dates.hasnans:
            raise ValueError('Source date coordinate has duplicates or missing dates')
        static = config.get('static', False)
        window = asset or config
        window_start = max(config['start'], window.get('start', config['start']))
        window_end = min(config['end'], window.get('end', config['end']))
        if static:
            selected = np.arange(len(dates))
        else:
            selected = np.flatnonzero((dates >= window_start) &
                                      (dates < pd.Timestamp(window_end) + pd.Timedelta(days=1)))
        if not len(selected):
            raise ValueError('Source has no dates inside the requested window')
        if not static and not (dates[selected] == dates[selected].normalize()).all():
            raise ValueError('Expected daily source coordinates at the start of each UTC day')
        expected = (pd.DatetimeIndex([]) if static else
                    pd.date_range(window_start, window_end, freq='D'))
        missing_dates = expected.difference(dates[selected])
        chunks = values.chunks or (1, len(lat), len(lon))
        output = np.empty((len(selected), len(stations)), dtype=values.dtype)
        groups = {}
        for station_index, (y, x) in enumerate(zip(iy, ix)):
            groups.setdefault((int(y // chunks[1]), int(x // chunks[2])), []).append(station_index)
        time_groups = {}
        for output_index, source_index in enumerate(selected):
            time_groups.setdefault(int(source_index // chunks[0]), []).append((output_index, int(source_index)))
        for time_group, positions in time_groups.items():
            t0 = time_group * chunks[0]
            t1 = min(t0 + chunks[0], values.shape[0])
            for (cy, cx), station_positions in groups.items():
                y0, x0 = cy * chunks[1], cx * chunks[2]
                block = values[t0:t1, y0:min(y0+chunks[1], len(lat)), x0:min(x0+chunks[2], len(lon))]
                for out_index, source_index in positions:
                    output[out_index, station_positions] = block[
                        source_index-t0, iy[station_positions]-y0, ix[station_positions]-x0]
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp = output_path.with_suffix('.part')
    with temp.open('wb') as stream:
        np.savez_compressed(stream, values=output, dates=dates[selected].to_numpy(dtype='datetime64[ns]'),
                            station_ids=np.array([s['station_id'] for s in stations]),
                            latitude_indices=iy, longitude_indices=ix,
                            native_latitudes=lat[iy], native_longitudes=lon[ix])
    temp.replace(output_path)
    report = {'created_at': utc_now(), 'format': 'NumPy NPZ; values are unscaled source numeric values',
              'configuration_fingerprint': config_fingerprint(config),
              'sha256': sha256(output_path), 'station_count': len(stations), 'date_count': len(selected),
              'first_date': dates[selected[0]].isoformat(), 'last_date': dates[selected[-1]].isoformat(),
              'source_variable': variables['value'], 'source_attributes': attrs,
              'source_creation_properties': creation,
              'source_calendar': calendar, 'source_time_units': units,
              'expected_date_count': len(expected) if not static else None,
              'missing_source_dates': [day.date().isoformat() for day in missing_dates],
              'sampling': config['sampling'], 'quality_filtering': False, 'gap_filling': False,
              'interpolation': False, 'missing_value_replacement': False,
              'spatial_source_chunks': len(groups), 'static': static}
    sidecar = output_path.with_suffix('.json')
    if sidecar.exists():
        existing = json.loads(sidecar.read_text())
        if all(existing.get(key) == value for key, value in report.items() if key != 'created_at'):
            return existing
        # Existing immutable reports with explicit CF missing-code attributes
        # need no creation-fill inference. Preserve their already-published
        # metadata instead of adding an irrelevant field on an ordinary resume.
        if ('source_creation_properties' not in existing and
                ('_FillValue' in attrs or 'missing_value' in attrs) and
                all(existing.get(key) == value for key, value in report.items()
                    if key not in ('created_at', 'source_creation_properties'))):
            return existing
    write_json(sidecar, report)
    return report

