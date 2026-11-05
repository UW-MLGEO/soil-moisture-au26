"""Inspect local daily station data and native GLDAS extracts without modifying them.

From repository root (root Pixi environment; completed acquisition/daily caches):
  pixi run python scripts/inspect_data.py
  pixi run python scripts/inspect_data.py --station-id 908 --output-dir output/inspection_pilot --figure-dir /tmp/inspection-pilot-figures

Writes candidate/coverage CSVs and JSON under --output-dir; report PNGs default
to docs/figures so they can be committed with the documentation.
No credentials/network access, filtering, gap filling, new data downloads or
Morgan event-table processing. Missing measurements remain missing.
"""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from sm_drydown.inspection.workflow import inspect_data


def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--daily-dir',type=Path,default=Path('output/daily_stations_v1'))
    parser.add_argument('--ismn-config',type=Path,default=Path('config/ismn_conus_sm_temperature.json'))
    parser.add_argument('--gldas-config',type=Path,default=Path('config/gldas_soils_conus.json'))
    parser.add_argument('--ancillary-cache-dir',type=Path,default=Path('output/ancillary'))
    parser.add_argument('--output-dir',type=Path,default=Path('output/inspection'))
    parser.add_argument('--figure-dir',type=Path,default=Path('docs/figures'),help='PNG destination (default: docs/figures)')
    parser.add_argument('--flatline-min-days',type=int,default=3,help='Exact repeats on at least this many consecutive daily dates')
    parser.add_argument('--chirps-positive-quantile',type=float,default=.999)
    parser.add_argument('--gldas-sum-tolerance',type=float,default=.01)
    parser.add_argument('--network')
    parser.add_argument('--station-id',type=int)
    args=parser.parse_args()
    result=inspect_data(args.daily_dir,args.ismn_config,args.gldas_config,args.ancillary_cache_dir,args.output_dir,
        flatline_min_days=args.flatline_min_days,chirps_positive_quantile=args.chirps_positive_quantile,
        gldas_sum_tolerance=args.gldas_sum_tolerance,network=args.network,station_id=args.station_id,
        figure_dir=args.figure_dir)
    print(f"Inspected {result['stations']} stations; reports: {args.output_dir}; figures: {args.figure_dir}")
    print('Inspection complete: no source data changed or measurements filled.')


if __name__=='__main__':main()
