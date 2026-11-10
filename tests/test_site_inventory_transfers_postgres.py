"""Warehouse-Test-only Site Transfer validation in a disposable non-public schema."""

import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import database
from database import APP_ENV, TEST_DATABASE_URL, create_database_engine, test_database_identity

EXPECTED_TEST_REF = "kdzhcizesmjseadwegvd"
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
    raise RuntimeError("Site Transfer PostgreSQL validation refused: Warehouse-Test identity was not verified")

from fastapi import HTTPException
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from migrations.site_inventory_foundation import migrate as migrate_foundation
from migrations.site_inventory_transfers import TRANSFER_TABLES, migrate as migrate_transfers
from models import (
    AuditLog,
    Product,
    Site,
    SiteBalance,
    SiteMaterialMovement,
    SiteOpeningBalanceRequest,
    SiteTransfer,
    SiteTransferCorrection,
    SiteTransferDispatch,
    SiteTransferItem,
    SiteTransferReceipt,
    SiteTransferReceiptItem,
)
import site_inventory_service as inventory
import site_transfer_service as transfers


class SiteTransferPostgreSQLValidation(TestCase):
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
            raise RuntimeError("Refusing PostgreSQL connection before verified Warehouse-Test identity")

        cls.engine = create_database_engine(TEST_DATABASE_URL)
        cls.schema = f"site_transfer_test_{uuid4().hex[:18]}"
        cls.allowed_schemas = {cls.schema}
        cls.schema_created = False
        cls.engine_translated = cls.engine.execution_options(schema_translate_map={None: cls.schema})
        cls.Session = sessionmaker(bind=cls.engine_translated, expire_on_commit=False)

        @event.listens_for(cls.engine, "before_cursor_execute")
        def reject_outside_schema_writes(_connection, _cursor, statement, _parameters, _context, _executemany):
            command = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
            if command in {"INSERT", "UPDATE", "DELETE", "TRUNCATE", "CREATE", "ALTER", "DROP", "REVOKE", "GRANT", "LOCK"}:
                lowered = statement.casefold()
                if "public." in lowered or not any(name.casefold() in lowered for name in cls.allowed_schemas):
                    raise AssertionError("PostgreSQL validation attempted a write outside its temporary schema")

        with cls.engine.connect() as connection:
            cls.external_tables_before = cls._external_table_names(connection)
        try:
            with cls.engine.begin() as connection:
                connection.exec_driver_sql(f'CREATE SCHEMA "{cls.schema}"')
                cls.schema_created = True
            with cls.engine_translated.begin() as connection:
                for table in (Site.__table__, Product.__table__, AuditLog.__table__):
                    table.create(bind=connection)
            cls.foundation_migration = migrate_foundation(cls.engine, cls.schema)
            cls.transfer_migration = migrate_transfers(cls.engine, cls.schema)
            cls.transfer_migration_repeat = migrate_transfers(cls.engine, cls.schema)
            cls.warehouse_snapshot = cls._warehouse_snapshot()
        except Exception:
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
            schema_exists = connection.execute(
                text("SELECT 1 FROM pg_namespace WHERE nspname=:schema"), {"schema": cls.schema}
            ).first() is not None
            external_after = cls._external_table_names(connection)
        if schema_exists:
            raise AssertionError("Temporary transfer test schema was not removed")
        if external_after != cls.external_tables_before:
            raise AssertionError("Database tables outside the temporary schema changed")
        cls.engine.dispose()

    @classmethod
    def _external_table_names(cls, connection):
        return set(connection.execute(text(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema', :schema)"
        ), {"schema": cls.schema}).all())

    @classmethod
    def _warehouse_snapshot(cls):
        with cls.engine.connect() as connection:
            return {
                table: connection.execute(text(
                    f'SELECT count(*) FROM "{cls.schema}"."{table}"'
                )).scalar_one()
                for table in ("stock_balances", "stock_movements", "technician_balances")
                if inspect(cls.engine_translated).has_table(table, schema=cls.schema)
            }

    def setUp(self):
        suffix = uuid4().hex[:12]
        with self.Session() as db:
            with db.begin():
                self.source = Site(program="FTTH", name=f"PG transfer source {suffix}", site_inventory_enabled=True)
                self.destination = Site(program="FTTH", name=f"PG transfer destination {suffix}", site_inventory_enabled=True)
                self.product = Product(program="FTTH", sku=f"PG-TR-{suffix}", name="PG transfer cable", unit="M")
                db.add_all([self.source, self.destination, self.product])
                db.flush()
                self.source_id, self.destination_id, self.product_id = self.source.id, self.destination.id, self.product.id
                opening = inventory.create_opening_balance_request(
                    db,
                    program="FTTH",
                    site_id=self.source_id,
                    product_id=self.product_id,
                    quantity=100,
                    uom="M",
                    effective_date="2026-11-07",
                    reference=f"PG-OPEN-{suffix}",
                    note="Synthetic Warehouse-Test opening balance",
                    entered_by="test-opening-user",
                    request_token=f"pg-opening-{suffix}-request-token",
                )
                inventory.approve_opening_balance(db, opening.id, "FTTH", "test-admin")

    def create_transfer(self, quantity=30, *, request_token=None, product_id=None, from_site_id=None,
                        to_site_id=None, uom="M", extra_items=None):
        lines = [{"product_id": self.product_id if product_id is None else product_id, "quantity": quantity, "uom": uom}]
        if extra_items:
            lines.extend(extra_items)
        with self.Session() as db:
            with db.begin():
                row = transfers.create_transfer(
                    db,
                    program="FTTH",
                    from_site_id=self.source_id if from_site_id is None else from_site_id,
                    to_site_id=self.destination_id if to_site_id is None else to_site_id,
                    items=lines,
                    reference="PG-SITE-XFER",
                    notes="Synthetic acceptance fixture",
                    actor="test-admin",
                    request_token=request_token or f"pg-transfer-{uuid4().hex}-request",
                )
                return row.id

    def approve_and_dispatch(self, transfer_id, token=None):
        with self.Session() as db:
            with db.begin():
                transfers.approve_transfer(db, transfer_id, "FTTH", "test-admin")
        with self.Session() as db:
            with db.begin():
                transfers.dispatch_transfer(
                    db, transfer_id, "FTTH", "test-admin", token or f"pg-dispatch-{uuid4().hex}-token"
                )

    def transfer_view(self, transfer_id):
        with self.Session() as db:
            return transfers.get_transfer(db, transfer_id, "FTTH")

    def balance(self, site_id):
        with self.Session() as db:
            row = db.query(SiteBalance).filter_by(
                program="FTTH", site_id=site_id, product_id=self.product_id
            ).first()
            return Decimal("0") if row is None else Decimal(str(row.on_hand_qty))

    def test_versioned_migration_and_constraints_are_isolated_and_rls_enabled(self):
        self.assertEqual(self.transfer_migration["schema"], self.schema)
        self.assertFalse(self.transfer_migration["already_applied"])
        self.assertTrue(self.transfer_migration_repeat["already_applied"])
        inspector = inspect(self.engine_translated)
        self.assertTrue(set(TRANSFER_TABLES).issubset(set(inspector.get_table_names(schema=self.schema))))
        self.assertGreaterEqual(len(inspector.get_foreign_keys("site_transfers", schema=self.schema)), 2)
        self.assertGreaterEqual(len(inspector.get_foreign_keys("site_transfer_receipt_items", schema=self.schema)), 3)
        with self.engine.connect() as connection:
            versions = connection.execute(text(
                f'SELECT version FROM "{self.schema}".site_inventory_schema_versions ORDER BY version'
            )).scalars().all()
            self.assertIn("site-inventory-foundation-v1", versions)
            self.assertIn("site-inventory-transfers-v1", versions)
            rls = connection.execute(text(
                "SELECT c.relname, c.relrowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=:schema AND c.relname = ANY(:tables)"
            ), {"schema": self.schema, "tables": list(TRANSFER_TABLES)}).all()
            self.assertEqual(len(rls), len(TRANSFER_TABLES))
            self.assertTrue(all(row.relrowsecurity for row in rls))

    def test_dispatch_and_receipt_ledger_identity_partial_replay_and_full_receipt(self):
        transfer_id = self.create_transfer(30, request_token="pg-transfer-create-retry-token-001")
        replay_id = self.create_transfer(30, request_token="pg-transfer-create-retry-token-001")
        self.assertEqual(transfer_id, replay_id)
        self.approve_and_dispatch(transfer_id, "pg-dispatch-idempotency-token-001")
        view = self.transfer_view(transfer_id)
        item_id = view["items"][0]["id"]
        self.assertEqual(view["status"], "in_transit")
        self.assertEqual(view["items"][0]["in_transit_qty"], "30.000000")
        self.assertEqual(self.balance(self.source_id), Decimal("70.000000"))
        self.assertEqual(self.balance(self.destination_id), Decimal("0"))

        with self.Session() as db:
            with db.begin():
                receipt = transfers.receive_transfer(
                    db, transfer_id, "FTTH", "test-admin",
                    items=[{"transfer_item_id": item_id, "quantity": 12, "uom": "M"}],
                    reference="PG-R-1", notes="Partial one", request_token="pg-receipt-idempotency-token-001",
                )
                receipt_id = receipt.id
        with self.Session() as db:
            with db.begin():
                replay = transfers.receive_transfer(
                    db, transfer_id, "FTTH", "test-admin",
                    items=[{"transfer_item_id": item_id, "quantity": 12, "uom": "M"}],
                    reference="PG-R-1", notes="Partial one", request_token="pg-receipt-idempotency-token-001",
                )
                self.assertEqual(replay.id, receipt_id)
        self.assertEqual(self.balance(self.destination_id), Decimal("12.000000"))
        self.assertEqual(self.transfer_view(transfer_id)["items"][0]["in_transit_qty"], "18.000000")
        with self.Session() as db:
            with db.begin():
                transfers.receive_transfer(
                    db, transfer_id, "FTTH", "test-admin",
                    items=[{"transfer_item_id": item_id, "quantity": 18, "uom": "M"}],
                    reference="PG-R-2", notes="Complete", request_token="pg-receipt-full-token-0001",
                )
        final = self.transfer_view(transfer_id)
        self.assertEqual(final["status"], "received")
        self.assertEqual(final["items"][0]["in_transit_qty"], "0.000000")
        self.assertEqual(self.balance(self.destination_id), Decimal("30.000000"))
        with self.Session() as db:
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(
                event_type="site_transfer_dispatched", document_id=str(transfer_id)
            ).count(), 1)
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(event_type="site_transfer_received").count(), 2)

    def test_invalid_route_uom_quantity_and_over_receipt_are_rejected(self):
        with self.assertRaises(HTTPException):
            self.create_transfer(3, from_site_id=self.source_id, to_site_id=self.source_id)
        with self.assertRaises(HTTPException) as uom:
            self.create_transfer(3, uom="PCS")
        self.assertEqual(uom.exception.status_code, 400)
        with self.assertRaises(HTTPException) as qty:
            self.create_transfer(0)
        self.assertEqual(qty.exception.status_code, 400)
        transfer_id = self.create_transfer(10)
        self.approve_and_dispatch(transfer_id)
        item_id = self.transfer_view(transfer_id)["items"][0]["id"]
        with self.Session() as db:
            with db.begin():
                transfers.receive_transfer(
                    db, transfer_id, "FTTH", "test-admin",
                    items=[{"transfer_item_id": item_id, "quantity": 7, "uom": "M"}],
                    reference="PG-OVER-1", notes="Partial", request_token="pg-over-receipt-token-001",
                )
        with self.Session() as db:
            with self.assertRaises(HTTPException) as over:
                with db.begin():
                    transfers.receive_transfer(
                        db, transfer_id, "FTTH", "test-admin",
                        items=[{"transfer_item_id": item_id, "quantity": 4, "uom": "M"}],
                        reference="PG-OVER-2", notes="Too much", request_token="pg-over-receipt-token-002",
                    )
        self.assertEqual(over.exception.status_code, 409)
        self.assertEqual(self.balance(self.destination_id), Decimal("7.000000"))

    def test_transfer_creation_requires_both_sites_to_be_active(self):
        with self.Session() as db:
            db.get(Site, self.source_id).site_inventory_enabled = False
            db.commit()
        with self.assertRaises(HTTPException) as inactive_source:
            self.create_transfer(5)
        self.assertEqual(inactive_source.exception.status_code, 409)
        self.assertEqual(inactive_source.exception.detail, "Site Inventory is not active for this Site.")

        with self.Session() as db:
            db.get(Site, self.source_id).site_inventory_enabled = True
            db.get(Site, self.destination_id).site_inventory_enabled = False
            db.commit()
        with self.assertRaises(HTTPException) as inactive_destination:
            self.create_transfer(5)
        self.assertEqual(inactive_destination.exception.status_code, 409)
        self.assertEqual(inactive_destination.exception.detail, "Site Inventory is not active for this Site.")
        with self.Session() as db:
            self.assertEqual(db.query(SiteTransfer).filter(
                SiteTransfer.from_site_id == self.source_id,
                SiteTransfer.to_site_id == self.destination_id,
            ).count(), 0)
            self.assertEqual(db.query(SiteMaterialMovement).filter(
                SiteMaterialMovement.site_id.in_([self.source_id, self.destination_id]),
                SiteMaterialMovement.document_type.like("site_transfer%"),
            ).count(), 0)

    def test_insufficient_dispatch_is_atomic_and_cannot_be_retried_as_a_second_dispatch(self):
        first = self.create_transfer(75)
        self.approve_and_dispatch(first)
        with self.Session() as db:
            with self.assertRaises(HTTPException) as duplicate:
                with db.begin():
                    transfers.dispatch_transfer(db, first, "FTTH", "test-admin", "pg-dispatch-second-token-001")
        self.assertEqual(duplicate.exception.status_code, 409)

        poor = self.create_transfer(30)
        with self.Session() as db:
            with db.begin():
                transfers.approve_transfer(db, poor, "FTTH", "test-admin")
        with self.Session() as db:
            with self.assertRaises(HTTPException) as insufficient:
                with db.begin():
                    transfers.dispatch_transfer(db, poor, "FTTH", "test-admin", "pg-dispatch-insufficient-token-01")
        self.assertEqual(insufficient.exception.status_code, 409)
        self.assertEqual(self.balance(self.source_id), Decimal("25.000000"))
        self.assertEqual(self.transfer_view(poor)["status"], "approved")
        with self.Session() as db:
            self.assertEqual(db.query(SiteTransferDispatch).filter_by(transfer_id=poor).count(), 0)

    def test_concurrent_transfers_cannot_overspend_source_balance(self):
        first = self.create_transfer(60)
        second = self.create_transfer(60)
        for transfer_id in (first, second):
            with self.Session() as db:
                with db.begin():
                    transfers.approve_transfer(db, transfer_id, "FTTH", "test-admin")
        barrier = Barrier(2)

        def dispatch(transfer_id):
            with self.Session() as db:
                barrier.wait(timeout=10)
                try:
                    with db.begin():
                        transfers.dispatch_transfer(
                            db, transfer_id, "FTTH", "test-admin", f"pg-concurrent-{transfer_id}-dispatch-token"
                        )
                    return "ok"
                except HTTPException as exc:
                    return f"rejected:{exc.status_code}"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(dispatch, (first, second)))
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("rejected:409"), 1)
        self.assertEqual(self.balance(self.source_id), Decimal("40.000000"))
        with self.Session() as db:
            self.assertEqual(db.query(SiteTransferDispatch).filter(SiteTransferDispatch.transfer_id.in_([first, second])).count(), 1)
            self.assertEqual(db.query(SiteMaterialMovement).filter(
                SiteMaterialMovement.event_type == "site_transfer_dispatched",
                SiteMaterialMovement.document_id.in_([str(first), str(second)]),
            ).count(), 1)

    def test_rollback_and_transfer_corrections_append_reversal_movements(self):
        failed = self.create_transfer(10)
        with self.Session() as db:
            with db.begin():
                transfers.approve_transfer(db, failed, "FTTH", "test-admin")
        original_stage = inventory._stage

        def fail_after_insert(stage):
            if stage == "after_movement":
                raise RuntimeError("isolated rollback injection")
            return original_stage(stage)

        with self.Session() as db, patch.object(inventory, "_stage", side_effect=fail_after_insert):
            with self.assertRaises(RuntimeError):
                with db.begin():
                    transfers.dispatch_transfer(db, failed, "FTTH", "test-admin", "pg-dispatch-rollback-token-001")
        self.assertEqual(self.balance(self.source_id), Decimal("100.000000"))
        self.assertEqual(self.transfer_view(failed)["status"], "approved")

        transfer_id = self.create_transfer(10)
        self.approve_and_dispatch(transfer_id)
        item_id = self.transfer_view(transfer_id)["items"][0]["id"]
        with self.Session() as db:
            with db.begin():
                transfers.receive_transfer(
                    db, transfer_id, "FTTH", "test-admin",
                    items=[{"transfer_item_id": item_id, "quantity": 5, "uom": "M"}],
                    reference="PG-CORR-R", notes="Partial", request_token="pg-correction-receipt-token-001",
                )
        with self.Session() as db:
            receipt_item = (
                db.query(SiteTransferReceiptItem)
                .join(SiteTransferReceipt, SiteTransferReceipt.id == SiteTransferReceiptItem.receipt_id)
                .filter(SiteTransferReceipt.transfer_id == transfer_id)
                .one()
            )
            receipt_item_id, receipt_movement_id = receipt_item.id, receipt_item.movement_id
            movement_count = db.query(SiteMaterialMovement).count()
        with self.Session() as db:
            with db.begin():
                transfers.reverse_transfer_receipt(
                    db, transfer_id, "FTTH", receipt_item_id, "test-admin", "Receipt quantity correction",
                    "pg-reverse-receipt-token-001",
                )
        self.assertEqual(self.balance(self.destination_id), Decimal("0"))
        self.assertEqual(self.transfer_view(transfer_id)["status"], "in_transit")
        with self.Session() as db:
            with db.begin():
                transfers.reverse_transfer_dispatch(
                    db, transfer_id, "FTTH", "test-admin", "Returned physically to source",
                    "pg-reverse-dispatch-token-001", confirm_physical_return=True,
                )
        self.assertEqual(self.balance(self.source_id), Decimal("100.000000"))
        self.assertEqual(self.transfer_view(transfer_id)["status"], "cancelled")
        with self.Session() as db:
            self.assertEqual(db.query(SiteMaterialMovement).count(), movement_count + 2)
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(reverses_movement_id=receipt_movement_id).count(), 1)
            self.assertEqual(db.query(SiteTransferCorrection).filter_by(transfer_id=transfer_id).count(), 2)
        with self.Session() as db:
            with self.assertRaises(DBAPIError):
                with db.begin():
                    db.query(SiteTransferReceiptItem).filter_by(id=receipt_item_id).delete(synchronize_session=False)

    def test_postgres_triggers_protect_dispatched_transfer_identity_and_event_history(self):
        transfer_id = self.create_transfer(6)
        self.approve_and_dispatch(transfer_id)
        item_id = self.transfer_view(transfer_id)["items"][0]["id"]
        with self.Session() as db:
            with self.assertRaises(DBAPIError):
                with db.begin():
                    db.query(SiteTransferItem).filter_by(id=item_id).update({"requested_qty": 7})
            with self.assertRaises(DBAPIError):
                with db.begin():
                    db.query(SiteTransfer).filter_by(id=transfer_id).update({"from_site_id": self.destination_id})
            with self.assertRaises(DBAPIError):
                with db.begin():
                    db.query(SiteTransferDispatch).filter_by(transfer_id=transfer_id).delete(synchronize_session=False)
            with self.assertRaises(DBAPIError):
                with db.begin():
                    db.query(SiteMaterialMovement).filter_by(
                        event_type="site_transfer_dispatched", document_id=str(transfer_id)
                    ).delete(synchronize_session=False)

    def test_warehouse_inventory_tables_remain_unchanged(self):
        self.assertEqual(self._warehouse_snapshot(), self.warehouse_snapshot)


if __name__ == "__main__":
    import unittest

    unittest.main()
