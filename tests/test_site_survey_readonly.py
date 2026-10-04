import unittest
from unittest.mock import patch

import site_survey_readonly as survey


class SiteSurveyReadOnlyTests(unittest.TestCase):
    def test_missing_connection_returns_unavailable_without_connecting(self):
        with patch.dict("os.environ", {"SITE_SURVEY_READONLY_DATABASE_URL": ""}):
            result = survey.installed_pole_counts()
        self.assertFalse(result["available"])
        self.assertIsNone(result["total"])
        self.assertIsNone(result["by_city"]["Misurata"])

    def test_city_names_are_normalized_to_dashboard_labels(self):
        self.assertEqual(survey.canonical_city("Misrata"), "Misurata")
        self.assertEqual(survey.canonical_city("Tripoli"), "Tripoli")
        self.assertEqual(survey.canonical_city("Benghazi"), "")

    def test_manual_tripoli_count_is_temporary_until_live_count_catches_up(self):
        result = {
            "available": True,
            "total": 393,
            "by_city": {"Misurata": 329, "Tripoli": 64},
        }
        updated = survey.apply_tripoli_installed_override(result, 571)
        self.assertEqual(updated["by_city"], {"Misurata": 329, "Tripoli": 571})
        self.assertEqual(updated["total"], 900)
        self.assertTrue(updated["manual_override"]["applied"])
        self.assertEqual(result["by_city"]["Tripoli"], 64)

        caught_up = survey.apply_tripoli_installed_override(
            {"available": True, "total": 900, "by_city": {"Misurata": 329, "Tripoli": 571}}, 571
        )
        self.assertEqual(caught_up["by_city"]["Tripoli"], 571)
        self.assertFalse(caught_up["manual_override"]["applied"])

    def test_disabled_override_uses_live_count(self):
        result = survey.apply_tripoli_installed_override(
            {"available": True, "total": 393, "by_city": {"Misurata": 329, "Tripoli": 64}},
            571,
            enabled=False,
        )
        self.assertEqual(result["by_city"]["Tripoli"], 64)
        self.assertEqual(result["total"], 393)
        self.assertFalse(result["manual_override"]["applied"])


if __name__ == "__main__":
    unittest.main()
