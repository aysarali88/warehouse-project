import os
import tempfile
import unittest
from pathlib import Path

TEST_DB_PATH = Path(tempfile.gettempdir()) / f"warehouse-rollout-tests-{os.getpid()}.db"
os.environ["FTTH_DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["SESSION_SECRET"] = "isolated-test-session-secret-at-least-thirty-two-characters-long"

from scripts.validate_public_rls import application_tables, load_exceptions, validate_exception_reasons


class RlsValidationTests(unittest.TestCase):
    def test_application_tables_come_from_sqlalchemy_models(self):
        tables = application_tables()
        self.assertIn("app_users", tables)
        self.assertIn("rollout_records", tables)
        self.assertIn("material_requisitions", tables)

    def test_documented_exceptions_require_application_table_and_reason(self):
        tables = {"app_users"}
        errors = validate_exception_reasons({"app_users": "", "unknown_table": "legacy"}, tables)
        self.assertIn("Exception must include a reason: app_users", errors)
        self.assertIn("Exception table is not application-owned: unknown_table", errors)

    def test_exception_file_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "exceptions.json"
            path.write_text('{"public_rls_disabled_exceptions":{"app_users":"legacy review"}}', encoding="utf-8")
            self.assertEqual(load_exceptions(path), {"app_users": "legacy review"})


if __name__ == "__main__":
    unittest.main()
