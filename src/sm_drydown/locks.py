"""Plan locks: the reviewed, committed approval for an acquisition.

``scripts/plan.py <source>`` writes two files:

- ``locks/<source>.json``: the full provider request specification
  (sm_drydown.requests). Its SHA-256 is the acquisition identity used in Kopah
  keys, provenance records and every derived extract.
- ``locks/<source>.summary.yml``: what a reviewer needs (counts, size estimate,
  hash, selection) before approving by committing both files.

``scripts/acquire.py`` refuses to contact a provider unless the specification
rebuilt from the current config equals the lock byte for byte and the lock is
committed (no pasted hashes; editing the config means re-planning).
"""

import subprocess
from pathlib import Path

import yaml

from sm_drydown.config import REPO_ROOT, config_hash
from sm_drydown.requests import build_request, serialize
from sm_drydown.storage.files import sha256, utc_now


def lock_paths(source, locks_dir):
    locks_dir = Path(locks_dir)
    return locks_dir / f'{source}.json', locks_dir / f'{source}.summary.yml'


def summarize(request, source):
    """Counts a reviewer checks before approving a download."""
    summary = {'stations': len(request.get('stations', [])), 'start': request.get('start'),
               'end': request.get('end'), 'collection_id': request.get('collection_id', request.get('commit'))}
    if source == 'ismn':
        summary.update(networks=len(request['networks']), export_requests=len(request['networks']),
                       sm_sensors=sum(len(s['soil_moisture_sensors']) for s in request['stations']),
                       variables=request['requested_variables'])
    elif source == 'modis_land_cover':
        granules = [g for a in request['assets'] for g in a['granules']]
        summary.update(annual_assets=len(request['assets']), granules=len(granules),
                       estimated_bytes=sum(g['size_estimate_bytes'] for g in granules))
    elif source == 'morgan_2025':
        summary.update(files=len(request['files']), rows=request['table_description']['rows'])
    else:
        summary.update(assets=len(request['assets']))
    return summary


def write_lock(cfg, selection, source, locks_dir):
    """Write the request specification and its review summary; keep planned_at if unchanged."""
    spec_path, summary_path = lock_paths(source, locks_dir)
    body = serialize(build_request(cfg, selection, source))
    previous = yaml.safe_load(summary_path.read_text()) if summary_path.exists() else {}
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_bytes(body)
    digest = sha256(spec_path)
    request = yaml.safe_load(body)  # JSON is YAML
    summary = {'source': source, 'request_sha256': digest, 'selection': selection.name,
               'pipeline_section_sha256': config_hash(getattr(cfg, source, None)) if hasattr(cfg, source) else None,
               'planned_at': previous.get('planned_at') if previous.get('request_sha256') == digest else utc_now(),
               **summarize(request, source),
               'approval': 'Review this summary and locks/%s.json, then commit both to approve acquisition.' % source}
    summary_path.write_text('# Written by scripts/plan.py; commit to approve. Do not edit.\n'
                            + yaml.safe_dump(summary, sort_keys=False))
    return spec_path


def _git(*args):
    return subprocess.run(['git', *args], cwd=REPO_ROOT, capture_output=True, text=True)


def verify_lock(cfg, selection, source, locks_dir, *, allow_uncommitted=False):
    """Return the lock path if it matches the current config and is committed; raise otherwise."""
    spec_path, summary_path = lock_paths(source, locks_dir)
    if not spec_path.exists():
        raise ValueError(f'No plan lock for {source}: run `pixi run plan {source}`, review, commit')
    if serialize(build_request(cfg, selection, source)) != spec_path.read_bytes():
        raise ValueError(f'{spec_path} no longer matches config/selection: rerun `pixi run plan {source}`, '
                         'review the change and commit it')
    if not allow_uncommitted:
        for path in (spec_path, summary_path):
            relative = Path(path).resolve().relative_to(REPO_ROOT).as_posix()
            tracked = _git('ls-files', '--error-unmatch', relative).returncode == 0
            clean = _git('diff', '--quiet', 'HEAD', '--', relative).returncode == 0
            if not (tracked and clean):
                raise ValueError(f'{relative} is not committed: commit the reviewed lock to approve '
                                 '(or pass --allow-uncommitted-lock for a pilot)')
    return spec_path
