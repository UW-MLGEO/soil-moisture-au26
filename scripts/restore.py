"""Stage 1′: rebuild local acquisition caches from Kopah scratch (instead of acquiring).

    pixi run restore                               # everything homogenize reads (~3.2 GB)
    pixi run python scripts/restore.py --run pilot_arm      # ISMN archives of the subset networks only
    pixi run python scripts/restore.py --source chirps --source gldas

Downloads the native ISMN ZIPs (and rebuilds each checksum sidecar from its
uploaded provenance record) and the CHIRPS, GLDAS, MODIS LAI and land-cover
station extracts named by the plan locks in locks/. Every object is checked
against the SHA-256 recorded on Kopah; files already matching are reused. Each
cache folder gets restore.json (source URIs, hashes, restore times). Needs only
the `kopah` AWS profile; no provider is contacted and nothing is uploaded.
"""

import argparse
import json

from sm_drydown.config import LOCKS_DIR, load_config
from sm_drydown.storage.files import ObjectStore
from sm_drydown.storage.restore import restore_extracts, restore_ismn

GRIDDED = ('chirps', 'gldas', 'modis_lai', 'modis_land_cover')

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--run', default='baseline')
parser.add_argument('--paths', default=None)
parser.add_argument('--source', action='append', choices=('ismn',) + GRIDDED, help='default: all')
args = parser.parse_args()
cfg = load_config(args.run, args.paths)
files = []
for source in args.source or ('ismn',) + GRIDDED:
    lock = LOCKS_DIR / f'{source}.json'
    store = ObjectStore(**json.loads(lock.read_text())['storage'])
    if source == 'ismn':
        networks = None if cfg.subset.networks == 'all' else cfg.subset.networks
        files += restore_ismn(store, lock, cfg.paths.ismn_cache, networks)
    else:
        files += restore_extracts(store, lock, cfg.paths.ancillary_cache)
fetched = [f for f in files if f['downloaded']]
print(f"Verified {len(files)} files ({sum(f['bytes'] for f in files)} bytes); "
      f"downloaded {len(fetched)} ({sum(f['bytes'] for f in fetched)} bytes).")
