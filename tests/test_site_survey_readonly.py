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


if __name__ == "__main__":
    unittest.main()
