"""Verified local acquisition caches for one feature group (inputs are hashed, never modified)."""

import json
from pathlib import Path

from sm_drydown.features import gridded
from sm_drydown.storage.files import sha256


class Inputs:
    """Verified local acquisition caches for one group, and their hashes for the cache key."""

    def __init__(self, cfg, group: Group, locks_dir):
        self.request_path = Path(locks_dir) / f'{group.request}.json'
        self.request = json.loads(self.request_path.read_text())
        self.hashes = {'request': sha256(self.request_path)}
        if group.name == 'ismn':
            self.run_dir = Path(cfg.paths.ismn_cache) / self.request['collection_id']
            self.catalog = None
        else:
            self.catalog = gridded.ExtractCatalog(self.request_path, cfg.paths.ancillary_cache)
            self.hashes.update(self.catalog.inputs)

    def archive(self, network):
        """The network's native ISMN ZIP, checked against its acquisition sidecar."""
        archive = self.run_dir / 'archives' / f'{network}.zip'
        sidecar = archive.with_name(archive.name + '.provenance.json')
        if not archive.exists() or not sidecar.exists():
            raise FileNotFoundError(f'Missing {archive}: run `pixi run acquire ismn` or `pixi run restore`')
        digest = sha256(archive)
        if json.loads(sidecar.read_text())['sha256'] != digest:
            raise ValueError(f'{archive} does not match its acquisition checksum sidecar')
        return archive, digest

    def network_hashes(self, network):
        if self.catalog is not None:
            return {}
        return {f'{network}.zip': self.archive(network)[1]}
