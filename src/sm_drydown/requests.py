"""Build each provider request specification from config + selection + source facts.

A request specification is the complete, frozen description of one acquisition
(stations, dates, assets, bands, storage). ``scripts/plan.py`` writes it to
``locks/<source>.json``; its SHA-256 is the acquisition's identity on Kopah and in
every extract and table derived from it. The provider modules in
``sm_drydown.providers`` consume these specifications unchanged.

Each builder merges three things and nothing else:
- scientific choices from ``config/pipeline.yml`` (via ``cfg``);
- the station/sensor selection in ``data/selections/<name>/``;
- provider facts and wording from ``config/sources/<source>.yml`` (``request:``).
With the baseline config the result is byte-identical to the specifications the
published corpus was acquired with (tests/test_requests.py).
"""

import json
from dataclasses import asdict
from datetime import date

from sm_drydown.selections import Selection, read_csv

STATION_KEYS = ('latitude', 'longitude', 'network', 'station_id', 'station_name')


def _merge(fixed: dict, computed: dict, source: str) -> dict:
    clash = set(fixed) & set(computed)
    if clash:
        raise ValueError(f'{source}: keys set both in config/sources and by the builder: {sorted(clash)}')
    return {**fixed, **computed}


def storage(cfg, area='scratch'):
    kopah = cfg.storage['kopah']
    return {'destination': kopah[area], 'endpoint': kopah['endpoint'], 'profile': kopah['profile']}


def years(cfg):
    return range(date.fromisoformat(cfg.study.start).year, date.fromisoformat(cfg.study.end).year + 1)


def covered(stations, coverage):
    """Stations inside a source's declared coverage box (lat in [min, max), lon in [min, max))."""
    if not coverage:
        return list(stations)
    (lat0, lat1), (lon0, lon1) = coverage['latitude'], coverage['longitude']
    return [s for s in stations if lat0 <= s['latitude'] < lat1 and lon0 <= s['longitude'] < lon1]


def basic_stations(stations):
    return [{k: s[k] for k in STATION_KEYS} for s in stations]


def collection_id(cfg, selection: Selection, source):
    """Ancillary collection IDs: <prefix>_<start>_<end>_<first 12 hex of the station snapshot>."""
    prefix = cfg.sources[source]['collection_prefix']
    snapshot = selection.record['station_snapshot']['sha256'][:12]
    return f'{prefix}_{cfg.study.start}_{cfg.study.end}_{snapshot}'


def build_ismn(cfg, selection: Selection):
    fixed = cfg.sources['ismn']['request']
    sensors = selection.sensors_by_station()
    stations = []
    for s in selection.stations:
        if not sensors[s['station_id']]:
            raise ValueError(f"Station {s['station_id']} has no selected soil-moisture sensor")
        stations.append({**{k: s[k] for k in STATION_KEYS}, 'available_variables': s['available_variables'],
                         'soil_moisture_sensors': [{k: t[k] for k in ('depth_from', 'depth_to', 'end_date',
                                                                       'start_date', 'timeseries_id')}
                                                   for t in sensors[s['station_id']]]})
    record = selection.record
    return _merge(fixed, {
        'collection_id': record['ismn_collection_id'],
        'start': cfg.study.start, 'end': cfg.study.end,
        'inventory_date': record['inventory_date'],
        'networks': selection.networks,
        'provider_options': asdict(cfg.ismn.provider_options),
        'requested_variables': list(cfg.ismn.request_variables),
        'selection_note': record['selection_note'],
        'sources': record['sources'],
        'spatial_rule': record['spatial_rule'],
        'stations': stations,
        'stations_without_sm_in_window': [{k: e[k] for k in ('available_variables', 'network', 'reason',
                                                             'station_id', 'station_name')}
                                          for e in selection.excluded],
        'storage': storage(cfg),
        **({'supplements_collection': record['supplements_collection']}
           if record.get('supplements_collection') else {}),
    }, 'ismn')


