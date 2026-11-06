"""Rebuild local acquisition caches from verified Kopah scratch objects.

Restores only what homogenization and inspection read: native ISMN ZIPs with
their checksum sidecars, and ancillary station-cell extracts. No provider is
contacted, nothing is uploaded, and no observation bytes are parsed or changed.
"""

import json
from pathlib import Path

from sm_drydown.providers.ismn import load_config
from sm_drydown.storage.files import sha256, utc_now, write_json

# Fields that files.download() writes to an archive sidecar; collect_exports()
# copies them unchanged into each network's uploaded provenance record.
ARCHIVE_SIDECAR_KEYS = ('members', 'response_bytes', 'retrieved_at', 'sha256',
                        'uncompressed_bytes', 'url')


def record_restore(run_dir, config_path, files):
    """Merge restored files into restore.json, keeping first restore times for unchanged bytes."""
    path = Path(run_dir) / 'restore.json'
    try:
        previous = {f['path']: f for f in json.loads(path.read_text())['files']}
    except (OSError, ValueError, KeyError):
        previous = {}
    for f in files:
        relative = Path(f['path']).relative_to(run_dir).as_posix()
        prior = previous.get(relative)
        restored_at = (prior['restored_at'] if prior and prior['sha256'] == f['sha256']
                       and not f['downloaded'] else utc_now())
        previous[relative] = {'path': relative, 'uri': f['uri'], 'sha256': f['sha256'],
                              'bytes': f['bytes'], 'restored_at': restored_at}
    write_json(path, {'config_sha256': sha256(config_path),
                      'processing': 'Unchanged Kopah object bytes; no provider access or preprocessing',
                      'files': [previous[k] for k in sorted(previous)]})


def _fetch(store, key, path, run_dir):
    result = store.download(key, path)
    action = 'downloaded' if result['downloaded'] else 'verified cache'
    print(f"{Path(path).relative_to(run_dir.parent)}: {action}", flush=True)
    return {**result, 'path': str(path)}


def restore_ismn(store, config_path, cache_dir, networks=None):
    """Restore native ZIPs and rebuild the sidecars that homogenization checks."""
    selection = load_config(config_path)
    digest = sha256(config_path)
    networks = sorted(set(networks or selection['networks']))
    if not set(networks) <= set(selection['networks']):
        raise ValueError('Unknown network in configuration')
    run_dir = Path(cache_dir) / selection['collection_id']
    prefix = 'ismn/' + selection['collection_id']
    files = []
    for network in networks:
        # Provenance is keyed by the selection hash, so an edited config finds nothing.
        record_path = run_dir / 'provenance' / f'{network}.json'
        files.append(_fetch(store, f'{prefix}/provenance/{digest}/{network}.json', record_path, run_dir))
        record = json.loads(record_path.read_text())
        if record['network'] != network or record['selection_sha256'] != digest:
            raise ValueError(f'{network}: provenance record does not describe this selection')
        archive = run_dir / 'archives' / f'{network}.zip'
        files.append(_fetch(store, f'{prefix}/raw/{network}.zip', archive, run_dir))
        if files[-1]['uri'] != record['archive_uri'] or sha256(archive) != record['sha256']:
            raise ValueError(f'{network}: archive does not match its provenance record')
        write_json(archive.with_name(archive.name + '.provenance.json'),
                   {k: record[k] for k in ARCHIVE_SIDECAR_KEYS})
    record_restore(run_dir, config_path, files)
    return files


def restore_extracts(store, config_path, cache_dir):
    """Restore every configured station-cell extract and its metadata sidecar."""
    config = json.loads(Path(config_path).read_text())
    run_dir = Path(cache_dir) / config['collection_id']
    prefix = f"{config['dataset']}/{config['collection_id']}/extracts"
    files = []
    for asset in config['assets']:
        extract = run_dir / 'extracts' / f"{asset['asset_id']}.npz"
        for path in (extract, extract.with_suffix('.json')):
            files.append(_fetch(store, f'{prefix}/{path.name}', path, run_dir))
        if sha256(extract) != json.loads(extract.with_suffix('.json').read_text())['sha256']:
            raise ValueError(f'{extract.name}: extract does not match its metadata sidecar')
    record_restore(run_dir, config_path, files)
    return files
