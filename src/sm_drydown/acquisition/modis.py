"""Native-pixel MODIS station acquisition; preserve all bands and service nulls."""

from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path

import numpy as np

from sm_drydown.acquisition.chirps import cached_response
from sm_drydown.storage.files import sha256, utc_now, write_json

# Earth Engine serves MODIS LAI only; land cover (MCD12Q1) is acquired through
# direct NASA LP DAAC tiles (acquisition/lpdaac.py).
PRODUCTS = {'modis_lai': ('MODIS/061/MCD15A3H', 4)}
RADIUS = 6371007.181  # MODIS sinusoidal sphere, metres (SR-ORG:6974).


def load_config(path):
    config = json.loads(Path(path).read_text())
    if config.get('schema_version') != 1 or config.get('dataset') not in PRODUCTS:
        raise ValueError('Unsupported MODIS configuration')
    collection, cadence = PRODUCTS[config['dataset']]
    if (config['collection'] != collection or config['cadence'] != cadence or
            config['strategy'] != 'earth_engine_getRegion' or config['sampling'] != 'native_cell'):
        raise ValueError('MODIS collection, cadence or native sampling differs from contract')
    if date.fromisoformat(config['start']) > date.fromisoformat(config['end']):
        raise ValueError('Reversed MODIS dates')
    if not config['collection_id'] or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in config['collection_id']):
        raise ValueError('Unsafe collection ID')
    ids = [s['station_id'] for s in config['stations']]
    if not ids or len(ids) != len(set(ids)) or len(set(config['bands'])) != len(config['bands']):
        raise ValueError('Empty or duplicate station/band selection')
    for s in config['stations']:
        if not (-90 < s['latitude'] < 90 and -180 <= s['longitude'] < 180):
            raise ValueError('Invalid station coordinates')
    expected = [str(y) for y in range(date.fromisoformat(config['start']).year,
                                     date.fromisoformat(config['end']).year + 1)]
    if [a['asset_id'] for a in config['assets']] != expected:
        raise ValueError('Annual assets must cover the full selection')
    if not 100 < config['maximum_values'] < 1048576:
        raise ValueError('Invalid getRegion request bound')
    return config


def make_plan(path):
    c = load_config(path)
    return {'config_sha256': sha256(path), 'dataset': c['dataset'],
            'collection_id': c['collection_id'], 'collection': c['collection'],
            'start': c['start'], 'end': c['end'], 'station_count': len(c['stations']),
            'bands': c['bands'], 'annual_assets': len(c['assets']),
            'maximum_values_per_request': c['maximum_values'],
            'quality_filtering': False, 'gap_filling': False, 'blocked': [],
            'storage': c['storage']}


def require_approval(path, approved_hash):
    plan = make_plan(path)
    if approved_hash != plan['config_sha256']:
        raise ValueError('Explicit full-download approval must match this configuration SHA-256')
    return plan


def interval_end(start, cadence):
    """Exclusive end of a composite of `cadence` days, cut at the year boundary."""
    start = date.fromisoformat(start)
    return min(start + timedelta(days=cadence), date(start.year + 1, 1, 1)).isoformat()


def _grid(projection):
    if projection['crs'] != 'SR-ORG:6974':
        raise ValueError('Expected native MODIS sinusoidal CRS')
    a, b, c, d, e, f = projection['transform']
    if b or d or a <= 0 or e >= 0:
        raise ValueError('Unsupported MODIS affine transform')
    return a, c, e, f


def pixel(lon, lat, projection):
    a, c, e, f = _grid(projection)
    x = RADIUS * math.radians(lon) * math.cos(math.radians(lat))
    y = RADIUS * math.radians(lat)
    return math.floor((y-f)/e), math.floor((x-c)/a)


def native_cells(stations, projection):
    a, c, e, f = _grid(projection)
    cells = {}
    for s in stations:
        iy, ix = pixel(s['longitude'], s['latitude'], projection)
        y, x = f+(iy+.5)*e, c+(ix+.5)*a
        lat = math.degrees(y/RADIUS)
        lon = math.degrees(x/(RADIUS*math.cos(y/RADIUS)))
        cell = cells.setdefault((iy, ix), {'iy': iy, 'ix': ix, 'longitude': lon,
                                          'latitude': lat, 'station_ids': []})
        cell['station_ids'].append(s['station_id'])
    return [cells[key] for key in sorted(cells)]


