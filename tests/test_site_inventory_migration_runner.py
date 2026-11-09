"""Unit coverage for the guarded Production migration runner."""

import contextlib
import io
import re
import unittest
from pathlib import Path

from migrations.run_site_inventory_production import (
    FOUNDATION_TABLE_SET,
    MIGRATION_ORDER,
    PRODUCTION_PROJECT_REF,
    TRANSFER_TABLE_SET,
    WAREHOUSE_TEST_PROJECT_REF,
    MigrationPreflightError,
    execute_migrations,
    production_url_identity,
    resolve_explicit_database_url,
    validate_migration_state,
    validate_schema_prerequisites,
    validate_target_identity,
    main,
)


def production_url(*, host=None, username=None, database="postgres", port=5432):
    host = host or "aws-0-eu-west-1.pooler.supabase.com"
    username = username or f"postgres.{PRODUCTION_PROJECT_REF}"
    return f"postgresql://{username}@{host}:{port}/{database}"


class ProductionMigrationRunnerTests(unittest.TestCase):
    def test_accepts_only_the_expected_production_database_identity(self):
        identity = production_url_identity(production_url())
        self.assertTrue(identity["project_ref_match"])
        self.assertTrue(identity["production_project_ref_detected"])
        self.assertFalse(identity["test_project_ref_detected"])
        self.assertEqual(identity["database"], "postgres")

    def test_identity_report_never_contains_database_credentials(self):
        identity = production_url_identity(
            f"postgresql://postgres.{PRODUCTION_PROJECT_REF}:synthetic-secret@"
            "aws-0-eu-west-1.pooler.supabase.com:5432/postgres"
        )
        self.assertNotIn("synthetic-secret", repr(identity))
        self.assertNotIn("password", identity)

    def test_rejects_warehouse_test_and_other_project_identities(self):
        unsafe_urls = (
            production_url(username=f"postgres.{WAREHOUSE_TEST_PROJECT_REF}"),
            production_url(host="aws-0-eu-west-1.pooler.supabase.com", username="postgres.otherproject"),
            production_url(host=f"db.{WAREHOUSE_TEST_PROJECT_REF}.supabase.co", username="postgres"),
        )
        for value in unsafe_urls:
            with self.subTest(value=value), self.assertRaises(MigrationPreflightError):
                production_url_identity(value)

    def test_rejects_wrong_database_and_unsupported_connection_shapes(self):
        for value in (
            production_url(database="warehouse"),
            production_url(host=f"db.{PRODUCTION_PROJECT_REF}.supabase.co", username="postgres", port=6543),
            "sqlite:///warehouse.db",
        ):
            with self.subTest(value=value), self.assertRaises(MigrationPreflightError):
                production_url_identity(value)

    def test_database_url_must_be_explicitly_selected_and_never_test_database_url(self):
        env = {"DATABASE_URL": production_url(), "TEST_DATABASE_URL": production_url()}
        self.assertEqual(resolve_explicit_database_url("DATABASE_URL", env), env["DATABASE_URL"])
        for name in ("TEST_DATABASE_URL", "UNKNOWN_DATABASE_URL"):
            with self.subTest(name=name), self.assertRaises(MigrationPreflightError):
                resolve_explicit_database_url(name, env)
        with self.assertRaises(MigrationPreflightError):
            resolve_explicit_database_url("FTTH_DATABASE_URL", {})

    def test_target_database_and_schema_must_match_explicit_target(self):
        validate_target_identity("postgres", "public", "public")
        for args in (
            ("warehouse", "public", "public"),
            ("postgres", "other", "public"),
            ("postgres", "public", "bad;drop"),
        ):
            with self.subTest(args=args), self.assertRaises(MigrationPreflightError):
                validate_target_identity(*args)

    def test_missing_prerequisite_tables_or_columns_fail_closed(self):
        with self.assertRaises(MigrationPreflightError):
            validate_schema_prerequisites({})
        with self.assertRaises(MigrationPreflightError):
            validate_schema_prerequisites({
                "sites": {"id", "program", "name"},
                "products": {"id", "program"},
                "audit_logs": {"id", "action", "entity_type", "entity_id", "actor", "details", "created_at"},
                "rollout_records": {"id", "record_id", "date", "entry_time", "status", "area"},
            })

    def test_clean_and_valid_version_prefixes_are_accepted(self):
        self.assertEqual(validate_migration_state(
            version_table_exists=False,
            version_columns=None,
            version_primary_key=None,
            recorded_versions=[],
            existing_managed_tables=set(),
            has_rollout_cutover_column=False,
        ), "clean")

        for count in range(1, len(MIGRATION_ORDER) + 1):
            with self.subTest(count=count):
                state = validate_migration_state(
                    version_table_exists=True,
                    version_columns={"version", "applied_at"},
                    version_primary_key={"version"},
                    recorded_versions=list(MIGRATION_ORDER[:count]),
                    existing_managed_tables=(
                        (set(FOUNDATION_TABLE_SET) if count >= 1 else set())
                        | (set(TRANSFER_TABLE_SET) if count >= 3 else set())
                    ),
                    has_rollout_cutover_column=count >= 2,
                )
                expected = "complete" if count == len(MIGRATION_ORDER) else f"prefix-{count}-of-{len(MIGRATION_ORDER)}"
                self.assertEqual(state, expected)

    def test_partial_unknown_and_mismatched_migration_states_are_rejected(self):
        invalid_states = (
            dict(version_table_exists=False, version_columns=None, version_primary_key=None,
                 recorded_versions=[], existing_managed_tables={"site_balances"}, has_rollout_cutover_column=False),
            dict(version_table_exists=False, version_columns=None, version_primary_key=None,
                 recorded_versions=[], existing_managed_tables=set(), has_rollout_cutover_column=True),
            dict(version_table_exists=True, version_columns={"version", "applied_at"}, version_primary_key={"version"},
                 recorded_versions=[], existing_managed_tables=set(), has_rollout_cutover_column=False),
            dict(version_table_exists=True, version_columns={"version", "applied_at"}, version_primary_key={"version"},
                 recorded_versions=[MIGRATION_ORDER[1]], existing_managed_tables=set(), has_rollout_cutover_column=False),
            dict(version_table_exists=True, version_columns={"version", "applied_at"}, version_primary_key={"version"},
                 recorded_versions=list(MIGRATION_ORDER[:1]), existing_managed_tables=set(FOUNDATION_TABLE_SET - {"site_balances"}),
                 has_rollout_cutover_column=False),
            dict(version_table_exists=True, version_columns={"version", "applied_at"}, version_primary_key={"version"},
                 recorded_versions=list(MIGRATION_ORDER[:1]), existing_managed_tables=set(FOUNDATION_TABLE_SET),
                 has_rollout_cutover_column=True),
            dict(version_table_exists=True, version_columns={"version", "applied_at", "extra"}, version_primary_key={"version"},
                 recorded_versions=list(MIGRATION_ORDER[:1]), existing_managed_tables=set(FOUNDATION_TABLE_SET),
                 has_rollout_cutover_column=False),
        )
        for kwargs in invalid_states:
            with self.subTest(kwargs=kwargs), self.assertRaises(MigrationPreflightError):
                validate_migration_state(**kwargs)

    def test_dry_run_never_calls_migration_functions(self):
        calls = []

        def migrate(_engine, schema=None):
            calls.append(schema)
            return {"version": MIGRATION_ORDER[0]}

        result = execute_migrations(
            object(), "isolated_schema", execute=False,
            migration_specs=((MIGRATION_ORDER[0], migrate),),
        )
        self.assertEqual(result, [])
        self.assertEqual(calls, [])

    def test_execution_stops_at_first_migration_failure(self):
        calls = []

        def first(_engine, schema=None):
            calls.append(("first", schema))
            return {"version": MIGRATION_ORDER[0]}

        def second(_engine, schema=None):
            calls.append(("second", schema))
            raise RuntimeError("synthetic failure")

        def third(_engine, schema=None):
            calls.append(("third", schema))
            return {"version": MIGRATION_ORDER[2]}

        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            execute_migrations(object(), "isolated_schema", execute=True, migration_specs=(
                (MIGRATION_ORDER[0], first), (MIGRATION_ORDER[1], second), (MIGRATION_ORDER[2], third),
            ))
        self.assertEqual(calls, [("first", "isolated_schema"), ("second", "isolated_schema")])

    def test_cli_refuses_test_environment_before_opening_any_connection(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = main(
                ["--database-url-env", "DATABASE_URL", "--schema", "public", "--execute"],
                environ={"APP_ENV": "test", "DATABASE_URL": "not-a-database-url"},
            )
        self.assertEqual(result, 2)
        self.assertIn("APP_ENV must equal production", stderr.getvalue())

    def test_migrations_are_additive_and_do_not_backfill_rollout_history(self):
        root = Path(__file__).resolve().parents[1]
        paths = (
            root / "migrations" / "site_inventory_foundation.py",
            root / "migrations" / "site_inventory_rollout_cutover.py",
            root / "migrations" / "site_inventory_transfers.py",
        )
        destructive = re.compile(r"\bDROP\s+(?:TABLE|COLUMN)\b|\bDELETE\s+FROM\b|\bUPDATE\s+rollout_records\b", re.IGNORECASE)
        for path in paths:
            with self.subTest(path=path.name):
                self.assertIsNone(destructive.search(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
