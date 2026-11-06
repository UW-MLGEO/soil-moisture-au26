"""CHIRPS and MODIS LAI native-cell extraction, explicit gaps and cache repair.

Run from the repository root: pixi run test
All fixtures are synthetic; no credentials, network or existing datasets.
"""

import copy
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

import numpy as np

from fixtures import MemoryStore

from sm_drydown.providers import chirps, modis
from sm_drydown.features.gridded import ExtractCatalog, chirps_source
from sm_drydown.storage.files import sha256, write_json


CHIRPS_PROJECTION = {'crs': 'EPSG:4326', 'transform': [1, 0, -180, 0, -1, 50]}


HEADER = ['id', 'longitude', 'latitude', 'time', 'precipitation']


def timestamp(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)


def chirps_description():
    return {'type': 'ImageCollection', 'features': [
        {'id': f'UCSB-CHG/CHIRPS/DAILY/{day}',
         'bands': [{'id': 'precipitation', 'crs': 'EPSG:4326',
                    'crs_transform': CHIRPS_PROJECTION['transform']}],
         'properties': {'system:index': day, 'system:time_start': timestamp(day)}}
        for day in ('2025-01-01', '2025-01-03')]}


class FakeClient:
    project = 'offline-test'

    def __init__(self):
        self.descriptions = 0
        self.requests = 0

    def describe(self, config, asset):
        self.descriptions += 1
        return chirps_description()

    def retrieve(self, config, asset, images, cells, projection):
        self.requests += 1
        rows = [HEADER]
        for image in images:
            for cell in cells:
                if image['date'] == '2025-01-03' and 3 in cell['station_ids']:
                    continue  # Absent service row stays distinct from JSON null.
                value = None if image['date'] == '2025-01-03' else 0
                rows.append([image['index'], cell['longitude'], cell['latitude'], image['timestamp'], value])
        return rows


class CHIRPSTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'config.json'
        source = {'start': '2025-01-01', 'end': '2025-01-03', 'collection_id': 'ismn_snapshot',
                  'storage': {'destination': 's3://test/scratch/', 'profile': 'test', 'endpoint': 'https://test'},
                  'stations': [{'station_id': sid, 'network': 'TEST', 'station_name': str(sid),
                                'latitude': 40.2, 'longitude': lon}
                               for sid, lon in [(1, -100.2), (2, -100.3), (3, -102.2)]]}
        self.config = {**source, 'schema_version': 1, 'dataset': 'chirps',
                       'collection_id': 'chirps_fixture', 'strategy': 'earth_engine_getRegion',
                       'collection': 'UCSB-CHG/CHIRPS/DAILY', 'band': 'precipitation',
                       'sampling': 'native_cell', 'units': 'mm/day',
                       'source_urls': ['https://example.test/chirps'],
                       'batching': {'maximum_values': 20, 'provider_value_limit': 1048576,
                                    'columns_per_row': 5, 'concurrent_requests': 1},
                       'assets': [{'asset_id': '2025', 'filename': '2025.json',
                                   'url': 'https://example.test/chirps',
                                   'start': source['start'], 'end': source['end']}]}
        write_json(self.path, self.config)
        self.cache = self.root / 'cache'
        self.client, self.store = FakeClient(), MemoryStore()

    def fetch(self, approved=None):
        return chirps.fetch_and_stage(self.path, self.cache, self.client, self.store,
                               approved if approved is not None else sha256(self.path))

    def test_independent_config_and_offline_plan(self):
        self.assertEqual(chirps.load_config(self.path), self.config)
        self.assertEqual(chirps.make_plan(self.path)['station_count'], 3)
        self.assertEqual(self.client.descriptions, 0)
        invalid = deepcopy(self.config)
        invalid['assets'][0]['start'] = '2025-01-02'
        write_json(self.path, invalid)
        with self.assertRaisesRegex(ValueError, 'without gaps'):
            chirps.load_config(self.path)

    def test_approval_before_any_remote_work(self):
        with self.assertRaisesRegex(ValueError, 'approval'):
            self.fetch('incorrect')
        self.assertEqual(self.client.descriptions, 0)
        self.assertEqual(self.store.hashes, {})

    def test_native_duplicates_and_bounds(self):
        cells = chirps.native_cells(self.config['stations'], CHIRPS_PROJECTION)
        self.assertEqual(len(cells), 2)
        self.assertEqual(sorted(len(c['station_ids']) for c in cells), [1, 2])
        many = cells * 700
        for count in (365, 366, 8582):
            batches = chirps.batch_cells(many, count, 900000)
            self.assertEqual(sum(map(len, batches)), 1400)
            self.assertTrue(all(5 + len(batch) * count * 5 <= 900000 for batch in batches))
        with self.assertRaisesRegex(ValueError, 'Single-cell'):
            chirps.batch_cells(cells, 100, 20)
        with self.assertRaisesRegex(ValueError, 'Unsupported source grid'):
            chirps.native_cells(self.config['stations'], {**CHIRPS_PROJECTION, 'crs': 'EPSG:3857'})

    def test_source_projection_duplicates_and_outside_dates(self):
        asset = self.config['assets'][0]
        for mode in ('projection', 'duplicate', 'outside'):
            response = chirps_description()
            if mode == 'projection':
                response['features'][1]['bands'][0]['crs'] = 'EPSG:3857'
            elif mode == 'duplicate':
                response['features'].append(response['features'][0])
            else:
                response['features'][0]['properties']['system:time_start'] = timestamp('2024-12-31')
            with self.assertRaises(ValueError):
                chirps.source_images(response, asset)

    def test_null_zero_missing_day_and_absent_row(self):
        self.fetch()
        catalog = ExtractCatalog(self.path,self.cache)
        series = chirps_source(catalog, 1)
        self.assertEqual(len(series),2)
        self.assertEqual(series.value.iloc[0],0)
        self.assertTrue(np.isnan(series.value.iloc[1]))
        self.assertEqual(series.provider_null.sum(),1)
        self.assertEqual(catalog.assets[0][2]['missing_source_dates'],['2025-01-02'])
        self.assertEqual(chirps_source(catalog, 3).provider_absent_row.sum(),1)
        second = chirps_source(catalog, 2)
        np.testing.assert_array_equal(series.value.to_numpy(),second.value.to_numpy())
        run = self.cache / self.config['collection_id']
        raw = [json.loads(path.read_text()) for path in (run / 'raw/2025').glob('batch_*.json')
               if not path.name.endswith('.provenance.json')]
        self.assertTrue(any(row[-1] is None for table in raw for row in table[1:]))

    def test_resume_immutable_metadata_and_cache_repair(self):
        self.fetch()
        before = dict(self.store.hashes)
        self.fetch()
        self.assertEqual(self.client.descriptions, 1)
        self.assertEqual(self.client.requests, 2)
        self.assertEqual(before, self.store.hashes)
        raw = self.cache / self.config['collection_id'] / 'raw/2025/batch_0000.json'
        raw.write_text('damaged')
        self.fetch()
        self.assertEqual(self.client.requests, 3)
        self.assertEqual(before, self.store.hashes)
        extract = self.cache / self.config['collection_id'] / 'extracts/2025.npz'
        extract.write_bytes(b'damaged')
        self.fetch()
        self.assertEqual(self.client.requests, 3)
        self.assertEqual(before, self.store.hashes)

    def test_table_rejects_duplicate_unexpected_and_shifted_pixel(self):
        images, projection = chirps.source_images(chirps_description(), self.config['assets'][0])
        cells = chirps.native_cells(self.config['stations'], projection)
        table = self.client.retrieve(self.config, self.config['assets'][0], images, cells, projection)
        for mode in ('duplicate', 'outside', 'shift', 'nonfinite'):
            response = deepcopy(table)
            if mode == 'duplicate':
                response.append(response[1])
            elif mode == 'outside':
                response[1][0] = 'not-source-image'
            elif mode == 'shift':
                response[1][1] += .1
            else:
                response[1][4] = float('nan')
            with self.assertRaises(ValueError):
                chirps.validate_table(response, images, cells, projection, 100)

    def test_empty_year_is_explicitly_unavailable(self):
        self.client.describe = lambda *_: {'features': []}
        self.fetch()
        catalog = ExtractCatalog(self.path,self.cache)
        series = chirps_source(catalog, 1)
        self.assertTrue(series.empty)
        self.assertEqual(catalog.assets[0][2]['missing_source_dates'],
                         ['2025-01-01', '2025-01-02', '2025-01-03'])


