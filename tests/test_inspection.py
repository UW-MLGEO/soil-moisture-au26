"""Inspection checks: candidates and distributions, with thresholds passed from config."""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from sm_drydown.config import load_config
from sm_drydown.inspection.checks import (Samples, flatlines, gldas_checks, precipitation_extremes,
                                          seasonal_precipitation)

CFG = load_config('baseline')
SEASONS = CFG.inspection.seasons
Q = CFG.inspection.summary_quantiles


class InspectionChecks(unittest.TestCase):
    def test_flatlines_exact_three_days_and_missing_or_calendar_gaps_break_runs(self):
        dates = pd.date_range('2024-01-01', periods=12)
        values = [0, 0, 0, np.nan, .2, .2, .2+1e-12, .4, .4, .4, .4, np.nan]
        runs = flatlines(values, dates, 3)
        self.assertEqual([r['consecutive_days'] for r in runs], [3, 4])
        self.assertEqual([r['value'] for r in runs], [0, .4])
        self.assertEqual(flatlines([.1, .1, .1], pd.to_datetime(['2024-01-01','2024-01-02','2024-01-04']), 3), [])
        self.assertEqual(flatlines([.1, .1], dates[:2], 3), [])
        self.assertEqual(flatlines([np.nan]*3, dates[:3], 3), [])
        with self.assertRaises(ValueError):
            flatlines([1,1], [dates[0],dates[0]], 3)
        with self.assertRaises(ValueError):
            flatlines([1,1], dates[:2][::-1], 3)

    def test_precip_positive_only_strict_threshold_missing_and_all_zero(self):
        flags, threshold, count = precipitation_extremes([-1,0,10,100,np.nan,np.inf], .999)
        self.assertEqual(count, 2)
        self.assertAlmostEqual(threshold, 99.91)
        self.assertEqual(flags.tolist(), [False,False,False,True,False,False])
        self.assertFalse(precipitation_extremes([10,10], .999)[0].any())
        flags, threshold, count = precipitation_extremes([0,np.nan,-1], .999)
        self.assertEqual(count, 0)
        self.assertTrue(np.isnan(threshold))
        self.assertFalse(flags.any())

    def test_season_definitions_must_cover_every_month_once(self):
        with self.assertRaisesRegex(ValueError, 'every calendar month'):
            seasonal_precipitation([1.], pd.date_range('2024-01-01', periods=1), {'JJA': [6, 7, 8]})

    def test_seasons_leap_djf_edge_partial_and_missing_sum_not_zero(self):
        dates = pd.date_range('2023-12-01','2024-02-29')
        result = seasonal_precipitation(np.ones(len(dates)), dates, SEASONS).iloc[0]
        self.assertEqual(result.season_year, 2024)
        self.assertEqual(result.calendar_days, 91)
        self.assertEqual(result.complete_season_total_mm, 91)
        self.assertEqual(result.coverage_fraction, 1)
        result = seasonal_precipitation([1,-2,np.nan], dates[:3], SEASONS).iloc[0]
        self.assertEqual(result.observed_sum_mm, -1)
        self.assertEqual(result.negative_days, 1)
        self.assertEqual(result.missing_days_in_selection, 1)
        self.assertAlmostEqual(result.coverage_fraction, 2/91)
        self.assertFalse(result.complete_season)
        self.assertTrue(np.isnan(result.complete_season_total_mm))
        result = seasonal_precipitation([np.nan,np.nan],dates[:2], SEASONS).iloc[0]
        self.assertTrue(np.isnan(result.observed_sum_mm))

    def test_gldas_bounds_closure_float_boundary_texture_and_missing(self):
        source = pd.DataFrame({'gldas_sand_frac':[.5,.5,.5,.5,np.nan,-.1],
            'gldas_silt_frac':[.3,.3,.3,.3,.3,.3], 'gldas_clay_frac':[.2,.21,.19,.22,.2,.8],
            'gldas_porosity':[0,1,1.1,np.nan,.5,np.inf], 'gldas_texture_class':[3.0000005,16,0,np.nan,3.1,17]})
        before = source.copy(deep=True)
        result = gldas_checks(source, sum_tolerance=.01, texture_codes=[1, 16], texture_tolerance=1e-4,
                              bounds=[0, 1])
        self.assertEqual(result.closure_outside_tolerance.tolist(), [False,False,False,True,False,False])
        self.assertEqual(result.closure_evaluable.tolist(), [True,True,True,True,False,True])
        self.assertEqual(result.any_physical_range_failure.tolist(), [False,False,True,False,True,True])
        pd.testing.assert_frame_equal(before, source)
        pd.testing.assert_frame_equal(result[source.columns],source)

    def test_distribution_empty_missing_and_categorical_without_ordinal_mean(self):
        samples = Samples()
        samples.add([np.nan,np.inf])
        result = samples.summary(Q)
        self.assertEqual(result['missing'],1)
        self.assertEqual(result['nonfinite_nonmissing'],1)
        self.assertIsNone(result['mean'])
        samples = Samples()
        samples.add([1,1,5,np.nan])
        result = samples.summary(Q, categorical=True)
        self.assertEqual(result['class_counts'], {'1':2,'5':1})
        self.assertNotIn('mean',result)


class InspectionWorkflow(unittest.TestCase):
    def test_smoke_run_reports_candidates_without_touching_inputs(self):
        """End to end on the synthetic fixture: counts are fixed by tests/smoke_fixture.py; inputs unchanged."""
        import json
        import subprocess
        import sys
        from smoke_fixture import build
        from sm_drydown.storage.files import sha256
        with tempfile.TemporaryDirectory() as folder:
            paths, locks = build(folder)
            common = ['--run', 'smoke', '--paths', str(paths)]
            subprocess.run([sys.executable, 'scripts/homogenize.py', *common, '--locks-dir', str(locks)],
                           check=True, capture_output=True)
            inputs = {p: sha256(p) for p in Path(folder).rglob('*') if p.is_file() and 'output' not in p.parts}
            subprocess.run([sys.executable, 'scripts/inspect_tables.py', *common], check=True, capture_output=True)
            summary = json.loads((Path(folder) / 'output/runs/smoke/inspection_summary.json').read_text())
            self.assertEqual(summary['stations'], 2)
            self.assertEqual(summary['chirps_extreme_station_days'], 1)
            self.assertEqual(summary['flag_masking_by_variable']['soil_moisture']['non_kept_flag_values_masked'], 1)
            self.assertEqual(summary['distributions']['land_cover_station_years']['class_counts'], {'10': 1})
            self.assertEqual(summary['distributions']['lai_station_composites']['finite'], 6)
            self.assertEqual({p.name for p in (Path(folder) / 'figures').glob('*.png')},
                             {'ismn_sm_distribution.png', 'chirps_precip_distribution.png'})
            self.assertEqual({p: sha256(p) for p in inputs}, inputs)


if __name__ == '__main__':
    unittest.main()
