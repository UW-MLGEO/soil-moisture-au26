"""Offline diagnostics: exact daily runs, missing/partial seasons and unmodified outliers."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sm_drydown.inspection.checks import (flatlines, precipitation_extremes,
    seasonal_precipitation, gldas_checks, Samples)
from sm_drydown.inspection.workflow import inspect_data
from sm_drydown.storage.files import sha256, write_json


class InspectionChecks(unittest.TestCase):
    def test_flatlines_exact_three_days_and_missing_or_calendar_gaps_break_runs(self):
        dates = pd.date_range('2024-01-01', periods=12)
        values = [0, 0, 0, np.nan, .2, .2, .2+1e-12, .4, .4, .4, .4, np.nan]
        runs = flatlines(values, dates)
        self.assertEqual([r['consecutive_days'] for r in runs], [3, 4])
        self.assertEqual([r['value'] for r in runs], [0, .4])
        self.assertEqual(flatlines([.1, .1, .1], pd.to_datetime(['2024-01-01','2024-01-02','2024-01-04'])), [])
        self.assertEqual(flatlines([.1, .1], dates[:2]), [])
        self.assertEqual(flatlines([np.nan]*3, dates[:3]), [])
        with self.assertRaises(ValueError):
            flatlines([1,1], [dates[0],dates[0]])
        with self.assertRaises(ValueError):
            flatlines([1,1], dates[:2][::-1])

    def test_precip_positive_only_strict_threshold_missing_and_all_zero(self):
        flags, threshold, count = precipitation_extremes([-1,0,10,100,np.nan,np.inf])
        self.assertEqual(count, 2)
        self.assertAlmostEqual(threshold, 99.91)
        self.assertEqual(flags.tolist(), [False,False,False,True,False,False])
        self.assertFalse(precipitation_extremes([10,10])[0].any())
        flags, threshold, count = precipitation_extremes([0,np.nan,-1])
        self.assertEqual(count, 0)
        self.assertTrue(np.isnan(threshold))
        self.assertFalse(flags.any())

    def test_seasons_leap_djf_edge_partial_and_missing_sum_not_zero(self):
        dates = pd.date_range('2023-12-01','2024-02-29')
        result = seasonal_precipitation(np.ones(len(dates)), dates).iloc[0]
        self.assertEqual(result.season_year, 2024)
        self.assertEqual(result.calendar_days, 91)
        self.assertEqual(result.complete_season_total_mm, 91)
        self.assertEqual(result.coverage_fraction, 1)
        result = seasonal_precipitation([1,-2,np.nan], dates[:3]).iloc[0]
        self.assertEqual(result.observed_sum_mm, -1)
        self.assertEqual(result.negative_days, 1)
        self.assertEqual(result.missing_days_in_selection, 1)
        self.assertAlmostEqual(result.coverage_fraction, 2/91)
        self.assertFalse(result.complete_season)
        self.assertTrue(np.isnan(result.complete_season_total_mm))
        result = seasonal_precipitation([np.nan,np.nan],dates[:2]).iloc[0]
        self.assertTrue(np.isnan(result.observed_sum_mm))

    def test_gldas_bounds_closure_float_boundary_texture_and_missing(self):
        source = pd.DataFrame({'sand_fraction':[.5,.5,.5,.5,np.nan,-.1],
            'silt_fraction':[.3,.3,.3,.3,.3,.3], 'clay_fraction':[.2,.21,.19,.22,.2,.8],
            'porosity':[0,1,1.1,np.nan,.5,np.inf], 'soil_texture_class':[3.0000005,16,0,np.nan,3.1,17]})
        before = source.copy(deep=True)
        result = gldas_checks(source)
        self.assertEqual(result.closure_outside_tolerance.tolist(), [False,False,False,True,False,False])
        self.assertEqual(result.closure_evaluable.tolist(), [True,True,True,True,False,True])
        self.assertEqual(result.any_physical_range_failure.tolist(), [False,False,True,False,True,True])
        pd.testing.assert_frame_equal(before, source)
        pd.testing.assert_frame_equal(result[source.columns],source)

    def test_distribution_empty_missing_and_categorical_without_ordinal_mean(self):
        samples = Samples()
        samples.add([np.nan,np.inf])
        result = samples.summary()
        self.assertEqual(result['missing'],1)
        self.assertEqual(result['nonfinite_nonmissing'],1)
        self.assertIsNone(result['mean'])
        samples = Samples()
        samples.add([1,1,5,np.nan])
        result = samples.summary(categorical=True)
        self.assertEqual(result['class_counts'], {'1':2,'5':1})
        self.assertNotIn('mean',result)


class InspectionWorkflow(unittest.TestCase):
    def test_local_bundle_reports_candidates_plots_counts_and_preserves_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            station = {'station_id':1,'network':'TEST','station_name':'one','latitude':40.,'longitude':-100.}
            selection = root/'ismn.json'
            write_json(selection, {'stations':[station],'start':'2024-02-27','end':'2024-03-02'})
            assets = [{'asset_id':f,'field':f} for f in ['soil_texture','sand_fraction','silt_fraction','clay_fraction','porosity']]
            config = root/'gldas.json'
            write_json(config, {'collection_id':'gldas_test','assets':assets})
            cache = root/'ancillary/gldas_test/extracts'
            cache.mkdir(parents=True)
            for asset, value in zip(assets,[3.0000005,.5,.3,.25,1.1]):
                path = cache/(asset['asset_id']+'.npz')
                np.savez_compressed(path,station_ids=[1],values=[[value]],latitude_indices=[0],longitude_indices=[0])
                write_json(path.with_suffix('.json'),{'sha256':sha256(path),
                    'source_attributes':{'units':'-','_FillValue':-9999}})
            inputs = {'selection_sha256':sha256(selection),'sources':{'gldas':{'configuration_sha256':sha256(config)}}}
            signature = hashlib.sha256(json.dumps(inputs,sort_keys=True).encode()).hexdigest()
            daily = root/'daily/TEST/1'/signature
            daily.mkdir(parents=True)
            frame = pd.DataFrame({'date':pd.date_range('2024-02-27',periods=5),
                'sm_one':[.2,.2,.2,np.nan,.3], 'chirps_precip_mm_day':[-1,10,100,np.nan,0],
                'modis_lai_m2_m2':[1.5,1.5,2,2,2],
                'modis_lai_m2_m2__source_start':pd.to_datetime(['2024-02-25']*2+['2024-02-29']*3),
                'modis_land_cover':[10.]*5,
                'modis_land_cover__source_start':pd.to_datetime(['2024-01-01']*5)})
            table = pa.Table.from_pandas(frame,preserve_index=False).replace_schema_metadata({b'schema_version':b'1'})
            pq.write_table(table,daily/'daily.parquet')
            meta = {'artifact_role':'daily_pre_drydown_station_table','schema_version':1,'inputs':inputs,
                'input_signature':signature,'table_sha256':sha256(daily/'daily.parquet'),'rows':5,
                'start':'2024-02-27','end_inclusive':'2024-03-02','station':station,
                'sensors':{'sm_one':{'variable':'soil_moisture','member':'raw/sm','depth_from':.05,'depth_to':.05,
                    'observations_in_window':10,'decoded_missing_values':1,'values_masked_by_quality':3}},
                'source_coordinates':{k:[{'native_latitudes':40.,'native_longitudes':-100.}]
                                      for k in ['chirps','lai','land_cover']}}
            write_json(daily/'description.json',meta)
            hashes = {p:sha256(p) for p in root.rglob('*') if p.is_file()}
            result = inspect_data(root/'daily',selection,config,root/'ancillary',root/'reports',
                                  figure_dir=root/'figures')
            self.assertEqual(result['stations'],1)
            self.assertEqual(result['flatline_candidate_runs'],1)
            self.assertEqual(result['chirps_extreme_station_days'],1)
            self.assertEqual(result['chirps_negative_station_days'],1)
            self.assertEqual(result['gldas']['physical_range_failure_stations'],1)
            self.assertEqual(result['gldas']['closure_failure_stations'],1)
            self.assertEqual(result['g_masking_by_variable']['soil_moisture']['g_finite_values_accepted'],6)
            self.assertEqual(result['distributions']['lai_station_composites']['finite'],2)
            self.assertEqual(result['distributions']['land_cover_station_years']['finite'],1)
            self.assertFalse(result['network_access'])
            self.assertFalse(result['source_data_modified'])
            self.assertEqual(result['values_filled'],0)
            self.assertEqual({p.name for p in (root/'figures').glob('*.png')},
                {'ismn_sm_distribution.png','chirps_precip_distribution.png'})
            self.assertFalse(list((root/'reports').glob('*.png')))
            self.assertEqual(len(list((root/'reports').glob('*.csv'))),7)
            self.assertEqual(json.loads((root/'reports/summary.json').read_text())['stations'],1)
            self.assertEqual({p:sha256(p) for p in hashes},hashes)
            with self.assertRaisesRegex(ValueError,'outside'):
                inspect_data(root/'daily',selection,config,root/'ancillary',root/'daily/inspection',
                             figure_dir=root/'figures')
            with self.assertRaisesRegex(ValueError,'outside'):
                inspect_data(root/'daily',selection,config,root/'ancillary',root/'reports',
                             figure_dir=root/'ancillary/figures')


if __name__ == '__main__':
    unittest.main()
