"""Produce local daily station bundles from existing acquisition caches, without fitting drydowns."""

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from urllib.parse import quote

import pyarrow as pa
import pyarrow.parquet as pq

from sm_drydown.homogenization.daily import station_daily
from sm_drydown.homogenization.sources import ExtractCatalog, read_station_sensors, configuration_provenance
from sm_drydown.storage.files import sha256, write_json


def json_safe(value):
    """Encode missing scientific metadata as JSON null, never nonstandard NaN literals."""
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_safe(v) for v in value]
    if hasattr(value, 'item'):
        return json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _policies(path):
    policy = json.loads(Path(path).read_text())
    for key in ('temperature_quality', 'land_cover_scheme', 'land_cover_quality', 'lai_quality',
                'ismn_missing_codes', 'lai_band', 'land_cover_qc_bands', 'lai_qc_bands'):
        if key not in policy:
            raise ValueError(f'Explicit preprocessing decision required: {key}')
    if policy['temperature_quality'] != 'G_only':
        raise ValueError('Current approved temperature rule is G_only using its own ISMN flag')
    for key in ('soil_moisture', 'soil_temperature'):
        if key not in policy['ismn_missing_codes']:
            raise ValueError(f'Declared native missing codes required for {key}')
    return policy


def validate_station_bundle(folder):
    """Verify identity, table checksum, schema version and row count before reuse/upload."""
    folder = Path(folder)
    description = json.loads((folder / 'description.json').read_text())
    if (description.get('artifact_role') != 'daily_pre_drydown_station_table'
            or description.get('schema_version') != 1):
        raise ValueError('Unrecognized daily station bundle schema')
    signature = hashlib.sha256(json.dumps(description['inputs'], sort_keys=True).encode()).hexdigest()
    if signature != description['input_signature'] or folder.name != signature:
        raise ValueError('Daily station bundle input identity is inconsistent')
    table = folder / 'daily.parquet'
    if sha256(table) != description['table_sha256']:
        raise ValueError('Daily station table checksum mismatch')
    metadata = pq.read_metadata(table)
    if metadata.num_rows != description['rows'] or metadata.metadata.get(b'schema_version') != b'1':
        raise ValueError('Daily station table row count/schema mismatch')
    return description


def upload_station_bundles(output_dir, store, *, network=None, station_id=None):
    """Upload validated immutable daily bundles to processed storage, never scratch."""
    if 'scratch' in store.prefix.strip('/').split('/') or store.bucket == 'scratch':
        raise ValueError('Homogenized outputs must use non-scratch storage')
    if not Path(output_dir).is_dir():
        raise FileNotFoundError('Daily station output directory does not exist')
    uploaded = []
    for metadata in sorted(Path(output_dir).glob('*/*/*/description.json')):
        folder = metadata.parent
        description = validate_station_bundle(folder)
        station = description['station']
        if network is not None and station['network'] != network:
            continue
        if station_id is not None and station['station_id'] != station_id:
            continue
        expected = (quote(station['network'], safe=''), str(station['station_id']))
        if (folder.parent.parent.name, folder.parent.name) != expected:
            raise ValueError('Daily bundle path and recorded station identity disagree')
        for name in ('daily.parquet', 'description.json'):
            key = '/'.join(('daily', *expected, description['input_signature'], name))
            uploaded.append(store.upload(folder / name, key))
    return uploaded


