"""Station selection rules on a synthetic provider inventory (no downloads)."""
import json
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import box

from sm_drydown.config import load_config
from sm_drydown.selector import (NO_SERIES, NOT_ACTIVE, TOO_DEEP, compare, load_inventory, overlap_days,
                                 region_geometry, select_stations)


def series(tid, start, end, depth=0.05):
    return {'timeseries_id': tid, 'depth_from': depth, 'depth_to': depth, 'start_date': start, 'end_date': end}


INVENTORY = {'created_at': '2026-11-05T00:00:00', 'Networks': [
    {'networkID': 'A', 'Stations': [
        {'stationID': 1, 'station_name': 'inside', 'lat': 40.0, 'lng': -100.0, 'variableText': 'soil moisture<br>',
         'Timeseries': [series(11, '2001-01-01', '2002-07-04'), series(12, '2001-01-01', '2002-07-03')]},
        {'stationID': 2, 'station_name': 'edge', 'lat': 45.003, 'lng': -100.0, 'variableText': '',
         'Timeseries': [series(21, '2010-01-01', '2010-12-31')]},
        {'stationID': 3, 'station_name': 'outside', 'lat': 46.0, 'lng': -100.0, 'variableText': '',
         'Timeseries': [series(31, '2010-01-01', '2010-12-31')]},
        {'stationID': 4, 'station_name': 'old', 'lat': 41.0, 'lng': -101.0, 'variableText': 'x',
         'Timeseries': [series(41, '1990-01-01', '2000-01-01')]},
        {'stationID': 5, 'station_name': 'deep', 'lat': 41.0, 'lng': -102.0, 'variableText': 'x',
         'Timeseries': [series(51, '2010-01-01', '2011-01-01', depth=3.0)]},
        {'stationID': 6, 'station_name': 'empty', 'lat': 41.0, 'lng': -103.0, 'variableText': '', 'Timeseries': []}]},
    {'networkID': 'B', 'Stations': [
        {'stationID': 7, 'station_name': 'other network', 'lat': 41.0, 'lng': -104.0, 'variableText': '',
         'Timeseries': [series(71, '2010-01-01', '2011-01-01')]}]}]}


class SelectorTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / 'inventory.json'
        path.write_text(json.dumps(INVENTORY))
        self.inventory, created = load_inventory(path)
        self.assertEqual(created, '2026-11-05T00:00:00')
        self.cfg = load_config('baseline', overrides={'selection': {
            'region': {'kind': 'bbox', 'bbox': [-110.0, 35.0, -90.0, 45.0], 'buffer_deg': 0.005},
            'station_crs': 'EPSG:4326'}})

    def test_rules_keep_selected_sensors_and_explain_every_in_region_exclusion(self):
        stations, sensors, excluded = select_stations(self.cfg, self.inventory, region_geometry(self.cfg))
        self.assertEqual([s['station_id'] for s in stations], [1, 2, 7])   # 2 is inside only thanks to the buffer
        self.assertEqual([t['timeseries_id'] for t in sensors], [11, 21, 71])  # 12 ends the day before the window
        self.assertEqual({e['station_id']: e['reason'] for e in excluded}, {4: NOT_ACTIVE, 5: TOO_DEEP, 6: NO_SERIES})

    def test_network_rules_and_global_region(self):
        cfg = load_config('baseline', overrides={'selection': {'region': {'kind': 'global'},
                                                                'exclude_networks': ['B']}})
        stations, _, _ = select_stations(cfg, self.inventory, region_geometry(cfg))
        self.assertEqual([s['station_id'] for s in stations], [1, 2, 3])

    def test_overlap_counts_inclusive_days(self):
        self.assertEqual(overlap_days('2002-07-04', '2002-07-04', '2002-07-04', '2025-12-31'), 1)
        self.assertEqual(overlap_days('2002-07-01', '2002-07-03', '2002-07-04', '2025-12-31'), 0)
        self.assertEqual(overlap_days('', '2002-07-03', '2002-07-04', '2025-12-31'), 0)

    def test_compare_reports_added_and_removed(self):
        from types import SimpleNamespace
        new = SimpleNamespace(stations=[{'station_id': 1}, {'station_id': 2}], sensors=[{'timeseries_id': 9}])
        old = SimpleNamespace(stations=[{'station_id': 2}], sensors=[])
        self.assertEqual(compare(new, old), {'stations_added': [1], 'stations_removed': [], 'sensors_added': 1,
                                             'sensors_removed': 0, 'same_order': False})


if __name__ == '__main__':
    unittest.main()
