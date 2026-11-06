"""One-off migration of the legacy JSON acquisition specifications (run once; kept for the record).

    pixi run python scripts/migrate_config.py --legacy-dir retired/config

Reads the frozen specifications the 2026-11 corpus was acquired with
(config/{ismn_conus_sm_temperature,chirps_earthengine_conus,gldas_soils_conus,
modis_lai_conus,modis_land_cover_conus,published_drydowns_morgan_2025}.json) and
splits each into:

- data/selections/<selection.name>/: stations.csv, sm_sensors.csv,
  excluded_stations.csv, modis_land_cover_granules.csv and selection.yml;
- config/sources/<source>.yml: provider facts, provenance and request wording.

It then rebuilds every specification with sm_drydown.requests and stops unless
each is byte-identical to its legacy file, and writes them to locks/ as the
current plan locks. Nothing is downloaded; the legacy files are not modified.
"""

import argparse
import json
from pathlib import Path

import yaml

from sm_drydown.config import CONFIG_DIR, LOCKS_DIR, REPO_ROOT, load_config
from sm_drydown.locks import write_lock
from sm_drydown.requests import build_request, serialize
from sm_drydown.selections import (EXCLUDED_COLUMNS, GRANULE_COLUMNS, SENSOR_COLUMNS, selection_dir,
                                    write_csv, write_selection)
from sm_drydown.storage.files import sha256

LEGACY = {'ismn': 'ismn_conus_sm_temperature.json', 'chirps': 'chirps_earthengine_conus.json',
          'gldas': 'gldas_soils_conus.json', 'modis_lai': 'modis_lai_conus.json',
          'modis_land_cover': 'modis_land_cover_conus.json', 'morgan_2025': 'published_drydowns_morgan_2025.json'}
# Keys the request builders compute from pipeline.yml, the selection and storage.yml.
COMPUTED = {
    'ismn': {'collection_id', 'start', 'end', 'inventory_date', 'networks', 'provider_options',
             'requested_variables', 'selection_note', 'sources', 'spatial_rule', 'stations',
             'stations_without_sm_in_window', 'storage', 'supplements_collection'},
    'chirps': {'assets', 'band', 'collection', 'batching', 'collection_id', 'start', 'end', 'sampling',
               'station_snapshot', 'stations', 'storage'},
    'gldas': {'collection_id', 'start', 'end', 'sampling', 'sensor_depth_assignment',
              'maximum_processing_depth_m', 'sensor_depths', 'station_snapshot', 'stations', 'storage'},
    'modis_lai': {'assets', 'bands', 'cadence', 'collection', 'collection_id', 'maximum_values', 'project',
                  'start', 'end', 'sampling', 'stations', 'storage'},
    'modis_land_cover': {'assets', 'bands', 'collection', 'collection_id', 'start', 'end', 'sampling',
                         'stations', 'storage'},
    'morgan_2025': {'storage'},
}
# Provider facts used by the builders, besides the verbatim request wording.
FACTS = {
    'ismn': {},
    'chirps': {'collection_prefix': 'chirps_v2_ee', 'asset_format': 'earth_engine_getRegion_json',
               'catalog_url': 'https://developers.google.com/earth-engine/datasets/catalog/UCSB-CHG_CHIRPS_DAILY',
               'getregion_columns': 5,  # not-science: provider fact recorded once in config/sources
               'coverage': {'latitude': [-50, 50], 'longitude': [-180, 180]}},  # not-science: provider fact recorded once in config/sources
    'gldas': {'collection_prefix': 'gldas_soils', 'coverage': {'latitude': [-60, 90], 'longitude': [-180, 180]}},  # not-science: provider fact recorded once in config/sources
    'modis_lai': {'collection_prefix': 'modis_lai_061', 'coverage': None},
    'modis_land_cover': {'collection_prefix': 'modis_land_cover_061_lpdaac_hdf4', 'coverage': None},
    'morgan_2025': {},
}
HEADER = """# {title}
#
# Provider facts and provenance for `{source}` (no scientific choices: those are in
# config/pipeline.yml). `request:` holds the provider wording and fixed facts that
# are copied verbatim into locks/{source}.json by scripts/plan.py; the stations,
# dates, bands and storage are added from the selection and pipeline.yml.
# Full rights evidence: records/source_provenance.json (sources.{record}).
"""
TITLES = {'ismn': 'International Soil Moisture Network (ISMN) in situ observations',
          'chirps': 'CHIRPS v2.0 daily precipitation (Earth Engine)',
          'gldas': 'GLDAS static soil texture, sand/silt/clay fractions and porosity',
          'modis_lai': 'MODIS MCD15A3H v6.1 leaf area index (Earth Engine)',
          'modis_land_cover': 'MODIS MCD12Q1 v6.1 land cover (NASA LP DAAC HDF4)',
          'morgan_2025': 'Morgan et al. (2025) published drydown events and fitted q labels'}


