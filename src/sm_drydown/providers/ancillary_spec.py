"""Explicit ancillary selection snapshots; downloaders consume only their JSON."""

from datetime import date
import json
from pathlib import Path
from urllib.parse import urlparse

from sm_drydown.storage.files import sha256

def load_config(path):
    config = json.loads(Path(path).read_text())
    if config['schema_version'] != 1 or config['dataset'] not in ('chirps', 'gldas'):
        raise ValueError('Unsupported ancillary configuration')
    if date.fromisoformat(config['start']) > date.fromisoformat(config['end']):
        raise ValueError('Reversed requested date range')
    ids = [station['station_id'] for station in config['stations']]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError('Empty or duplicate station selection')
    if not config['collection_id'] or any(ch not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for ch in config['collection_id']):
        raise ValueError('Invalid collection ID')
    assets = config.get('assets', [])
    asset_ids = [asset['asset_id'] for asset in assets]
    if len(asset_ids) != len(set(asset_ids)):
        raise ValueError('Duplicate source asset ID')
    for asset in assets:
        for key in ('asset_id', 'filename'):
            value = asset[key]
            if not value or value in ('.', '..') or Path(value).name != value:
                raise ValueError('Asset names must be safe relative filenames')
        parsed = urlparse(asset['url'])
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError('Ancillary sources must be public HTTPS URLs without embedded credentials')
    return config


def make_plan(path):
    config = load_config(path)
    blocked = []
    if config['sampling'] != 'native_cell':
        blocked.append('Native-cell spatial sampling has not been selected')
    if not config['strategy']:
        blocked.append('Download strategy has not been selected')
    elif config['strategy'] not in ('http_files',):
        blocked.append('Selected provider strategy is not implemented by this native-file workflow')
    if config['dataset'] == 'gldas' and not config['sensor_depth_assignment']:
        blocked.append('GLDAS depth-assignment policy has not been selected')
    return {'config_sha256': sha256(path), 'collection_id': config['collection_id'],
            'dataset': config['dataset'], 'station_count': len(config['stations']),
            'start': config['start'], 'end': config['end'], 'strategy': config['strategy'],
            'request_count': len(config.get('assets', [])) or len(config.get('cells', [])) * len(config.get('windows', [])),
            'blocked': blocked, 'storage': config['storage']}


def require_approval(path, approved_hash):
    plan = make_plan(path)
    if approved_hash != plan['config_sha256']:
        raise ValueError('Explicit full-download approval must match this configuration SHA-256')
    if plan['blocked']:
        raise ValueError('Unresolved configuration decisions: ' + '; '.join(plan['blocked']))
    return plan
