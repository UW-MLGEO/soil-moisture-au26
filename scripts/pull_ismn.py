"""Request and stage native ISMN SM/precipitation/soil-temperature exports on Kopah.

Run from the repository root (see README.md for prerequisites):
    pixi run python scripts/pull_ismn.py plan
    pixi run python scripts/pull_ismn.py submit --approved-config-sha256 APPROVED_HASH
    pixi run python scripts/pull_ismn.py collect --approved-config-sha256 APPROVED_HASH
    pixi run python scripts/pull_ismn.py extract --cache-dir CACHE_DIR --approved-config-sha256 APPROVED_HASH

One export per network. Collect returns 2 while exports are pending; rerun it
after ISMN finishes. Raw observation bytes are never parsed or transformed.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sm_drydown.acquisition.ismn_workflow import collect_exports, make_plan, require_approval, submit_exports
from sm_drydown.storage.files import ObjectStore, write_json
from sm_drydown.acquisition.ismn import Client, load_config


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['plan', 'submit', 'collect', 'extract'])
    p.add_argument('--config', type=Path, default=Path('config/ismn_conus_sm_temperature.json'))
    p.add_argument('--credentials', type=Path, default=Path('~/.config/ismn/credentials.toml'))
    p.add_argument('--cache-dir', type=Path, default=Path('output/ismn'))
    p.add_argument('--network', action='append', help='Limit to named networks; repeatable')
    p.add_argument('--approved-config-sha256', help='Exact config hash explicitly approved by the user')
    p.add_argument('--wait', action='store_true', help='Keep collecting until all selected provider exports are ready')
    p.add_argument('--poll-interval', type=int, default=30, help='Seconds between collection polls (1–60)')
    args = p.parse_args()
    if args.wait and args.action != 'collect':
        p.error('--wait is only supported for collect')
    if not 1 <= args.poll_interval <= 60:
        p.error('--poll-interval must be 1–60 seconds')
    selection = load_config(args.config)
    plan = make_plan(args.config, args.network)
    networks = list(plan['requests'])
    run_id = selection['collection_id']
    run_dir = args.cache_dir / run_id
    if args.action == 'plan':
        write_json(run_dir / 'plan.json', plan)
        print(f"Configuration SHA-256: {plan['config_sha256']}")
        print(f"{plan['station_count']} stations; {plan['sm_sensor_count']} SM sensors; "
              f"{len(networks)} networks; {plan['stations_advertising_precipitation']} stations advertise precipitation.")
        print(f"Window: {plan['start']} through {plan['end']}; variables: {', '.join(plan['variables'])}")
        print(f"Destination: {plan['storage']['destination']}")
        print(f"Plan saved: {run_dir / 'plan.json'}; no network requests made.")
        return 0
    try:
        require_approval(plan, args.approved_config_sha256)
    except ValueError as e:
        p.error(str(e))
    if args.action == 'extract':
        from sm_drydown.acquisition.ismn_extract import expand_exports
        reports = expand_exports(ObjectStore(**selection['storage']), args.config, run_dir, networks)
        print(f"Extracted and verified {len(reports)} networks; "
              f"{sum(r['files'] for r in reports)} files; {sum(r['bytes'] for r in reports)} bytes.")
        return 0
    client = Client(args.credentials)
    if args.action == 'submit':
        submit_exports(client, selection, run_dir, networks)
        return 0
    store = ObjectStore(**selection['storage'])
    while True:
        result = collect_exports(client, store, args.config, run_dir,
                                 'ismn/' + run_id, networks)
        print(f"Uploaded {len(result['archives'])} networks; pending {len(result['pending'])}.", flush=True)
        if not args.wait or not result['pending']:
            break
        time.sleep(args.poll_interval)
    return 2 if result['pending'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
