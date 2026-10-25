"""Local proc sampling and private atomic preferences; no network or model."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chelus.stats import RuntimeStats, StatsPreferences
from tests.test_telegram import MEMINFO, FOOTER


class RuntimeStatsTests(unittest.TestCase):
    def test_revised_default_footer(self):
        sample = json.dumps({"version": 1, "timestamp": 1000, "cpu_percent": 3.9,
                             "cpu_temp_c": 30.3, "battery_percent": 89, "battery_temp_c": 27.2})
        def read(path, **kw):
            return MEMINFO if str(path) == "/proc/meminfo" else sample
        with patch.object(Path, "read_text", autospec=True, side_effect=read), patch("time.time", return_value=1000):
            self.assertEqual(RuntimeStats().footer(), "mem 1.2/1.7G | cpu 3.9% - 30°C | bat 89% - 27°C")

    def test_optional_fields_selected_in_order_and_disk_is_lazy(self):
        sample = json.dumps({"version": 1, "timestamp": 1000, "uptime_seconds": 54659})
        def read(path, **kw):
            self.assertNotEqual(str(path), "/proc/uptime")
            return MEMINFO if str(path) == "/proc/meminfo" else sample
        import os
        disk = os.statvfs_result((4096, 4096, 5976883, 4744806, 4744700, 1, 1, 1, 0, 255))
        with patch.object(Path, "read_text", autospec=True, side_effect=read), \
             patch("time.time", return_value=1000), patch("chelus.stats.os.statvfs", return_value=disk) as stat:
            stats = RuntimeStats(Path("/chosen/workdir"))
            self.assertEqual(stats.footer(), FOOTER)
            stat.assert_not_called()
            self.assertEqual(stats.footer(("mem", "swap", "up", "disk")),
                             FOOTER + " | swap 0.6/1.5G | up 15h10 | disk 4.7/22.8G")
            stat.assert_called_once_with(Path("/chosen/workdir"))
            self.assertEqual(stats.footer(()), "")
            info = stats.info(("mem",))
            self.assertIn("Selected fields: mem", info)
            self.assertIn("f_blocks - f_bfree", info)
            self.assertIn("uptime_seconds=54659", info)
            self.assertEqual(stat.call_count, 1)

    def test_battery_and_uptime_validation_are_independent(self):
        base = {"version": 1, "timestamp": 1000, "cpu_percent": None, "cpu_temp_c": None,
                "battery_percent": 89, "battery_temp_c": 27.2, "uptime_seconds": 54659}
        cases = [("battery_percent", v, "bat 27°C | up 15h10") for v in
                 (None, True, "89", -0.1, 100.1, float("nan"), float("inf"))]
        cases += [("battery_temp_c", v, "bat 89% | up 15h10") for v in
                  (None, False, "27", -20.01, 100.01, float("nan"), float("inf"))]
        cases += [("uptime_seconds", v, "bat 89% - 27°C") for v in
                  (None, True, "123", -1, float("nan"), float("inf"))]
        cases += [("battery_percent", 0, "bat 0% - 27°C | up 15h10"),
                  ("battery_percent", 100, "bat 100% - 27°C | up 15h10"),
                  ("battery_temp_c", -20, "bat 89% - -20°C | up 15h10"),
                  ("battery_temp_c", 100, "bat 89% - 100°C | up 15h10"),
                  ("uptime_seconds", 0, "bat 89% - 27°C | up 0h00"),
                  ("uptime_seconds", 90061, "bat 89% - 27°C | up 25h01")]
        for key, value, expected in cases:
            with self.subTest(key=key, value=value):
                sample = json.dumps({**base, key: value})
                def read(path, **kw):
                    return MEMINFO if str(path) == "/proc/meminfo" else sample
                with patch.object(Path, "read_text", autospec=True, side_effect=read), patch("time.time", return_value=1000):
                    self.assertEqual(RuntimeStats().footer(("bat", "up")), expected)

    def test_stale_cache_omits_battery_and_device_uptime(self):
        sample = json.dumps({"version": 1, "timestamp": 970, "battery_percent": 89,
                             "battery_temp_c": 27, "uptime_seconds": 54659})
        def read(path, **kw):
            return MEMINFO if str(path) == "/proc/meminfo" else sample
        with patch.object(Path, "read_text", autospec=True, side_effect=read), \
             patch("time.time", side_effect=[1000, 1001, 1001]), \
             patch("time.monotonic", side_effect=[100, 101]):
            stats = RuntimeStats()
            self.assertEqual(stats.footer(("bat", "up")), "bat 89% - 27°C | up 15h10")
            self.assertEqual(stats.footer(("bat", "up")), "")

    def test_leading_zero_and_independent_swap_units(self):
        source = "MemTotal: 524288 kB\nMemAvailable: 262144 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB"
        with patch.object(Path, "read_text", return_value=source):
            self.assertEqual(RuntimeStats().footer(("mem", "swap")), "mem 0.2/0.5G | swap 0/0G")

    def test_disk_failure_is_cached_and_does_not_hide_memory(self):
        with patch.object(Path, "read_text", return_value=MEMINFO), \
             patch("chelus.stats.os.statvfs", side_effect=PermissionError()) as stat, \
             patch("time.monotonic", return_value=100):
            stats = RuntimeStats()
            self.assertEqual(stats.footer(("disk", "mem")), FOOTER)
            self.assertEqual(stats.footer(("disk", "mem")), FOOTER)
            self.assertIn("Disk unavailable", stats.info())
            stat.assert_called_once_with(Path.home())

    def test_footer_omits_redundant_decimal_zeroes(self):
        sample = json.dumps({"version": 1, "timestamp": 1000,
                             "cpu_percent": 25.0, "cpu_temp_c": 31.0})
        def read(path, **kw):
            return MEMINFO if str(path) == "/proc/meminfo" else sample
        with patch.object(Path, "read_text", autospec=True, side_effect=read), \
             patch("time.time", return_value=1000):
            self.assertEqual(RuntimeStats().footer(), "mem 1.2/1.7G | cpu 25% - 31°C")

    def test_optional_privileged_cpu_cache_footer(self):
        sample = json.dumps({"version": 1, "timestamp": 1000, "cpu_percent": 2.1, "cpu_temp_c": 30.3})
        def read(path, **kwargs):
            return MEMINFO if str(path) == "/proc/meminfo" else sample
        with patch.object(Path, "read_text", autospec=True, side_effect=read), patch("time.time", return_value=1005):
            stats = RuntimeStats()
            self.assertEqual(stats.footer(), FOOTER + " | cpu 2.1% - 30°C")
            self.assertIn("privileged", stats.info())
            self.assertIn("type 0/AP", stats.info())

    def test_cpu_cache_validation_and_independent_fields(self):
        base = {"version": 1, "timestamp": 1000, "cpu_percent": 25, "cpu_temp_c": 31}
        cases = [
            ({}, " | cpu 25% - 31°C"),
            ({"timestamp": 970}, " | cpu 25% - 31°C"),
            ({"timestamp": 969.99}, ""),
            ({"timestamp": 1005}, " | cpu 25% - 31°C"),
            ({"timestamp": 1005.01}, ""),
            ({"timestamp": True}, ""), ({"timestamp": "1000"}, ""),
            ({"timestamp": float("inf")}, ""), ({"timestamp": float("nan")}, ""),
            ({"version": True}, ""), ({"version": 1.0}, ""), ({"version": 2}, ""),
            ({"cpu_percent": -0.1}, " | cpu 31°C"),
            ({"cpu_percent": 100.1}, " | cpu 31°C"),
            ({"cpu_percent": True}, " | cpu 31°C"),
            ({"cpu_percent": "25"}, " | cpu 31°C"),
            ({"cpu_percent": float("nan")}, " | cpu 31°C"),
            ({"cpu_percent": float("inf")}, " | cpu 31°C"),
            ({"cpu_temp_c": False}, " | cpu 25%"),
            ({"cpu_temp_c": "31"}, " | cpu 25%"),
            ({"cpu_temp_c": float("inf")}, " | cpu 25%"),
            ({"cpu_temp_c": float("nan")}, " | cpu 25%"),
            ({"cpu_temp_c": -20.01}, " | cpu 25%"),
            ({"cpu_temp_c": 150.01}, " | cpu 25%"),
            ({"cpu_temp_c": -20}, " | cpu 25% - -20°C"),
            ({"cpu_temp_c": 150}, " | cpu 25% - 150°C"),
            ({"cpu_percent": None, "cpu_temp_c": None}, ""),
            ({"cpu_percent": 0, "cpu_temp_c": 0}, " | cpu 0% - 0°C"),
            ({"cpu_percent": 100}, " | cpu 100% - 31°C"),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                sample = json.dumps({**base, **changes})
                def read(path, **kw):
                    return MEMINFO if str(path) == "/proc/meminfo" else sample
                with patch.object(Path, "read_text", autospec=True, side_effect=read), patch("time.time", return_value=1000):
                    self.assertEqual(RuntimeStats().footer(), FOOTER + expected)

    def test_cpu_cache_denied_missing_malformed_are_optional(self):
        for sample in (PermissionError(), FileNotFoundError(), "{", "[]", "null", "42", '{}'):
            with self.subTest(sample=sample):
                def read(path, **kw):
                    if str(path) == "/proc/meminfo":
                        return MEMINFO
                    if isinstance(sample, Exception):
                        raise sample
                    return sample
                with patch.object(Path, "read_text", autospec=True, side_effect=read):
                    stats = RuntimeStats()
                    self.assertEqual(stats.footer(), FOOTER)
                    self.assertIn("CPU unavailable", stats.info())

    def test_cpu_cache_expires_even_inside_five_second_read_cache(self):
        sample = json.dumps({"version": 1, "timestamp": 970, "cpu_percent": 25, "cpu_temp_c": None})
        def read(path, **kw):
            return MEMINFO if str(path) == "/proc/meminfo" else sample
        with patch.object(Path, "read_text", autospec=True, side_effect=read), \
             patch("time.time", side_effect=[1000, 1001, 1001]), \
             patch("time.monotonic", side_effect=[100, 101]):
            stats = RuntimeStats()
            self.assertIn("25%", stats.footer())
            self.assertEqual(stats.footer(), FOOTER)

    def test_sample_cached_for_five_monotonic_seconds(self):
        stats = RuntimeStats()
        with patch.object(Path, "read_text", return_value=MEMINFO) as read, \
             patch("time.monotonic", side_effect=[100, 104.99, 105]):
            self.assertEqual(stats.footer(), FOOTER)
            self.assertEqual(stats.footer(), FOOTER)
            self.assertEqual(read.call_count, 2)  # proc + optional CPU cache
            self.assertEqual(stats.footer(), FOOTER)
            self.assertEqual(read.call_count, 4)

    def test_unavailable_metrics_independent_and_explained(self):
        cases = [
            ("MemTotal: 1024 kB\nMemAvailable: 2048 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB", "", "RAM unavailable"),
            ("MemTotal: 1024 kB\nMemAvailable: 0 kB\nSwapTotal: broken kB", "mem 0/0G", "Swap unavailable"),
            ("MemTotal: " + "9" * 100 + " kB\nMemAvailable: 0 kB", "", "RAM unavailable"),
            ("MemTotal: 1024 kB\nMemTotal: 1024 kB\nMemAvailable: 0 kB", "", "RAM unavailable"),
            ("MemTotal: -1 kB\nMemAvailable: 0 kB", "", "RAM unavailable"),
            ("MemTotal: 1024 MB\nMemAvailable: 0 kB", "", "RAM unavailable"),
            ("", "", "RAM unavailable"),
            (PermissionError("denied"), "", "PermissionError"),
            (FileNotFoundError(), "", "FileNotFoundError"),
        ]
        for source, expected, reason in cases:
            with self.subTest(source=source):
                stats = RuntimeStats()
                kwargs = {"side_effect": source} if isinstance(source, Exception) else {"return_value": source}
                with patch.object(Path, "read_text", **kwargs):
                    self.assertEqual(stats.footer(), expected)
                    self.assertTrue(callable(getattr(stats, "info", None)), "telemetry needs unavailable explanations")
                    self.assertIn(reason, stats.info())


class PreferencesTests(unittest.TestCase):
    def test_legacy_migration_and_field_preferences_survive_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "telegram_stats.json"
            path.write_text('{"1111": false, "2222": true}')
            prefs = StatsPreferences(Path(d))
            self.assertFalse(prefs.enabled(1111))
            self.assertEqual(prefs.fields(1111), ("mem", "cpu", "bat"))
            prefs.update(1111, "add", ["swap", "up", "disk", "swap"])
            prefs.update(1111, "remove", ["cpu"])
            prefs = StatsPreferences(Path(d))
            self.assertFalse(prefs.enabled(1111))
            self.assertEqual(prefs.fields(1111), ("mem", "bat", "swap", "up", "disk"))
            self.assertTrue(prefs.enabled(2222))
            self.assertEqual(prefs.fields(2222), ("mem", "cpu", "bat"))
            prefs.set(1111, True)
            self.assertEqual(prefs.fields(1111), ("mem", "bat", "swap", "up", "disk"))
            before = path.read_bytes()
            for action, fields in (("add", ["swap", "oops"]), ("remove", []), ("reset", ["cpu"]), ("oops", [])):
                with self.assertRaises(ValueError):
                    prefs.update(1111, action, fields)
                self.assertEqual(path.read_bytes(), before)
            prefs.update(1111, "remove", ["mem", "bat", "swap", "up", "disk"])
            self.assertEqual(prefs.fields(1111), ())
            prefs.set(1111, False)
            prefs.update(1111, "reset", [])
            self.assertTrue(prefs.enabled(1111))
            self.assertEqual(prefs.fields(1111), ("mem", "cpu", "bat"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_readding_default_field_keeps_default_order_before_optional_fields(self):
        with tempfile.TemporaryDirectory() as d:
            prefs = StatsPreferences(Path(d))
            prefs.update(1111, "add", ["disk", "up", "swap"])
            prefs.update(1111, "remove", ["cpu", "mem"])
            prefs.update(1111, "add", ["cpu", "mem"])
            self.assertEqual(prefs.fields(1111), ("mem", "cpu", "bat", "disk", "up", "swap"))

    def test_failed_atomic_replace_keeps_disk_memory_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as d:
            prefs = StatsPreferences(Path(d))
            prefs.set(1111, False)
            before = prefs.path.read_bytes()
            with patch("chelus.stats.os.replace", side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    prefs.set(1111, True)
            self.assertFalse(prefs.enabled(1111))
            self.assertEqual(prefs.path.read_bytes(), before)
            self.assertEqual(list(Path(d).iterdir()), [prefs.path])

    def test_malformed_preferences_default_on_and_explained(self):
        with tempfile.TemporaryDirectory() as d:
            for value in ('{', '[]', '{"1111": 0}', '{"../1111": false}',
                          '{"1111": {"enabled": true, "fields": ["mem", "mem"]}}',
                          '{"1111": {"enabled": true, "fields": ["oops"]}}',
                          '{"1111": {"enabled": 1, "fields": []}}',
                          '{"1111": {"enabled": true, "fields": "mem"}}',
                          '{"1111": {"enabled": true, "fields": [{}]}}'):
                with self.subTest(value=value):
                    (Path(d) / "telegram_stats.json").write_text(value)
                    prefs = StatsPreferences(Path(d))
                    self.assertTrue(prefs.enabled(1111))
                    self.assertIn("Preferences unavailable", prefs.error)

    def test_excessively_nested_preferences_fall_back_safely(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "telegram_stats.json").write_text('[' * 2000 + ']' * 2000)
            prefs = StatsPreferences(Path(d))
            self.assertTrue(prefs.enabled(1111))
            self.assertEqual(prefs.fields(1111), ("mem", "cpu", "bat"))
            self.assertIn("Preferences unavailable", prefs.error)

    def test_failed_field_save_or_reset_preserves_selection(self):
        with tempfile.TemporaryDirectory() as d:
            prefs = StatsPreferences(Path(d))
            prefs.update(1111, "add", ["swap"])
            prefs.set(1111, False)
            before = prefs.path.read_bytes()
            with patch("chelus.stats.os.replace", side_effect=PermissionError("denied")):
                for action, fields in (("add", ["disk"]), ("remove", ["mem"]), ("reset", [])):
                    with self.assertRaises(PermissionError):
                        prefs.update(1111, action, fields)
                    self.assertEqual(prefs.fields(1111), ("mem", "cpu", "bat", "swap"))
                    self.assertFalse(prefs.enabled(1111))
                    self.assertEqual(prefs.path.read_bytes(), before)
                    self.assertEqual(list(Path(d).iterdir()), [prefs.path])
