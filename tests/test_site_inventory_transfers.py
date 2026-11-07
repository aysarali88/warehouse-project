import os
import unittest
from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

os.environ["APP_ENV"] = "test"
os.environ["TEST_DATABASE_URL"] = "sqlite://"
os.environ["SESSION_SECRET"] = "site-transfer-local-test-secret-at-least-32-characters"
os.environ["SITE_INVENTORY_TRANSFER_ENABLED"] = "false"
os.environ.pop("SITE_INVENTORY_CUTOVER_PROGRAMS", None)

from fastapi import HTTPException, Request
from sqlalchemy import inspect
from sqlalchemy.exc import DBAPIError

import main
import site_inventory_api
import site_transfer_api
import site_transfer_service as transfers
from database import SessionLocal, engine, normalize_program
from migrations.site_inventory_foundation import migrate as migrate_foundation
from migrations.site_inventory_transfers import migrate as migrate_transfers
from models import (
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
from site_inventory_service import approve_opening_balance, create_opening_balance_request


class SiteTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getenv("APP_ENV") != "test" or engine.dialect.name != "sqlite":
            raise RuntimeError("Local transfer tests require an isolated SQLite test database")
        if inspect(engine).has_table("site_transfers"):
            raise RuntimeError("Transfer tables must not be created by application startup")
        migrate_foundation(engine, schema=None)
        cls.migration = migrate_transfers(engine, schema=None)
        cls.repeat_migration = migrate_transfers(engine, schema=None)

    @classmethod
    def tearDownClass(cls):
        engine.dispose(close=True)

    def setUp(self):
        suffix = uuid4().hex[:12]
        with SessionLocal() as db:
            with db.begin():
                self.source = Site(program="FTTH", name=f"Transfer Source {suffix}")
                self.destination = Site(program="FTTH", name=f"Transfer Destination {suffix}")
                self.other_program_site = Site(program="SINGLE_RAN", name=f"Other Program {suffix}")
                self.product = Product(program="FTTH", sku=f"TR-{suffix}", name="Transfer cable", unit="M")
                db.add_all([self.source, self.destination, self.other_program_site, self.product])
                db.flush()
                self.source_id = self.source.id
                self.destination_id = self.destination.id
                self.other_program_site_id = self.other_program_site.id
                self.product_id = self.product.id
                request = create_opening_balance_request(
                    db,
                    program="FTTH",
                    site_id=self.source_id,
                    product_id=self.product_id,
                    quantity=50,
                    uom="M",
                    effective_date="2026-11-07",
                    reference=f"OPEN-{suffix}",
                    note="Synthetic isolated test opening",
                    entered_by="opening-user",
                    request_token=f"opening-{suffix}-request-token",
                )
                approve_opening_balance(db, request.id, "FTTH", "test-admin")

    def _create(self, *, quantity=30, request_token=None, uom="M", from_site_id=None, to_site_id=None,
                product_id=None, program="FTTH"):
        token = request_token or f"transfer-{uuid4().hex}-request"
        with SessionLocal() as db:
            with db.begin():
                row = transfers.create_transfer(
                    db,
                    program=program,
                    from_site_id=self.source_id if from_site_id is None else from_site_id,
                    to_site_id=self.destination_id if to_site_id is None else to_site_id,
                    items=[{"product_id": self.product_id if product_id is None else product_id, "quantity": quantity, "uom": uom}],
                    reference="Synthetic transfer",
                    notes="Warehouse-Test style local fixture",
                    actor="test-admin",
                    request_token=token,
                )
                return row.id

    def _approve_dispatch(self, transfer_id, token=None):
        with SessionLocal() as db:
            with db.begin():
                transfers.approve_transfer(db, transfer_id, "FTTH", "test-admin")
        with SessionLocal() as db:
            with db.begin():
                transfers.dispatch_transfer(
                    db, transfer_id, "FTTH", "test-admin", token or f"dispatch-{uuid4().hex}-token"
                )

    def _balance(self, site_id):
        with SessionLocal() as db:
            row = db.query(SiteBalance).filter_by(
                program="FTTH", site_id=site_id, product_id=self.product_id
            ).first()
            return Decimal("0") if row is None else Decimal(str(row.on_hand_qty))

    def _view(self, transfer_id):
        with SessionLocal() as db:
            return transfers.get_transfer(db, transfer_id, "FTTH")

    def test_transfer_tables_are_explicit_versioned_and_not_startup_created(self):
        self.assertFalse(self.migration["already_applied"])
        self.assertTrue(self.repeat_migration["already_applied"])
        self.assertEqual(self.migration["version"], "site-inventory-transfers-v1")
        names = set(inspect(engine).get_table_names())
        self.assertTrue({
            "site_transfers", "site_transfer_items", "site_transfer_dispatches",
            "site_transfer_receipts", "site_transfer_receipt_items", "site_transfer_corrections",
        }.issubset(names))

    def test_creation_validates_numeric_sites_program_product_uom_and_positive_quantity(self):
        with self.assertRaises(HTTPException) as same_site:
            self._create(from_site_id=self.source_id, to_site_id=self.source_id)
        self.assertEqual(same_site.exception.status_code, 400)
        with self.assertRaises(HTTPException) as missing_site:
            self._create(to_site_id=self.other_program_site_id)
        self.assertEqual(missing_site.exception.status_code, 404)
        with self.assertRaises(HTTPException) as bad_uom:
            self._create(uom="PCS")
        self.assertEqual(bad_uom.exception.status_code, 400)
        with self.assertRaises(HTTPException) as zero_quantity:
            self._create(quantity=0)
        self.assertEqual(zero_quantity.exception.status_code, 400)
        with self.assertRaises(HTTPException) as missing_product:
            self._create(product_id=999999)
        self.assertEqual(missing_product.exception.status_code, 404)

    def test_create_dispatch_and_partial_receipts_update_ledger_and_transit(self):
        transfer_id = self._create(quantity=30)
        self.assertEqual(self._view(transfer_id)["status"], "draft")
        self.assertEqual(self._balance(self.source_id), Decimal("50.000000"))
        self.assertEqual(self._balance(self.destination_id), Decimal("0"))
        self._approve_dispatch(transfer_id, "dispatch-transfer-partials-token-0001")
        after_dispatch = self._view(transfer_id)
        self.assertEqual(after_dispatch["status"], "in_transit")
        self.assertEqual(after_dispatch["items"][0]["in_transit_qty"], "30.000000")
        self.assertEqual(self._balance(self.source_id), Decimal("20.000000"))
        self.assertEqual(self._balance(self.destination_id), Decimal("0"))

        with SessionLocal() as db:
            with db.begin():
                first = transfers.receive_transfer(
                    db, transfer_id, "FTTH", "test-admin",
                    items=[{"transfer_item_id": after_dispatch["items"][0]["id"], "quantity": 8, "uom": "M"}],
                    reference="RCV-1", notes="First partial", request_token="receipt-partial-request-token-0001",
                )
                first_receipt_id = first.id
        self.assertEqual(self._view(transfer_id)["status"], "partially_received")
        self.assertEqual(self._view(transfer_id)["items"][0]["in_transit_qty"], "22.000000")
        self.assertEqual(self._balance(self.destination_id), Decimal("8.000000"))

        # Replaying the same receipt token is an exact replay, not a second credit.
        with SessionLocal() as db:
            with db.begin():
                replay = transfers.receive_transfer(
                    db, transfer_id, "FTTH", "test-admin",
                    items=[{"transfer_item_id": after_dispatch["items"][0]["id"], "quantity": 8, "uom": "M"}],
                    reference="RCV-1", notes="First partial", request_token="receipt-partial-request-token-0001",
                )
                self.assertEqual(replay.id, first_receipt_id)
        self.assertEqual(self._balance(self.destination_id), Decimal("8.000000"))

        with SessionLocal() as db:
            with db.begin():
                transfers.receive_transfer(
                    db, transfer_id, "FTTH", "test-admin",
                    items=[{"transfer_item_id": after_dispatch["items"][0]["id"], "quantity": 20, "uom": "M"}],
                    reference="RCV-2", notes="Second partial", request_token="receipt-partial-request-token-0002",
                )
        self.assertEqual(self._view(transfer_id)["status"], "partially_received")
        self.assertEqual(self._view(transfer_id)["items"][0]["in_transit_qty"], "2.000000")
        with SessionLocal() as db:
            with db.begin():
                transfers.receive_transfer(
                    db, transfer_id, "FTTH", "test-admin",
                    items=[{"transfer_item_id": after_dispatch["items"][0]["id"], "quantity": 2, "uom": "M"}],
                    reference="RCV-3", notes="Final two", request_token="receipt-partial-request-token-0003",
                )
        self.assertEqual(self._view(transfer_id)["status"], "received")
        self.assertEqual(self._view(transfer_id)["items"][0]["in_transit_qty"], "0.000000")
        self.assertEqual(self._balance(self.destination_id), Decimal("30.000000"))
        self.assertEqual(self._balance(self.source_id), Decimal("20.000000"))

    def test_over_receipt_is_rejected_and_receipt_uom_is_exact(self):
        transfer_id = self._create(quantity=10)
        self._approve_dispatch(transfer_id)
        item_id = self._view(transfer_id)["items"][0]["id"]
        with SessionLocal() as db:
            with db.begin():
                transfers.receive_transfer(
                    db, transfer_id, "FTTH", "test-admin",
                    items=[{"transfer_item_id": item_id, "quantity": 7, "uom": "M"}],
                    reference="RCV-OVER-1", notes="Partial", request_token="receipt-over-test-token-0001",
                )
        for quantity, uom, expected_status in ((4, "M", 409), (1, "PCS", 400)):
            with self.subTest(quantity=quantity, uom=uom):
                with SessionLocal() as db:
                    with self.assertRaises(HTTPException) as error:
                        with db.begin():
                            transfers.receive_transfer(
                                db, transfer_id, "FTTH", "test-admin",
                                items=[{"transfer_item_id": item_id, "quantity": quantity, "uom": uom}],
                                reference=f"RCV-OVER-{quantity}", notes="Check", request_token=f"receipt-over-{uuid4().hex}",
                            )
                self.assertEqual(error.exception.status_code, expected_status)
        self.assertEqual(self._balance(self.destination_id), Decimal("7.000000"))

    def test_dispatch_retry_cannot_dispatch_twice_and_insufficient_source_rolls_back(self):
        transfer_id = self._create(quantity=30)
        self._approve_dispatch(transfer_id, "dispatch-retry-transfer-token-0001")
        with SessionLocal() as db:
            with self.assertRaises(HTTPException) as duplicate:
                with db.begin():
                    transfers.dispatch_transfer(db, transfer_id, "FTTH", "test-admin", "dispatch-different-token-0001")
        self.assertEqual(duplicate.exception.status_code, 409)
        self.assertEqual(self._balance(self.source_id), Decimal("20.000000"))

        poor_transfer = self._create(quantity=25)
        with SessionLocal() as db:
            with db.begin():
                transfers.approve_transfer(db, poor_transfer, "FTTH", "test-admin")
        with SessionLocal() as db:
            with self.assertRaises(HTTPException) as error:
                with db.begin():
                    transfers.dispatch_transfer(db, poor_transfer, "FTTH", "test-admin", "dispatch-insufficient-token-001")
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self._balance(self.source_id), Decimal("20.000000"))
        self.assertEqual(self._view(poor_transfer)["status"], "approved")
        with SessionLocal() as db:
            self.assertIsNone(db.query(SiteTransferDispatch).filter_by(transfer_id=poor_transfer).first())
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(
                event_type="site_transfer_dispatched", document_id=str(poor_transfer)
            ).count(), 0)

    def test_multi_item_dispatch_rolls_back_all_lines_on_mid_transaction_failure(self):
        with SessionLocal() as db:
            with db.begin():
                second_product = Product(program="FTTH", sku=f"TR2-{uuid4().hex[:10]}", name="Second cable", unit="M")
                db.add(second_product)
                db.flush()
                second_product_id = second_product.id
                opening = create_opening_balance_request(
                    db, program="FTTH", site_id=self.source_id, product_id=second_product_id,
                    quantity=50, uom="M", effective_date="2026-11-07", reference="OPEN-2",
                    note="Synthetic second opening", entered_by="opening-user", request_token=f"opening2-{uuid4().hex}-token",
                )
                approve_opening_balance(db, opening.id, "FTTH", "test-admin")
                transfer = transfers.create_transfer(
                    db, program="FTTH", from_site_id=self.source_id, to_site_id=self.destination_id,
                    items=[
                        {"product_id": self.product_id, "quantity": 10, "uom": "M"},
                        {"product_id": second_product_id, "quantity": 12, "uom": "M"},
                    ], reference="MULTI", notes="Atomic lines", actor="test-admin",
                    request_token=f"multi-transfer-{uuid4().hex}-token",
                )
                transfer_id = transfer.id
        with SessionLocal() as db:
            with db.begin():
                transfers.approve_transfer(db, transfer_id, "FTTH", "test-admin")
        import site_inventory_service
        original_stage = site_inventory_service._stage

        def fail_after_ledger(stage):
            if stage == "after_movement":
                raise RuntimeError("synthetic rollback injection")
            return original_stage(stage)

        with SessionLocal() as db, patch.object(site_inventory_service, "_stage", side_effect=fail_after_ledger):
            with self.assertRaises(RuntimeError):
                with db.begin():
                    transfers.dispatch_transfer(db, transfer_id, "FTTH", "test-admin", "dispatch-rollback-token-0001")
        self.assertEqual(self._balance(self.source_id), Decimal("50.000000"))
        self.assertEqual(self._view(transfer_id)["status"], "approved")
        with SessionLocal() as db:
            self.assertEqual(db.query(SiteTransferDispatch).filter_by(transfer_id=transfer_id).count(), 0)
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(
                event_type="site_transfer_dispatched", document_id=str(transfer_id)
            ).count(), 0)

    def test_admin_corrections_use_reversals_and_keep_history(self):
        transfer_id = self._create(quantity=10)
        self._approve_dispatch(transfer_id)
        item_id = self._view(transfer_id)["items"][0]["id"]
        with SessionLocal() as db:
            with db.begin():
                transfers.receive_transfer(
                    db, transfer_id, "FTTH", "test-admin",
                    items=[{"transfer_item_id": item_id, "quantity": 5, "uom": "M"}],
                    reference="RCV-CORRECT", notes="Partial receipt", request_token="receipt-correction-token-001",
                )
        with SessionLocal() as db:
            receipt_item = (
                db.query(SiteTransferReceiptItem)
                .join(SiteTransferReceipt, SiteTransferReceipt.id == SiteTransferReceiptItem.receipt_id)
                .filter(SiteTransferReceipt.transfer_id == transfer_id)
                .one()
            )
            receipt_item_id = receipt_item.id
            original_receipt_movement_id = receipt_item.movement_id
            before_count = db.query(SiteMaterialMovement).count()
        with SessionLocal() as db:
            with db.begin():
                transfers.reverse_transfer_receipt(
                    db, transfer_id, "FTTH", receipt_item_id, "test-admin", "Physical count correction",
                    "reverse-receipt-correction-token-001",
                )
        self.assertEqual(self._balance(self.destination_id), Decimal("0.000000"))
        self.assertEqual(self._view(transfer_id)["status"], "in_transit")
        with SessionLocal() as db:
            with self.assertRaises(HTTPException) as error:
                with db.begin():
                    transfers.reverse_transfer_dispatch(
                        db, transfer_id, "FTTH", "test-admin", "Return to source", "reverse-dispatch-token-0001",
                        confirm_physical_return=False,
                    )
            self.assertEqual(error.exception.status_code, 400)
        with SessionLocal() as db:
            with db.begin():
                transfers.reverse_transfer_dispatch(
                    db, transfer_id, "FTTH", "test-admin", "Physically returned to source",
                    "reverse-dispatch-token-0001", confirm_physical_return=True,
                )
        self.assertEqual(self._view(transfer_id)["status"], "cancelled")
        self.assertEqual(self._view(transfer_id)["items"][0]["in_transit_qty"], "0")
        self.assertEqual(self._balance(self.source_id), Decimal("50.000000"))
        self.assertEqual(self._balance(self.destination_id), Decimal("0.000000"))
        with SessionLocal() as db:
            self.assertEqual(db.query(SiteMaterialMovement).count(), before_count + 2)
            reversal = db.query(SiteMaterialMovement).filter_by(reverses_movement_id=original_receipt_movement_id).one()
            self.assertEqual(reversal.movement_type, "reversal")
        with SessionLocal() as db:
            with self.assertRaises(DBAPIError):
                with db.begin():
                    db.query(SiteTransferReceiptItem).filter_by(id=receipt_item_id).delete(synchronize_session=False)

    def test_transfer_history_is_immutable_and_generic_movement_reversal_is_blocked(self):
        transfer_id = self._create(quantity=5)
        self._approve_dispatch(transfer_id)
        with SessionLocal() as db:
            movement = db.query(SiteMaterialMovement).filter_by(
                event_type="site_transfer_dispatched", document_id=str(transfer_id)
            ).one()
            movement_id = movement.id
        with SessionLocal() as db:
            with self.assertRaises(HTTPException) as error:
                with db.begin():
                    from site_inventory_service import reverse_site_movement
                    reverse_site_movement(
                        db, movement_id, "FTTH", "test-admin", "REF", "Use transfer correction", "generic-transfer-reversal-token-1",
                    )
            self.assertEqual(error.exception.status_code, 409)
        with SessionLocal() as db:
            with self.assertRaises(DBAPIError):
                with db.begin():
                    db.query(SiteTransferDispatch).filter_by(transfer_id=transfer_id).delete(synchronize_session=False)
            with self.assertRaises(DBAPIError):
                with db.begin():
                    db.query(SiteTransfer).filter_by(id=transfer_id).delete(synchronize_session=False)

    def test_transfer_api_is_admin_only_and_cutover_gate_defaults_closed(self):
        router = site_transfer_api.build_site_transfer_router(
            lambda: None, normalize_program, main.require_roles, main.request_actor
        )
        endpoint = next(
            route.endpoint for route in router.routes
            if route.path == "/api/warehouse/site-transfers" and "POST" in route.methods
        )
        payload = site_transfer_api.SiteTransferCreate(
            program="FTTH", from_site_id=1, to_site_id=2,
            items=[site_transfer_api.TransferLineCreate(product_id=1, quantity=1, uom="M")],
            request_token="api-feature-disabled-token-0001",
        )
        for role in (None, "Requester", "Technician", "Warehouse Manager", "Management", "Approval"):
            request = Request({"type": "http", "headers": [], "query_string": b""})
            if role:
                request.state.current_user = SimpleNamespace(role=role, name=role, username=role.lower())
            with self.subTest(role=role), patch.object(site_transfer_api, "create_transfer") as create:
                with self.assertRaises(HTTPException) as error:
                    endpoint(payload, request, SimpleNamespace(begin=nullcontext))
                self.assertEqual(error.exception.status_code, 401 if role is None else 403)
                create.assert_not_called()

        admin_request = Request({"type": "http", "headers": [], "query_string": b""})
        admin_request.state.current_user = SimpleNamespace(role="Admin", name="Admin", username="admin")
        with patch.object(site_transfer_api, "create_transfer") as create:
            with self.assertRaises(HTTPException) as error:
                endpoint(payload, admin_request, SimpleNamespace(begin=nullcontext))
            self.assertEqual(error.exception.status_code, 503)
            create.assert_not_called()
        self.assertFalse(site_transfer_api.site_transfer_enabled("FTTH", normalize_program))
        with patch.dict(os.environ, {
            "SITE_INVENTORY_TRANSFER_ENABLED": "true",
            "SITE_INVENTORY_CUTOVER_PROGRAMS": "FTTH",
        }):
            self.assertTrue(site_transfer_api.site_transfer_enabled("FTTH", normalize_program))
            self.assertFalse(site_transfer_api.site_transfer_enabled("SINGLE_RAN", normalize_program))


if __name__ == "__main__":
    unittest.main()
