"""Raw ancillary retrieval, independent configuration, cache and scratch staging."""

import json
import os
import shutil
from pathlib import Path

import h5py

from sm_drydown.acquisition.config import load_config, require_approval
from sm_drydown.acquisition.grid import extract_native_cells
from sm_drydown.storage.files import sha256, utc_now, write_json


def _validate_netcdf(path):
    with h5py.File(path, 'r') as source:
        if not list(source):
            raise ValueError('Empty native HDF5/NetCDF source')


def fetch_native_file(session, asset, path):
    """Retain original provider body; validate format/hash, never observation quality."""
    path = Path(path)
    sidecar = path.with_suffix(path.suffix + '.json')
    previous = None
    try:
        meta = json.loads(sidecar.read_text())
        previous = meta
        if (meta['url'] != asset['url'] or sha256(path) != meta['sha256'] or
                path.stat().st_size != meta['response_bytes']):
            raise ValueError('Native cache mismatch')
        _validate_netcdf(path)
        return meta
    except (OSError, ValueError, KeyError, TypeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.part')
    with session.get(asset['url'], stream=True, timeout=(30, 120)) as response:
        response.raise_for_status()
        with temporary.open('wb') as stream:
            for block in response.iter_content(1024 * 1024):
                stream.write(block)
        if response.headers.get('Content-Length') and int(response.headers['Content-Length']) != temporary.stat().st_size:
            raise ValueError('Incomplete native source response')
        headers = {key: response.headers.get(key) for key in ('ETag', 'Last-Modified', 'Content-Type')}
    _validate_netcdf(temporary)
    os.replace(temporary, path)
    meta = {'url': asset['url'], 'retrieved_at': utc_now(), 'sha256': sha256(path),
            'response_bytes': path.stat().st_size, 'headers': headers,
            'processing': 'Unchanged original provider file; no quality filtering or preprocessing'}
    if previous and all(previous.get(key) == meta[key] for key in ('url', 'sha256', 'response_bytes', 'headers')):
        meta = previous
    write_json(sidecar, meta)
    return meta


def fetch_and_stage(config_path, cache_dir, session, store, approved_hash, *, asset_ids=None):
    """One asset at a time; approval checked before any source or object-store work."""
    plan = require_approval(config_path, approved_hash)
    config = load_config(config_path)
    if config['dataset'] != 'gldas' or config['strategy'] != 'http_files':
        raise ValueError('This workflow requires GLDAS native HTTP files')
    assets = config['assets']
    if asset_ids:
        wanted = set(asset_ids)
        if wanted - {asset['asset_id'] for asset in assets}:
            raise ValueError('Unknown asset selection')
        assets = [asset for asset in assets if asset['asset_id'] in wanted]
    run = Path(cache_dir) / config['collection_id']
    prefix = f"{config['dataset']}/{config['collection_id']}"
    config_uri = store.upload(config_path, prefix + f"/config/{plan['config_sha256']}.json")
    completed = []
    for asset in assets:
        aid = asset['asset_id']
        print(f"{config['dataset']}/{aid}: validating cache and retrieving missing source bytes", flush=True)
        source_id = asset.get('source_id', aid)
        if not source_id or Path(source_id).name != source_id or source_id in ('.', '..'):
            raise ValueError('Unsafe source asset ID')
        raw = run / 'raw' / source_id / asset['filename']
        # Reuse byte-identical provider files from earlier selection snapshots.
        if not raw.exists():
            for candidate in Path(cache_dir).glob('*/raw/*/' + asset['filename']):
                sidecar = candidate.with_suffix(candidate.suffix + '.json')
                try:
                    prior = json.loads(sidecar.read_text())
                    if prior['url'] != asset['url'] or sha256(candidate) != prior['sha256']:
                        continue
                    _validate_netcdf(candidate)
                except (OSError, ValueError, KeyError):
                    continue
                raw.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, raw)
                shutil.copy2(sidecar, raw.with_suffix(raw.suffix + '.json'))
                break
        native_meta = fetch_native_file(session, asset, raw)
        raw_uri = store.upload(raw, prefix + f'/raw/{source_id}/{asset["filename"]}')
        extract_meta = extract_native_cells(raw, config, run / 'extracts' / f'{aid}.npz', asset=asset)
        extract_path = run / 'extracts' / f'{aid}.npz'
        extract_uri = store.upload(extract_path, prefix + f'/extracts/{aid}.npz')
        extract_metadata_uri = store.upload(extract_path.with_suffix('.json'), prefix + f'/extracts/{aid}.json')
        provenance = {'asset_id': aid, 'config_sha256': plan['config_sha256'],
                      'source_url': asset['url'], 'raw_uri': raw_uri, 'raw': native_meta,
                      'extract_uri': extract_uri, 'extract_metadata_uri': extract_metadata_uri,
                      'extract_sha256': extract_meta['sha256']}
        provenance_path = run / 'provenance' / f'{aid}.json'
        write_json(provenance_path, provenance)
        store.upload(provenance_path, prefix + f'/provenance/{aid}.json')
        completed.append({'asset_id': aid, 'raw_uri': raw_uri, 'extract_uri': extract_uri})
        print(f"{config['dataset']}/{aid}: verified scratch upload", flush=True)
    report = {'config_uri': config_uri, 'assets': completed,
              'complete': len(assets) == len(config['assets'])}
    write_json(run / 'status.json', report)
    if report['complete']:
        store.upload(run / 'status.json', prefix + f"/manifests/{plan['config_sha256']}.json")
    return report
