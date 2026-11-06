"""Feature rules: explicit gaps, separate sensors, QC words, source intervals and coverage.

Each test states the rule it pins; configuration comes from config/pipeline.yml
(baseline) with explicit overrides, never from defaults in the code.
"""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from sm_drydown.config import QcRule, load_config
from sm_drydown.features.common import align_daily, daily_index, decode_values, interval_days
from sm_drydown.features.gridded import outside_coverage
from sm_drydown.features.ismn import Sensor, observed_span, read_station_sensors, sensor_daily
from sm_drydown.storage.files import sha256

CFG = load_config('baseline')
LAI_RULES = CFG.modis_lai.qc_rules


def observations(values=(0.2, 0.4, 0.6), flags=('G', 'D', 'G')):
    return pd.DataFrame({'value': values, 'ismn_flag': flags, 'provider_flag': ['x', 'y', 'x']},
                        index=pd.to_datetime(['2002-07-04T12:00Z', '2002-07-04T13:00Z', '2002-07-06T12:00Z']))


def ismn(**overrides):
    return load_config('baseline', overrides={'ismn': overrides}).ismn if overrides else CFG.ismn


class ISMNRules(unittest.TestCase):
    def test_kept_flags_mask_others_and_counts_and_reasons_are_explicit(self):
        """Only flags in ismn.flags_kept are averaged; n_obs/n_good/n_valid and the reason record why."""
        dates = daily_index('2002-07-04', '2002-07-06')
        for flags, mean, valid, masked in ((('G', 'D', 'G'), .2, 1, 1), (('G', 'G', 'G'), .3, 2, 0),
                                           (('D', 'D', 'G'), np.nan, 0, 2)):
            with self.subTest(flags=flags):
                sensor = Sensor('N/S/a_sm_0.05_0.05_x.stm', 'soil_moisture', observations(flags=flags), 'm3/m3', {})
                out, audit, summary = sensor_daily(sensor, dates, CFG.ismn)
                np.testing.assert_allclose(out.value, [mean, np.nan, .6], equal_nan=True)
                self.assertEqual(list(out.n_obs), [2, 0, 1])
                self.assertEqual(list(out.n_valid), [valid, 0, 1])
                self.assertEqual(out.missing_reason.iloc[1], 'no_observations')
                self.assertEqual(summary['values_masked_by_quality'], masked)
                if not valid:
                    self.assertEqual(out.missing_reason.iloc[0], 'quality_masked')
                self.assertEqual(sum(json.loads(audit.ismn_flag_counts.iloc[0]).values()), 2)

    def test_min_obs_per_day_and_median_are_configurable(self):
        """A day with fewer accepted values than ismn.min_obs_per_day is missing with its own reason."""
        dates = daily_index('2002-07-04', '2002-07-06')
        sensor = Sensor('m', 'soil_moisture', observations(flags=('G', 'G', 'G')), 'm3/m3', {})
        out, _, _ = sensor_daily(sensor, dates, ismn(min_obs_per_day=2, daily_statistic='median'))
        np.testing.assert_allclose(out.value, [.3, np.nan, np.nan], equal_nan=True)
        self.assertEqual(out.missing_reason.tolist(), ['available', 'no_observations', 'below_min_obs_per_day'])

    def test_flags_kept_can_widen_acceptance(self):
        dates = daily_index('2002-07-04', '2002-07-06')
        sensor = Sensor('m', 'soil_moisture', observations(), 'm3/m3', {})
        out, _, summary = sensor_daily(sensor, dates, ismn(flags_kept=['G', 'D']))
        self.assertAlmostEqual(out.value.iloc[0], .3)
        self.assertEqual(summary['values_masked_by_quality'], 0)

    def test_temperature_kelvin_to_celsius_and_missing_codes(self):
        """Temperature uses its own flags; declared missing codes are missing, not values."""
        frame = observations((273.15, 283.15, -9999), ('D', 'G', 'G'))
        sensor = Sensor('t', 'soil_temperature', frame, 'K', {})
        out, _, _ = sensor_daily(sensor, daily_index('2002-07-04', '2002-07-06'),
                                 ismn(missing_codes={'soil_moisture': [], 'soil_temperature': [-9999]}))
        np.testing.assert_allclose(out.value, [10, np.nan, np.nan], equal_nan=True)
        self.assertEqual(out.missing_reason.iloc[2], 'missing_values')

    def test_flag_histograms_keep_missing_words(self):
        frame = observations((.2, .4, .6), ('G', None, 'G')).iloc[[2, 0, 1]].copy()
        frame['provider_flag'] = ['x', None, 'x']
        _, audit, _ = sensor_daily(Sensor('f', 'soil_moisture', frame, 'm3/m3', {}),
                                   daily_index('2002-07-04', '2002-07-06'), CFG.ismn)
        self.assertEqual(json.loads(audit.ismn_flag_counts.iloc[0]), {'<missing>': 1, 'G': 1})
        self.assertEqual(json.loads(audit.provider_flag_counts.iloc[0]), {'<missing>': 1, 'x': 1})

    def test_duplicate_timestamp_is_an_error_even_with_a_bad_flag(self):
        frame = observations()
        frame.index = pd.DatetimeIndex([frame.index[0], frame.index[0], frame.index[2]])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            sensor_daily(Sensor('x', 'soil_moisture', frame, 'm3/m3', {}), daily_index('2002-07-04', '2002-07-06'),
                         CFG.ismn)

    def test_observed_span_keeps_inner_gaps_and_drops_empty_edges(self):
        dates = daily_index('2002-07-03', '2002-07-08')
        sensor = Sensor('s', 'soil_moisture', observations(flags=('G', 'G', 'G')), 'm3/m3', {})
        out, _, _ = sensor_daily(sensor, dates, CFG.ismn)
        span = observed_span(out)
        self.assertEqual(span.index[0], pd.Timestamp('2002-07-04'))
        self.assertEqual(span.index[-1], pd.Timestamp('2002-07-06'))
        self.assertEqual(span.missing_reason.tolist(), ['available', 'no_observations', 'available'])

    def test_native_reader_keeps_replicates_and_skips_unrequested_variables(self):
        """Each archive member is its own sensor; precipitation (p) never becomes a daily feature."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.zip'
            with zipfile.ZipFile(path, 'w') as z:
                for var in ('sm', 'ts', 'p'):
                    for replicate in (1, 2):
                        z.writestr(f'TEST/Station/a_{var}_1.0_1.0_sensor{replicate}_20020704_20020706.stm',
                                   'camp TEST Station 40 -120 100 1.0 1.0 sensor\n'
                                   '2002/07/04 12:00 0.2 G P\n2002/07/05 12:00 NaN G P\n')
            sensors = read_station_sensors(path, {'network': 'TEST', 'station_name': 'Station', 'station_id': 1},
                                           variables=['soil_moisture', 'soil_temperature'])
            self.assertEqual(len(sensors), 4)
            self.assertEqual(len({s.sensor_id for s in sensors}), 4)
            self.assertTrue(all(s.metadata['depth_to'] == 1 for s in sensors))
            only_sm = read_station_sensors(path, {'network': 'TEST', 'station_name': 'Station', 'station_id': 1},
                                           variables=['soil_moisture'])
            self.assertEqual({s.variable for s in only_sm}, {'soil_moisture'})


class GriddedRules(unittest.TestCase):
    def test_missing_rain_is_never_zero_and_missing_dates_stay_visible(self):
        dates = daily_index('2002-07-04', '2002-07-06')
        out = align_daily(pd.Series([0, np.nan], index=dates[:2]), dates)
        np.testing.assert_allclose(out.value, [0, np.nan, np.nan], equal_nan=True)
        self.assertEqual(list(out.missing_reason), ['available', 'missing_value', 'no_source_record'])

    def test_provider_null_and_absent_row_stay_distinct_unless_configured_as_zero(self):
        dates = daily_index('2002-07-04', '2002-07-06')
        values = pd.Series([0, np.nan, np.nan], index=dates)
        null, absent = pd.Series([False, True, False], index=dates), pd.Series([False, False, True], index=dates)
        out = align_daily(values, dates, source_present=~absent, source_null=null, source_absent=absent)
        self.assertEqual(out.missing_reason.tolist(), ['available', 'provider_null', 'provider_absent_row'])
        zero = align_daily(values, dates, source_present=~absent, source_null=null, source_absent=absent,
                           null_is_zero=True)
        np.testing.assert_allclose(zero.value, [0, 0, np.nan], equal_nan=True)
        self.assertEqual(zero.missing_reason.iloc[1], 'provider_null_set_to_zero')

    def test_lai_qc_accepts_main_algorithm_and_saturation_only(self):
        """SCF_QC (bits 5-7 of FparLai_QC) must be 0 or 1; missing QC is never assumed good."""
        dates = pd.date_range('2025-01-01', periods=6)
        source = pd.DataFrame({'start': dates, 'end': dates + pd.Timedelta(days=1), 'value': [2.] * 6,
                               'qc_FparLai_QC': [0, 32, 64, 96, 128, np.nan]})
        out = interval_days(source, dates, qc_mode='bit_rules', rules=LAI_RULES, missing_qc_is_good=False)
        np.testing.assert_allclose(out.value, [2., 2., np.nan, np.nan, np.nan, np.nan], equal_nan=True)
        self.assertEqual(out.missing_reason.tolist()[-2:], ['quality_masked', 'missing_qc'])
        self.assertEqual(out.qc_FparLai_QC.iloc[1], 32)
        lenient = interval_days(source, dates, qc_mode='bit_rules', rules=LAI_RULES, missing_qc_is_good=True)
        self.assertEqual(lenient.value.iloc[-1], 2.)

    def test_composite_values_never_cross_their_interval(self):
        dates = daily_index('2002-07-01', '2002-07-05')
        source = pd.DataFrame({'start': ['2002-07-01'], 'end': ['2002-07-03'], 'value': [1.5], 'qc_FparLai_QC': [0]})
        out = interval_days(source, dates, qc_mode='bit_rules', rules=LAI_RULES, missing_qc_is_good=False)
        np.testing.assert_allclose(out.value, [1.5, 1.5, np.nan, np.nan, np.nan], equal_nan=True)
        self.assertEqual(out.missing_reason.tolist()[2:], ['no_source_interval'] * 3)
        with self.assertRaisesRegex(ValueError, 'nonoverlapping'):
            interval_days(pd.concat([source, source]), dates, qc_mode='bit_rules', rules=LAI_RULES,
                          missing_qc_is_good=False)

    def test_custom_qc_rule(self):
        dates = daily_index('2002-07-01', '2002-07-02')
        source = pd.DataFrame({'start': dates, 'end': dates + pd.Timedelta(days=1), 'value': [1., 1.],
                               'qc_word': [0, 1]})
        out = interval_days(source, dates, qc_mode='bit_rules', rules=[QcRule('qc_word', 0, 1, [0])],
                            missing_qc_is_good=False)
        self.assertEqual(out.missing_reason.tolist(), ['available', 'quality_masked'])

    def test_fill_masked_before_scale_and_offset(self):
        values, count = decode_values([10, 255, np.nan], missing_codes=[255], scale_factor=0.1, add_offset=1)
        np.testing.assert_allclose(values, [2, np.nan, np.nan], equal_nan=True)
        self.assertEqual(count, 2)

    def test_annual_category_stops_at_year_boundary_and_stays_integer(self):
        frame = pd.DataFrame({'start': ['2002-01-01'], 'end': ['2003-01-01'], 'value': [8]})
        out = interval_days(frame, daily_index('2002-12-31', '2003-01-01'), qc_mode='retain_flagged', rules=[],
                            missing_qc_is_good=False, categorical=True)
        self.assertEqual(str(out.value.dtype), 'Int64')
        self.assertEqual(out.value.iloc[0], 8)
        self.assertTrue(pd.isna(out.value.iloc[1]))

    def test_outside_coverage_is_labelled_not_guessed(self):
        out = outside_coverage(daily_index('2002-07-01', '2002-07-02'), {'value': 'float64'})
        self.assertTrue(out.value.isna().all())
        self.assertTrue(out.missing_reason.eq('outside_source_coverage').all())


class SpatialPartitionTests(unittest.TestCase):
    def test_frozen_regions_cover_selection_once_and_share_no_native_soil_cells(self):
        """records/spatial_partitions.json still describes the selection it was made from (locks/ismn.json)."""
        config = json.loads(Path('records/spatial_partitions.json').read_text())
        self.assertEqual(config['source_selection']['sha256'], sha256('locks/ismn.json'))
        stations = {s['station_id']: s for s in json.loads(Path('locks/ismn.json').read_text())['stations']}
        self.assertEqual(config['station_count'], len(stations))
        expected = {'eval_south': 277, 'fold_1_northwest': 275, 'fold_2_southwest': 280,
                    'fold_3_northeast': 283, 'fold_4_southeast': 273}
        assigned, cells = set(), {}
        for name, region in config['regions'].items():
            ids = region['station_ids']
            self.assertEqual(len(set(ids)), expected[name])
            self.assertFalse(assigned.intersection(ids))
            assigned.update(ids)
            for sid in ids:
                for axis in ('latitude', 'longitude'):
                    if axis + '_min_inclusive' in region:
                        self.assertGreaterEqual(stations[sid][axis], region[axis + '_min_inclusive'])
                    if axis + '_max_exclusive' in region:
                        self.assertLess(stations[sid][axis], region[axis + '_max_exclusive'])
                cell = tuple(int(np.floor(stations[sid][axis] * 4)) for axis in ('latitude', 'longitude'))
                self.assertEqual(cells.setdefault(cell, name), name)
        self.assertEqual(assigned, set(stations))


if __name__ == '__main__':
    unittest.main()
