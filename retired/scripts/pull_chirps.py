"""Plan or stage native-grid CHIRPS v2 Earth Engine responses and station NPZ.

Prerequisites: configured Pixi environment, Earth Engine authentication and an
Earth Engine enabled Cloud project, plus the configured Kopah profile for fetch.
Run from repository root:
  pixi run python scripts/pull_chirps.py plan
  pixi run python scripts/pull_chirps.py fetch --project PROJECT_ID \
      --approved-config-sha256 APPROVED_HASH
Raw service JSON preserves nulls and source metadata. No interpolation or filling.
"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sm_drydown.providers.chirps import (EarthEngineClient, fetch_and_stage,
                                              load_config, make_plan, require_approval)
from sm_drydown.storage.files import ObjectStore, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'fetch'])
    parser.add_argument('--config', type=Path, default=Path('config/chirps_earthengine_conus.json'))
    parser.add_argument('--cache-dir', type=Path, default=Path('output/ancillary'))
    parser.add_argument('--project')
    parser.add_argument('--asset', action='append', help='Optional explicit annual asset IDs')
    parser.add_argument('--approved-config-sha256')
    args = parser.parse_args()
    config, plan = load_config(args.config), make_plan(args.config)
    if args.action == 'plan':
        path = args.cache_dir / config['collection_id'] / 'plan.json'
        write_json(path, plan)
        print(f"Configuration SHA-256: {plan['config_sha256']}")
        print(f"{plan['station_count']} stations; {plan['start']} through {plan['end']}; "
              f"{plan['annual_assets']} annual assets; at most {plan['maximum_values_per_request']} values per request")
        print(f'Plan saved: {path}; no network requests made.')
        return
    try:
        require_approval(args.config, args.approved_config_sha256)
    except ValueError as error:
        parser.error(str(error))
    if not args.project:
        parser.error('--project is required for Earth Engine retrieval')
    fetch_and_stage(args.config, args.cache_dir, EarthEngineClient(args.project),
                    ObjectStore(**config['storage']), args.approved_config_sha256, asset_ids=args.asset)


if __name__ == '__main__':
    main()
