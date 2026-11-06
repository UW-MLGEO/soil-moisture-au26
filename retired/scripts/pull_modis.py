"""Fetch all native MODIS bands at configured station pixels.

LAI (MCD15A3H) uses Earth Engine; land cover (MCD12Q1) downloads native HDF4
tiles directly from NASA LP DAAC. The config's strategy selects the backend.
Run from repository root:
  pixi run python scripts/pull_modis.py plan --config config/modis_lai_conus.json
  pixi run python scripts/pull_modis.py fetch --config config/modis_lai_conus.json \
    --project soil-moisture-drydown-au26 --approved-config-sha256 APPROVED_HASH
  pixi run python scripts/pull_modis.py plan --config config/modis_land_cover_conus.json
  pixi run python scripts/pull_modis.py fetch --config config/modis_land_cover_conus.json \
    --approved-config-sha256 APPROVED_HASH
Prerequisites: Pixi and the configured Kopah profile; Earth Engine authentication
for LAI; Earthdata USER=/PASS= lines in a chmod-600 file (default
~/.earthdata/config) for land cover. Both backends validate and reuse their cache.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from sm_drydown.providers import lpdaac, modis
from sm_drydown.storage.files import ObjectStore, write_json


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('action', choices=['plan', 'fetch'])
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--cache-dir', type=Path, default=Path('output/ancillary'))
    p.add_argument('--project', help='Earth Engine Cloud project (LAI only)')
    p.add_argument('--asset', action='append', help='Annual asset subset; repeat for multiple years')
    p.add_argument('--credentials', type=Path, default=Path('~/.earthdata/config'),
                   help='Earthdata USER=/PASS= file (LP DAAC land cover only)')
    p.add_argument('--approved-config-sha256')
    args = p.parse_args()
    direct = json.loads(args.config.read_text()).get('strategy') == 'lpdaac_https_hdf4'
    backend = lpdaac if direct else modis
    c, plan = backend.load_config(args.config), backend.make_plan(args.config)
    if args.action == 'plan':
        path = args.cache_dir/c['collection_id']/'plan.json'
        write_json(path, plan)
        print(f"Configuration SHA-256: {plan['config_sha256']}")
        print(f"{plan['dataset']} via {c['strategy']}: {plan['station_count']} stations; "
              f"{plan['annual_assets']} annual assets; {len(plan['bands'])} bands")
        print(f'Plan saved: {path}; no network requests made.')
        return 0
    store = ObjectStore(**c['storage'])
    if direct:
        if args.project:
            p.error('--project applies only to Earth Engine configs')
        try:
            lpdaac.require_approval(args.config, args.approved_config_sha256)
        except ValueError as error:
            p.error(str(error))
        result = lpdaac.fetch_and_stage(args.config, args.cache_dir, lpdaac.EarthdataSession(args.credentials),
                                        store, args.approved_config_sha256, asset_ids=args.asset)
        print(f"Staged {len(result['assets'])} annual LP DAAC land-cover extracts.")
        return 0
    if not args.project:
        p.error('--project is required for Earth Engine fetch')
    modis.fetch_and_stage(args.config, args.cache_dir, modis.EarthEngineClient(args.project),
                          store, args.approved_config_sha256, asset_ids=args.asset)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
