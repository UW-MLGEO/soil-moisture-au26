"""Synthetic acquisition caches for the committed smoke selection (data/selections/smoke/).

``build(root)`` writes, under ``root``: a paths file, plan locks for the smoke
run, and caches shaped exactly like real acquisitions (ISMN ZIP + checksum
sidecar; CHIRPS/GLDAS/MODIS station-cell NPZ extracts + JSON sidecars). Every
edge case the pipeline must keep visible is present once:

- ISMN: a non-G flag day (quality_masked), a NaN value (missing_values), a gap
  inside a sensor's record (no_observations rows), a record ending before the
  window end (observed span shorter than the window), an exact 3-day flatline,
  a soil-temperature sensor and an ignored precipitation member;
- CHIRPS: a provider null, an absent row, a 0 mm day and one extreme day;
- GLDAS: float32 texture noise (2.9999998 -> class 3), percent fractions and a
  missing porosity;
- MODIS LAI: a QC-rejected composite (SCF_QC = 2) and a composite with missing
  QC; land cover with a fill code at one station.
"""

import json
import zipfile
from pathlib import Path

import numpy as np
import yaml

from sm_drydown.config import load_config
from sm_drydown.locks import write_lock
from sm_drydown.selections import load_selection
from sm_drydown.storage.files import sha256, write_json

SOURCES = ('ismn', 'chirps', 'gldas', 'modis_lai', 'modis_land_cover')
DAYS = np.arange(np.datetime64('2024-02-25'), np.datetime64('2024-03-11'))  # 15 days incl. 29 Feb


def _member(network, station_dir, variable, depth, sensor, rows):
    name = (f'{network}/{station_dir}/{network}_{network}_{station_dir}_{variable}_{depth:.6f}_{depth:.6f}_'
            f'{sensor}_20240101_20241231.stm')
    header = f'SMOKE {network} {station_dir} 40.0 -100.0 100.0 {depth} {depth} {sensor}'
    return name, header + '\n' + ''.join(f'{d} {t} {v} {f} M\n' for d, t, v, f in rows)


def _stamp(day):
    return str(day).replace('-', '/')