MODIS_PROJECTION = {'crs': 'SR-ORG:6974', 'transform': [463.3127165279165, 0, -20015109.354,
                                                0, -463.3127165279167, 7783653.637669001]}


def lai_config():
    return {'schema_version': 1, 'dataset': 'modis_lai', 'collection': 'MODIS/061/MCD15A3H',
            'collection_id': 'test', 'start': '2025-01-01', 'end': '2025-01-08', 'cadence': 4,
            'strategy': 'earth_engine_getRegion', 'sampling': 'native_cell', 'maximum_values': 10000,
            'assets': [{'asset_id': '2025'}], 'storage': {}, 'bands': ['Lai', 'FparLai_QC'],
            'source_attributes': {'Lai': {'scale_factor': .1, 'units': 'm2/m2', '_FillValue': 255}},
            'stations': [{'station_id': 1, 'latitude': 45., 'longitude': -120.},
                         {'station_id': 2, 'latitude': 45., 'longitude': -120.}]}


def lai_metadata(c):
    return {'features': [{'id': 'MODIS/061/MCD15A3H/2025_01_01',
        'properties': {'system:index': '2025_01_01', 'system:time_start': 1735689600000},
        'bands': [{'id': b, 'crs': MODIS_PROJECTION['crs'], 'crs_transform': MODIS_PROJECTION['transform']}
                  for b in c['bands']]}]}


