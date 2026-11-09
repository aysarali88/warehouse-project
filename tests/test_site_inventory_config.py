import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from site_inventory_config import (
    feature_flag_enabled,
    integration_enabled,
    parse_cutover_timestamp,
    cutover_programs,
    site_inventory_readiness,
    validate_rollout_cutover_eligibility,
)


def normalize_program(value):
    return str(value or "").strip().upper()


class SiteInventoryConfigurationTests(unittest.TestCase):
    def test_all_integrations_are_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(feature_flag_enabled("SITE_INVENTORY_MR_ENABLED"))
            self.assertFalse(feature_flag_enabled("SITE_INVENTORY_RETURN_ENABLED"))
            self.assertFalse(feature_flag_enabled("SITE_INVENTORY_ROLLOUT_ENABLED"))
            self.assertFalse(feature_flag_enabled("SITE_INVENTORY_TRANSFER_ENABLED"))
            self.assertFalse(integration_enabled("SITE_INVENTORY_MR_ENABLED", "FTTH", normalize_program))
            status = site_inventory_readiness("FTTH", normalize_program)
        self.assertFalse(status["configuration_ready"])
        self.assertFalse(status["cutover_program_enabled"])
        self.assertEqual(cutover_programs(normalize_program), set())
        self.assertIsNone(parse_cutover_timestamp())
        self.assertEqual(status["cutover_timestamp_status"], "missing")
        self.assertTrue(all(not state["enabled"] for state in status["integrations"].values()))

    def test_each_integration_requires_its_own_flag_and_cutover_program(self):
        env = {
            "SITE_INVENTORY_CUTOVER_PROGRAMS": " ftth ",
            "SITE_INVENTORY_MR_ENABLED": "true",
            "SITE_INVENTORY_RETURN_ENABLED": "1",
            "SITE_INVENTORY_ROLLOUT_ENABLED": "on",
            "SITE_INVENTORY_TRANSFER_ENABLED": "yes",
            "SITE_INVENTORY_CUTOVER_AT": "2026-11-01T00:00:00+02:00",
        }
        with patch.dict(os.environ, env, clear=True):
            status = site_inventory_readiness("FTTH", normalize_program)
            self.assertTrue(integration_enabled("SITE_INVENTORY_MR_ENABLED", "FTTH", normalize_program))
            self.assertFalse(integration_enabled("SITE_INVENTORY_MR_ENABLED", "SINGLE_RAN", normalize_program))
        self.assertTrue(status["configuration_ready"])
        self.assertTrue(all(state["enabled"] for state in status["integrations"].values()))

    def test_unknown_cutover_program_cannot_normalize_into_ftth(self):
        with patch.dict(os.environ, {
            "SITE_INVENTORY_CUTOVER_PROGRAMS": "unrecognized-program",
            "SITE_INVENTORY_MR_ENABLED": "true",
        }, clear=True):
            self.assertFalse(integration_enabled("SITE_INVENTORY_MR_ENABLED", "FTTH", normalize_program))

    def test_invalid_or_timezone_naive_cutover_is_not_ready(self):
        for cutoff in ("not-a-date", "2026-11-01T00:00:00"):
            with self.subTest(cutoff=cutoff), patch.dict(os.environ, {
                "SITE_INVENTORY_CUTOVER_PROGRAMS": "FTTH",
                "SITE_INVENTORY_ROLLOUT_ENABLED": "true",
                "SITE_INVENTORY_CUTOVER_AT": cutoff,
            }, clear=True):
                status = site_inventory_readiness("FTTH", normalize_program)
                self.assertEqual(status["cutover_timestamp_status"], "invalid")
                self.assertFalse(status["configuration_ready"])
                self.assertFalse(status["integrations"]["rollout"]["enabled"])

    def test_rollout_cutover_rejects_pre_cutover_and_unverifiable_rows(self):
        cutoff = parse_cutover_timestamp("2026-11-01T12:00:00+02:00")
        eligible = SimpleNamespace(
            site_inventory_recorded_at=datetime(2026, 11, 2, 10, tzinfo=timezone.utc),
            date="2026-11-02",
            entry_time="2026-11-02 11:00:00",
        )
        validate_rollout_cutover_eligibility(eligible, cutoff)

        rejected_rows = [
            SimpleNamespace(
                site_inventory_recorded_at=datetime(2026, 11, 2, 10, tzinfo=timezone.utc),
                date="2026-10-31", entry_time="2026-10-31 11:00:00",
            ),
            SimpleNamespace(site_inventory_recorded_at=None, date="2026-11-02", entry_time="2026-11-02 11:00:00"),
            SimpleNamespace(
                site_inventory_recorded_at=datetime(2026, 11, 2, 10, tzinfo=timezone.utc),
                date="08/11/2026", entry_time="2026-11-08 11:00:00",
            ),
            SimpleNamespace(
                site_inventory_recorded_at=datetime(2026, 11, 2, 10, tzinfo=timezone.utc),
                date="2026-11-01", entry_time="",
            ),
        ]
        for row in rejected_rows:
            with self.subTest(row=row), self.assertRaises(ValueError):
                validate_rollout_cutover_eligibility(row, cutoff)

    def test_timezone_aware_cutover_is_required(self):
        self.assertIsNone(parse_cutover_timestamp(""))
        with self.assertRaises(ValueError):
            parse_cutover_timestamp("2026-11-01T00:00:00")


if __name__ == "__main__":
    unittest.main()
