"""Offline checks for date boundaries, inventory counts, caching and estimates.

Run from the repository root: pixi run python tests/test_data_volume.py
"""

import importlib.util
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("volume", Path(__file__).resolve().parents[1] / "scripts/estimate_data_volume.py")
volume = importlib.util.module_from_spec(spec)
spec.loader.exec_module(volume)


class VolumeTests(unittest.TestCase):
    def test_inclusive_dates_leap_day_and_missing_intervals(self):
        self.assertEqual(volume.overlap_days("2004-02-28", "2004-03-01"), 3)
        self.assertEqual(volume.overlap_days("2002-07-03", "2002-07-04"), 1)
        self.assertEqual(volume.overlap_days("2000-01-01", "2001-12-31"), 0)
        self.assertEqual(volume.overlap_days("2026-01-01", "2026-01-03"), 0)
        self.assertEqual(volume.overlap_days("never", "never"), 0)
        self.assertEqual(volume.overlap_days(None, None), 0)
        with self.assertRaises(ValueError):
            volume.overlap_days("2004-03-01", "2004-02-28")

    def test_modis_calendar_resets_each_year(self):
        self.assertEqual(volume.composite_count(date(2004, 1, 1), date(2004, 12, 31)), 46)
        self.assertEqual(volume.composite_count(date(2004, 12, 26), date(2005, 1, 1)), 2)
        self.assertEqual(volume.composite_count(), 1081)

    def test_full_archive_is_counted_but_old_and_missing_records_do_not_add_volume(self):
        base = {"lat": 40, "lng": -100, "minimum": "2004-01-01", "maximum": "2004-01-02",
                "variableText": "soil moisture<br>soil temperature<br>precipitation<br>",
                "Timeseries": [{"start_date": "2004-01-01", "end_date": "2004-01-02"}]}
        stations = [{**base, "stationID": 1},
                    {**base, "stationID": 2, "lat": 60},
                    {**base, "stationID": 3, "minimum": "1990-01-01", "maximum": "1991-01-01",
                     "Timeseries": [{"start_date": "1990-01-01", "end_date": "1991-01-01"}]},
                    {**base, "stationID": 4, "minimum": "never", "maximum": "never", "Timeseries": []}]
        summary, _ = volume.summarize_stations([{"networkID": "TEST", "Stations": stations}], {1})
        self.assertEqual(summary["global"]["all_stations"], 4)
        self.assertEqual(summary["global"]["sm_overlap_stations"], 2)
        self.assertEqual(summary["global"]["missing_station_dates"], 1)
        self.assertEqual(summary["global"]["sm_sensor_days"], 4)
        self.assertEqual(summary["global"]["temperature_proxy_sensor_days"], 4)
        self.assertEqual(summary["global"]["precipitation_proxy_station_days"], 4)
        self.assertEqual(summary["conus"]["all_stations"], 1)
        self.assertEqual(summary["chirps_domain"]["sm_overlap_stations"], 1)

    def test_cache_reuses_valid_and_refetches_invalid_entries(self):
        """Reuse is conditional on bytes, provenance and source URL matching."""
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            with patch.object(volume.subprocess, "check_output", return_value=b'{"valid": true}') as call:
                volume.fetch(cache, "sample.json", "https://example.test/sample")
                volume.fetch(cache, "sample.json", "https://example.test/sample")
                self.assertEqual(call.call_count, 1)
                (cache / "sample.json").write_text("corrupt")
                data, _ = volume.fetch(cache, "sample.json", "https://example.test/sample")
                self.assertTrue(json.loads(data)["valid"])
                self.assertEqual(call.call_count, 2)
                (cache / "sample.json.provenance.json").write_text("[]")
                volume.fetch(cache, "sample.json", "https://example.test/sample")
                self.assertEqual(call.call_count, 3)
                volume.fetch(cache, "sample.json", "https://example.test/changed")
                self.assertEqual(call.call_count, 4)

    def test_invalid_remote_metadata_is_not_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(volume.subprocess, "check_output", return_value=b'not json'):
                with self.assertRaises(ValueError):
                    volume.fetch(Path(folder), "bad.json", "https://example.test/bad")
                self.assertFalse((Path(folder) / "bad.json").exists())
        with self.assertRaises(ValueError):
            volume.entries(b'{"feed": {"entry": []}}')

    def test_file_size_uses_final_response_not_redirect(self):
        redirect = b"HTTP/2 302\r\nContent-Length: 100\r\n\r\n"
        self.assertEqual(volume.content_length(redirect + b"HTTP/2 200\r\nContent-Length: 42\r\n"), 42)
        for header in (b"HTTP/2 200\r\n\r\n", b"HTTP/2 200\r\nContent-Length: 0\r\n"):
            with self.subTest(header=header), self.assertRaises(ValueError):
                volume.validate(redirect + header, "head")

    def test_report_totals_and_station_accounting(self):
        """Frozen totals catch formula drift; empty locations still retain sources."""
        snapshot = json.loads(Path("docs/data_volume_inputs.json").read_text())
        results = volume.estimate(snapshot)["regions"]
        for region, full_total, station_total in (("global", 1460.475, 123.037),
                                                  ("conus", 291.132, 99.423)):
            r = results[region]
            self.assertAlmostEqual(r["full_native_total_GB"], full_total, places=3)
            self.assertAlmostEqual(r["station_remote_total_GB"], station_total, places=3)
            self.assertEqual(r["station_remote_GB"]["ISMN_unpacked"], r["full_native_GB"]["ISMN_unpacked"])
        snapshot["stations"]["chirps_domain"]["overlap_stations"] = 0
        snapshot["stations"]["conus"]["overlap_stations"] = 0
        empty = volume.estimate(snapshot)["regions"]
        # Global CHIRPS retains its 2,920-station array, with no remote requests.
        self.assertEqual(empty["global"]["station_remote_GB"]["CHIRPS"], 0.10023776)
        for key in ("CHIRPS", "MCD12Q1", "MCD15A2H"):
            self.assertEqual(empty["conus"]["station_remote_GB"][key], 0)
        for key in ("dPET", "GLDAS"):
            self.assertEqual(empty["conus"]["station_remote_GB"][key], results["conus"]["full_native_GB"][key])

    def test_inconsistent_study_years_are_rejected(self):
        """Missing annual files and reversed windows must not yield small totals."""
        snapshot = json.loads(Path("docs/data_volume_inputs.json").read_text())
        del snapshot["dpet_annual_bytes"]["2002"]
        with self.assertRaisesRegex(ValueError, "study years"):
            volume.estimate(snapshot)
        snapshot["start"], snapshot["end"] = snapshot["end"], snapshot["start"]
        with self.assertRaisesRegex(ValueError, "Reversed"):
            volume.estimate(snapshot)


if __name__ == "__main__":
    unittest.main()
