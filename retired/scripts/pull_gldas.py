"""Plan or stage GLDAS native data and separate station extracts in scratch.

Run from repository root:
    pixi run python scripts/pull_gldas.py plan --config config/gldas_soils_conus.json
    pixi run python scripts/pull_gldas.py fetch --config config/gldas_soils_conus.json \
        --approved-config-sha256 USER_APPROVED_HASH
Requires Pixi, HTTPS access, and the configured Kopah profile for fetch.
No large request is authorized merely by running plan.
"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sm_drydown.providers.ancillary_spec import load_config, make_plan, require_approval
from sm_drydown.providers.gldas import fetch_and_stage
from sm_drydown.storage.files import ObjectStore, write_json
from sm_drydown.providers.http import session


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['plan', 'fetch'])
    p.add_argument('--config', type=Path, default=Path('config/gldas_soils_conus.json'))
    p.add_argument('--cache-dir', type=Path, default=Path('output/ancillary'))
    p.add_argument('--asset', action='append', help='Limit to explicit asset IDs, such as texture_025d')
    p.add_argument('--approved-config-sha256')
    args = p.parse_args()
    config, plan = load_config(args.config), make_plan(args.config)
    if args.action == 'plan':
        output = args.cache_dir / config['collection_id'] / 'plan.json'
        write_json(output, plan)
        print(f"Configuration SHA-256: {plan['config_sha256']}")
        print(f"{plan['dataset']}: {plan['station_count']} stations; {plan['start']} through {plan['end']}; "
              f"{plan['request_count']} source assets; strategy={plan['strategy']}")
        print(f"Unresolved decisions: {'; '.join(plan['blocked']) or 'none'}")
        print(f'Plan saved: {output}; no network requests made.')
        return 2 if plan['blocked'] else 0
    try:
        require_approval(args.config, args.approved_config_sha256)
    except ValueError as error:
        p.error(str(error))
    store = ObjectStore(**config['storage'])
    fetch_and_stage(args.config, args.cache_dir, session(), store,
                    args.approved_config_sha256, asset_ids=args.asset)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