def ismn_archive(path):
    alpha_a = [(_stamp(d), h, 0.30 if 3 <= i <= 5 else round(0.20 + 0.01 * i, 4), 'G')
               for i, d in enumerate(DAYS) for h in ('06:00', '18:00')]
    alpha_b = [(_stamp(d), '12:00', 'NaN' if i == 1 else 0.25, 'D01' if i == 2 else 'G') for i, d in enumerate(DAYS)]
    temperature = [(_stamp(d), '12:00', 5.5 + i, 'G') for i, d in enumerate(DAYS)]
    beta = [(_stamp(d), '00:00', 0.15, 'G') for i, d in enumerate(DAYS) if i not in (5, 6, 7) and i <= 12]
    members = [
        _member('SMOKE', 'AlphaSite', 'sm', 0.05, 'ProbeA', alpha_a),
        _member('SMOKE', 'AlphaSite', 'sm', 0.05, 'ProbeB', alpha_b),
        _member('SMOKE', 'AlphaSite', 'ts', 0.05, 'Thermo', temperature),
        _member('SMOKE', 'AlphaSite', 'p', 0.0, 'Gauge', [(_stamp(DAYS[0]), '00:00', 1.0, 'G')]),
        _member('SMOKE', 'Beta-Site', 'sm', 0.1, 'ProbeC', beta),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w') as archive:
        for name, body in members:
            archive.writestr(name, body)
    write_json(path.with_name(path.name + '.provenance.json'), {'sha256': sha256(path)})


def _extract(folder, asset_id, arrays, attributes):
    path = Path(folder) / f'{asset_id}.npz'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as stream:
        np.savez_compressed(stream, **arrays)
    write_json(path.with_suffix('.json'), {'sha256': sha256(path), 'source_attributes': attributes})


def gridded(cache, requests):
    ids = np.array([1, 2])
    lat, lon = np.array([40.025, 30.525]), np.array([-100.025, -90.225])
    common = {'station_ids': ids, 'native_latitudes': lat, 'native_longitudes': lon}
    # CHIRPS: one asset (2024); station 1 null on day 1, station 2 absent row on day 4, 200 mm on day 10.
    values = np.zeros((len(DAYS), 2))
    values[10, 0] = 200.0
    values[[3, 8], 1] = [5.0, 12.5]
    null, absent = np.zeros_like(values, dtype=bool), np.zeros_like(values, dtype=bool)
    null[1, 0] = absent[4, 1] = True
    values[null | absent] = np.nan
    folder = Path(cache) / requests['chirps']['collection_id'] / 'extracts'
    _extract(folder, '2024', {**common, 'values': values, 'dates': DAYS.astype('datetime64[ns]'),
                              'null_mask': null, 'absent_row_mask': absent}, {})
    # GLDAS: five static fields.
    folder = Path(cache) / requests['gldas']['collection_id'] / 'extracts'
    fields = {'soil_texture': ([2.9999998, 6.0], 'category', np.float32), 'sand_fraction': ([40, 20], '%', float),
              'silt_fraction': ([30, 45], '%', float), 'clay_fraction': ([30, 35], '%', float),
              'porosity': ([0.45, np.nan], '1', float)}
    for asset in requests['gldas']['assets']:
        raw, units, dtype = fields[asset['field']]
        _extract(folder, asset['asset_id'], {**common, 'values': np.array([raw], dtype=dtype),
                                             'dates': DAYS[:1].astype('datetime64[ns]'),
                                             'latitude_indices': np.array([520, 482]),
                                             'longitude_indices': np.array([319, 358])}, {'units': units})
    # MODIS LAI: 4-day composites; composite 1 SCF_QC = 2 (rejected), composite 2 QC missing.
    lai = requests['modis_lai']
    starts = np.array(['2024-02-22', '2024-02-26', '2024-03-01', '2024-03-05', '2024-03-09'], dtype='datetime64[D]')
    bands = lai['bands']
    values = np.zeros((len(starts), 2, len(bands)))
    values[:, :, bands.index('Lai')] = [[12, 20], [15, 22], [18, 25], [21, 28], [24, 31]]
    values[1, :, bands.index('FparLai_QC')] = 2 << 5
    null = np.zeros_like(values, dtype=bool)
    null[2, :, bands.index('FparLai_QC')] = True
    values[null] = np.nan
    folder = Path(cache) / lai['collection_id'] / 'extracts'
    _extract(folder, '2024', {**common, 'values': values, 'null_mask': null,
                              'absent_row_mask': np.zeros((len(starts), 2), dtype=bool), 'bands': np.array(bands),
                              'dates': starts.astype('datetime64[ns]'),
                              'interval_start': starts.astype('datetime64[ns]'),
                              'interval_end': (starts + np.timedelta64(4, 'D')).astype('datetime64[ns]')}, lai['source_attributes'])
    # Land cover: one annual value; station 2 holds the fill code 255.
    lc = requests['modis_land_cover']
    values = np.full((1, 2, len(lc['bands'])), 7.0)
    values[0, :, lc['bands'].index('LC_Type1')] = [10, 255]
    attributes = {b: {'_FillValue': 255, 'valid_range': [1, 17] if b == 'LC_Type1' else [0, 254]} for b in lc['bands']}
    folder = Path(cache) / lc['collection_id'] / 'extracts'
    _extract(folder, '2024', {**common, 'values': values, 'null_mask': np.zeros_like(values, dtype=bool),
                              'absent_row_mask': np.zeros((1, 2), dtype=bool), 'bands': np.array(lc['bands']),
                              'dates': np.array(['2024-01-01'], dtype='datetime64[ns]'),
                              'interval_start': np.array(['2024-01-01'], dtype='datetime64[ns]'),
                              'interval_end': np.array(['2025-01-01'], dtype='datetime64[ns]')}, attributes)


def build(root):
    """Write paths.yml, locks/ and caches under ``root``; return (paths file, locks dir)."""
    root = Path(root)
    paths = {'ismn_cache': str(root / 'ismn'), 'ancillary_cache': str(root / 'ancillary'),
             'morgan_cache': str(root / 'morgan'), 'selection_cache': str(root / 'selection'),
             'outputs_dir': str(root / 'output'), 'figure_dir': str(root / 'figures'), 'drydown_q_prepared_dir': None}
    paths_file = root / 'paths.yml'
    paths_file.write_text(yaml.safe_dump(paths))
    cfg = load_config('smoke', paths_file)
    selection = load_selection('smoke')
    locks = root / 'locks'
    requests = {s: json.loads(write_lock(cfg, selection, s, locks).read_text()) for s in SOURCES}
    ismn_archive(Path(cfg.paths.ismn_cache) / requests['ismn']['collection_id'] / 'archives' / 'SMOKE.zip')
    gridded(cfg.paths.ancillary_cache, requests)
    return paths_file, locks
