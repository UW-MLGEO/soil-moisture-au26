"""Stage -1: select ISMN stations and soil-moisture sensors (writes data/selections/<name>/).

    pixi run select                         # baseline rules in config/pipeline.yml
    pixi run python scripts/select_stations.py --run global
    pixi run python scripts/select_stations.py --compare conus_2026_11 --dry-run

Downloads (once, then reuses by SHA-256) the ISMN data-viewer station inventory
and, for a named boundary or polygon file, the region boundary into
paths.selection_cache. Set `selection.inventory_snapshot` to a recorded SHA-256
to rebuild a selection from that exact inventory. Writes stations.csv,
sm_sensors.csv, excluded_stations.csv and selection.yml (commit them).
Needs public HTTPS only; no credentials.
"""

import argparse
import shutil
from pathlib import Path

from sm_drydown.config import REPO_ROOT, load_config
from sm_drydown.providers.http import session
from sm_drydown.selections import load_selection, selection_dir, write_selection
from sm_drydown.selector import cited_networks, compare, load_inventory, region_geometry, select_stations
from sm_drydown.storage.files import download, sha256

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--run', default='baseline', help='run file config/runs/<run>.yml')
parser.add_argument('--compare', help='report differences against another selection name')
parser.add_argument('--dry-run', action='store_true', help='report only; do not write data/selections/')
parser.add_argument('--force', action='store_true', help='overwrite an existing selection folder')
args = parser.parse_args()
cfg = load_config(args.run)
sel, cache = cfg.selection, Path(cfg.paths.selection_cache)

# ── Step 1. Provider inventory (pinned by SHA-256 once recorded) ──────────────
if sel.inventory_snapshot:
    inventory_path = cache / 'inventory' / f'{sel.inventory_snapshot}.json'
    if not inventory_path.exists() or sha256(inventory_path) != sel.inventory_snapshot:
        raise SystemExit(f'Pinned inventory {sel.inventory_snapshot} is not in {inventory_path.parent}; '
                         'restore it (e.g. from Kopah scratch selection/inventory/) or unpin it')
    inventory_record = {'url': sel.inventory_url, 'sha256': sel.inventory_snapshot,
                        'response_bytes': inventory_path.stat().st_size}
else:
    latest = cache / 'inventory' / 'latest' / Path(sel.inventory_url).name
    inventory_record = download(session(), sel.inventory_url, latest)
    inventory_path = cache / 'inventory' / f"{inventory_record['sha256']}.json"
    if not inventory_path.exists():
        shutil.copy2(latest, inventory_path)
inventory, created_at = load_inventory(inventory_path)
print(f'step 1: inventory {inventory_record["sha256"][:12]} (provider created_at {created_at}): '
      f'{len(inventory)} stations')

# ── Step 2. Region ──────────────────────────────────────────────────────────────
region, boundary_record = sel.region, None
if region.kind in ('named_boundary', 'polygon_file'):
    url = cfg.regions[region.name]['url'] if region.kind == 'named_boundary' else region.polygon_file
    if '://' in url:
        boundary_record = download(session(), url, cache / 'boundaries' / Path(url).name)
        boundary = cache / 'boundaries' / Path(url).name
    else:
        boundary = Path(url) if Path(url).is_absolute() else REPO_ROOT / url
        boundary_record = {'path': str(url), 'sha256': sha256(boundary)}
geometry = region_geometry(cfg, boundary if boundary_record else None)
rule = (cfg.regions[region.name]['spatial_rule'] if region.kind == 'named_boundary'
        else f'{region.kind}; buffer {region.buffer_deg} deg; station lon/lat treated as {sel.station_crs}')
print(f'step 2: region {region.kind} {region.name or region.bbox or region.polygon_file or ""}: {rule}')

# ── Step 3. Stations and sensors ──────────────────────────────────────────────
stations, sensors, excluded = select_stations(cfg, inventory, geometry)
print(f'step 3: {len(stations)} stations, {len(sensors)} SM sensors in {len({s["network"] for s in stations})} '
      f'networks; {len(excluded)} in-region stations excluded')
for reason in sorted({e['reason'] for e in excluded}):
    print(f'    {sum(e["reason"] == reason for e in excluded):5d}  {reason}')
uncited = sorted({s['network'] for s in stations} - cited_networks(REPO_ROOT / 'records'))
if uncited:
    print(f'    networks without a citation record in records/: {uncited} '
          '(plan ismn will refuse until they are added)')

if args.compare:
    from types import SimpleNamespace
    print('compare with', args.compare, compare(SimpleNamespace(stations=stations, sensors=sensors),
                                                 load_selection(args.compare)))

# ── Step 4. Write the selection ───────────────────────────────────────────────
if args.dry_run:
    raise SystemExit(0)
if (selection_dir(sel.name) / 'selection.yml').exists() and not args.force:
    raise SystemExit(f'data/selections/{sel.name} exists; choose a new selection.name or pass --force')
record = {'inventory_date': str(created_at)[:10], 'selection_note': f'scripts/select_stations.py --run {args.run}',  # not-science: ISO date prefix
          'spatial_rule': rule,
          'sources': {Path(sel.inventory_url).name: inventory_record,
                      **({'boundary': boundary_record} if boundary_record else {})},
          'rules': {'study': [cfg.study.start, cfg.study.end], 'networks': sel.networks,
                    'exclude_networks': sel.exclude_networks, 'min_overlap_days': sel.min_overlap_days,
                    'depth_range_m': sel.depth_range_m}}
written = write_selection(sel.name, stations, sensors, excluded, {**record, 'ismn_collection_id': None,
                                                                  'station_snapshot': None})
digest = written.record['files']['stations.csv']
collection = f"{cfg.study.start}_{cfg.study.end}_{sha256(written.directory / 'sm_sensors.csv')[:12]}_ismn"
written = write_selection(sel.name, stations, sensors, excluded, {
    **record, 'ismn_collection_id': collection,
    'station_snapshot': {'collection_id': collection, 'sha256': digest,
                         'source_file': f'data/selections/{sel.name}/stations.csv'}})
print(f'step 4: wrote data/selections/{sel.name}/ (ismn collection {collection})')