def build_chirps(cfg, selection: Selection):
    source = cfg.sources['chirps']
    fixed, ee = source['request'], cfg.storage['earth_engine']
    assets = []
    for year in years(cfg):
        start = max(cfg.study.start, f'{year}-01-01')
        end = min(cfg.study.end, f'{year}-12-31')
        assets.append({'asset_id': str(year), 'start': start, 'end': end, 'filename': f'{year}.json',
                       'format': source['asset_format'], 'url': source['catalog_url']})
    return _merge(fixed, {
        'assets': assets, 'band': cfg.chirps.band, 'collection': cfg.chirps.collection,
        'batching': {'columns_per_row': source['getregion_columns'],
                     'concurrent_requests': ee['concurrent_requests'],
                     'maximum_values': ee['max_values_per_request'],
                     'provider_value_limit': ee['provider_value_limit']},
        'collection_id': collection_id(cfg, selection, 'chirps'),
        'start': cfg.study.start, 'end': cfg.study.end, 'sampling': cfg.chirps.sampling,
        'station_snapshot': selection.record['station_snapshot'],
        'stations': basic_stations(covered(selection.stations, source.get('coverage'))),
        'storage': storage(cfg),
    }, 'chirps')


def build_gldas(cfg, selection: Selection):
    source = cfg.sources['gldas']
    sensors = selection.sensors_by_station()
    return _merge(source['request'], {
        'collection_id': collection_id(cfg, selection, 'gldas'),
        'start': cfg.study.start, 'end': cfg.study.end, 'sampling': cfg.gldas.sampling,
        'sensor_depth_assignment': cfg.gldas.depth_assignment,
        'maximum_processing_depth_m': cfg.gldas.max_sensor_depth_m,
        'sensor_depths': [{'station_id': s['station_id'],
                           'sensors': [{k: t[k] for k in ('depth_from', 'depth_to', 'timeseries_id')}
                                       for t in sensors[s['station_id']]]}
                          for s in selection.stations],
        'station_snapshot': selection.record['station_snapshot'],
        'stations': basic_stations(covered(selection.stations, source.get('coverage'))),
        'storage': storage(cfg),
    }, 'gldas')


def build_modis_lai(cfg, selection: Selection):
    source, lai = cfg.sources['modis_lai'], cfg.modis_lai
    return _merge(source['request'], {
        'assets': [{'asset_id': str(year)} for year in years(cfg)],
        'bands': list(lai.bands), 'cadence': lai.cadence_days, 'collection': lai.collection,
        'collection_id': collection_id(cfg, selection, 'modis_lai'),
        'maximum_values': cfg.storage['earth_engine']['max_values_per_request'],
        'project': cfg.storage['earth_engine']['project'],
        'start': cfg.study.start, 'end': cfg.study.end, 'sampling': lai.sampling,
        'stations': basic_stations(covered(selection.stations, source.get('coverage'))),
        'storage': storage(cfg),
    }, 'modis_lai')


def build_modis_land_cover(cfg, selection: Selection):
    source, lc = cfg.sources['modis_land_cover'], cfg.modis_land_cover
    path = selection.directory / 'modis_land_cover_granules.csv'
    if not path.exists():
        raise FileNotFoundError(f'{path} is missing: run `pixi run plan modis_land_cover --discover-granules`')
    granules = read_csv(path)
    keys = ('cmr_collection_id', 'cmr_granule_id', 'cmr_updated', 'filename', 'size_estimate_bytes', 'tile', 'url')
    assets = []
    for year in years(cfg):
        rows = sorted((g for g in granules if g['year'] == year), key=lambda g: g['tile'])
        assets.append({'asset_id': str(year), 'granules': [{k: g[k] for k in keys} for g in rows]})
    return _merge(source['request'], {
        'assets': assets, 'bands': list(lc.bands), 'collection': lc.collection,
        'collection_id': collection_id(cfg, selection, 'modis_land_cover'),
        'start': cfg.study.start, 'end': cfg.study.end, 'sampling': lc.sampling,
        'stations': basic_stations(covered(selection.stations, source.get('coverage'))),
        'storage': storage(cfg),
    }, 'modis_land_cover')


def build_morgan_2025(cfg, selection: Selection):
    """Pinned published labels: independent of the selection; stored in processed space."""
    return _merge(cfg.sources['morgan_2025']['request'], {'storage': storage(cfg, 'processed')}, 'morgan_2025')


BUILDERS = {'ismn': build_ismn, 'chirps': build_chirps, 'gldas': build_gldas, 'modis_lai': build_modis_lai,
            'modis_land_cover': build_modis_land_cover, 'morgan_2025': build_morgan_2025}


def build_request(cfg, selection: Selection, source: str) -> dict:
    return BUILDERS[source](cfg, selection)


def serialize(request: dict) -> bytes:
    """The canonical on-disk form (sorted keys, 2-space indent, trailing newline)."""
    return (json.dumps(request, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()
