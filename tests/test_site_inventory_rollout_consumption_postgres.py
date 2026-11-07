"""Rollout consumption PostgreSQL validation in an isolated Warehouse-Test schema."""

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv(".env.test", override=False)

import database
from database import APP_ENV, TEST_DATABASE_URL, create_database_engine, test_database_identity as _test_database_identity

EXPECTED_TEST_REF = "fpxxzepcfzbetgijommc"
PRODUCTION_REF = "ndhccmrxpujpqhyewoac"
IDENTITY = _test_database_identity(TEST_DATABASE_URL)
if (
    APP_ENV != "test"
    or not IDENTITY["safe"]
    or not IDENTITY["test_ref_match"]
    or IDENTITY["production_ref_detected"]
    or IDENTITY["database"] != "postgres"
    or IDENTITY["port"] != 5432
    or IDENTITY["mode"] != "Supabase Shared Session Pooler"
    or database.WAREHOUSE_TEST_PROJECT_REF != EXPECTED_TEST_REF
    or database.PRODUCTION_PROJECT_REF != PRODUCTION_REF
):
    raise RuntimeError("PostgreSQL rollout-consumption validation refused: Warehouse-Test identity was not verified")

from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from database import Base
from migrations.site_inventory_foundation import migrate
from models import AuditLog, Product, RolloutRecord, Site, SiteBalance, SiteMaterialMovement
import site_inventory_service as inventory


class _EmptyQuery:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: self

    def all(self):
        return []

    def first(self):
        return None

    def count(self):
        return 0

    def __iter__(self):
        return iter(())


class _NoopSession:
    def query(self, *_args, **_kwargs):
        return _EmptyQuery()

    def add(self, *_args, **_kwargs):
        pass

    def add_all(self, *_args, **_kwargs):
        pass

    def flush(self, *_args, **_kwargs):
        pass

    def commit(self, *_args, **_kwargs):
        pass

    def rollback(self, *_args, **_kwargs):
        pass

    def close(self):
        pass


