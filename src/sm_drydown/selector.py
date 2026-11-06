"""Select ISMN stations and soil-moisture sensors from the provider inventory.

The rules, all set in ``config/pipeline.yml`` (``study`` and ``selection``):

1. Region: keep stations whose lon/lat (interpreted in ``selection.station_crs``)
   fall inside the region buffered by ``region.buffer_deg`` (``global`` keeps all).
2. Networks: keep ``selection.networks`` minus ``selection.exclude_networks``.
3. Sensors: keep soil-moisture time series whose active interval overlaps the
   study window by at least ``min_overlap_days`` and whose depth_from and
   depth_to both lie in ``depth_range_m``.
4. A station is selected if it keeps at least one sensor; otherwise it is listed
   in excluded_stations.csv with the rule that removed it.

Inventory order is preserved (networks, then stations, then time series), so the
same inventory always gives the same CSV bytes.
"""

import json
import zipfile
from datetime import date
from pathlib import Path

NO_SERIES = 'No soil-moisture time series in the provider inventory'
NOT_ACTIVE = 'No soil-moisture sensor interval in requested window'
TOO_DEEP = 'No soil-moisture sensor inside selection.depth_range_m'


def load_inventory(path):
    """Stations of the ISMN data-viewer inventory in provider order."""
    inventory = json.loads(Path(path).read_text())
    stations = []
    for network in inventory['Networks']:
        for s in network['Stations']:
            stations.append({'station_id': s['stationID'], 'network': network['networkID'],
                             'station_name': s['station_name'], 'latitude': s['lat'], 'longitude': s['lng'],
                             'available_variables': s['variableText'] or '',
                             'timeseries': s['Timeseries'] or []})
    return stations, inventory.get('created_at')


def overlap_days(first, last, start, end):
    """Days shared by [first, last] and [start, end] (inclusive dates); 0 if undated."""
    if not first or not last or 'never' in (first, last):
        return 0
    first, last = date.fromisoformat(first[:10]), date.fromisoformat(last[:10])  # not-science: ISO date prefix
    if first > last:
        raise ValueError('Reversed sensor date range in inventory')
    return max(0, (min(last, date.fromisoformat(end)) - max(first, date.fromisoformat(start))).days + 1)


def region_geometry(cfg, boundary_file=None):
    """Shapely geometry of the buffered region in station_crs, or None for global."""
    import geopandas as gpd
    from shapely.geometry import box

    region = cfg.selection.region
    if region.kind == 'global':
        return None
    if region.kind == 'bbox':
        frame = gpd.GeoDataFrame(geometry=[box(*region.bbox)], crs='EPSG:4326')
    else:
        source = f'zip://{boundary_file}' if zipfile.is_zipfile(boundary_file) else str(boundary_file)
        frame = gpd.read_file(source)
        if region.kind == 'named_boundary':
            spec = cfg.regions[region.name]
            frame = frame[~frame[spec['attribute']].isin(spec['exclude'])]
    frame = frame.to_crs(cfg.selection.station_crs)
    return frame.union_all().buffer(region.buffer_deg)


def select_stations(cfg, inventory_stations, geometry):
    """Apply the selection rules; return (stations, sensors, excluded)."""
    from shapely.geometry import Point

    sel = cfg.selection
    low, high = sel.depth_range_m
    stations, sensors, excluded = [], [], []
    for s in inventory_stations:
        if geometry is not None and not geometry.covers(Point(s['longitude'], s['latitude'])):
            continue
        if (sel.networks != 'all' and s['network'] not in sel.networks) or s['network'] in sel.exclude_networks:
            continue
        row = {k: s[k] for k in ('station_id', 'network', 'station_name', 'latitude', 'longitude',
                                 'available_variables')}
        active = [t for t in s['timeseries'] if overlap_days(t['start_date'], t['end_date'], cfg.study.start,
                                                             cfg.study.end) >= sel.min_overlap_days]
        kept = [t for t in active if low <= t['depth_from'] <= high and low <= t['depth_to'] <= high]
        if kept:
            stations.append(row)
            sensors += [{'station_id': s['station_id'], **{k: t[k] for k in ('timeseries_id', 'depth_from', 'depth_to',
                                                                              'start_date', 'end_date')}}
                        for t in kept]
        else:
            reason = NO_SERIES if not s['timeseries'] else NOT_ACTIVE if not active else TOO_DEEP
            excluded.append({**{k: row[k] for k in ('station_id', 'network', 'station_name',
                                                    'available_variables')}, 'reason': reason})
    return stations, sensors, excluded


def cited_networks(records_dir):
    """ISMN networks with a citation record: a bibliography entry (``note = {Network: X}``)
    or an entry in the network provenance index (references or acknowledgement)."""
    import re
    records_dir = Path(records_dir)
    bib = set(re.findall(r'note\s*=\s*\{Network:\s*([^}]+)\}',
                         (records_dir / 'ismn_network_references.bib').read_text()))
    index = json.loads((records_dir / 'ismn_network_provenance.json').read_text())
    return bib | {n['network'] for n in index['networks']
                  if n.get('network_references') or n.get('acknowledgement')}


def compare(new, old):
    """Stations and sensors added or removed relative to another selection."""
    def ids(sel):
        return {s['station_id'] for s in sel.stations}, {t['timeseries_id'] for t in sel.sensors}
    (s1, t1), (s0, t0) = ids(new), ids(old)
    return {'stations_added': sorted(s1 - s0), 'stations_removed': sorted(s0 - s1),
            'sensors_added': len(t1 - t0), 'sensors_removed': len(t0 - t1),
            'same_order': [s['station_id'] for s in new.stations] == [s['station_id'] for s in old.stations]}
