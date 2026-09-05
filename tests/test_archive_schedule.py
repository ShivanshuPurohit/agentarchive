"""Schedule behavior preserved from the original archive test suite."""

import inspect
import pathlib
import subprocess
import unittest
from unittest import mock
import agent_archive as aa
from agentarchive import catalog, common, reports, schedule


class ArchiveScheduleTests(unittest.TestCase):
    def test_default_schedule_allows_battery_but_defers_low_power_mode(self):
        config = aa.default_config(pathlib.Path("/tmp/agentarchive-test"))
        self.assertFalse(config["require_ac_power"])
        self.assertTrue(config["verify_require_ac_power"])
        self.assertTrue(config["skip_low_power_mode"])

    def test_scheduled_staging_does_not_read_downloads_directly(self):
        source = inspect.getsource(aa.stage_sources)
        self.assertNotIn("Downloads", source)
        scheduled = inspect.getsource(aa.run_scheduled)
        self.assertIn("refresh_legacy_documents=False", scheduled)

    def test_scheduled_work_is_power_gated(self):
        config = {"require_ac_power": True, "skip_low_power_mode": True}

        battery = subprocess.CompletedProcess([], 0, "Now drawing from 'Battery Power'\n", "")
        with mock.patch.object(common, "run", return_value=battery) as mocked:
            eligible, reason = aa.power_eligible(config)
        self.assertFalse(eligible)
        self.assertIn("battery", reason)
        self.assertEqual(mocked.call_count, 1)

        ac = subprocess.CompletedProcess([], 0, "Now drawing from 'AC Power'\n", "")
        low_power = subprocess.CompletedProcess([], 0, " powermode 1\n", "")
        with mock.patch.object(common, "run", side_effect=[ac, low_power]):
            eligible, reason = aa.power_eligible(config)
        self.assertFalse(eligible)
        self.assertIn("Low Power Mode", reason)

        normal = subprocess.CompletedProcess([], 0, " powermode 0\n", "")
        with mock.patch.object(common, "run", side_effect=[ac, normal]):
            eligible, reason = aa.power_eligible(config)
        self.assertTrue(eligible)
        self.assertIn("AC power", reason)

        battery_allowed = {
            "require_ac_power": False,
            "skip_low_power_mode": True,
        }
        with mock.patch.object(common, "run", side_effect=[battery, normal]):
            eligible, reason = aa.power_eligible(battery_allowed)
        self.assertTrue(eligible)
        self.assertIn("battery power permitted", reason)

    def test_weekly_verification_has_an_independent_ac_power_gate(self):
        config = aa.default_config(pathlib.Path("/tmp/agentarchive-test"))
        with (
            mock.patch.object(
                schedule,
                "power_eligible",
                return_value=(False, "deferred: Mac is running on battery"),
            ) as eligible,
            mock.patch.object(catalog, "connect_catalog") as connect,
            mock.patch.object(catalog, "record_health"),
            mock.patch.object(reports, "render_status"),
        ):
            result = aa.run_verify_scheduled(config)
        eligible.assert_called_once_with(config, require_ac_power=True)
        connect.return_value.close.assert_called_once_with()
        self.assertEqual(result["status"], "deferred")
