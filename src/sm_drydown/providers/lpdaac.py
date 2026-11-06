"""Direct, native-pixel MCD12Q1 acquisition from NASA LP DAAC HTTPS HDF4 files.

CMR inventory discovery is separate from the downloader: resolved granule URLs,
sizes, identities and station selections are frozen in the input JSON. No QA
masking, filling, scaling or resampling is applied to the raw station extracts.
"""

import json
import math
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlparse

import numpy as np
import requests
from requests.auth import HTTPBasicAuth
from pyhdf.SD import SD, SDC

from sm_drydown.providers.http import session as http_session
from sm_drydown.providers.modis import RADIUS, pixel
from sm_drydown.storage.files import download, sha256, utc_now, write_json

CMR = 'https://cmr.earthdata.nasa.gov/search/granules.json'
HOST = 'data.lpdaac.earthdatacloud.nasa.gov'
PRODUCT = 'MCD12Q1.061'
FILENAME = re.compile(r'MCD12Q1\.A(\d{4})001\.(h\d\dv\d\d)\.061\.\d{13}\.hdf')
TILE_SIZE = 2400


def station_cells(config):
    cells = []
    for station in config['stations']:
        iy, ix = pixel(station['longitude'], station['latitude'], config['source_projection'])
        if not (0 <= iy < 18*TILE_SIZE and 0 <= ix < 36*TILE_SIZE):
            raise ValueError('Station falls outside the MODIS global grid')
        cells.append({'iy': iy, 'ix': ix, 'tile': f'h{ix//TILE_SIZE:02d}v{iy//TILE_SIZE:02d}',
                      'line': iy % TILE_SIZE, 'sample': ix % TILE_SIZE})
    return cells


def discover_year(config, year, session):
    """Resolve a repeatable CMR inventory; callers persist these entries in config/."""
    response = session.get(CMR, params={'short_name': 'MCD12Q1', 'version': '061',
        'provider': 'LPCLOUD', 'temporal': f'{year}-01-01T00:00:00Z,{year}-12-31T23:59:59Z',
        'page_size': 2000}, timeout=90)
    response.raise_for_status()
    metadata = response.json()
    entries = metadata['feed']['entry']
    if len(entries) >= 2000:
        raise ValueError('CMR page may be truncated')
    tiles = {cell['tile'] for cell in station_cells(config)}
    selected = {}
    for entry in entries:
        match = FILENAME.fullmatch(entry['title'] + '.hdf')
        if match is None or match[1] != str(year) or match[2] not in tiles:
            continue
        tile = match[2]
        if tile in selected:
            raise ValueError(f'Duplicate CMR granule for {year}/{tile}; select explicitly')
        urls = [link['href'] for link in entry['links'] if not link.get('inherited', False)
                and urlparse(link.get('href', '')).hostname == HOST
                and link['href'].endswith('/' + match[0])]
        if len(urls) != 1:
            raise ValueError('Expected one LP DAAC HTTPS HDF link per granule')
        selected[tile] = {'tile': tile, 'url': urls[0], 'filename': match[0],
            'size_estimate_bytes': round(float(entry['granule_size']) * 2**20),
            'cmr_granule_id': entry['id'], 'cmr_collection_id': entry['collection_concept_id'],
            'cmr_updated': entry['updated']}
    if set(selected) != tiles:
        raise ValueError(f'CMR lacks required station tiles for {year}: {sorted(tiles-set(selected))}')
    return [selected[tile] for tile in sorted(selected)], metadata


