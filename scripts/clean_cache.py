"""Remove stage cache folders that no run record names (lists only, unless --delete).

    pixi run clean-cache                      # list every cache key folder and what would be removed
    pixi run clean-cache --delete             # remove the unused ones
    pixi run python scripts/clean_cache.py --paths config/paths.yml --delete

Each config or code change gives a stage a new key (output/cache/<stage>/<key>/),
so old key folders pile up. A folder is kept when a run record names it
(runs/<run>/manifest.json for feature and assemble keys,
runs/<run>/inspection_summary.json for the inspect key), when it was written after
the newest run record (it may belong to a run still in progress), or when there
are no run records at all. Everything else is unused and can be rebuilt by
rerunning homogenize/inspect with the config that made it. Only
<outputs_dir>/cache/ is touched: raw caches (ISMN archives, station extracts), run
records, exports and Kopah are never changed. Offline.
"""

import argparse
import shutil
from pathlib import Path

from sm_drydown.cache import cache_usage
from sm_drydown.config import load_config

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--paths', default=None, help='paths file (default config/paths.yml, else paths.example.yml)')
parser.add_argument('--delete', action='store_true', help='remove unused folders (default: list only)')
args = parser.parse_args()
outputs = Path(load_config('baseline', args.paths).paths.outputs_dir)


def megabytes(size):
    return f'{size / 2 ** 20:8.1f} MB'  # not-science: console output (bytes -> MiB)


entries = cache_usage(outputs)
for folder, stage, status, reason, size in entries:
    print(f'{status:7} {megabytes(size)}  {stage}/{folder.name[:12]}  {reason}')
unused = [(folder, size) for folder, _, status, _, size in entries if status == 'unused']
total = sum(size for _, size in unused)
if not args.delete:
    print(f'{len(unused)} of {len(entries)} key folders unused ({megabytes(total).strip()}) under {outputs / "cache"}; '
          'rerun with --delete to remove them.')
    raise SystemExit(0)
for folder, _ in unused:
    shutil.rmtree(folder)
    parent = folder.parent
    while parent != outputs / 'cache' and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent
print(f'Removed {len(unused)} of {len(entries)} key folders ({megabytes(total).strip()}) under {outputs / "cache"}.')
