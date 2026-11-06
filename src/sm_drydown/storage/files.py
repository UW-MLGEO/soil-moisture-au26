"""Atomic files, integrity-checked downloads, and private Kopah uploads and restores."""

import hashlib
import json
import os
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import ClientError


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    """Publish a complete JSON document, never a partial checkpoint."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        json.dump(value, f, indent=2, sort_keys=True, allow_nan=False)
        f.write('\n')
        name = f.name
    os.replace(name, path)


def validate_zip(path):
    """Check transport/container integrity only; do not assess observations."""
    with zipfile.ZipFile(path) as z:
        if not z.namelist() or z.testzip() is not None:
            raise ValueError('Empty ZIP or failed ZIP CRC check')
        return {'members': len(z.infolist()),
                'uncompressed_bytes': sum(i.file_size for i in z.infolist())}


def download(session, url, path, expected_sha256=None, archive=False, max_attempts=3):
    """Reuse only a hash-verified cache entry; atomically download otherwise."""
    path = Path(path)
    meta_path = path.with_name(path.name + '.provenance.json')
    try:
        meta = json.loads(meta_path.read_text())
        digest = sha256(path)
        if (meta['url'] != url or digest != meta['sha256'] or
                (expected_sha256 and digest != expected_sha256)):
            raise ValueError('Cache mismatch')
        if archive:
            validate_zip(path)
        return meta
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile):
        pass
    # An expected digest can pin reference metadata; archive digests are recorded
    # on first retrieval and checked on every subsequent cache read.
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.part')
    if max_attempts < 1:
        raise ValueError('max_attempts must be positive')
    for attempt in range(max_attempts):
        try:
            # Restart from byte zero: ISMN does not support HTTP range resume.
            with session.get(url, stream=True, timeout=(30, 120)) as response:
                response.raise_for_status()
                with temp.open('wb') as f:
                    for block in response.iter_content(1024 * 1024):
                        f.write(block)
                length = response.headers.get('Content-Length')
                encoding = response.headers.get('Content-Encoding')
                if encoding in (None, 'identity') and isinstance(length, str) and length.isdigit() and int(length) != temp.stat().st_size:
                    raise requests.exceptions.ChunkedEncodingError('Incomplete source body')
            break
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
            if attempt + 1 == max_attempts:
                raise
            print(f'Transfer interrupted; restarting unchanged source (attempt {attempt+2}/{max_attempts})', flush=True)
            time.sleep(min(2 ** attempt, 10))
    digest = sha256(temp)
    if expected_sha256 and digest != expected_sha256:
        raise ValueError(f'Source snapshot changed: {path.name}; review selection before proceeding')
    info = validate_zip(temp) if archive else {}
    meta = {'url': url, 'retrieved_at': utc_now(), 'sha256': digest,
            'response_bytes': temp.stat().st_size, **info}
    os.replace(temp, path)
    write_json(meta_path, meta)
    return meta


class ObjectStore:
    """Upload unchanged bytes; skip matching objects and refuse collisions."""
    def __init__(self, destination, profile, endpoint):
        u = urlparse(destination)
        if u.scheme != 's3' or not u.netloc:
            raise ValueError('Destination must be an s3://bucket/prefix URL')
        self.bucket, self.prefix = u.netloc, u.path.strip('/')
        self.client = boto3.Session(profile_name=profile).client(
            's3', endpoint_url=endpoint,
            config=Config(s3={'addressing_style': 'path', 'payload_signing_enabled': False},
                          request_checksum_calculation='when_required',
                          response_checksum_validation='when_required',
                          connect_timeout=15, read_timeout=120))

    def upload(self, path, key):
        path = Path(path)
        key = '/'.join(x for x in (self.prefix, key) if x)
        digest, size = sha256(path), path.stat().st_size
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response['Error']['Code'] not in ('404', 'NoSuchKey', 'NotFound'):
                raise
        else:
            if head['ContentLength'] == size and head['Metadata'].get('sha256') == digest:
                return f's3://{self.bucket}/{key}'
            raise ValueError(f'Refusing to overwrite different object: {key}')
        self.client.upload_file(str(path), self.bucket, key, ExtraArgs={
            'ACL': 'private', 'Metadata': {'sha256': digest},
            'ContentType': {'.zip': 'application/zip', '.json': 'application/json',
                            '.nc': 'application/x-netcdf', '.nc4': 'application/x-netcdf'}
                           .get(path.suffix.lower(), 'application/octet-stream')})
        head = self.client.head_object(Bucket=self.bucket, Key=key)
        if head['ContentLength'] != size or head['Metadata'].get('sha256') != digest:
            raise ValueError('Uploaded object size/checksum metadata mismatch')
        return f's3://{self.bucket}/{key}'

    def download(self, key, path):
        """Reuse a local file matching the object's recorded SHA-256; otherwise fetch atomically."""
        path = Path(path)
        key = '/'.join(x for x in (self.prefix, key) if x)
        uri = f's3://{self.bucket}/{key}'
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response['Error']['Code'] in ('404', 'NoSuchKey', 'NotFound'):
                raise FileNotFoundError(f'Missing object: {uri}') from None
            raise
        expected, size = head.get('Metadata', {}).get('sha256'), head['ContentLength']
        if not expected:
            raise ValueError(f'Object has no recorded SHA-256: {uri}')
        result = {'uri': uri, 'sha256': expected, 'bytes': size}
        if path.is_file() and path.stat().st_size == size and sha256(path) == expected:
            return {**result, 'downloaded': False}
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + '.part')
        self.client.download_file(self.bucket, key, str(temp))
        if temp.stat().st_size != size or sha256(temp) != expected:
            temp.unlink()
            raise ValueError(f'Downloaded bytes do not match recorded SHA-256: {uri}')
        os.replace(temp, path)
        return {**result, 'downloaded': True}
