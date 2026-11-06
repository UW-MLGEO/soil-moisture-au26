"""Expand preserved ISMN ZIPs into a separate scratch prefix without parsing data."""

import json
import os
import shutil
import stat
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

from sm_drydown.storage.files import sha256, utc_now, write_json


def extract_zip(archive, destination):
    """Validate ZIP paths and CRCs, repairing only missing/corrupt cached members."""
    destination = Path(destination)
    members = []
    with zipfile.ZipFile(archive) as source:
        seen = set()
        for item in source.infolist():
            name = PurePosixPath(item.filename)
            if (name.is_absolute() or '..' in name.parts or '\\' in item.filename
                    or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError(f'Unsafe ZIP member: {item.filename}')
            if item.is_dir():
                continue
            if str(name) in seen:
                raise ValueError(f'Duplicate ZIP member: {item.filename}')
            seen.add(str(name))
            target = destination.joinpath(*name.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError(f'ZIP member escapes cache: {item.filename}')
            members.append((item, target))
        result = []
        for item, target in members:
            valid = False
            if target.is_file() and target.stat().st_size == item.file_size:
                crc = 0
                with target.open('rb') as cached:
                    for block in iter(lambda: cached.read(1024 * 1024), b''):
                        crc = zlib.crc32(block, crc)
                valid = crc == item.CRC
            if not valid:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + '.part')
                with source.open(item) as incoming, temporary.open('wb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                os.replace(temporary, target)
            result.append({'member': item.filename, 'bytes': item.file_size,
                           'sha256': sha256(target)})
    return result


def expand_exports(store, selection_path, run_dir, networks, workers=8):
    """Restore ZIPs from Kopah, extract on this host, then upload unchanged members."""
    selection = json.loads(Path(selection_path).read_text())
    object_prefix = 'ismn/' + selection['collection_id']
    reports = []
    for network in networks:
        raw_key = '/'.join(filter(None, [store.prefix, object_prefix, 'raw', network + '.zip']))
        head = store.client.head_object(Bucket=store.bucket, Key=raw_key)
        expected = head.get('Metadata', {}).get('sha256')
        if not expected:
            raise ValueError(f'Missing source archive checksum: {raw_key}')
        archive = run_dir / 'archives' / (network + '.zip')
        if not (archive.is_file() and archive.stat().st_size == head['ContentLength']
                and sha256(archive) == expected):
            archive.parent.mkdir(parents=True, exist_ok=True)
            temporary = archive.with_suffix('.zip.part')
            store.client.download_file(store.bucket, raw_key, str(temporary))
            if sha256(temporary) != expected:
                raise ValueError(f'Archive checksum mismatch: {network}')
            os.replace(temporary, archive)
        directory = run_dir / 'extracted' / network
        print(f'{network}: extracting verified archive', flush=True)
        members = extract_zip(archive, directory)
        target_prefix = object_prefix + '/extracted/' + network

        def upload(member):
            store.upload(directory / member['member'], target_prefix + '/' + member['member'])

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for _ in pool.map(upload, members):
                pass
        metadata_path = run_dir / 'extraction_provenance' / (network + '.json')
        report = {'archive_uri': f's3://{store.bucket}/{raw_key}',
                  'archive_sha256': expected,
                  'destination': f's3://{store.bucket}/{store.prefix}/{target_prefix}/',
                  'processing': 'ZIP decompression only; unchanged member bytes, no observation filtering or preprocessing',
                  'files': len(members), 'bytes': sum(m['bytes'] for m in members),
                  'members': members}
        # Preserve provenance timestamps when repeating the same extraction.
        try:
            previous = json.loads(metadata_path.read_text())
        except (OSError, ValueError):
            previous = {}
        created = previous.pop('extracted_at', None)
        if previous == report and created:
            report['extracted_at'] = created
        else:
            # Reuse remote provenance when rebuilding an empty local cache.
            key = '/'.join(filter(None, [store.prefix, object_prefix,
                                        'extraction_provenance', network + '.json']))
            from botocore.exceptions import ClientError
            try:
                remote = json.loads(store.client.get_object(Bucket=store.bucket, Key=key)['Body'].read())
            except ClientError as e:
                if e.response['Error']['Code'] not in ('404', 'NoSuchKey', 'NotFound'):
                    raise
                remote = {}
            created = remote.pop('extracted_at', None)
            report['extracted_at'] = created if remote == report and created else utc_now()
        write_json(metadata_path, report)
        store.upload(metadata_path, object_prefix + '/extraction_provenance/' + network + '.json')
        reports.append(report)
        print(f"{network}: verified {report['files']} extracted files, {report['bytes']} bytes", flush=True)
    return reports
