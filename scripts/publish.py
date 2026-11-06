"""Stage 5: upload one run's tables and manifest to processed Kopah storage.

    pixi run publish                     # baseline run (full selection only)
    pixi run python scripts/publish.py --run pilot_arm --allow-subset

Uploads every file listed in <outputs_dir>/runs/<run>/manifest.json to
<processed>/<layout>/<stage>/<key>/... (config/storage.yml; e.g.
s3://gaia/ml4geo_sm_drydown/v2/), re-hashing each file first, then the manifest
itself to <layout>/manifests/<sha256>.json. Objects are private and immutable:
an identical object is skipped and a different object at the same key is
refused. Scratch destinations are rejected. Needs the `kopah` AWS profile.
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sm_drydown.config import load_config
from sm_drydown.storage.files import ObjectStore, sha256

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--run', default='baseline')
parser.add_argument('--paths', default=None)
parser.add_argument('--allow-subset', action='store_true', help='publish a pilot subset run')
args = parser.parse_args()
cfg = load_config(args.run, args.paths)
outputs = Path(cfg.paths.outputs_dir)
manifest_path = outputs / 'runs' / cfg.run_name / 'manifest.json'
manifest = json.loads(manifest_path.read_text())
if (manifest['subset']['networks'] != 'all' or manifest['subset']['station_ids'] != 'all') and not args.allow_subset:
    raise SystemExit('This run is a subset; pass --allow-subset to publish it')
kopah = cfg.storage['kopah']
store = ObjectStore(kopah['processed'], kopah['profile'], kopah['endpoint'])
if 'scratch' in store.prefix.split('/') or store.bucket == 'scratch':
    raise SystemExit('Processed tables must not go to scratch storage')
layout = kopah['processed_layout']

entries = []
for stage in manifest['stages'].values():
    for marker in stage['partitions'].values():
        for relative, meta in marker['files'].items():
            entries.append((outputs / stage['folder'] / relative, meta['sha256'],
                            f"{layout}/{stage['stage']}/{stage['key']}/{relative}"))


def upload(entry):
    path, digest, key = entry
    if sha256(path) != digest:
        raise ValueError(f'{path} changed after the manifest was written')
    return store.upload(path, key)


workers = cfg.storage['transfer']['upload_workers']
with ThreadPoolExecutor(max_workers=workers) as pool:
    uris = list(pool.map(upload, entries))
uri = store.upload(manifest_path, f'{layout}/manifests/{sha256(manifest_path)}.json')
print(f'Uploaded or verified {len(uris)} table files; run manifest {uri}')
