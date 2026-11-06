"""Stage 0: write the plan lock for one source (offline; approve by committing it).

    pixi run plan ismn                 # or chirps | gldas | modis_lai | modis_land_cover | morgan_2025
    pixi run python scripts/plan.py modis_land_cover --discover-granules   # new selection: resolve LP DAAC tiles

Builds the full provider request from config/pipeline.yml, the committed station
selection (data/selections/<selection.name>/) and config/sources/<source>.yml,
writes locks/<source>.json and locks/<source>.summary.yml, and prints what an
acquisition would request. No provider is contacted (except --discover-granules,
which queries NASA CMR's public granule search). Review, then commit both lock
files: `pixi run acquire <source>` refuses uncommitted or out-of-date locks.
"""

import argparse

import yaml

from sm_drydown.config import LOCKS_DIR, REPO_ROOT, SOURCES, load_config
from sm_drydown.locks import lock_paths, write_lock
from sm_drydown.selections import GRANULE_COLUMNS, load_selection, write_csv
from sm_drydown.selector import cited_networks
from sm_drydown.storage.files import sha256

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('source', choices=SOURCES)
parser.add_argument('--run', default='baseline', help='run file config/runs/<run>.yml')
parser.add_argument('--discover-granules', action='store_true',
                    help='modis_land_cover: query NASA CMR for the tiles the selected stations need')
args = parser.parse_args()
cfg = load_config(args.run)
selection = load_selection(cfg.selection.name)

if args.source == 'ismn':
    uncited = sorted(set(selection.networks) - cited_networks(REPO_ROOT / 'records'))
    if uncited:
        raise SystemExit(f'ISMN terms require citing every contributing network; add {uncited} to '
                         'records/ismn_network_references.bib or records/ismn_network_provenance.json')

if args.discover_granules:
    if args.source != 'modis_land_cover':
        raise SystemExit('--discover-granules applies to modis_land_cover only')
    from sm_drydown.providers import lpdaac
    from sm_drydown.providers.http import session
    from sm_drydown.requests import basic_stations, years
    spec = {'stations': basic_stations(selection.stations),
            'source_projection': cfg.sources['modis_land_cover']['request']['source_projection']}
    rows = []
    for year in years(cfg):
        granules, _ = lpdaac.discover_year(spec, year, session())
        rows += [{'year': year, **g} for g in granules]
        print(f'{year}: {len(granules)} tiles')
    path = selection.directory / 'modis_land_cover_granules.csv'
    write_csv(path, rows, GRANULE_COLUMNS)
    record_path = selection.directory / 'selection.yml'
    header, body = record_path.read_text().split('\n', 2)[:2], yaml.safe_load(record_path.read_text())
    body['files'][path.name] = sha256(path)
    record_path.write_text('\n'.join(header) + '\n' + yaml.safe_dump(body, sort_keys=True))
    selection = load_selection(cfg.selection.name)

write_lock(cfg, selection, args.source, LOCKS_DIR)
spec_path, summary_path = lock_paths(args.source, LOCKS_DIR)
print(summary_path.read_text())
print(f'Review {spec_path.relative_to(REPO_ROOT)} and {summary_path.relative_to(REPO_ROOT)}, then commit both to '
      f'approve `pixi run acquire {args.source}`. No provider was contacted.')
