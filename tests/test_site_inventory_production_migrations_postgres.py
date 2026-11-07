"""Production-layout migration validation in an isolated Warehouse-Test schema."""

import os
import unittest
from uuid import uuid4

from dotenv import load_dotenv
from sqlalchemy import create_engine, event, inspect, text

load_dotenv(".env.test", override=False)

import database
from database import APP_ENV, TEST_DATABASE_URL, create_database_engine, test_database_identity
from migrations.site_inventory_foundation import migrate as migrate_foundation
from migrations.site_inventory_rollout_cutover import migrate as migrate_rollout_cutover
from migrations.site_inventory_transfers import TRANSFER_TABLES, migrate as migrate_transfers
from models import AuditLog, Product, Site


EXPECTED_TEST_REF = "fpxxzepcfzbetgijommc"
PRODUCTION_REF = "ndhccmrxpujpqhyewoac"
IDENTITY = test_database_identity(TEST_DATABASE_URL)
if (
    APP_ENV != "test"
    or database.WAREHOUSE_TEST_PROJECT_REF != EXPECTED_TEST_REF
    or database.PRODUCTION_PROJECT_REF != PRODUCTION_REF
    or not IDENTITY["safe"]
    or not IDENTITY["test_ref_match"]
    or IDENTITY["production_ref_detected"]
    or IDENTITY["database"] != "postgres"
    or IDENTITY["port"] != 5432
    or IDENTITY["mode"] != "Supabase Shared Session Pooler"
):
    raise RuntimeError("Production-layout migration test refused: Warehouse-Test identity was not verified")


class ProductionLayoutMigrationPostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        identity = test_database_identity(TEST_DATABASE_URL)
        if not (
            APP_ENV == "test"
            and identity["safe"]
            and identity["test_ref_match"]
            and not identity["production_ref_detected"]
            and identity["database"] == "postgres"
            and identity["port"] == 5432
            and identity["mode"] == "Supabase Shared Session Pooler"
        ):
            raise RuntimeError("PostgreSQL connection refused before Warehouse-Test identity confirmation")

        cls.engine = create_database_engine(TEST_DATABASE_URL)
        cls.app_engine = None
        cls.schema = f"site_inv_prod_layout_{uuid4().hex[:16]}"
        cls.schema_created = False
        cls.allowed_schemas = {cls.schema}

        def reject_writes_outside_test_schema(_connection, _cursor, statement, _params, _context, _executemany):
            command = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
            if command in {"INSERT", "UPDATE", "DELETE", "TRUNCATE", "CREATE", "ALTER", "DROP", "REVOKE", "GRANT", "LOCK"}:
                normalized = statement.casefold()
                if "public." in normalized or not any(name.casefold() in normalized for name in cls.allowed_schemas):
                    raise AssertionError("Migration attempted a write outside its isolated non-public test schema")

        event.listen(cls.engine, "before_cursor_execute", reject_writes_outside_test_schema)

        with cls.engine.connect() as connection:
            cls.tables_before = cls._outside_tables(connection)
        try:
            with cls.engine.begin() as connection:
                connection.exec_driver_sql(f'CREATE SCHEMA "{cls.schema}"')
                cls.schema_created = True
                translated = connection.execution_options(schema_translate_map={None: cls.schema})
                for table in (Site.__table__, Product.__table__, AuditLog.__table__):
                    table.create(bind=translated)
                connection.exec_driver_sql(
                    f'CREATE TABLE "{cls.schema}"."rollout_records" ('
                    "id BIGSERIAL PRIMARY KEY, record_id VARCHAR(80) NOT NULL, "
                    "date VARCHAR(80) NOT NULL DEFAULT '', entry_time VARCHAR(80) NOT NULL DEFAULT '', "
                    "status VARCHAR(32) NOT NULL DEFAULT '', area VARCHAR(200) NOT NULL DEFAULT '')"
                )

            cls.app_engine = create_engine(
                TEST_DATABASE_URL,
                connect_args={"options": f"-c search_path={cls.schema} -c lock_timeout=5000 -c statement_timeout=10000"},
            )
            event.listen(cls.app_engine, "before_cursor_execute", reject_writes_outside_test_schema)
            cls.foundation = migrate_foundation(cls.app_engine)
            cls.rollout_cutover = migrate_rollout_cutover(cls.app_engine)
            cls.transfers = migrate_transfers(cls.app_engine)
            cls.repeat_results = (
                migrate_foundation(cls.app_engine),
                migrate_rollout_cutover(cls.app_engine),
                migrate_transfers(cls.app_engine),
            )
        except Exception:
            if cls.app_engine is not None:
                cls.app_engine.dispose()
            cls._drop_schema_if_created()
            cls.engine.dispose()
            raise

    @classmethod
    def _outside_tables(cls, connection):
        return set(connection.execute(text(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema', :schema)"
        ), {"schema": cls.schema}).all())

    @classmethod
    def _drop_schema_if_created(cls):
        if cls.schema_created:
            with cls.engine.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE')
            cls.schema_created = False

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "engine"):
            return
        if cls.app_engine is not None:
            cls.app_engine.dispose()
        cls._drop_schema_if_created()
        with cls.engine.connect() as connection:
            remaining = connection.execute(text(
                "SELECT 1 FROM pg_namespace WHERE nspname=:schema"
            ), {"schema": cls.schema}).first()
            outside_after = cls._outside_tables(connection)
        cls.engine.dispose()
        if remaining:
            raise AssertionError("Temporary production-layout schema was not cleaned up")
        if outside_after != cls.tables_before:
            raise AssertionError("Migration changed tables outside its temporary schema")

    def test_migrations_are_additive_versioned_and_rerunnable_in_target_schema(self):
        self.assertEqual(self.foundation["schema"], self.schema)
        self.assertEqual(self.rollout_cutover["schema"], self.schema)
        self.assertEqual(self.transfers["schema"], self.schema)
        self.assertTrue(all(result["already_applied"] for result in self.repeat_results))

        inspector = inspect(self.engine)
        tables = set(inspector.get_table_names(schema=self.schema))
        self.assertTrue(set(TRANSFER_TABLES).issubset(tables))
        self.assertTrue({
            "site_aliases", "site_balances", "site_material_movements",
            "site_opening_balance_requests", "site_inventory_schema_versions",
            "rollout_records",
        }.issubset(tables))
        columns = {column["name"]: column for column in inspector.get_columns(
            "rollout_records", schema=self.schema,
        )}
        stamp = columns["site_inventory_recorded_at"]
        self.assertTrue(stamp["nullable"])
        self.assertTrue(getattr(stamp["type"], "timezone", False))
        with self.engine.connect() as connection:
            versions = set(connection.execute(text(
                f'SELECT version FROM "{self.schema}".site_inventory_schema_versions'
            )).scalars())
            outside_after = self._outside_tables(connection)
        self.assertTrue({
            "site-inventory-foundation-v1",
            "site-inventory-rollout-cutover-v1",
            "site-inventory-transfers-v1",
        }.issubset(versions))
        self.assertEqual(outside_after, self.tables_before)

    def test_legacy_rollout_rows_remain_unstamped_and_ineligible_by_default(self):
        with self.engine.begin() as connection:
            connection.execute(text(
                f'INSERT INTO "{self.schema}"."rollout_records" (record_id, date, entry_time, status, area) '
                "VALUES ('LEGACY-ROW', '2026-11-02', '2026-11-02 09:00:00', 'Done', 'Synthetic Site')"
            ))
            stamp = connection.execute(text(
                f'SELECT site_inventory_recorded_at FROM "{self.schema}"."rollout_records" '
                "WHERE record_id='LEGACY-ROW'"
            )).scalar_one()
        self.assertIsNone(stamp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