def load_config(path):
    c = json.loads(Path(path).read_text())
    if (c.get('schema_version') != 1 or c.get('dataset') != 'modis_land_cover' or
            c.get('strategy') != 'lpdaac_https_hdf4' or c.get('collection') != PRODUCT or
            c.get('cadence') != 'annual' or c.get('sampling') != 'native_cell'):
        raise ValueError('Expected native MCD12Q1.061 LP DAAC HDF4 configuration')
    if not re.fullmatch(r'[\w-]+', c['collection_id'], flags=re.ASCII):
        raise ValueError('Unsafe collection ID')
    from datetime import date
    first, last = date.fromisoformat(c['start']), date.fromisoformat(c['end'])
    if first > last or [a['asset_id'] for a in c['assets']] != [str(y) for y in range(first.year,last.year+1)]:
        raise ValueError('Annual assets must cover the full date selection')
    ids = [s['station_id'] for s in c['stations']]
    if not ids or len(ids) != len(set(ids)) or not c['bands'] or len(c['bands']) != len(set(c['bands'])):
        raise ValueError('Empty or duplicate station/band selection')
    if 'LC_Type1' not in c['bands']:
        raise ValueError('LC_Type1 required for availability checks')
    for s in c['stations']:
        if not (-90 < s['latitude'] < 90 and -180 <= s['longitude'] < 180):
            raise ValueError('Invalid station coordinates')
    projection = c['source_projection']
    a, b, x, d, e, y = projection['transform']
    if (projection['crs'] != 'SR-ORG:6974' or b or d or
            not math.isclose(a, 463.3127165279165, abs_tol=1e-6) or
            not math.isclose(e, -a, abs_tol=1e-6) or
            abs(x + 20015109.354) > .01 or abs(y - 10007554.677) > .01):
        raise ValueError('Expected canonical global MODIS sinusoidal affine grid')
    tiles = {cell['tile'] for cell in station_cells(c)}
    for asset in c['assets']:
        if sorted(g['tile'] for g in asset['granules']) != sorted(tiles):
            raise ValueError('Exactly one explicit granule per required station tile is necessary')
        for g in asset['granules']:
            match = FILENAME.fullmatch(g['filename'])
            url = urlparse(g['url'])
            if (not match or match[1] != asset['asset_id'] or match[2] != g['tile'] or
                    url.scheme != 'https' or url.hostname != HOST or url.username or url.password or
                    url.path.rsplit('/',1)[1] != g['filename'] or url.query or url.fragment or
                    not isinstance(g['size_estimate_bytes'], int) or g['size_estimate_bytes'] <= 0):
                raise ValueError('Invalid or mismatched LP DAAC granule specification')
    return c


def make_plan(path):
    c = load_config(path)
    return {'config_sha256': sha256(path), 'dataset': c['dataset'], 'collection_id': c['collection_id'],
        'collection': PRODUCT, 'strategy': c['strategy'], 'start': c['start'], 'end': c['end'],
        'station_count': len(c['stations']), 'bands': c['bands'], 'annual_assets': len(c['assets']),
        'granule_count': sum(len(a['granules']) for a in c['assets']),
        'source_bytes_estimate': sum(g['size_estimate_bytes'] for a in c['assets'] for g in a['granules']),
        'quality_filtering': False, 'gap_filling': False, 'storage': c['storage']}


def require_approval(path, approved_hash):
    plan = make_plan(path)
    if approved_hash != plan['config_sha256']:
        raise ValueError('Explicit full-download approval must match this configuration SHA-256')
    return plan


def read_credentials(path):
    path = Path(path).expanduser()
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError(f'Credential file must be private: chmod 600 {path}')
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.removeprefix('export ').split('=', 1)
            values[key.strip()] = value.strip().strip('"\'')
    if not values.get('USER') or not values.get('PASS'):
        raise ValueError('Credential file needs nonempty USER= and PASS= lines; contents suppressed')
    return values['USER'], values['PASS']


class EarthdataSession(requests.Session):
    """Send Basic credentials only to Earthdata Login; follow NASA/S3 redirects."""
    def __init__(self, credential_file):
        super().__init__()
        self._login = HTTPBasicAuth(*read_credentials(credential_file))
        configured = http_session()
        self.mount('https://', configured.get_adapter('https://'))

    def rebuild_auth(self, prepared_request, response):
        super().rebuild_auth(prepared_request, response)
        # Explicitly strip any inherited/netrc Authorization outside the login host.
        prepared_request.headers.pop('Authorization', None)
        if urlparse(prepared_request.url).hostname == 'urs.earthdata.nasa.gov':
            prepared_request.prepare_auth(self._login)


def _json_value(value):
    if isinstance(value, np.ndarray):
        return [_json_value(v) for v in value]
    if isinstance(value, np.generic):
        return _json_value(value.item())
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value.rstrip('\x00') if isinstance(value, str) else value


