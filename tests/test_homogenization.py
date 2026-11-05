"""Offline daily-table checks: explicit gaps, separate sensors, source intervals and persisted provenance."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from botocore.exceptions import ClientError

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sm_drydown.homogenization.daily import (Sensor, align_daily, daily_index, decode_values,
    interval_daily, sensor_daily, station_daily)
from sm_drydown.homogenization.sources import read_station_sensors, ExtractCatalog
from sm_drydown.homogenization.workflow import run_homogenization, upload_station_bundles
from sm_drydown.storage.files import sha256, write_json, ObjectStore
from sm_drydown.storage.catalog import daily_manifest, publish_daily_manifest, upload_daily_files


def observations(values=(0.2, 0.4, 0.6), flags=('G', 'D', 'G')):
    return pd.DataFrame({'value': values, 'ismn_flag': flags, 'provider_flag': ['x', 'y', 'x']},
        index=pd.to_datetime(['2002-07-04T12:00Z', '2002-07-04T13:00Z', '2002-07-06T12:00Z']))


def policies():
    return {'temperature_quality': 'G_only', 'land_cover_scheme': 'LC_Type1',
            'land_cover_quality': {'mode': 'retain_flagged'},
            'lai_quality': {'mode': 'bit_rules', 'rules': [
                {'column': 'qc_FparLai_QC', 'offset': 0, 'mask': 1, 'allowed': [0]}]},
            'ismn_missing_codes': {'soil_moisture': [-9999], 'soil_temperature': [-9999]},
            'lai_band': 'Lai', 'land_cover_qc_bands': ['QC'], 'lai_qc_bands': ['FparLai_QC']}


class HomogenizationTests(unittest.TestCase):
    def test_reviewed_policy_accepts_main_algorithm_and_saturation_only(self):
        policy = json.loads(Path('config/daily_homogenization_policy.json').read_text())
        self.assertEqual(policy['land_cover_scheme'], 'LC_Type1')
        self.assertEqual(policy['land_cover_quality']['mode'], 'retain_flagged')
        dates = pd.date_range('2025-01-01', periods=6)
        source = pd.DataFrame({'start': dates, 'end': dates+pd.Timedelta(days=1),
            'value': [2.] * 6, 'qc_FparLai_QC': [0,32,64,96,128,np.nan]})
        result = interval_daily(source, dates, name='lai', policy=policy['lai_quality'])
        np.testing.assert_allclose(result.lai, [2.,2.,np.nan,np.nan,np.nan,np.nan], equal_nan=True)
        self.assertEqual(result.lai__qc_FparLai_QC.iloc[1], 32)

    def test_complete_daily_grid_g_mask_counts_flags_and_gap_without_filling(self):
        cases = [
            (('G', 'D', 'G'), .2, 1, 1, {'D': 1, 'G': 1}),
            (('G', 'G', 'G'), .3, 2, 0, {'G': 2}),
            (('D', 'D', 'G'), np.nan, 0, 2, {'D': 2}),
        ]
        for flags, mean, valid, masked, counts in cases:
            with self.subTest(flags=flags):
                sensor = Sensor('station/sm', 'soil_moisture', observations(flags=flags),
                                'm3/m3', [-9999], {'depth_to': 1.0})
                out, meta = sensor_daily(sensor, daily_index('2002-07-04', '2002-07-06'),
                                         temperature_quality='G_only')
                col = next(iter(meta))
                np.testing.assert_allclose(out[col], [mean, np.nan, .6], equal_nan=True)
                self.assertEqual(list(out[col + '__observation_count']), [2, 0, 1])
                self.assertEqual(list(out[col + '__valid_count']), [valid, 0, 1])
                self.assertEqual(out.iloc[1][col + '__missing_reason'], 'no_observations')
                self.assertEqual(meta[col]['values_masked_by_quality'], masked)
                self.assertEqual(json.loads(out.iloc[0][col + '__ismn_flag_counts']), counts)
                if not valid:
                    self.assertEqual(out.iloc[0][col + '__missing_reason'], 'quality_masked')

    def test_temperature_uses_own_flags_and_kelvin_to_celsius(self):
        frame = observations((273.15, 283.15, -9999), ('D', 'G', 'G'))
        sensor = Sensor('temp', 'soil_temperature', frame, 'K', [-9999], {})
        out, meta = sensor_daily(sensor, daily_index('2002-07-04', '2002-07-06'), temperature_quality='G_only')
        col = next(iter(meta))
        np.testing.assert_allclose(out[col], [10, np.nan, np.nan], equal_nan=True)
        self.assertEqual(out.iloc[2][col + '__missing_reason'], 'missing_values')

    def test_grouped_flag_counts_keep_missing_words_and_unsorted_observations(self):
        frame = observations((.2, .4, .6), ('G', None, 'G')).iloc[[2, 0, 1]].copy()
        frame['provider_flag'] = ['x', None, 'x']
        sensor = Sensor('flags', 'soil_moisture', frame, 'm3/m3', [], {})
        out, meta = sensor_daily(sensor, daily_index('2002-07-04', '2002-07-06'), temperature_quality='G_only')
        col = next(iter(meta))
        self.assertEqual(json.loads(out.iloc[0][col+'__ismn_flag_counts']), {'<missing>': 1, 'G': 1})
        self.assertEqual(json.loads(out.iloc[0][col+'__provider_flag_counts']), {'<missing>': 1, 'x': 1})
        self.assertTrue(pd.isna(out.iloc[1][col+'__provider_flag_counts']))
        np.testing.assert_allclose(out[col], [.2, np.nan, .6], equal_nan=True)

    def test_duplicate_timestamp_rejected_even_when_bad_flag(self):
        frame = observations()
        frame.index = pd.DatetimeIndex([frame.index[0], frame.index[0], frame.index[2]])
        with self.assertRaises(ValueError):
            sensor_daily(Sensor('x', 'soil_moisture', frame, 'm3/m3', [], {}),
                         daily_index('2002-07-04', '2002-07-06'), temperature_quality='G_only')

    def test_source_missing_is_not_zero_rainfall_and_missing_dates_remain_visible(self):
        dates = daily_index('2002-07-04', '2002-07-06')
        out = align_daily(pd.Series([0, np.nan], index=dates[:2]), dates, name='p')
        np.testing.assert_allclose(out.p, [0, np.nan, np.nan], equal_nan=True)
        self.assertEqual(list(out.p__missing_reason), ['available', 'missing_value', 'no_source_record'])

    def test_chirps_adapter_preserves_null_absent_and_missing_dates_with_portable_source_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {'dataset': 'chirps', 'product': 'CHIRPS v2.0 daily precipitation',
                'collection': 'UCSB-CHG/CHIRPS/DAILY', 'collection_id': 'chirps_test',
                'start': '2002-07-04', 'end': '2002-07-07', 'assets': [{'asset_id': '2002'}],
                'source_urls': ['https://example.org/provider', 'https://doi.org/example'],
                'license': 'declared fixture license', 'storage': {'destination': 's3://gaia/scratch/project/'}}
            config_path = root / 'selection.json'
            write_json(config_path, config)
            extract = root / 'chirps_test/extracts/2002.npz'
            extract.parent.mkdir(parents=True)
            np.savez_compressed(extract, station_ids=[1],
                dates=np.array(['2002-07-04', '2002-07-05', '2002-07-06'], dtype='datetime64[ns]'),
                values=[[0], [np.nan], [np.nan]], null_mask=[[False], [True], [False]],
                absent_row_mask=[[False], [False], [True]])
            write_json(extract.with_suffix('.json'), {'sha256': sha256(extract),
                'source_attributes': {'units': 'mm/day'}, 'projection': {'crs': 'EPSG:4326'},
                'created_at': '2002-07-08T00:00:00Z'})
            catalog = ExtractCatalog(config_path, root)
            source = catalog.chirps(1)
            result = align_daily(source.value, daily_index('2002-07-04', '2002-07-07'), name='p',
                source_present=source.source_present, source_null=source.provider_null,
                source_absent=source.provider_absent_row)
            self.assertEqual(list(result.p__missing_reason),
                ['available', 'provider_null', 'provider_absent_row', 'no_source_record'])
            np.testing.assert_allclose(result.p, [0, np.nan, np.nan, np.nan], equal_nan=True)
            self.assertTrue(result.p__provider_null.iloc[1])
            self.assertTrue(result.p__provider_absent_row.iloc[2])
            self.assertTrue(pd.isna(result.p__provider_null.iloc[3]))
            document = catalog.provenance['source_description']
            self.assertEqual(document['license'], 'declared fixture license')
            self.assertEqual(document['citation'], ['https://doi.org/example'])
            self.assertEqual(document['provider_collection'], config['collection'])
            self.assertTrue(document['configuration_reference']['object_uri'].startswith('s3://gaia/scratch/project/chirps/chirps_test/config/'))
            metadata = catalog.provenance['assets'][0]
            self.assertEqual(metadata['extraction_metadata']['projection']['crs'], 'EPSG:4326')
            self.assertEqual(metadata['metadata_reference']['object_uri'],
                             's3://gaia/scratch/project/chirps/chirps_test/extracts/2002.json')

    def test_native_four_day_interval_never_carries_across_absent_or_rejected_composite(self):
        dates = daily_index('2002-07-01', '2002-07-12')
        frame = pd.DataFrame({'start': ['2002-07-01', '2002-07-09'],
            'end': ['2002-07-05', '2002-07-13'], 'value': [1.2, 4.2], 'qc_FparLai_QC': [0, 1]})
        out = interval_daily(frame, dates, name='lai', policy=policies()['lai_quality'])
        np.testing.assert_allclose(out.lai.iloc[:4], 1.2)
        self.assertTrue(out.lai.iloc[4:].isna().all())
        self.assertEqual(out.iloc[4].lai__missing_reason, 'no_source_interval')
        self.assertEqual(out.iloc[8].lai__missing_reason, 'quality_masked')
        self.assertEqual(out.iloc[8].lai__qc_FparLai_QC, 1)

    def test_missing_qc_is_not_defaulted_to_good_and_intervals_cannot_overlap(self):
        dates = daily_index('2002-07-01', '2002-07-04')
        frame = pd.DataFrame({'start': ['2002-07-01'], 'end': ['2002-07-05'],
                              'value': [1.2], 'qc_FparLai_QC': [np.nan]})
        out = interval_daily(frame, dates, name='lai', policy=policies()['lai_quality'])
        self.assertTrue(out.lai.isna().all())
        self.assertEqual(out.iloc[0].lai__missing_reason, 'missing_qc')
        with self.assertRaises(ValueError):
            interval_daily(pd.concat([frame, frame]), dates, name='lai', policy=policies()['lai_quality'])

    def test_all_missing_modis_interval_and_empty_collection_keep_nullable_qc(self):
        dates = daily_index('2002-07-01', '2002-07-05').as_unit('us')
        source = pd.DataFrame({'start': np.array(['2002-07-01'], dtype='datetime64[ns]'),
            'end': np.array(['2002-07-05'], dtype='datetime64[ns]'),
            'value': [np.nan], 'qc_FparLai_QC': [np.nan], 'qc_extra': [np.nan]})
        out = interval_daily(source, dates, name='lai', policy=policies()['lai_quality'])
        self.assertTrue(out.lai.isna().all())
        self.assertTrue(out.lai__qc_FparLai_QC.isna().all())
        self.assertEqual(str(out.lai__qc_FparLai_QC.dtype), 'Int64')
        self.assertEqual(out.lai__missing_reason.tolist(), ['missing_value']*4+['no_source_interval'])
        empty = interval_daily(source.iloc[:0], dates, name='lai', policy=policies()['lai_quality'])
        self.assertTrue(empty.lai.isna().all())
        self.assertTrue(empty.lai__missing_reason.eq('no_source_interval').all())

    def test_source_fill_mask_before_scale_and_offset(self):
        values, count = decode_values([10, 255, np.nan], missing_codes=[255], scale_factor=0.1, add_offset=1)
        np.testing.assert_allclose(values, [2, np.nan, np.nan], equal_nan=True)
        self.assertEqual(count, 2)

    def test_annual_category_stops_at_year_boundary_and_never_becomes_fractional(self):
        frame = pd.DataFrame({'start': ['2002-01-01'], 'end': ['2003-01-01'], 'value': [8]})
        out = interval_daily(frame, daily_index('2002-12-31', '2003-01-01'),
                             name='lc', policy={'mode': 'retain_flagged'}, categorical=True)
        self.assertEqual(str(out.lc.dtype), 'Int64')
        self.assertEqual(out.lc.iloc[0], 8)
        self.assertTrue(pd.isna(out.lc.iloc[1]))

    def test_native_reader_keeps_replicates_deep_sensors_and_temperature_ignores_rain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.zip'
            with zipfile.ZipFile(path, 'w') as z:
                for var in ('sm', 'ts', 'p'):
                    for replicate in (1, 2):
                        name = f'TEST/Station/a_{var}_1.0_1.0_sensor{replicate}_20020704_20020706.stm'
                        z.writestr(name, 'camp TEST Station 40 -120 100 1.0 1.0 sensor\n2002/07/04 12:00 0.2 G P\n2002/07/05 12:00 NaN G P\n')
            sensors = read_station_sensors(path, {'network': 'TEST', 'station_name': 'Station'},
                missing_codes=policies()['ismn_missing_codes'])
            self.assertEqual(len(sensors), 4)
            self.assertEqual(len({s.identity for s in sensors}), 4)
            self.assertTrue(all(s.metadata['depth_to'] == 1 for s in sensors))
            self.assertTrue(all(pd.isna(s.observations.value.iloc[1]) for s in sensors))

    def test_end_to_end_cache_adapters_parquet_missingness_and_verified_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            station = {'station_id': 1, 'network': 'TEST', 'station_name': 'Station', 'latitude': 40, 'longitude': -120}
            selection = {'collection_id': 'ismn', 'start': '2002-07-04', 'end': '2002-07-06', 'stations': [station]}
            selection_path = root / 'ismn.json'
            write_json(selection_path, selection)
            policy_path = root / 'policy.json'
            write_json(policy_path, policies())
            archive = root / 'ismn/archives/TEST.zip'
            archive.parent.mkdir(parents=True)
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('TEST/Station/a_sm_0.05_0.05_sensor_20020704_20020706.stm',
                    'camp TEST Station 40 -120 100 0.05 0.05 sensor\n2002/07/04 12:00 0.2 G P\n')
            write_json(archive.with_suffix('.zip.provenance.json'), {'sha256': sha256(archive)})
            configs = {}

            def asset(config, aid, arrays, attrs, field=None):
                entry = {'asset_id': aid}
                if field:
                    entry['field'] = field
                config['assets'].append(entry)
                path = root / config['collection_id'] / 'extracts' / (aid + '.npz')
                path.parent.mkdir(parents=True, exist_ok=True)
                np.savez_compressed(path, station_ids=[1], native_latitudes=[40], native_longitudes=[-120], **arrays)
                write_json(path.with_suffix('.json'), {'sha256': sha256(path), 'source_attributes': attrs})

            for dataset in ('chirps', 'gldas', 'land_cover', 'lai'):
                config = {**selection, 'collection_id': dataset, 'assets': []}
                dates = np.array(['2002-07-04', '2002-07-05', '2002-07-06'], dtype='datetime64[ns]')
                if dataset == 'chirps':
                    asset(config, 'year', {'dates': dates, 'values': [[0], [np.nan], [2]],
                        'null_mask': [[False], [True], [False]], 'absent_row_mask': [[False], [False], [False]]}, {})
                elif dataset == 'gldas':
                    for field in ('soil_texture', 'sand_fraction', 'silt_fraction', 'clay_fraction', 'porosity'):
                        asset(config, field, {'dates': dates[:1], 'values': [[np.nan if field == 'porosity' else 40 if field != 'soil_texture' else 2.9999998]]},
                              {'units': 'category' if field == 'soil_texture' else '%'}, field)
                else:
                    bands = ['LC_Type1', 'QC'] if dataset == 'land_cover' else ['Lai', 'FparLai_QC']
                    asset(config, 'year', {'dates': dates[:1], 'interval_start': dates[:1],
                        'interval_end': np.array(['2002-07-06'], dtype='datetime64[ns]'),
                        'bands': bands, 'values': [[[8 if dataset == 'land_cover' else 20, 0]]],
                        'null_mask': [[[False, False]]], 'absent_row_mask': [[False]]},
                        {b: {'scale_factor': 0.1 if b == 'Lai' else 1} for b in bands})
                path = root / (dataset + '.json')
                write_json(path, config)
                configs[dataset] = path
            out = root / 'output'
            first = run_homogenization(selection_path, configs, policy_path, root, root, out)
            second = run_homogenization(selection_path, configs, policy_path, root, root, out)
            self.assertEqual(first, second)
            bundle = Path(first['bundles'][0])
            frame = pq.read_table(bundle / 'daily.parquet').to_pandas()
            self.assertEqual(len(frame), 3)
            self.assertTrue(frame.gldas_porosity.isna().all())
            # Float32 noise in the GLDAS texture source becomes an integer class.
            self.assertEqual(str(frame.gldas_soil_texture_class.dtype), 'Int64')
            self.assertTrue((frame.gldas_soil_texture_class == 3).all())
            self.assertTrue(pd.isna(frame.chirps_precip_mm_day.iloc[1]))
            np.testing.assert_allclose(frame.modis_lai_m2_m2, [2, 2, np.nan], equal_nan=True)
            description = json.loads((bundle / 'description.json').read_text())
            self.assertEqual(description['values_filled'], 0)
            self.assertEqual(description['schema_version'], 1)
            self.assertTrue(description['gldas_decoding']['soil_texture_class']['float_noise_removed'])

            class MemoryS3:
                def __init__(self):
                    self.objects = {}
                    self.uploads = 0

                def head_object(self, *, Bucket, Key):
                    if Key not in self.objects:
                        raise ClientError({'Error': {'Code': '404'}}, 'HeadObject')
                    payload, metadata = self.objects[Key]
                    return {'ContentLength': len(payload), 'Metadata': metadata}

                def upload_file(self, path, bucket, key, *, ExtraArgs):
                    self.objects[key] = (Path(path).read_bytes(), ExtraArgs['Metadata'])
                    self.uploads += 1

            store = ObjectStore.__new__(ObjectStore)
            store.bucket, store.prefix, store.client = 'gaia', 'ml4geo_sm_drydown', MemoryS3()
            uris = upload_station_bundles(out, store)
            self.assertEqual(len(uris), 2)
            self.assertEqual(uris, upload_station_bundles(out, store))
            self.assertEqual(store.client.uploads, 2)
            self.assertTrue(all('/ml4geo_sm_drydown/daily/TEST/1/' in uri for uri in uris))
            manifest = daily_manifest(out, selection_path)
            self.assertEqual((manifest['stations'], manifest['rows']), (1, 3))
            self.assertFalse(manifest['published_label_sources_processed'])
            self.assertEqual(len(manifest['bundles'][0]['files']), 2)
            self.assertEqual(uris, upload_daily_files(out, manifest, store, workers=2))
            manifest_uri = publish_daily_manifest(out, manifest, store)
            self.assertEqual(manifest_uri, publish_daily_manifest(out, manifest, store))
            self.assertIn('/daily/manifests/', manifest_uri)
            with self.assertRaisesRegex(ValueError, 'Incomplete'):
                daily_manifest(root / 'missing-outputs', selection_path)
            remote_key = next(iter(store.client.objects))
            payload, metadata = store.client.objects[remote_key]
            store.client.objects[remote_key] = (payload, {'sha256': 'wrong'})
            with self.assertRaises(ValueError):
                upload_station_bundles(out, store)
            store.prefix = 'scratch/ml4geo_sm_drydown'
            with self.assertRaises(ValueError):
                upload_station_bundles(out, store)
            (bundle / 'daily.parquet').write_bytes(b'corrupt')
            store.prefix = 'ml4geo_sm_drydown'
            with self.assertRaises(ValueError):
                upload_station_bundles(out, store)
            with self.assertRaises(ValueError):
                run_homogenization(selection_path, configs, policy_path, root, root, out)


class SpatialPartitionTests(unittest.TestCase):
    def test_frozen_regions_cover_selection_once_and_share_no_native_soil_cells(self):
        config = json.loads(Path('config/spatial_partitions.json').read_text())
        source = Path(config['source_selection']['path'])
        self.assertEqual(config['source_selection']['sha256'], sha256(source))
        stations = {s['station_id']: s for s in json.loads(source.read_text())['stations']}
        self.assertEqual(config['grouping_key'], 'station_id')
        self.assertEqual(config['station_count'], len(stations))
        expected = {'eval_south': 277, 'fold_1_northwest': 275, 'fold_2_southwest': 280,
                    'fold_3_northeast': 283, 'fold_4_southeast': 273}
        assigned, cells = set(), {}
        for name, region in config['regions'].items():
            with self.subTest(region=name):
                ids = region['station_ids']
                self.assertEqual(len(ids), len(set(ids)))
                self.assertEqual(len(ids), expected[name])
                self.assertEqual(region['station_count'], len(ids))
                self.assertFalse(assigned.intersection(ids))
                assigned.update(ids)
                for sid in ids:
                    station = stations[sid]
                    for axis in ('latitude', 'longitude'):
                        value = station[axis]
                        if axis+'_min_inclusive' in region:
                            self.assertGreaterEqual(value, region[axis+'_min_inclusive'])
                        if axis+'_max_exclusive' in region:
                            self.assertLess(value, region[axis+'_max_exclusive'])
                    # Native 0.25-degree GLDAS cells have edges at quarter degrees.
                    cell = tuple(int(np.floor(station[axis]*4)) for axis in ('latitude', 'longitude'))
                    self.assertEqual(cells.setdefault(cell, name), name)
        self.assertEqual(assigned, set(stations))
        cv = config['cross_validation']
        self.assertEqual(cv['evaluation_region'], 'eval_south')
        self.assertEqual(set(cv['development_regions']), set(expected)-{'eval_south'})
        self.assertEqual({config['regions'][r]['fold'] for r in cv['development_regions']}, {1,2,3,4})


if __name__ == '__main__':
    unittest.main()