class LAITests(unittest.TestCase):
    def test_configs_and_explicit_approval(self):
        from sm_drydown.providers import lpdaac
        for loader, path in [(modis.load_config, 'locks/modis_lai.json'),
                             (lpdaac.load_config, 'locks/modis_land_cover.json')]:
            c = loader(path)
            self.assertEqual(len(c['stations']), 1388)
            self.assertEqual(len(c['assets']), 24)
            with self.assertRaisesRegex(ValueError, 'approval'):
                (modis.require_approval if loader is modis.load_config else lpdaac.require_approval)(path, 'not-approved')
        # The Earth Engine loader has no land-cover product.
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            modis.load_config('locks/modis_land_cover.json')

    def test_composite_year_end_intervals(self):
        self.assertEqual(modis.interval_end('2024-12-30', 4), '2025-01-01')
        self.assertEqual(modis.interval_end('2025-01-01', 4), '2025-01-05')

    def test_native_cell_matches_verified_earth_engine_probe_and_deduplicates(self):
        cells = modis.native_cells(lai_config()['stations'], MODIS_PROJECTION)
        self.assertEqual(len(cells), 1)
        c = cells[0]
        self.assertEqual(c['station_ids'], [1, 2])
        self.assertAlmostEqual(c['longitude'], -120.00333054991994, places=7)
        self.assertAlmostEqual(c['latitude'], 45.0020833293055, places=7)
        self.assertEqual(modis.pixel(c['longitude'], c['latitude'], MODIS_PROJECTION), (c['iy'], c['ix']))

    def test_source_grids_must_align_and_duplicate_dates_rejected(self):
        c = lai_config(); response = lai_metadata(c)
        images, _ = modis.source_images(response, c)
        self.assertEqual(images[0]['interval_end'], '2025-01-05')
        bad = copy.deepcopy(response); bad['features'] += copy.deepcopy(bad['features'])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            modis.source_images(bad, c)
        bad = copy.deepcopy(response)
        bad['features'][0]['bands'][1]['crs_transform'] = list(MODIS_PROJECTION['transform'])
        bad['features'][0]['bands'][1]['crs_transform'][2] += 100
        with self.assertRaisesRegex(ValueError, 'align'):
            modis.source_images(bad, c)

    def test_values_nulls_absent_rows_and_encoded_qc_are_preserved(self):
        c = lai_config(); images, projection = modis.source_images(lai_metadata(c), c)
        cells = modis.native_cells(c['stations'], projection); cell = cells[0]
        header = ['id', 'longitude', 'latitude', 'time']+c['bands']
        row = [images[0]['index'], cell['longitude'], cell['latitude'], images[0]['timestamp'], None, 255]
        table = [header, row]
        modis.validate_table(table, images, cells, projection, c['bands'], 10000)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'test.npz'
            report = modis.extract_tables(c, images, projection, cells, [table], ['hash'], path, 'config-hash')
            with np.load(path) as raw:
                self.assertEqual(raw['values'].shape, (1, 2, 2))
                self.assertTrue(np.isnan(raw['values'][0, :, 0]).all())
                np.testing.assert_array_equal(raw['values'][0, :, 1], [255,255])
                self.assertTrue(raw['null_mask'][0, :, 0].all())
                self.assertFalse(raw['absent_row_mask'].any())
            self.assertEqual(modis.extract_tables(c, images, projection, cells, [table], ['hash'], path, 'config-hash'), report)
            absent = Path(folder)/'absent.npz'
            modis.extract_tables(c, images, projection, cells, [[header]], ['other'], absent, 'config-hash')
            with np.load(absent) as raw:
                self.assertTrue(raw['absent_row_mask'].all())
                self.assertFalse(raw['null_mask'].any())

    def test_response_rejects_unexpected_columns_duplicate_rows_and_wrong_cell(self):
        c = lai_config(); images, projection = modis.source_images(lai_metadata(c), c)
        cells = modis.native_cells(c['stations'], projection); cell = cells[0]
        row = [images[0]['index'], cell['longitude'], cell['latitude'], images[0]['timestamp'], 20,0]
        header = ['id', 'longitude', 'latitude', 'time']+c['bands']
        for table in [[header,row,row], [header, [row[0], -90., 35., *row[3:]]], [['wrong']]]:
            with self.assertRaises(ValueError):
                modis.validate_table(table, images, cells, projection, c['bands'], 10000)

    def test_fetch_reuses_cache_and_never_qc_masks_or_scales(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); c = lai_config(); path = root/'selection.json'
            path.write_text(json.dumps(c))
            from sm_drydown.storage.files import sha256
            client = Mock(project='test-project'); client.describe.return_value = lai_metadata(c)
            images, projection = modis.source_images(lai_metadata(c), c); cell = modis.native_cells(c['stations'], projection)[0]
            client.retrieve.return_value = [['id', 'longitude', 'latitude', 'time']+c['bands'],
                [images[0]['index'], cell['longitude'], cell['latitude'], images[0]['timestamp'], 20,255]]
            store = Mock();store.upload.return_value = 's3://test/source'
            first = modis.fetch_and_stage(path, root, client, store, sha256(path))
            second = modis.fetch_and_stage(path, root, client, store, sha256(path))
            self.assertEqual(first, second)
            self.assertEqual(client.describe.call_count, 1)
            self.assertEqual(client.retrieve.call_count, 1)
            with np.load(root/'test/extracts/2025.npz') as raw:
                np.testing.assert_array_equal(raw['values'][0,0], [20,255])


if __name__ == '__main__':
    unittest.main()
