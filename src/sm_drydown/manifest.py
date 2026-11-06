"""Run manifests: everything needed to identify and verify one run's outputs.

``<outputs_dir>/runs/<run>/manifest.json`` records the resolved config (and its
differences from pipeline.yml), the git commit, the plan locks used (SHA-256),
each stage's key and the SHA-256 of every file it wrote, and package versions.
It replaces per-station signatures: a table is identified by its stage key and
file hash, and the manifest says which keys make up a run.
"""

import importlib.metadata
import platform
import subprocess

from sm_drydown.config import REPO_ROOT, as_dict, differences_from_default
from sm_drydown.storage.files import sha256, utc_now, write_json

PACKAGES = ('numpy', 'pandas', 'pyarrow', 'h5py', 'pyhdf', 'xarray', 'geopandas', 'boto3', 'earthengine-api')


def git_state():
    def run(*args):
        return subprocess.run(['git', *args], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
    return {'commit': run('rev-parse', 'HEAD') or None, 'dirty_files': run('status', '--porcelain').count('\n') + bool(
        run('status', '--porcelain'))}


def versions():
    out = {'python': platform.python_version()}
    for name in PACKAGES:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = None
    return out


def write_manifest(path, cfg, *, locks, stages, extra=None):
    """``stages`` maps stage name -> Stage; records each partition's marker."""
    manifest = {
        'run': cfg.run_name, 'created_at': utc_now(), 'git': git_state(), 'versions': versions(),
        'pixi_lock_sha256': sha256(REPO_ROOT / 'pixi.lock'),
        'config_differences_from_pipeline_yml': {k: list(v) for k, v in differences_from_default(cfg).items()},
        'subset': as_dict(cfg.subset), 'features': list(cfg.features),
        'locks': {name: sha256(p) for name, p in locks.items()},
        'stages': {name: {'stage': stage.stage, 'key': stage.key, 'folder': f'cache/{stage.stage}/{stage.key}',
                          'partitions': stage.markers()} for name, stage in stages.items()},
        **(extra or {}),
    }
    write_json(path, manifest)
    return manifest
