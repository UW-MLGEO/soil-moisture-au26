"""Preserve the pinned published Morgan event/label dataset in non-scratch Kopah.

From repository root:
  pixi run python scripts/pull_published_drydowns.py plan
  pixi run python scripts/pull_published_drydowns.py fetch --approved-config-sha256 HASH

Requires the root Pixi environment, public HTTPS access and local Kopah profile.
No provider account is needed. Cached files are SHA-256 checked; only missing or
invalid files are downloaded. This workflow never invokes homogenization.
"""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from sm_drydown.acquisition.published import load_config,make_plan,fetch_and_stage
from sm_drydown.storage.files import ObjectStore


def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action',choices=['plan','fetch'])
    parser.add_argument('--config',type=Path,default=Path('config/published_drydowns_morgan_2025.json'))
    parser.add_argument('--cache-dir',type=Path,default=Path('output/reference/ismn-drydowns'))
    parser.add_argument('--approved-config-sha256')
    args=parser.parse_args();c=load_config(args.config);plan=make_plan(args.config)
    if args.action=='plan':
        print(f"Configuration SHA-256: {plan['config_sha256']}")
        print(f"Published alternate labels: {plan['published_events']} events; {plan['source_files']} source files; homogenize=False")
        return
    result=fetch_and_stage(args.config,args.cache_dir,ObjectStore(**c['storage']),args.approved_config_sha256)
    print(f"Published {result['table']['rows']} unchanged event rows: {result['table']['uri']}")
    print(f"Manifest: {result['manifest_uri']}")


if __name__=='__main__':main()
