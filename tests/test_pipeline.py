"""Stage cache, assembly and extensibility on small in-memory tables."""
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from sm_drydown import features as registry
from sm_drydown.assemble import sensor_day
from sm_drydown.cache import Stage, cache_usage, stage_key
from sm_drydown.config import load_config
from sm_drydown.schemas import SCHEMAS

DATES = pd.date_range('2024-01-01', periods=3)


def theta():
    return pd.DataFrame({'sensor_id': ['sm_a'] * 3 + ['sm_b'] * 2, 'station_id': [1, 1, 1, 2, 2],
                         'date': list(DATES) + list(DATES[:2]), 'n_obs': [1, 1, 0, 2, 2], 'n_good': [1, 1, 0, 2, 2],
                         'n_valid': [1, 1, 0, 2, 2], 'theta_m3m3': [.2, .3, np.nan, .1, .1],
                         'missing_reason': ['available'] * 2 + ['no_observations'] + ['available'] * 2})


def station_days(column, values):
    return pd.DataFrame({'station_id': [1] * 3 + [2] * 3, 'date': list(DATES) * 2, column: values,
                         'missing_reason': 'available'})


class AssembleTests(unittest.TestCase):
    def tables(self):
        static = pd.DataFrame({'station_id': [1, 2], **{f'{c}': [v, v] for c, v in (
            ('gldas_texture_class', 3), ('gldas_sand_frac', .4), ('gldas_silt_frac', .3), ('gldas_clay_frac', .3),
            ('gldas_porosity', .45))}, **{f'{c}__missing_reason': 'available' for c in (
                'gldas_texture_class', 'gldas_sand_frac', 'gldas_silt_frac', 'gldas_clay_frac', 'gldas_porosity')}})
        return {'theta_m3m3': theta(), 'precip_chirps_mm_day': station_days('precip_chirps_mm_day', [0., 1, 2, 3, 4, 5]),
                'lai_m2m2': station_days('lai_m2m2', [1.] * 6), 'igbp_class': station_days('igbp_class', [10] * 6),
                'gldas_static': static}

    def test_join_keeps_every_sensor_day_and_orders_columns_by_config(self):
        cfg = load_config('baseline')
        out = sensor_day(cfg, self.tables(), 'NET')
        self.assertEqual(len(out), 5)
        self.assertEqual(out.precip_chirps_mm_day.tolist(), [0., 1, 2, 3, 4])
        joined = [c for c in out if not c.endswith('__missing_reason')][4:-3]
        self.assertEqual(joined, [f for f in cfg.features if registry.FEATURES[f].level != 'standalone'])

    def test_a_new_feature_needs_only_config_registry_and_schema(self):
        """Toy derived feature: 2-day trailing rain. Joined as a new column; nothing else changes."""
        tables = self.tables()
        rain = tables['precip_chirps_mm_day'].copy()
        rain['precip_2d_mm'] = rain.groupby('station_id').precip_chirps_mm_day.transform(
            lambda x: x.rolling(2, min_periods=2).sum())
        tables['precip_2d_mm'] = rain[['station_id', 'date', 'precip_2d_mm', 'missing_reason']]
        feature = registry.Feature('toy', 'precip_2d_mm', 'station_day', 'mm')
        cfg = replace(load_config('baseline'), features=[*load_config('baseline').features, 'precip_2d_mm'])
        registry.FEATURES['precip_2d_mm'] = feature
        SCHEMAS['precip_2d_mm'] = {'columns': {'station_id': 'int', 'date': 'date', 'precip_2d_mm': 'float',
                                               'missing_reason': 'str'}}
        self.addCleanup(registry.FEATURES.pop, 'precip_2d_mm')
        self.addCleanup(SCHEMAS.pop, 'precip_2d_mm')
        before = sensor_day(load_config('baseline'), self.tables(), 'NET')
        after = sensor_day(cfg, tables, 'NET')
        pd.testing.assert_frame_equal(after[before.columns], before)
        np.testing.assert_allclose(after.precip_2d_mm, [np.nan, 1, 3, np.nan, 7], equal_nan=True)

    def test_group_cache_keys_ignore_the_feature_list_and_registry(self):
        for group in registry.GROUPS.values():
            self.assertNotIn('features/__init__.py', group.code)
            self.assertNotIn('features', group.sections)


