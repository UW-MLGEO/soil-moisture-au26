"""Provider requests are rebuilt from config + selection + source facts, and locks gate acquisition."""
import tempfile
import unittest
from pathlib import Path

from sm_drydown.config import LOCKS_DIR, SOURCES, load_config
from sm_drydown.locks import verify_lock, write_lock
from sm_drydown.requests import build_request, covered, serialize
from sm_drydown.selections import load_selection
from sm_drydown.storage.files import sha256

# SHA-256 of the specifications the published 2026-11 corpus was acquired with
# (records/source_provenance.json `selection.sha256`; Kopah provenance keys).
PUBLISHED = {'ismn': '15b45472f15f63fd45a565fd646fa7c464dcd4f7c46c36e5b01856ce2faac032',
             'chirps': '04c7a2be4be68816185aa56be5b3f0c90b8988f5001a76a4fa1cfec7463fc6fc',
             'morgan_2025': '0b51fea6a95f26fd0ba55f896efa6fba985804cca770ee1802198dd692af712b'}


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config('baseline')
        self.selection = load_selection(self.cfg.selection.name)

    def test_baseline_rebuilds_every_committed_lock_byte_for_byte(self):
        """Nothing was lost moving the JSON specifications into pipeline.yml, CSVs and source facts."""
        for source in SOURCES:
            with self.subTest(source=source):
                self.assertEqual(serialize(build_request(self.cfg, self.selection, source)),
                                 (LOCKS_DIR / f'{source}.json').read_bytes())

    def test_locks_keep_the_published_acquisition_hashes(self):
        for source, digest in PUBLISHED.items():
            self.assertEqual(sha256(LOCKS_DIR / f'{source}.json'), digest)

    def test_a_scientific_change_changes_the_request_and_invalidates_the_lock(self):
        edited = load_config('baseline', overrides={'modis_lai': {'bands': ['Lai', 'FparLai_QC', 'FparExtra_QC']}})
        self.assertNotEqual(build_request(edited, self.selection, 'modis_lai')['bands'],
                            build_request(self.cfg, self.selection, 'modis_lai')['bands'])
        with self.assertRaisesRegex(ValueError, 'no longer matches'):
            verify_lock(edited, self.selection, 'modis_lai', LOCKS_DIR, allow_uncommitted=True)
        verify_lock(self.cfg, self.selection, 'modis_lai', LOCKS_DIR, allow_uncommitted=True)

    def test_uncommitted_locks_are_refused(self):
        (LOCKS_DIR.parent / 'output').mkdir(exist_ok=True)  # git-ignored, inside the repository
        with tempfile.TemporaryDirectory(dir=LOCKS_DIR.parent / 'output') as folder:
            write_lock(self.cfg, self.selection, 'gldas', folder)
            with self.assertRaisesRegex(ValueError, 'not committed'):
                verify_lock(self.cfg, self.selection, 'gldas', folder)
            verify_lock(self.cfg, self.selection, 'gldas', folder, allow_uncommitted=True)

    def test_plan_summary_keeps_planned_at_when_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            write_lock(self.cfg, self.selection, 'chirps', folder)
            first = (Path(folder) / 'chirps.summary.yml').read_text()
            write_lock(self.cfg, self.selection, 'chirps', folder)
            self.assertEqual(first, (Path(folder) / 'chirps.summary.yml').read_text())

    def test_source_coverage_excludes_stations_outside_it(self):
        stations = [{'latitude': 49.9, 'longitude': 0.0}, {'latitude': 50.0, 'longitude': 0.0},
                    {'latitude': -50.0, 'longitude': 179.9}]
        box = {'latitude': [-50, 50], 'longitude': [-180, 180]}
        self.assertEqual(covered(stations, box), [stations[0], stations[2]])
        self.assertEqual(covered(stations, None), stations)


if __name__ == '__main__':
    unittest.main()