def source_images(response, config):
    """Verify source grids align; retain source interval/image IDs, not daily fills."""
    images, projection, seen = [], None, set()
    for feature in response['features']:
        bands = feature['bands']
        if [b['id'] for b in bands] != config['bands']:
            raise ValueError('Unexpected MODIS source bands')
        for band in bands:
            current = {'crs': band['crs'], 'transform': band['crs_transform']}
            a, c, e, f = _grid(current)
            if projection is None:
                projection = current
            a0, c0, e0, f0 = _grid(projection)
            if (not math.isclose(a, a0, abs_tol=1e-6, rel_tol=0) or
                    not math.isclose(e, e0, abs_tol=1e-6, rel_tol=0) or
                    abs((c-c0)/a0-round((c-c0)/a0)) > 1e-4 or
                    abs((f-f0)/e0-round((f-f0)/e0)) > 1e-4):
                raise ValueError('MODIS grids do not align; implicit resampling refused')
        props = feature['properties']
        stamp = props['system:time_start']
        day = datetime.fromtimestamp(stamp/1000, timezone.utc).date().isoformat()
        end = interval_end(day, config['cadence'])
        if end <= config['start'] or day > config['end']:
            continue
        identity = props['system:index']
        if identity in seen or any(i['date'] == day for i in images):
            raise ValueError('Duplicate MODIS source image/date')
        seen.add(identity)
        images.append({'id': feature['id'], 'index': identity, 'timestamp': stamp,
                       'date': day, 'interval_end': end})
    return sorted(images, key=lambda i: i['timestamp']), projection


class EarthEngineClient:
    def __init__(self, project):
        self.project, self.ee = project, None

    def _collection(self, config, asset):
        if self.ee is None:
            import ee
            ee.Initialize(project=self.project)
            ee.data.setDeadline(120000)
            self.ee = ee
        year = int(asset['asset_id'])
        return self.ee.ImageCollection(config['collection']).filterDate(
            f'{year}-01-01', f'{year+1}-01-01').select(config['bands'])

    def describe(self, config, asset):
        return self._collection(config, asset).getInfo()

    def retrieve(self, config, asset, images, cells, projection):
        collection = self._collection(config, asset).filter(
            self.ee.Filter.inList('system:index', [i['index'] for i in images]))
        points = self.ee.Geometry.MultiPoint([[c['longitude'], c['latitude']] for c in cells])
        return collection.getRegion(points, crs=projection['crs'],
                                    crsTransform=projection['transform']).getInfo()


def validate_table(table, images, cells, projection, bands, maximum_values):
    header = ['id', 'longitude', 'latitude', 'time'] + bands
    if not isinstance(table, list) or not table or table[0] != header:
        raise ValueError('Unexpected MODIS response columns')
    if len(table)*len(header) > maximum_values:
        raise ValueError('Provider response exceeds request bound')
    allowed = {(c['iy'], c['ix']): c for c in cells}
    expected = {i['index']: i['timestamp'] for i in images}
    seen = set()
    for row in table[1:]:
        if len(row) != len(header):
            raise ValueError('Malformed MODIS response row')
        identity, lon, lat, stamp, *values = row
        location = pixel(lon, lat, projection)
        if identity not in expected or expected[identity] != stamp or location not in allowed:
            raise ValueError('Unexpected MODIS image, timestamp or pixel')
        cell = allowed[location]
        if any(not math.isclose(x, cell[k], abs_tol=1e-7, rel_tol=0)
               for x, k in [(lon, 'longitude'), (lat, 'latitude')]):
            raise ValueError('Response not at native pixel center')
        key = identity, location
        if key in seen:
            raise ValueError('Duplicate MODIS pixel/image response')
        seen.add(key)
        if any(v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))
                                  or not math.isfinite(v)) for v in values):
            raise ValueError('MODIS values must be finite numbers or null')


