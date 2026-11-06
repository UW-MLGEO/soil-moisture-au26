"""Restore local acquisition caches from Kopah scratch for offline homogenization.

Run from the repository root after configuring the `kopah` AWS profile (README.md):
    pixi run python scripts/restore_from_kopah.py
    pixi run python scripts/restore_from_kopah.py --network ARM   # pilot subset

Downloads the native ISMN ZIPs and the CHIRPS, GLDAS, MODIS LAI and land-cover
station extracts that homogenize_data.py and inspect_data.py read, and rebuilds
each ZIP's checksum sidecar from its uploaded provenance record. Files already
matching the SHA-256 recorded on Kopah are reused; nothing is uploaded and no
provider is contacted. Each cache records sources and times in restore.json.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sm_drydown.storage.files import ObjectStore
from sm_drydown.storage.restore import restore_extracts, restore_ismn


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--ismn-config', type=Path, default=Path('config/ismn_conus_sm_temperature.json'))
    p.add_argument('--chirps-config', type=Path, default=Path('config/chirps_earthengine_conus.json'))
    p.add_argument('--gldas-config', type=Path, default=Path('config/gldas_soils_conus.json'))
    p.add_argument('--land-cover-config', type=Path, default=Path('config/modis_land_cover_conus.json'))
    p.add_argument('--lai-config', type=Path, default=Path('config/modis_lai_conus.json'))
    p.add_argument('--ismn-cache-dir', type=Path, default=Path('output/ismn'))
    p.add_argument('--ancillary-cache-dir', type=Path, default=Path('output/ancillary'))
    p.add_argument('--network', action='append', help='Limit ISMN archives to named networks; repeatable')
    args = p.parse_args()
    files = []
    for config in (args.chirps_config, args.gldas_config, args.land_cover_config, args.lai_config):
        storage = json.loads(config.read_text())['storage']
        files += restore_extracts(ObjectStore(**storage), config, args.ancillary_cache_dir)
    storage = json.loads(args.ismn_config.read_text())['storage']
    files += restore_ismn(ObjectStore(**storage), args.ismn_config, args.ismn_cache_dir, args.network)
    fetched = [f for f in files if f['downloaded']]
    print(f"Verified {len(files)} files ({sum(f['bytes'] for f in files)} bytes); "
          f"downloaded {len(fetched)} ({sum(f['bytes'] for f in fetched)} bytes).")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
