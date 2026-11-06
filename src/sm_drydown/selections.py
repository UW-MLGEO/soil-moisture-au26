"""Frozen station/sensor selections as diffable CSV tables.

A selection lives in ``data/selections/<name>/``:

- ``stations.csv``: one row per selected ISMN station (the only station list);
- ``sm_sensors.csv``: selected soil-moisture sensors, in provider order;
- ``excluded_stations.csv``: stations dropped by a selection rule, with the rule;
- ``selection.yml``: how the selection was made (inventory snapshots, rule,
  collection IDs) and the SHA-256 of each CSV. Written by scripts/select_stations.py.

Row order is preserved exactly: it fixes the order of every provider request.
"""

import csv
import io
from dataclasses import dataclass
from pathlib import Path

import yaml

from sm_drydown.config import SELECTIONS_DIR
from sm_drydown.storage.files import sha256

STATION_COLUMNS = ['station_id', 'network', 'station_name', 'latitude', 'longitude', 'available_variables']
SENSOR_COLUMNS = ['station_id', 'timeseries_id', 'depth_from', 'depth_to', 'start_date', 'end_date']
EXCLUDED_COLUMNS = ['station_id', 'network', 'station_name', 'available_variables', 'reason']
GRANULE_COLUMNS = ['year', 'tile', 'filename', 'url', 'size_estimate_bytes', 'cmr_granule_id',
                   'cmr_collection_id', 'cmr_updated']
INTEGER = {'station_id', 'timeseries_id', 'year', 'size_estimate_bytes'}
FLOAT = {'latitude', 'longitude', 'depth_from', 'depth_to'}


def _parse(row):
    out = {}
    for key, value in row.items():
        if value == '' and key in INTEGER | FLOAT:
            out[key] = None
        elif key in INTEGER:
            out[key] = int(value)
        elif key in FLOAT:
            out[key] = float(value)
        else:
            out[key] = value
    return out


def read_csv(path) -> list[dict]:
    with open(path, newline='') as f:
        return [_parse(row) for row in csv.DictReader(f)]


def write_csv(path, rows, columns):
    """Write rows with exact float repr so values round-trip unchanged."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator='\n')
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ('' if row[k] is None else repr(row[k]) if isinstance(row[k], float) else row[k])
                         for k in columns})
    path.write_text(buffer.getvalue())


@dataclass
class Selection:
    """A loaded selection: stations with their SM sensors, exclusions and its record."""
    name: str
    directory: Path
    stations: list[dict]
    sensors: list[dict]
    excluded: list[dict]
    record: dict

    @property
    def networks(self):
        return sorted({s['network'] for s in self.stations})

    def sensors_by_station(self):
        grouped = {s['station_id']: [] for s in self.stations}
        for sensor in self.sensors:
            grouped[sensor['station_id']].append(sensor)
        return grouped

    def subset(self, networks='all', station_ids='all'):
        """Stations restricted to a pilot subset (never changes provider requests)."""
        return [s for s in self.stations
                if (networks == 'all' or s['network'] in networks)
                and (station_ids == 'all' or s['station_id'] in station_ids)]


def selection_dir(name) -> Path:
    return SELECTIONS_DIR / name


def load_selection(name) -> Selection:
    """Load and hash-check a committed selection."""
    directory = selection_dir(name)
    record_path = directory / 'selection.yml'
    if not record_path.exists():
        raise FileNotFoundError(f'No selection {name!r}: run scripts/select_stations.py first ({record_path})')
    record = yaml.safe_load(record_path.read_text())
    for filename, digest in record['files'].items():
        if sha256(directory / filename) != digest:
            raise ValueError(f'{directory / filename} was edited after selection; rerun scripts/select_stations.py')
    stations = read_csv(directory / 'stations.csv')
    sensors = read_csv(directory / 'sm_sensors.csv')
    excluded = read_csv(directory / 'excluded_stations.csv')
    ids = [s['station_id'] for s in stations]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError(f'{name}: empty or duplicate station selection')
    sensor_ids = [s['timeseries_id'] for s in sensors]
    if len(sensor_ids) != len(set(sensor_ids)) or not {s['station_id'] for s in sensors} <= set(ids):
        raise ValueError(f'{name}: duplicate sensors or sensors of unselected stations')
    return Selection(name, directory, stations, sensors, excluded, record)


def write_selection(name, stations, sensors, excluded, record, *, extra_files=()):
    """Write the CSVs and a selection.yml that records their hashes."""
    directory = selection_dir(name)
    write_csv(directory / 'stations.csv', stations, STATION_COLUMNS)
    write_csv(directory / 'sm_sensors.csv', sensors, SENSOR_COLUMNS)
    write_csv(directory / 'excluded_stations.csv', excluded, EXCLUDED_COLUMNS)
    files = ['stations.csv', 'sm_sensors.csv', 'excluded_stations.csv', *extra_files]
    record = {**record, 'files': {f: sha256(directory / f) for f in files}}
    header = ('# Written by scripts/select_stations.py (legacy selection: scripts/migrate_config.py).\n'
              '# Do not edit; change config/pipeline.yml `selection:` and rerun scripts/select_stations.py.\n')
    (directory / 'selection.yml').write_text(header + yaml.safe_dump(record, sort_keys=True))
    return load_selection(name)
