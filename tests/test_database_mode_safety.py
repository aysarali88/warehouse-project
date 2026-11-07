import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ENV_NAMES = (
    "DATABASE_URL",
    "FTTH_DATABASE_URL",
    "SR_DATABASE_URL",
)
RENDER_ENV_NAMES = (
    "RENDER",
    "RENDER_SERVICE_ID",
    "RENDER_INSTANCE_ID",
    "RENDER_SERVICE_NAME",
    "RENDER_EXTERNAL_URL",
    "RENDER_EXTERNAL_HOSTNAME",
    "RENDER_GIT_COMMIT",
)
TEST_URL = (
    "postgresql+psycopg://postgres.fpxxzepcfzbetgijommc:"
    "DO_NOT_LEAK_TEST_PASSWORD@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"
)


class DatabaseModeSafetyTests(unittest.TestCase):
    def run_database_import(self, values):
        env = os.environ.copy()
        for name in (*PRODUCTION_ENV_NAMES, "APP_ENV", "TEST_DATABASE_URL", *RENDER_ENV_NAMES):
            env.pop(name, None)
        env.update(values)
        env["PYTHONPATH"] = str(ROOT)
        return subprocess.run(
            [sys.executable, "-c", "import database; print('DATABASE_MODE_IMPORT_OK')"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_render_runtime_fails_closed_in_test_mode_without_leaking_credentials(self):
        for indicator in RENDER_ENV_NAMES:
            with self.subTest(indicator=indicator):
                result = self.run_database_import({
                    "APP_ENV": "test",
                    "TEST_DATABASE_URL": TEST_URL,
                    "DATABASE_URL": "sqlite:///production-fallback.db",
                    indicator: "true" if indicator == "RENDER" else "srv-test",
                })
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("APP_ENV=test is forbidden on Render", output)
                self.assertNotIn("DO_NOT_LEAK_TEST_PASSWORD", output)
                self.assertNotIn("aws-0-eu-west-1.pooler.supabase.com", output)

    def test_local_test_mode_uses_only_test_database_url(self):
        code = (
            "import database, os; "
            "assert database.DATABASE_URL == database.normalize_database_url(os.environ['TEST_DATABASE_URL']); "
            "assert database.SR_DATABASE_URL == ''; "
            "print('TEST_DATABASE_SELECTED')"
        )
        env_values = {
            "APP_ENV": "test",
            "TEST_DATABASE_URL": TEST_URL,
            "FTTH_DATABASE_URL": "sqlite:///production-ftth.db",
            "DATABASE_URL": "sqlite:///production-fallback.db",
            "SR_DATABASE_URL": "sqlite:///production-sr.db",
        }
        env = os.environ.copy()
        for name in (*PRODUCTION_ENV_NAMES, "APP_ENV", "TEST_DATABASE_URL", *RENDER_ENV_NAMES):
            env.pop(name, None)
        env.update(env_values)
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TEST_DATABASE_SELECTED", result.stdout)
        self.assertNotIn("DO_NOT_LEAK_TEST_PASSWORD", result.stdout + result.stderr)

    def test_non_test_mode_preserves_production_database_precedence(self):
        code = (
            "import database, os; "
            "assert database.DATABASE_URL == database.normalize_database_url(os.environ['FTTH_DATABASE_URL']); "
            "assert database.SR_DATABASE_URL == database.normalize_database_url(os.environ['SR_DATABASE_URL']); "
            "print('PRODUCTION_PRECEDENCE_PRESERVED')"
        )
        env = os.environ.copy()
        for name in (*PRODUCTION_ENV_NAMES, "APP_ENV", "TEST_DATABASE_URL", *RENDER_ENV_NAMES):
            env.pop(name, None)
        env.update({
            "APP_ENV": "production",
            "TEST_DATABASE_URL": TEST_URL,
            "FTTH_DATABASE_URL": "sqlite:///production-ftth.db",
            "DATABASE_URL": "sqlite:///production-fallback.db",
            "SR_DATABASE_URL": "sqlite:///production-sr.db",
        })
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PRODUCTION_PRECEDENCE_PRESERVED", result.stdout)
        self.assertNotIn("DO_NOT_LEAK_TEST_PASSWORD", result.stdout + result.stderr)

    def test_non_test_mode_preserves_database_url_fallback(self):
        code = (
            "import database, os; "
            "assert database.DATABASE_URL == database.normalize_database_url(os.environ['DATABASE_URL']); "
            "print('DATABASE_URL_FALLBACK_PRESERVED')"
        )
        env = os.environ.copy()
        for name in (*PRODUCTION_ENV_NAMES, "APP_ENV", "TEST_DATABASE_URL", *RENDER_ENV_NAMES):
            env.pop(name, None)
        env.update({
            "APP_ENV": "production",
            "TEST_DATABASE_URL": TEST_URL,
            "DATABASE_URL": "sqlite:///production-fallback.db",
        })
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DATABASE_URL_FALLBACK_PRESERVED", result.stdout)
        self.assertNotIn("DO_NOT_LEAK_TEST_PASSWORD", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
