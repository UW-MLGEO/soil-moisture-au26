"""Configuration loading: strict schema, run-file overrides and resolved records."""
import tempfile
import unittest
from pathlib import Path

import yaml

from sm_drydown.config import CONFIG_DIR, ConfigError, differences_from_default, load_config, merge, write_resolved


class ConfigTests(unittest.TestCase):
    def test_baseline_loads_and_has_no_differences(self):
        cfg = load_config('baseline')
        self.assertEqual((cfg.study.start, cfg.study.end), ('2002-07-04', '2025-12-31'))
        self.assertEqual(differences_from_default(cfg), {})

    def test_run_file_overrides_only_its_keys_and_differences_are_reported(self):
        cfg = load_config('pilot_arm')
        self.assertEqual(cfg.subset.networks, ['ARM'])
        self.assertEqual(differences_from_default(cfg), {'subset.networks': ('all', ['ARM'])})

    def test_unknown_keys_wrong_types_and_bad_options_are_rejected(self):
        for overrides, message in (({'ismn': {'flag_kept': ['G']}}, 'unknown key'),
                                   ({'ismn': {'min_obs_per_day': '1'}}, 'does not match'),
                                   ({'ismn': {'daily_statistic': 'max'}}, 'does not match'),
                                   ({'ismn': {'min_obs_per_day': True}}, 'does not match'),
                                   ({'nonsense': {}}, 'unknown key'),
                                   ({'modis_lai': {'band': 'NotAcquired'}}, 'acquired bands'),
                                   ({'ismn': {'daily_variables': ['soil_moisture'],
                                              'request_variables': ['precipitation']}}, 'requested')):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ConfigError, message):
                load_config('baseline', overrides=overrides)

    def test_nested_override_merges_inside_a_mapping(self):
        merged = merge({'a': {'b': 1, 'c': 2}}, {'a': {'c': 3}}, 'test')
        self.assertEqual(merged, {'a': {'b': 1, 'c': 3}})

    def test_resolved_config_round_trips(self):
        cfg = load_config('baseline')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.resolved.yml'
            write_resolved(cfg, path)
            resolved = yaml.safe_load(path.read_text())
        self.assertEqual(resolved['modis_lai']['qc_rules'][0]['allowed'], [0, 1])
        self.assertEqual(resolved['gldas']['texture_snap_tolerance'], 1e-4)

    def test_every_run_file_loads(self):
        for path in sorted((CONFIG_DIR / 'runs').glob('*.yml')):
            with self.subTest(run=path.stem):
                load_config(path.stem)


if __name__ == '__main__':
    unittest.main()
