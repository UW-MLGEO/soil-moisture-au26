"""Small provider responses and immutable uploads shared by offline tests."""

import hashlib
from pathlib import Path


class MemoryStore:
    bucket = 'gaia'

    def __init__(self):
        self.prefix = 'ml4geo_sm_drydown'
        self.objects = {}

    @property
    def hashes(self):
        return {key: hashlib.sha256(body).hexdigest() for key, body in self.objects.items()}

    def upload(self, path, key):
        body = Path(path).read_bytes()
        if key in self.objects and self.objects[key] != body:
            raise ValueError('Immutable object collision')
        self.objects[key] = body
        return f's3://{self.bucket}/{self.prefix}/{key}'


class HTTPResponse:
    status_code = 200

    def __init__(self, body):
        self.body = body
        self.headers = {'Content-Length': str(len(body)), 'ETag': '"version1"'}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self.body