class StageTests(unittest.TestCase):
    def test_partition_reused_only_for_same_inputs_and_unchanged_files(self):
        with tempfile.TemporaryDirectory() as folder:
            stage = Stage(folder, 'features/test', stage_key('test', {'a': 1}))
            table = station_days('precip_chirps_mm_day', [0., 1, 2, 3, 4, 5])
            table['provider_null'] = pd.array([False] * 6, dtype='boolean')
            table['provider_absent_row'] = pd.array([False] * 6, dtype='boolean')
            self.assertFalse(stage.done('NET', {'zip': 'x'}))
            stage.write('NET', {'precip_chirps_mm_day': table}, {'zip': 'x'})
            self.assertTrue(stage.done('NET', {'zip': 'x'}))
            self.assertFalse(stage.done('NET', {'zip': 'changed'}))
            pd.testing.assert_frame_equal(stage.read('precip_chirps_mm_day', ['NET']), table)
            next(stage.folder.glob('precip_chirps_mm_day/*.parquet')).write_bytes(b'truncated')
            self.assertFalse(stage.done('NET', {'zip': 'x'}))
            self.assertNotEqual(stage.key, stage_key('test', {'a': 2}))

    def test_schema_is_enforced_on_write(self):
        with tempfile.TemporaryDirectory() as folder:
            stage = Stage(folder, 'features/test', 'k')
            with self.assertRaisesRegex(ValueError, 'missing column'):
                stage.write('NET', {'lai_m2m2': station_days('lai_m2m2', [1.] * 6)})


class CleanCacheTests(unittest.TestCase):
    """Which cache/<stage>/<key>/ folders clean-cache may remove."""

    def setUp(self):
        self.out = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.past = 1_000_000_000  # not-science: a file time long before the run records

    def key_folder(self, stage, letter, old=True):
        folder = self.out / 'cache' / stage / (letter * 64)
        (folder / '_complete').mkdir(parents=True)
        (folder / '_complete' / 'NET.json').write_text('{}')
        if old:
            for path in (folder / '_complete' / 'NET.json', folder / '_complete', folder):
                os.utime(path, (self.past, self.past))
        return f'cache/{stage}/{letter * 64}'

    def record(self, used, inspect_key=None, summary=True):
        run = self.out / 'runs' / 'r'
        run.mkdir(parents=True)
        (run / 'manifest.json').write_text(json.dumps({'stages': {f: {'folder': f} for f in used}}))
        if summary:
            stage = {'stage': {'folder': inspect_key}} if inspect_key else {}
            (run / 'inspection_summary.json').write_text(json.dumps(stage))

    def statuses(self):
        return {e[0].relative_to(self.out).as_posix(): e[2] for e in cache_usage(self.out)}

    def test_only_folders_no_run_names_are_unused(self):
        current, stale = self.key_folder('features/ismn', 'a'), self.key_folder('features/ismn', 'b')
        inspected, old_inspect = self.key_folder('inspect', 'c'), self.key_folder('inspect', 'd')
        self.record([current], inspected)
        self.assertEqual(self.statuses(), {current: 'in_use', stale: 'unused', inspected: 'in_use',
                                           old_inspect: 'unused'})

    def test_folder_written_after_the_newest_run_record_is_kept(self):
        """A run in progress writes feature keys before its manifest exists."""
        current = self.key_folder('assemble', 'a')
        self.record([current])
        in_progress = self.key_folder('assemble', 'b', old=False)
        future = Path(self.out / in_progress / '_complete' / 'NET.json')
        os.utime(future, (future.stat().st_mtime + 60, future.stat().st_mtime + 60))  # not-science: 1 min later
        self.assertEqual(self.statuses()[in_progress], 'kept')

    def test_nothing_is_unused_without_run_records(self):
        self.key_folder('assemble', 'a')
        self.assertEqual(set(self.statuses().values()), {'kept'})

    def test_inspect_keys_kept_while_a_summary_lacks_its_key(self):
        """Summaries written before inspect recorded its key cannot say which inspect folder is theirs."""
        current, inspected = self.key_folder('assemble', 'a'), self.key_folder('inspect', 'c')
        self.record([current], summary=True)
        self.assertEqual(self.statuses()[inspected], 'kept')

    def test_non_key_folders_are_ignored(self):
        (self.out / 'cache' / 'features' / 'ismn' / 'notes').mkdir(parents=True)
        self.record([])
        self.assertEqual(self.statuses(), {})


if __name__ == '__main__':
    unittest.main()