def provenance(record):
    keep = ('provider', 'product', 'version', 'citation', 'citations', 'doi_metadata', 'network_citations')
    out = {k: record[k] for k in keep if k in record}
    rights = record.get('rights', {})
    out['rights'] = {k: rights[k] for k in ('declaration', 'scope', 'scope_status', 'note') if k in rights}
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--legacy-dir', type=Path, default=REPO_ROOT / 'retired' / 'config')
    parser.add_argument('--records-dir', type=Path, default=REPO_ROOT / 'records')
    args = parser.parse_args()
    legacy = {s: json.loads((args.legacy_dir / f).read_text()) for s, f in LEGACY.items()}
    registry = json.loads((args.records_dir / 'source_provenance.json').read_text())['sources']

    # 1. Provider facts, provenance and verbatim request wording.
    for source, spec in legacy.items():
        unknown = set(spec) - COMPUTED[source]
        body = {'provenance': provenance(registry[source]), **FACTS[source],
                'request': {k: spec[k] for k in sorted(unknown)}}
        text = HEADER.format(title=TITLES[source], source=source, record=source)
        (CONFIG_DIR / 'sources' / f'{source}.yml').write_text(
            text + yaml.safe_dump(body, sort_keys=False, allow_unicode=True, width=100))  # not-science: provider fact recorded once in config/sources

    # 2. The station/sensor selection.
    cfg = load_config('baseline')
    ismn = legacy['ismn']
    stations = [{k: s[k] for k in ('station_id', 'network', 'station_name', 'latitude', 'longitude',
                                   'available_variables')} for s in ismn['stations']]
    sensors = [{'station_id': s['station_id'], **{k: t[k] for k in SENSOR_COLUMNS[1:]}}
               for s in ismn['stations'] for t in s['soil_moisture_sensors']]
    excluded = [{k: e[k] for k in EXCLUDED_COLUMNS} for e in ismn['stations_without_sm_in_window']]
    granules = [{'year': int(a['asset_id']), **g} for a in legacy['modis_land_cover']['assets']
                for g in a['granules']]
    write_csv(selection_dir(cfg.selection.name) / 'modis_land_cover_granules.csv', granules, GRANULE_COLUMNS)
    snapshot = legacy['chirps']['station_snapshot']
    record = {'ismn_collection_id': ismn['collection_id'],
              'supplements_collection': ismn['supplements_collection'],
              'inventory_date': ismn['inventory_date'], 'selection_note': ismn['selection_note'],
              'spatial_rule': ismn['spatial_rule'], 'sources': ismn['sources'],
              'station_snapshot': snapshot,
              'provenance': ('Converted from the legacy specifications by scripts/migrate_config.py; '
                             'the original inventory snapshots are recorded under `sources`.'),
              'legacy_specification_sha256': {s: sha256(args.legacy_dir / f) for s, f in LEGACY.items()}}
    selection = write_selection(cfg.selection.name, stations, sensors, excluded, record,
                                extra_files=['modis_land_cover_granules.csv'])

    # 3. Rebuild every specification; it must be byte-identical to its legacy file.
    cfg = load_config('baseline')
    for source, filename in LEGACY.items():
        rebuilt = serialize(build_request(cfg, selection, source))
        original = (args.legacy_dir / filename).read_bytes()
        if rebuilt != original:
            raise SystemExit(f'{source}: rebuilt specification differs from {filename}; migration incomplete')
        lock = write_lock(cfg, selection, source, LOCKS_DIR)
        print(f'{source}: byte-identical to {filename}; lock {lock.name} sha256 {sha256(lock)[:12]}')


if __name__ == '__main__':
    main()