def read_tile(path, granule, config, cells):
    """Verify HDF grid geometry, read every band, and select containing pixels."""
    with Path(path).open('rb') as stream:
        if stream.read(4) != b'\x0e\x03\x13\x01':
            raise ValueError('Source is not HDF4')
    source = SD(str(path), SDC.READ)
    try:
        structure = source.attributes()['StructMetadata.0']
        if ('Projection=GCTP_SNSOID' not in structure or 'GridOrigin=HDFE_GD_UL' not in structure or
                'GridName="MCD12Q1"' not in structure):
            raise ValueError('Unexpected HDF grid/projection')
        def pair(key):
            match = re.search(key + r'=\(([-\d.]+),([-\d.]+)\)', structure)
            if not match:
                raise ValueError('Missing HDF grid boundary')
            return tuple(float(v) for v in match.groups())
        tile = re.fullmatch(r'h(\d\d)v(\d\d)', granule['tile'])
        h, v = int(tile[1]), int(tile[2])
        a, _, x, _, e, y = config['source_projection']['transform']
        expected = [(x+h*TILE_SIZE*a, y+v*TILE_SIZE*e),
                    (x+(h+1)*TILE_SIZE*a, y+(v+1)*TILE_SIZE*e)]
        for actual, wanted in zip([pair('UpperLeftPointMtrs'), pair('LowerRightMtrs')], expected):
            if any(abs(q-r) > .01 for q, r in zip(actual, wanted)):
                raise ValueError('HDF tile grid differs from configured native lattice')
        output = np.empty((len(cells), len(config['bands'])), dtype=np.uint8)
        attrs = {}
        lines = np.array([c['line'] for c in cells], dtype=int)
        samples = np.array([c['sample'] for c in cells], dtype=int)
        for b, band in enumerate(config['bands']):
            dataset = source.select(band)
            try:
                data = dataset.get()
                if data.shape != (TILE_SIZE, TILE_SIZE) or data.dtype != np.uint8:
                    raise ValueError('Unexpected HDF field shape/type')
                output[:, b] = data[lines, samples]
                attrs[band] = {key: _json_value(value) for key, value in dataset.attributes().items()}
            finally:
                dataset.endaccess()
        return output, attrs
    finally:
        source.end()


def valid_values(values, attributes):
    valid = np.isfinite(values)
    for key in ('_FillValue', 'missing_value'):
        for code in np.atleast_1d(attributes.get(key, [])):
            valid &= values != code
    if 'valid_range' in attributes:
        low, high = attributes['valid_range']
        valid &= (values >= low) & (values <= high)
    return valid


def write_extract(config, asset, values, attributes, hashes, path, config_hash):
    """Keep encoded values unchanged; summarize availability without masking them."""
    counts = {band: int(valid_values(values[:, b], attributes[band]).sum())
              for b, band in enumerate(config['bands'])}
    if not counts['LC_Type1']:
        raise ValueError(f"No valid LC_Type1 measurements in {asset['asset_id']}; refusing empty year")
    signature = {'configuration_sha256': config_hash, 'raw_file_sha256': hashes}
    try:
        previous = json.loads(path.with_suffix('.json').read_text())
        if all(previous.get(k) == v for k,v in signature.items()) and previous['sha256'] == sha256(path):
            return previous
    except (OSError, ValueError, KeyError):
        pass
    cells = station_cells(config)
    a, _, x0, _, e, y0 = config['source_projection']['transform']
    yy = np.array([y0+(c['iy']+.5)*e for c in cells])
    xx = np.array([x0+(c['ix']+.5)*a for c in cells])
    year = asset['asset_id']
    start = np.array([year+'-01-01'], dtype='datetime64[ns]')
    end = np.array([str(int(year)+1)+'-01-01'], dtype='datetime64[ns]')
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.part')
    with temp.open('wb') as stream:
        np.savez_compressed(stream, values=values[None, :, :],
            null_mask=np.zeros((1,*values.shape),dtype=bool),
            absent_row_mask=np.zeros((1,len(values)),dtype=bool), dates=start,
            interval_start=start, interval_end=end,
            station_ids=np.array([s['station_id'] for s in config['stations']]), bands=np.array(config['bands']),
            latitude_indices=np.array([c['iy'] for c in cells]), longitude_indices=np.array([c['ix'] for c in cells]),
            native_latitudes=np.degrees(yy/RADIUS), native_longitudes=np.degrees(xx/(RADIUS*np.cos(yy/RADIUS))))
    temp.replace(path)
    invalid = ~valid_values(values[:,config['bands'].index('LC_Type1')], attributes['LC_Type1'])
    report = {**signature, 'sha256': sha256(path), 'created_at': utc_now(),
        'source_format': 'lpdaac_hdf4', 'source_collection': PRODUCT, 'source_attributes': attributes,
        'projection': config['source_projection'], 'station_count': len(values), 'image_count': 1,
        'images': [{'date': year+'-01-01', 'interval_end': str(int(year)+1)+'-01-01',
                    'granules': [g['url'] for g in asset['granules']]}],
        'tiles': [g['tile'] for g in asset['granules']], 'null_values': 0, 'absent_rows': 0,
        'valid_stations_by_band': counts, 'primary_valid_stations': counts['LC_Type1'],
        'primary_missing_station_ids': [s['station_id'] for j,s in enumerate(config['stations']) if invalid[j]],
        'quality_filtering': False, 'gap_filling': False, 'interpolation': False,
        'values_shape': ['image','station','band'], 'interval_end_exclusive': True,
        'numeric_interpretation': 'Native uint8 values unchanged, including fill codes; no masks/scaling applied'}
    write_json(path.with_suffix('.json'), report)
    return report