class RolloutConsumptionPostgreSQLValidation(TestCase):
    @classmethod
    def setUpClass(cls):
        identity = _test_database_identity(TEST_DATABASE_URL)
        if not (
            APP_ENV == "test"
            and identity["safe"]
            and identity["test_ref_match"]
            and not identity["production_ref_detected"]
            and identity["database"] == "postgres"
            and identity["port"] == 5432
            and identity["mode"] == "Supabase Shared Session Pooler"
        ):
            raise RuntimeError("Refusing PostgreSQL connection because Warehouse-Test identity failed")

        cls.engine = create_database_engine(TEST_DATABASE_URL)
        cls.schema = f"rollout_consume_test_{uuid4().hex[:18]}"
        cls.translated_engine = cls.engine.execution_options(schema_translate_map={None: cls.schema})
        cls.Session = sessionmaker(bind=cls.translated_engine, expire_on_commit=False)
        cls.schema_created = False
        cls.allowed_schemas = {cls.schema}

        @event.listens_for(cls.engine, "before_cursor_execute")
        def _reject_writes_outside_schema(_connection, _cursor, statement, _parameters, _context, _executemany):
            command = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
            if command in {"INSERT", "UPDATE", "DELETE", "TRUNCATE", "CREATE", "ALTER", "DROP", "REVOKE", "GRANT", "LOCK"}:
                lowered = statement.casefold()
                if "public." in lowered or not any(schema.casefold() in lowered for schema in cls.allowed_schemas):
                    raise AssertionError("PostgreSQL validation attempted a write outside its temporary schema")

        with cls.engine.connect() as connection:
            cls.external_tables_before = cls._external_table_names(connection)
        try:
            with cls.engine.begin() as connection:
                connection.exec_driver_sql(f'CREATE SCHEMA "{cls.schema}"')
                cls.schema_created = True

            with cls.translated_engine.begin() as connection:
                for table in (Site.__table__, Product.__table__, RolloutRecord.__table__, AuditLog.__table__):
                    table.create(bind=connection)
            cls.migration_result = migrate(cls.engine, cls.schema)

            cls.database_originals = {
                name: getattr(database, name)
                for name in ("engine", "SessionLocal", "SRSessionLocal", "sr_engine", "all_engines", "all_sessionmakers", "get_sessionmaker")
            }
            database.engine = cls.translated_engine
            database.SessionLocal = _NoopSession
            database.SRSessionLocal = None
            database.sr_engine = None
            database.all_engines = lambda: []
            database.all_sessionmakers = lambda: []
            database.get_sessionmaker = lambda _program="FTTH": _NoopSession
            spec = importlib.util.spec_from_file_location(
                f"_rollout_consumption_test_main_{uuid4().hex}", os.path.join(os.getcwd(), "main.py")
            )
            cls.main_module = importlib.util.module_from_spec(spec)
            previous_session_secret = os.environ.get("SESSION_SECRET")
            os.environ["SESSION_SECRET"] = "postgres-rollout-consumption-test-secret-not-a-credential"
            try:
                spec.loader.exec_module(cls.main_module)
            finally:
                if previous_session_secret is None:
                    os.environ.pop("SESSION_SECRET", None)
                else:
                    os.environ["SESSION_SECRET"] = previous_session_secret
            for name, value in cls.database_originals.items():
                setattr(database, name, value)
        except Exception:
            if hasattr(cls, "database_originals"):
                for name, value in cls.database_originals.items():
                    setattr(database, name, value)
            if cls.schema_created:
                with cls.engine.begin() as connection:
                    connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE')
                cls.schema_created = False
            cls.engine.dispose()
            raise

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "engine"):
            return
        if cls.schema_created:
            with cls.engine.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE')
            cls.schema_created = False
        with cls.engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_namespace WHERE nspname=:schema"), {"schema": cls.schema}
            ).first()
            external_after = cls._external_table_names(connection)
        if exists:
            raise AssertionError("Temporary rollout-consumption schema was not removed")
        if external_after != cls.external_tables_before:
            raise AssertionError("PostgreSQL objects outside the temporary schema changed")
        cls.engine.dispose()

    @classmethod
    def _external_table_names(cls, connection):
        return set(connection.execute(text(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema', :schema)"
        ), {"schema": cls.schema}).all())

    def make_fixture(self, *, opening=50, unit="M", status="Done", area=None):
        suffix = uuid4().hex[:12]
        site_name = area or f"PG Rollout Consumption Site {suffix}"
        with self.Session() as db:
            with db.begin():
                site = Site(program="FTTH", name=site_name)
                product = Product(
                    program="FTTH", sku=f"PG-ROLLOUT-CONSUME-{suffix}",
                    name=f"PG rollout material {suffix}", unit=unit,
                )
                db.add_all([site, product])
                db.flush()
                inventory.post_site_movement(
                    db,
                    program="FTTH", site_id=site.id, product_id=product.id,
                    movement_type="incoming", event_type="test_site_opening",
                    quantity_delta=opening, uom=unit, document_type="test_fixture",
                    document_id=suffix, document_line_id="opening", reference=f"fixture-{suffix}",
                    note="Synthetic Warehouse-Test opening", created_by="test", approved_by="test",
                    effective_date="2026-11-07",
                )
                row = RolloutRecord(
                    record_id=f"RDP-PG-CONSUME-{suffix}", area=site_name, city="Misurata",
                    item="Cable", material_type="Single-Core Distribution Cable_50m",
                    actual=11, status=status, related_to_xbox="X1",
                )
                db.add(row)
                db.flush()
                return {"site_id": site.id, "site_name": site_name, "product_id": product.id,
                        "record_id": row.record_id, "record_pk": row.id, "suffix": suffix}

    def request(self, role="Admin"):
        return SimpleNamespace(state=SimpleNamespace(current_user=SimpleNamespace(
            role=role, name=f"PG {role} actor", username=f"pg-{role.lower()}", warehouse_name="",
        ), program="FTTH"))

    def confirm(self, fixture, *, quantity="6", role="Admin"):
        with self.Session() as db:
            return self.main_module.confirm_rollout_field_entry_consumption(
                fixture["record_id"],
                self.main_module.RolloutConsumptionConfirmationIn(
                    product_id=fixture["product_id"], confirmed_consumed_quantity=Decimal(quantity),
                    uom="M", effective_date="2026-11-07",
                ),
                self.request(role), db,
            )

    def balance(self, fixture):
        with self.Session() as db:
            row = db.query(SiteBalance).filter_by(
                program="FTTH", site_id=fixture["site_id"], product_id=fixture["product_id"],
            ).one()
            return Decimal(str(row.on_hand_qty))

    def movements(self, fixture):
        with self.Session() as db:
            return db.query(SiteMaterialMovement).filter_by(
                program="FTTH", document_type="rollout_record", document_id=str(fixture["record_pk"]),
                event_type="rollout_consumption_confirmed",
            ).all()

    def test_confirmed_consumption_and_replay_post_one_movement(self):
        fixture = self.make_fixture()
        first = self.confirm(fixture, quantity="6.5")
        retry = self.confirm(fixture, quantity="6.5")
        self.assertEqual(self.balance(fixture), Decimal("43.500000"))
        self.assertEqual(first["movement_id"], retry["movement_id"])
        self.assertTrue(retry["replayed"])
        self.assertEqual(len(self.movements(fixture)), 1)

    def test_concurrent_same_confirmation_is_idempotent(self):
        fixture = self.make_fixture(opening=100)
        barrier = Barrier(2)

        def confirm_once(_):
            barrier.wait(timeout=15)
            return self.confirm(fixture, quantity="20")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(confirm_once, range(2)))
        self.assertEqual(self.balance(fixture), Decimal("80.000000"))
        self.assertEqual(len(self.movements(fixture)), 1)
        self.assertEqual({result["movement_id"] for result in results}, {self.movements(fixture)[0].id})

    def test_concurrent_distinct_confirmations_cannot_overdraw_balance(self):
        fixture = self.make_fixture(opening=100)
        with self.Session() as db:
            with db.begin():
                record = RolloutRecord(
                    record_id=f"RDP-PG-CONSUME-SECOND-{fixture['suffix']}",
                    area=fixture["site_name"], city="Misurata", status="Done", actual=60,
                )
                db.add(record)
                db.flush()
                other = {**fixture, "record_id": record.record_id, "record_pk": record.id}
        barrier = Barrier(2)

        def confirm_once(item):
            barrier.wait(timeout=15)
            try:
                self.confirm(item, quantity="60")
                return "posted"
            except HTTPException as exc:
                return f"rejected:{exc.status_code}"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(confirm_once, (fixture, other)))
        self.assertEqual(sorted(outcomes), ["posted", "rejected:409"])
        self.assertEqual(self.balance(fixture), Decimal("40.000000"))
        with self.Session() as db:
            site_movements = db.query(SiteMaterialMovement).filter_by(
                program="FTTH", site_id=fixture["site_id"], product_id=fixture["product_id"],
                event_type="rollout_consumption_confirmed",
            ).all()
            self.assertEqual(len(site_movements), 1)
            self.assertEqual(site_movements[0].quantity_delta, Decimal("-60.000000"))
            second_record = db.query(RolloutRecord).filter_by(record_id=other["record_id"]).one()
            second_event_count = db.query(SiteMaterialMovement).filter_by(
                document_type="rollout_record", document_id=str(second_record.id),
                event_type="rollout_consumption_confirmed",
            ).count()
            self.assertEqual(second_event_count, 1 if outcomes[1] == "posted" else 0)

    def test_failure_rolls_back_ledger_projection_and_audit(self):
        fixture = self.make_fixture()
        with self.Session() as db:
            audit_before = db.query(AuditLog).filter_by(action="site_inventory_movement_posted").count()

        def fail_after_balance(stage):
            if stage == "after_balance":
                raise RuntimeError("injected PostgreSQL rollback")

        with patch.object(inventory, "_stage", side_effect=fail_after_balance):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.confirm(fixture)
        self.assertEqual(self.balance(fixture), Decimal("50.000000"))
        self.assertEqual(self.movements(fixture), [])
        with self.Session() as db:
            self.assertEqual(db.query(AuditLog).filter_by(action="site_inventory_movement_posted").count(), audit_before)

    def test_reversal_restores_balance_and_keeps_both_ledger_events(self):
        fixture = self.make_fixture()
        self.confirm(fixture, quantity="9")
        original = self.movements(fixture)[0]
        with self.Session() as db:
            with db.begin():
                reversal = inventory.reverse_site_movement(
                    db, original.id, "FTTH", "PG Admin", "PG-CORRECTION",
                    "Reverse explicit consumption in test", "pg-rollout-reverse-token-0001",
                )
                self.assertEqual(reversal.reverses_movement_id, original.id)
        self.assertEqual(self.balance(fixture), Decimal("50.000000"))
        with self.Session() as db:
            self.assertEqual(db.query(SiteMaterialMovement).filter(
                SiteMaterialMovement.id.in_([original.id, reversal.id]),
            ).count(), 2)


if __name__ == "__main__":
    import unittest

    unittest.main()
