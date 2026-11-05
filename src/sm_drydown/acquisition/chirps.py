"""Resumable native-grid CHIRPS v2 service responses and separate station extracts.

The cache preserves decoded Earth Engine JSON, not original UCSB NetCDF files.
Acquisition consumes its standalone JSON specification.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
import json
import math
import os
from pathlib import Path

import numpy as np

from sm_drydown.acquisition.config import load_config as load_ancillary_config

from sm_drydown.storage.files import sha256, utc_now, write_json

COLLECTION = 'UCSB-CHG/CHIRPS/DAILY'
SOURCE_FORMAT = 'earth_engine_getRegion_json'


def validate_response(response):
    if not isinstance(response, list) or not response:
        raise ValueError('Missing Earth Engine response table')
    if response[0] != ['id', 'longitude', 'latitude', 'time', 'precipitation']:
        raise ValueError('Unexpected Earth Engine response columns')
    if any(len(row) != 5 for row in response[1:]):
        raise ValueError('Malformed Earth Engine response row')


def load_config(path):
    config = load_ancillary_config(path)
    if (config['strategy'] != 'earth_engine_getRegion' or config['collection'] != COLLECTION or
            config['band'] != 'precipitation' or config['dataset'] != 'chirps' or
            config['sampling'] != 'native_cell' or config['units'] != 'mm/day'):
        raise ValueError('Only native-grid CHIRPS v2 precipitation is supported')
    batch = config['batching']
    if not 5 < batch['maximum_values'] < 1048576 or batch['columns_per_row'] != 5 or batch['concurrent_requests'] != 1:
        raise ValueError('Invalid bounded sequential getRegion batch specification')
    if any(not (-50 <= s['latitude'] < 50 and -180 <= s['longitude'] < 180) for s in config['stations']):
        raise ValueError('Station outside CHIRPS coverage')
    windows = [(a['start'], a['end']) for a in config['assets']]
    cursor = date.fromisoformat(config['start'])
    for start, end in windows:
        if date.fromisoformat(start) != cursor or start > end:
            raise ValueError('Assets must cover the window without gaps or overlaps')
        cursor = date.fromisoformat(end) + timedelta(days=1)
    if cursor != date.fromisoformat(config['end']) + timedelta(days=1):
        raise ValueError('Assets do not cover configured dates')
    return config


def make_plan(path):
    config = load_config(path)
    return {'config_sha256': sha256(path), 'collection_id': config['collection_id'],
            'dataset': 'chirps', 'collection': COLLECTION, 'strategy': config['strategy'],
            'station_count': len(config['stations']), 'start': config['start'], 'end': config['end'],
            'annual_assets': len(config['assets']), 'maximum_values_per_request': config['batching']['maximum_values'],
            'request_count': 'Resolved from verified native cells and source image counts at retrieval',
            'storage': config['storage'], 'blocked': []}


def require_approval(path, approved_hash):
    plan = make_plan(path)
    if approved_hash != plan['config_sha256']:
        raise ValueError('Explicit full-download approval must match this configuration SHA-256')
    return plan


def _pixel(longitude, latitude, projection):
    a, b, c, d, e, f = map(lambda x: Decimal(str(x)), projection['transform'])
    if projection['crs'] != 'EPSG:4326' or b or d or not a or not e:
        raise ValueError('Unsupported source grid; review projection before sampling')
    ix = int(((Decimal(str(longitude)) - c) / a).to_integral_value(rounding=ROUND_FLOOR))
    iy = int(((Decimal(str(latitude)) - f) / e).to_integral_value(rounding=ROUND_FLOOR))
    return iy, ix


def native_cells(stations, projection):
    cells = {}
    a, _, c, _, e, f = projection['transform']
    for station in stations:
        iy, ix = _pixel(station['longitude'], station['latitude'], projection)
        cell = cells.setdefault((iy, ix), {'iy': iy, 'ix': ix,
            'longitude': c + (ix + .5) * a, 'latitude': f + (iy + .5) * e, 'station_ids': []})
        cell['station_ids'].append(station['station_id'])
    return [cells[key] for key in sorted(cells)]


def batch_cells(cells, image_count, maximum_values):
    if image_count == 0:
        return []
    # Includes the header row; five columns comprise id/lon/lat/time/precip.
    count = (maximum_values - 5) // (image_count * 5)
    if count < 1:
        raise ValueError('Single-cell request exceeds configured value limit')
    return [cells[start:start + count] for start in range(0, len(cells), count)]


def source_images(response, asset):
    images = []
    projection = None
    seen_dates, seen_ids = set(), set()
    for feature in response['features']:
        band = feature['bands'][0]
        if len(feature['bands']) != 1 or band['id'] != 'precipitation':
            raise ValueError('Unexpected source image bands')
        current = {'crs': band['crs'], 'transform': band['crs_transform']}
        if projection is not None and projection != current:
            raise ValueError('Source projections differ; no implicit resampling is allowed')
        projection = current
        timestamp = feature['properties']['system:time_start']
        day = datetime.fromtimestamp(timestamp / 1000, timezone.utc).date().isoformat()
        index = feature['properties']['system:index']
        if not asset['start'] <= day <= asset['end'] or day in seen_dates or index in seen_ids:
            raise ValueError('Duplicate or out-of-window source image')
        seen_dates.add(day)
        seen_ids.add(index)
        images.append({'id': feature['id'], 'index': index, 'timestamp': timestamp, 'date': day})
    return sorted(images, key=lambda item: item['timestamp']), projection


class EarthEngineClient:
    """Lazy authenticated access; no initialization during planning/cache reuse."""
    def __init__(self, project):
        self.project = project
        self.ee = None

    def _collection(self, config, asset):
        if self.ee is None:
            import ee
            if not self.project:
                raise ValueError('An Earth Engine enabled Google Cloud project is required')
            ee.Initialize(project=self.project)
            ee.data.setDeadline(120000)
            self.ee = ee
        stop = (date.fromisoformat(asset['end']) + timedelta(days=1)).isoformat()
        return self.ee.ImageCollection(config['collection']).filterDate(asset['start'], stop).select(config['band'])

    def describe(self, config, asset):
        return self._collection(config, asset).getInfo()

    def retrieve(self, config, asset, images, cells, projection):
        collection = self._collection(config, asset).filter(
            self.ee.Filter.inList('system:index', [item['index'] for item in images]))
        geometry = self.ee.Geometry.MultiPoint([[cell['longitude'], cell['latitude']] for cell in cells])
        return collection.getRegion(geometry=geometry, crs=projection['crs'],
                                    crsTransform=projection['transform']).getInfo()


def cached_response(path, request, fetch, validate, project):
    """Cache the complete decoded service object; validate checksum and request."""
    path = Path(path)
    metadata_path = path.with_suffix('.provenance.json')
    previous = None
    try:
        meta = json.loads(metadata_path.read_text())
        previous = meta
        if (meta['request'] != request or meta['sha256'] != sha256(path) or
                meta['response_bytes'] != path.stat().st_size):
            raise ValueError('Response cache mismatch')
        response = json.loads(path.read_text())
        validate(response)
        return response, meta
    except (OSError, KeyError, TypeError, ValueError):
        pass
    response = fetch()
    validate(response)
    write_json(path, response)
    meta = {'request': request, 'sha256': sha256(path), 'response_bytes': path.stat().st_size,
            'retrieved_at': utc_now(), 'project': project, 'source_format': SOURCE_FORMAT,
            'representation': 'Decoded service JSON serialized without modifying values or nulls'}
    # If a damaged local body is repaired to the original checksum, retain the
    # provenance of that byte-identical snapshot and its immutable scratch key.
    if previous and all(previous.get(key) == meta[key] for key in
                        ('request', 'sha256', 'response_bytes', 'source_format')):
        meta = previous
    write_json(metadata_path, meta)
    return response, meta


def validate_table(response, images, cells, projection, maximum_values):
    validate_response(response)
    if len(response) * 5 > maximum_values:
        raise ValueError('Provider response exceeds configured request bound')
    expected = {item['index']: item['timestamp'] for item in images}
    allowed = {(cell['iy'], cell['ix']) for cell in cells}
    centers = {(cell['iy'], cell['ix']): (cell['longitude'], cell['latitude']) for cell in cells}
    seen = set()
    for identity, lon, lat, timestamp, value in response[1:]:
        pixel = _pixel(lon, lat, projection)
        if identity not in expected or timestamp != expected[identity] or pixel not in allowed:
            raise ValueError('Unexpected image, timestamp or native cell in response')
        if not all(math.isclose(actual, expected_center, rel_tol=0, abs_tol=1e-7)
                   for actual, expected_center in zip((lon, lat), centers[pixel])):
            raise ValueError('Response coordinates do not identify the native pixel center')
        key = (identity, pixel)
        if key in seen:
            raise ValueError('Duplicate native-cell image row')
        seen.add(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError('Service values must be finite JSON numbers or null')


def extract_tables(config, asset, images, projection, cells, tables, raw_hashes, path, config_hash):
    """Lossless numeric interpretation; preserve null and absent-row masks."""
    path = Path(path)
    signature = {'configuration_fingerprint': config_hash, 'raw_response_sha256': raw_hashes}
    existing = None
    try:
        existing = json.loads(path.with_suffix('.json').read_text())
        if any(existing[key] != value for key, value in signature.items()) or existing['sha256'] != sha256(path):
            raise ValueError('Extract cache mismatch')
        with np.load(path, allow_pickle=False) as data:
            if data['values'].shape != (len(images), len(config['stations'])):
                raise ValueError('Extract shape mismatch')
        return existing
    except (OSError, KeyError, TypeError, ValueError):
        pass
    station_ids = [s['station_id'] for s in config['stations']]
    station_index = {value: i for i, value in enumerate(station_ids)}
    row_index = {item['index']: i for i, item in enumerate(images)}
    shape = (len(images), len(station_ids))
    values = np.full(shape, np.nan)
    null_mask, absent_mask = np.zeros(shape, dtype=bool), np.ones(shape, dtype=bool)
    cell_map = {(cell['iy'], cell['ix']): cell for cell in cells}
    for table in tables:
        for identity, lon, lat, timestamp, value in table[1:]:
            indices = [station_index[sid] for sid in cell_map[_pixel(lon, lat, projection)]['station_ids']]
            row = row_index[identity]
            values[row, indices] = np.nan if value is None else value
            null_mask[row, indices] = value is None
            absent_mask[row, indices] = False
    dates = np.array([item['date'] for item in images], dtype='datetime64[ns]')
    expected = np.arange(np.datetime64(asset['start']),
                         np.datetime64(asset['end']) + np.timedelta64(1, 'D'))
    missing = np.setdiff1d(expected, dates.astype('datetime64[D]'))
    coordinates = {sid: cell for cell in cells for sid in cell['station_ids']}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.part')
    with temporary.open('wb') as stream:
        np.savez_compressed(stream, values=values, dates=dates, station_ids=station_ids,
            null_mask=null_mask, absent_row_mask=absent_mask,
            native_latitudes=[coordinates[s]['latitude'] for s in station_ids] if cells else [],
            native_longitudes=[coordinates[s]['longitude'] for s in station_ids] if cells else [])
    os.replace(temporary, path)
    meta = {**signature, 'sha256': sha256(path), 'created_at': utc_now(),
            'source_format': SOURCE_FORMAT, 'source_variable': config['band'], 'source_attributes': {},
            'source_collection': config['collection'], 'source_images': images, 'projection': projection,
            'station_count': len(station_ids), 'date_count': len(dates), 'expected_date_count': len(expected),
            'missing_source_dates': [str(value) for value in missing],
            'first_date': images[0]['date'] if images else None, 'last_date': images[-1]['date'] if images else None,
            'null_count': int(null_mask.sum()), 'absent_row_count': int(absent_mask.sum()),
            'numeric_interpretation': 'JSON number to float64; JSON null and absent rows to NaN with distinct masks',
            'sampling': 'native_cell', 'quality_filtering': False, 'gap_filling': False,
            'interpolation': False, 'aggregation': False}
    if existing and all(existing.get(key) == value for key, value in meta.items() if key != 'created_at'):
        meta = existing
    write_json(path.with_suffix('.json'), meta)
    return meta


def fetch_and_stage(config_path, cache_dir, client, store, approved_hash, *, asset_ids=None):
    plan = require_approval(config_path, approved_hash)
    config = load_config(config_path)
    assets = config['assets']
    if asset_ids:
        if set(asset_ids) - {asset['asset_id'] for asset in assets}:
            raise ValueError('Unknown asset selection')
        assets = [asset for asset in assets if asset['asset_id'] in set(asset_ids)]
    run = Path(cache_dir) / config['collection_id']
    prefix = f"chirps/{config['collection_id']}"
    config_uri = store.upload(config_path, f"{prefix}/config/{plan['config_sha256']}.json")
    complete = []
    for asset in assets:
        aid = asset['asset_id']
        print(f'CHIRPS Earth Engine/{aid}: validate cache and fetch missing responses', flush=True)
        directory = run / 'raw' / aid
        request = {'config_sha256': plan['config_sha256'], 'collection': COLLECTION,
                   'asset': asset, 'band': config['band'], 'operation': 'ImageCollection.getInfo'}
        description_path = directory / 'images.json'
        description, source_meta = cached_response(description_path, request,
            lambda: client.describe(config, asset), lambda data: source_images(data, asset), client.project)
        images, projection = source_images(description, asset)
        cells = native_cells(config['stations'], projection) if projection else []
        batches = batch_cells(cells, len(images), config['batching']['maximum_values'])
        raw_paths, tables = [description_path], []
        for number, batch in enumerate(batches):
            request = {'config_sha256': plan['config_sha256'], 'collection': COLLECTION,
                       'asset': asset, 'image_metadata_sha256': source_meta['sha256'],
                       'projection': projection, 'cells': batch, 'operation': 'ImageCollection.getRegion'}
            path = directory / f'batch_{number:04d}.json'
            table, _ = cached_response(path, request,
                lambda: client.retrieve(config, asset, images, batch, projection),
                lambda data: validate_table(data, images, batch, projection, config['batching']['maximum_values']),
                client.project)
            tables.append(table)
            raw_paths.append(path)
            print(f'CHIRPS Earth Engine/{aid}: batch {number + 1}/{len(batches)} cached', flush=True)
        hashes = {path.name: sha256(path) for path in raw_paths}
        extract = run / 'extracts' / f'{aid}.npz'
        meta = extract_tables(config, asset, images, projection, cells, tables, hashes, extract, plan['config_sha256'])
        raw_uris = []
        for path in raw_paths:
            raw_uris.append(store.upload(path, f'{prefix}/raw/{aid}/{path.name}'))
            sidecar = path.with_suffix('.provenance.json')
            store.upload(sidecar, f'{prefix}/raw/{aid}/{sidecar.name}')
        extract_uri = store.upload(extract, f'{prefix}/extracts/{aid}.npz')
        metadata_uri = store.upload(extract.with_suffix('.json'), f'{prefix}/extracts/{aid}.json')
        provenance = {'asset_id': aid, 'config_sha256': plan['config_sha256'],
                      'collection': COLLECTION, 'raw_uris': raw_uris, 'raw_response_sha256': hashes,
                      'extract_uri': extract_uri, 'extract_metadata_uri': metadata_uri,
                      'extract_sha256': meta['sha256']}
        provenance_path = run / 'provenance' / f'{aid}.json'
        write_json(provenance_path, provenance)
        store.upload(provenance_path, f'{prefix}/provenance/{aid}.json')
        complete.append({'asset_id': aid, 'raw_uris': raw_uris, 'extract_uri': extract_uri})
    report = {'config_uri': config_uri, 'assets': complete, 'complete': len(assets) == len(config['assets'])}
    write_json(run / 'status.json', report)
    if report['complete']:
        store.upload(run / 'status.json', f"{prefix}/manifests/{plan['config_sha256']}.json")
    return report
