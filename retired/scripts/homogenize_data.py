"""Write daily pre-drydown station tables from existing local acquisition caches.

Run `pixi run python scripts/homogenize_data.py --help` for input paths.
All unresolved QC/decoding choices must be supplied in an explicit policy JSON.
The run action reads local caches; upload publishes validated bundles to the
non-scratch processed prefix. Neither action detects or fits drydowns.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sm_drydown.homogenization.workflow import run_homogenization
from sm_drydown.storage.files import ObjectStore
from sm_drydown.storage.catalog import daily_manifest, publish_daily_manifest, upload_daily_files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'upload'])
    parser.add_argument('--ismn-config', type=Path, default=Path('config/ismn_conus_sm_temperature.json'))
    parser.add_argument('--chirps-config', type=Path, default=Path('config/chirps_earthengine_conus.json'))
    parser.add_argument('--gldas-config', type=Path, default=Path('config/gldas_soils_conus.json'))
    parser.add_argument('--land-cover-config', type=Path, default=Path('config/modis_land_cover_conus.json'))
    parser.add_argument('--lai-config', type=Path, default=Path('config/modis_lai_conus.json'))
    parser.add_argument('--policy', type=Path, help='Required for run: explicit decoding, band-selection and QC policy JSON')
    parser.add_argument('--ismn-cache-dir', type=Path, default=Path('output/ismn'))
    parser.add_argument('--ancillary-cache-dir', type=Path, default=Path('output/ancillary'))
    parser.add_argument('--output-dir', type=Path, default=Path('output/daily_stations'))
    parser.add_argument('--network')
    parser.add_argument('--station-id', type=int)
    parser.add_argument('--destination', default='s3://gaia/ml4geo_sm_drydown/', help='Processed destination; scratch is rejected')
    parser.add_argument('--profile', default='kopah')
    parser.add_argument('--endpoint', default='https://s3.kopah.uw.edu')
    parser.add_argument('--upload-workers', type=int, default=4, help='Concurrent verified uploads, 1-16')
    args = parser.parse_args()
    if args.action == 'upload':
        manifest = daily_manifest(args.output_dir, args.ismn_config,
                                  network=args.network, station_id=args.station_id)
        store = ObjectStore(args.destination, args.profile, args.endpoint)
        uploaded = upload_daily_files(args.output_dir, manifest, store, workers=args.upload_workers)
        manifest_uri = publish_daily_manifest(args.output_dir, manifest, store)
        print(f'Uploaded/verified {len(uploaded)} processed objects.')
        print(f'Daily completion manifest: {manifest_uri}')
        return
    if args.policy is None:
        parser.error('run requires --policy with explicit preprocessing decisions')
    result = run_homogenization(args.ismn_config,
        {'chirps': args.chirps_config, 'gldas': args.gldas_config,
         'land_cover': args.land_cover_config, 'lai': args.lai_config}, args.policy,
        args.ismn_cache_dir, args.ancillary_cache_dir, args.output_dir,
        network=args.network, station_id=args.station_id)
    print(f"Wrote/validated {result['stations']} stations; {result['rows']} daily rows; no measurements filled.")


if __name__ == '__main__':
    main()
