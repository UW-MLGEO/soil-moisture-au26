"""Write the committed synthetic smoke selection (data/selections/smoke/); deterministic.

    pixi run python tests/make_smoke_data.py

Two stations of a fictitious network SMOKE, three soil-moisture sensors and one
MODIS tile per year. Only the selection is committed; tests/smoke_fixture.py
builds the matching acquisition caches in a temporary folder at test time.
"""

from sm_drydown.selections import GRANULE_COLUMNS, selection_dir, write_csv, write_selection

STATIONS = [
    {'station_id': 1, 'network': 'SMOKE', 'station_name': 'Alpha Site', 'latitude': 40.0, 'longitude': -100.0,
     'available_variables': 'soil moisture<br>soil temperature<br>'},
    {'station_id': 2, 'network': 'SMOKE', 'station_name': 'Beta_Site', 'latitude': 30.5, 'longitude': -90.25,
     'available_variables': 'soil moisture<br>'},
]
SENSORS = [
    {'station_id': 1, 'timeseries_id': 11, 'depth_from': 0.05, 'depth_to': 0.05, 'start_date': '2024-01-01',
     'end_date': '2024-12-31'},
    {'station_id': 1, 'timeseries_id': 12, 'depth_from': 0.05, 'depth_to': 0.05, 'start_date': '2024-01-01',
     'end_date': '2024-12-31'},
    {'station_id': 2, 'timeseries_id': 21, 'depth_from': 0.1, 'depth_to': 0.1, 'start_date': '2024-01-01',
     'end_date': '2024-12-31'},
]
EXCLUDED = [{'station_id': 3, 'network': 'SMOKE', 'station_name': 'Gamma', 'available_variables': '',
             'reason': 'No soil-moisture time series in the provider inventory'}]
GRANULES = [{'year': 2024, 'tile': tile, 'filename': f'MCD12Q1.A2024001.{tile}.061.2025001000000.hdf',
             'url': f'https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/MCD12Q1.061/'
                    f'MCD12Q1.A2024001.{tile}.061.2025001000000/MCD12Q1.A2024001.{tile}.061.2025001000000.hdf',
             'size_estimate_bytes': 1000, 'cmr_granule_id': f'G-{tile}', 'cmr_collection_id': 'C-SMOKE',
             'cmr_updated': '2025-01-01T00:00:00Z'} for tile in ('h09v05', 'h10v05')]

if __name__ == '__main__':
    write_csv(selection_dir('smoke') / 'modis_land_cover_granules.csv', GRANULES, GRANULE_COLUMNS)
    record = {'ismn_collection_id': 'smoke_ismn', 'inventory_date': '2024-01-01',
              'selection_note': 'Synthetic smoke-test selection', 'spatial_rule': 'synthetic',
              'sources': {}, 'station_snapshot': {'collection_id': 'smoke_ismn', 'sha256': '0' * 64,
                                                  'source_file': 'tests/make_smoke_data.py'}}
    write_selection('smoke', STATIONS, SENSORS, EXCLUDED, record, extra_files=['modis_land_cover_granules.csv'])
    print('Wrote data/selections/smoke/')