def run_homogenization(selection_path, configs, policy_path, ismn_cache, ancillary_cache,
                       output_dir, *, network=None, station_id=None):
    """Generate one immutable Parquet table/JSON description per station from verified local inputs."""
    selection = json.loads(Path(selection_path).read_text())
    selection_digest = sha256(selection_path)
    policy = _policies(policy_path)
    policy_digest = sha256(policy_path)
    catalogs = {key: ExtractCatalog(configs[key], ancillary_cache)
                for key in ('chirps', 'gldas', 'land_cover', 'lai')}
    stations = [s for s in selection['stations'] if (network is None or s['network'] == network)
                and (station_id is None or s['station_id'] == station_id)]
    if not stations:
        raise ValueError('No stations match selection')
    for name, catalog in catalogs.items():
        if (catalog.config['start'], catalog.config['end']) != (selection['start'], selection['end']):
            raise ValueError(f'{name}: date window differs from ISMN selection')
        entries = {s['station_id']: s for s in catalog.config['stations']}
        for station in stations:
            match = entries.get(station['station_id'])
            if match is None or any(match[k] != station[k] for k in ('network', 'latitude', 'longitude')):
                raise ValueError(f'{name}: inconsistent station identity or coordinates')
    archives = {}
    for name in sorted({s['network'] for s in stations}):
        archive = Path(ismn_cache) / selection['collection_id'] / 'archives' / (name + '.zip')
        metadata = archive.with_suffix('.zip.provenance.json')
        digest = sha256(archive)
        if not metadata.exists() or json.loads(metadata.read_text())['sha256'] != digest:
            raise ValueError(f'{name}: archive does not match acquisition checksum sidecar')
        archives[name] = (archive, digest, json.loads(metadata.read_text()), sha256(metadata))
    package = Path(__file__).parents[1]
    software_files = list(Path(__file__).parent.glob('*.py')) + [
        package / p for p in ('formats/ismn.py', 'formats/cf.py',
                              'formats/soil.py', 'storage/files.py')]
    software = {p.relative_to(package).as_posix(): sha256(p) for p in sorted(software_files)}
    sources = {key: catalog.provenance for key, catalog in catalogs.items()}
    ismn_source = configuration_provenance(selection_path, selection, dataset='ismn')
    result = {'stations': 0, 'rows': 0, 'bundles': [], 'values_filled': 0}
    for station in stations:
        archive, digest, archive_metadata, archive_metadata_digest = archives[station['network']]
        identity = {'selection_sha256': selection_digest, 'policy_sha256': policy_digest,
                    'sources': sources, 'archive_sha256': digest, 'station_id': station['station_id'],
                    'archive_metadata_sha256': archive_metadata_digest,
                    'network': station['network'], 'software': software}
        signature = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        folder = Path(output_dir) / quote(station['network'], safe='') / str(station['station_id']) / signature
        if folder.exists():
            description = validate_station_bundle(folder)
            if description['input_signature'] != signature:
                raise ValueError('Existing daily station bundle is corrupt or inconsistent')
        else:
            sensors = read_station_sensors(archive, station, missing_codes=policy['ismn_missing_codes'], archive_sha256=digest)
            lc, lc_decode = catalogs['land_cover'].modis(station['station_id'], policy['land_cover_scheme'], policy['land_cover_qc_bands'])
            lai, lai_decode = catalogs['lai'].modis(station['station_id'], policy['lai_band'], policy['lai_qc_bands'])
            gldas, gldas_decode = catalogs['gldas'].gldas(station['station_id'])
            frame, description = station_daily(station, selection['start'], selection['end'], sensors,
                catalogs['chirps'].chirps(station['station_id']), gldas,
                lc, lai, policies=policy)
            description.update({'schema_version': 1, 'artifact_role': 'daily_pre_drydown_station_table',
                'input_signature': signature, 'inputs': identity,
                'source_provenance': {'ismn': {**ismn_source, 'archive_metadata': archive_metadata}, **sources},
                'source_coordinates': {key: c.coordinates(station['station_id']) for key, c in catalogs.items()},
                'modis_decoding': {'land_cover': lc_decode, 'lai': lai_decode},
                'gldas_decoding': gldas_decode,
                'missing_policy': 'null/NaN; no default measurements, interpolation or carry beyond source intervals',
                'columns': {name: str(dtype) for name, dtype in frame.dtypes.items()},
                'fitting_performed': False, 'drydown_detection_performed': False})
            folder.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.daily-', dir=folder.parent) as temp:
                temporary = Path(temp)
                table = pa.Table.from_pandas(frame, preserve_index=False)
                table = table.replace_schema_metadata({**(table.schema.metadata or {}),
                    b'timezone': b'UTC calendar days', b'schema_version': b'1',
                    b'field_units': json.dumps(description['units'], sort_keys=True).encode()})
                pq.write_table(table, temporary / 'daily.parquet', compression='zstd')
                description['table_sha256'] = sha256(temporary / 'daily.parquet')
                write_json(temporary / 'description.json', json_safe(description))
                os.rename(temporary, folder)
        result['stations'] += 1
        result['rows'] += description['rows']
        result['bundles'].append(str(folder))
        print(f"{station['network']}/{station['station_id']}: {description['rows']} daily rows", flush=True)
    return result
