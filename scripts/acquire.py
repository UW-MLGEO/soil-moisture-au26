"""Stage 1: acquire one source exactly as its committed plan lock describes.

    pixi run acquire chirps                       # Earth Engine; project from config/storage.yml
    pixi run acquire gldas                        # public HTTPS
    pixi run acquire modis_lai                    # Earth Engine
    pixi run acquire modis_land_cover             # NASA LP DAAC (Earthdata Login)
    pixi run acquire morgan_2025                  # public GitHub, pinned commit
    pixi run python scripts/acquire.py ismn --action submit     # then:
    pixi run python scripts/acquire.py ismn --action collect --wait
    pixi run python scripts/acquire.py ismn --action extract    # optional unpacked mirror on Kopah

Refuses to run unless locks/<source>.json equals the request rebuilt from the
current config and is committed (pass --allow-uncommitted-lock for a pilot).
Caches go to paths.ismn_cache / paths.ancillary_cache / paths.morgan_cache;
cached files are SHA-256 checked and only missing or invalid files are fetched.
Raw bytes and station extracts are uploaded (immutable, private) to the Kopah
destination in the lock. Prerequisites: the `kopah` AWS profile; Earth Engine
auth (chirps, modis_lai); Earthdata credentials (modis_land_cover); ISMN
credentials (ismn); see config/storage.yml for their locations.
"""

import argparse
import json
import time
from pathlib import Path

from sm_drydown.config import LOCKS_DIR, SOURCES, load_config
from sm_drydown.locks import verify_lock
from sm_drydown.selections import load_selection
from sm_drydown.storage.files import ObjectStore, sha256

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('source', choices=SOURCES)
parser.add_argument('--run', default='baseline')
parser.add_argument('--paths', default=None)
parser.add_argument('--action', choices=['submit', 'collect', 'extract'], help='ismn only')
parser.add_argument('--network', action='append', help='ismn: limit to these networks (repeatable)')
parser.add_argument('--asset', action='append', help='limit to these asset IDs (e.g. a year; repeatable)')
parser.add_argument('--wait', action='store_true', help='ismn collect: poll until all exports are ready')
parser.add_argument('--poll-interval', type=int, default=30, help='ismn collect --wait: seconds (1-60)')  # not-science: ISMN polling interval (transfer, not processing)
parser.add_argument('--allow-uncommitted-lock', action='store_true')
args = parser.parse_args()
cfg = load_config(args.run, args.paths)
lock = verify_lock(cfg, load_selection(cfg.selection.name), args.source, LOCKS_DIR,
                   allow_uncommitted=args.allow_uncommitted_lock)
approved, request = sha256(lock), json.loads(lock.read_text())
store = ObjectStore(**request['storage'])
paths, credentials = cfg.paths, cfg.storage
print(f'{args.source}: lock {lock.name} {approved[:12]} matches config; acquiring')

if args.source == 'ismn':
    from sm_drydown.providers.ismn import Client
    from sm_drydown.providers.ismn_workflow import collect_exports, make_plan, require_approval, submit_exports
    if args.action is None:
        parser.error('ismn needs --action submit | collect | extract')
    plan = make_plan(lock, args.network)
    require_approval(plan, approved)
    networks, run_dir = list(plan['requests']), Path(paths.ismn_cache) / request['collection_id']
    if args.action == 'extract':
        from sm_drydown.providers.ismn_extract import expand_exports
        expand_exports(store, lock, run_dir, networks)
        raise SystemExit(0)
    client = Client(Path(credentials['ismn']['credentials']).expanduser())
    if args.action == 'submit':
        submit_exports(client, json.loads(lock.read_text()), run_dir, networks)
        raise SystemExit(0)
    if not 1 <= args.poll_interval <= 60:  # not-science: ISMN polling interval (transfer, not processing)
        parser.error('--poll-interval must be 1-60 seconds')
    while True:
        result = collect_exports(client, store, lock, run_dir, 'ismn/' + request['collection_id'], networks)
        print(f"Uploaded {len(result['archives'])} networks; pending {len(result['pending'])}.", flush=True)
        if not args.wait or not result['pending']:
            break
        time.sleep(args.poll_interval)
    raise SystemExit(2 if result['pending'] else 0)

if args.source == 'chirps':
    from sm_drydown.providers import chirps
    chirps.fetch_and_stage(lock, paths.ancillary_cache, chirps.EarthEngineClient(credentials['earth_engine']['project']),
                           store, approved, asset_ids=args.asset)
elif args.source == 'modis_lai':
    from sm_drydown.providers import modis
    modis.fetch_and_stage(lock, paths.ancillary_cache, modis.EarthEngineClient(credentials['earth_engine']['project']),
                          store, approved, asset_ids=args.asset)
elif args.source == 'gldas':
    from sm_drydown.providers import gldas
    from sm_drydown.providers.http import session
    gldas.fetch_and_stage(lock, paths.ancillary_cache, session(), store, approved, asset_ids=args.asset)
elif args.source == 'modis_land_cover':
    from sm_drydown.providers import lpdaac
    lpdaac.fetch_and_stage(lock, paths.ancillary_cache, lpdaac.EarthdataSession(credentials['earthdata']['credentials']),
                           store, approved, asset_ids=args.asset)
else:
    from sm_drydown.providers import published
    result = published.fetch_and_stage(lock, paths.morgan_cache, store, approved)
    print(f"Published {result['table']['rows']} unchanged event rows: {result['table']['uri']}")
print(f'{args.source}: acquisition complete for lock {approved[:12]}')