def extract_tables(config, images, projection, cells, tables, hashes, path, config_hash):
    path = Path(path)
    signature = {'configuration_sha256': config_hash, 'raw_response_sha256': hashes}
    previous = None
    try:
        previous = json.loads(path.with_suffix('.json').read_text())
        if all(previous.get(k) == v for k, v in signature.items()) and previous['sha256'] == sha256(path):
            return previous
    except (OSError, ValueError, KeyError):
        pass
    shape = len(images), len(config['stations']), len(config['bands'])
    values = np.full(shape, np.nan)
    nulls = np.zeros(shape, dtype=bool)
    absent = np.ones(shape[:2], dtype=bool)
    station_lookup = {s['station_id']: j for j, s in enumerate(config['stations'])}
    image_lookup = {i['index']: j for j, i in enumerate(images)}
    cell_lookup = {(c['iy'], c['ix']): c for c in cells}
    for table in tables:
        for identity, lon, lat, stamp, *data in table[1:]:
            cell = cell_lookup[pixel(lon, lat, projection)]
            i = image_lookup[identity]
            for sid in cell['station_ids']:
                j = station_lookup[sid]
                absent[i, j] = False
                nulls[i, j] = [v is None for v in data]
                values[i, j] = [np.nan if v is None else v for v in data]
    coords = {sid: c for c in cells for sid in c['station_ids']}
    ordered = [coords.get(s['station_id']) for s in config['stations']]
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.part')
    with temp.open('wb') as stream:
        np.savez_compressed(stream, values=values, null_mask=nulls, absent_row_mask=absent,
            dates=np.array([i['date'] for i in images], dtype='datetime64[ns]'),
            interval_start=np.array([i['date'] for i in images], dtype='datetime64[ns]'),
            interval_end=np.array([i['interval_end'] for i in images], dtype='datetime64[ns]'),
            station_ids=np.array([s['station_id'] for s in config['stations']]),
            bands=np.array(config['bands']),
            latitude_indices=np.array([c['iy'] if c else np.nan for c in ordered]),
            longitude_indices=np.array([c['ix'] if c else np.nan for c in ordered]),
            native_latitudes=np.array([c['latitude'] if c else np.nan for c in ordered]),
            native_longitudes=np.array([c['longitude'] if c else np.nan for c in ordered]))
    temp.replace(path)
    report = {**signature, 'sha256': sha256(path), 'created_at': utc_now(),
              'source_format': 'earth_engine_getRegion_json', 'source_collection': config['collection'],
              'source_attributes': config['source_attributes'], 'projection': projection,
              'images': images, 'station_count': len(config['stations']), 'image_count': len(images),
              'null_values': int(nulls.sum()), 'absent_rows': int(absent.sum()),
              'quality_filtering': False, 'gap_filling': False, 'interpolation': False,
              'values_shape': ['image', 'station', 'band'], 'interval_end_exclusive': True}
    if previous and all(previous.get(k) == v for k, v in report.items() if k != 'created_at'):
        report = previous
    write_json(path.with_suffix('.json'), report)
    return report


def fetch_and_stage(config_path, cache_dir, client, store, approved_hash, *, asset_ids=None):
    plan = require_approval(config_path, approved_hash)
    c = load_config(config_path)
    assets = c['assets']
    if asset_ids:
        if set(asset_ids)-{a['asset_id'] for a in assets}:
            raise ValueError('Unknown MODIS annual asset')
        assets = [a for a in assets if a['asset_id'] in asset_ids]
    run = Path(cache_dir)/c['collection_id']
    prefix = c['dataset']+'/'+c['collection_id']
    config_uri = store.upload(config_path, prefix+'/config/'+plan['config_sha256']+'.json')
    complete = []
    for asset in assets:
        aid = asset['asset_id']
        print(f"{c['dataset']}/{aid}: source metadata and native station cells", flush=True)
        raw_dir = run/'raw'/aid
        description, meta = cached_response(raw_dir/'images.json',
            {'collection': c['collection'], 'asset': asset, 'bands': c['bands']},
            lambda: client.describe(c, asset), lambda r: source_images(r, c), client.project)
        images, projection = source_images(description, c)
        cells = native_cells(c['stations'], projection) if images else []
        raw_uris, hashes = [], []
        for path in [raw_dir/'images.json', raw_dir/'images.provenance.json']:
            raw_uris.append(store.upload(path, prefix+'/raw/'+aid+'/'+path.name))
        hashes.append(meta['sha256'])
        columns = 4+len(c['bands'])
        per_batch = max(1, (c['maximum_values']-columns)//(max(1, len(images))*columns))
        tables = []
        for start in range(0, len(cells), per_batch):
            batch = cells[start:start+per_batch]
            path = raw_dir/f'cells_{start:05d}.json'
            request = {'collection': c['collection'], 'images': images, 'cells': batch,
                       'projection': projection, 'bands': c['bands']}
            table, metadata = cached_response(path, request,
                lambda: client.retrieve(c, asset, images, batch, projection),
                lambda r: validate_table(r, images, batch, projection, c['bands'], c['maximum_values']), client.project)
            tables.append(table)
            hashes.append(metadata['sha256'])
            for raw in [path, path.with_suffix('.provenance.json')]:
                raw_uris.append(store.upload(raw, prefix+'/raw/'+aid+'/'+raw.name))
        extract = run/'extracts'/(aid+'.npz')
        report = extract_tables(c, images, projection, cells, tables, hashes, extract, plan['config_sha256'])
        uri = store.upload(extract, prefix+'/extracts/'+extract.name)
        store.upload(extract.with_suffix('.json'), prefix+'/extracts/'+aid+'.json')
        complete.append({'asset_id': aid, 'raw_uris': raw_uris, 'extract_uri': uri,
                         'image_count': report['image_count']})
        print(f"{c['dataset']}/{aid}: verified {len(images)} native source intervals", flush=True)
    result = {'config_uri': config_uri, 'assets': complete, 'complete': len(assets)==len(c['assets'])}
    write_json(run/'status.json', result)
    if result['complete']:
        store.upload(run/'status.json', prefix+'/manifests/'+plan['config_sha256']+'.json')
    return result