def fetch_and_stage(config_path, cache_dir, session, store, approved_hash, *, asset_ids=None):
    plan = require_approval(config_path, approved_hash)
    config = load_config(config_path)
    if asset_ids and set(asset_ids)-{a['asset_id'] for a in config['assets']}:
        raise ValueError('Unknown MODIS annual asset')
    assets = [a for a in config['assets'] if not asset_ids or a['asset_id'] in asset_ids]
    run = Path(cache_dir)/config['collection_id']
    prefix = config['dataset']+'/'+config['collection_id']
    config_uri = store.upload(config_path, prefix+'/config/'+plan['config_sha256']+'.json')
    cells = station_cells(config)
    completed = []
    for asset in assets:
        year = asset['asset_id']
        values = np.empty((len(cells),len(config['bands'])),dtype=np.uint8)
        attributes, hashes, raw_uris, transfers = None, {}, [], []
        print(f'LP DAAC/{year}: {len(asset["granules"])} native tiles', flush=True)
        for granule in asset['granules']:
            path = run/'raw'/year/granule['filename']
            started = time.perf_counter()
            meta = download(session, granule['url'], path)
            indices = [j for j,c in enumerate(cells) if c['tile'] == granule['tile']]
            sampled, attrs = read_tile(path, granule, config, [cells[j] for j in indices])
            if attributes is not None and attributes != attrs:
                raise ValueError('Inconsistent field attributes between tiles')
            attributes = attrs
            values[indices] = sampled
            hashes[granule['filename']] = meta['sha256']
            transfers.append({'filename': path.name, 'bytes': path.stat().st_size,
                              'retrieved_at': meta['retrieved_at']})
            for raw in [path, path.with_name(path.name+'.provenance.json')]:
                raw_uris.append(store.upload(raw, prefix+'/raw/'+year+'/'+raw.name))
            print(f'  {year}/{granule["tile"]}: {path.stat().st_size/1e6:.2f} MB; '
                  f'cache/download, native extraction and upload {time.perf_counter()-started:.1f}s', flush=True)
        extract = run/'extracts'/(year+'.npz')
        report = write_extract(config, asset, values, attributes, hashes, extract, plan['config_sha256'])
        uri = store.upload(extract, prefix+'/extracts/'+extract.name)
        store.upload(extract.with_suffix('.json'), prefix+'/extracts/'+year+'.json')
        completed.append({'asset_id': year, 'extract_uri': uri, 'raw_uris': raw_uris,
            'primary_valid_stations': report['primary_valid_stations'],
            'valid_stations_by_band': report['valid_stations_by_band'],
            'primary_missing_station_ids': report['primary_missing_station_ids'], 'transfers': transfers})
        result = {'config_uri': config_uri, 'assets': completed,
            'complete': len(completed)==len(config['assets']), 'source_bytes_estimate': plan['source_bytes_estimate']}
        write_json(run/'status.json', result)
        print(f'LP DAAC/{year}: {report["primary_valid_stations"]}/{len(cells)} valid IGBP station values; uploaded', flush=True)
    if result['complete']:
        store.upload(run/'status.json', prefix+'/manifests/'+plan['config_sha256']+'.json')
    return result
