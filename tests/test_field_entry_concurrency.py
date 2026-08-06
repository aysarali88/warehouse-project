import os
import tempfile
import unittest
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException


class FieldEntryConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.db_file.close()
        os.environ["FTTH_DATABASE_URL"] = f"sqlite:///{cls.db_file.name}"
        os.environ["SESSION_SECRET"] = "isolated-test-session-secret-at-least-thirty-two-characters-long"
        global main, SessionLocal, RolloutRecord, Warehouse, Product, StockBalance, MaterialRequisition, MaterialRequisitionItem, MaterialTransfer, MaterialTransferItem
        import main
        from database import SessionLocal
        from models import (
            MaterialRequisition,
            MaterialRequisitionItem,
            MaterialTransfer,
            MaterialTransferItem,
            Product,
            RolloutRecord,
            StockBalance,
            Warehouse,
        )

    @classmethod
    def tearDownClass(cls):
        main.engine.dispose()
        os.unlink(cls.db_file.name)

    def test_counter_and_submission_key_are_unique(self):
        db = SessionLocal()
        db.add(RolloutRecord(record_id="RDP-9", submission_key="existing-submission-key-0001"))
        db.commit()
        db.close()

        record_ids = []
        for _ in range(3):
            db = SessionLocal()
            counter = main.rollout_entry_counter(db)
            record_id = main.allocate_rollout_entry_id(db, counter)
            db.add(RolloutRecord(record_id=record_id, submission_key=f"unique-submission-key-{record_id}"))
            db.commit()
            record_ids.append(record_id)
            db.close()
        self.assertEqual(record_ids, ["RDP-10", "RDP-11", "RDP-12"])

        db = SessionLocal()
        db.add(RolloutRecord(record_id="RDP-13", submission_key="same-submission-key-0001"))
        db.commit()
        db.add(RolloutRecord(record_id="RDP-14", submission_key="same-submission-key-0001"))
        with self.assertRaises(IntegrityError):
            db.commit()
        db.rollback()
        db.close()

    def test_andalus_zone_two_reference_matches_legacy_area(self):
        refs = [row for row in main.rollout_code_reference_rows() if row["xbox"] == "X4"]
        self.assertTrue(refs)
        self.assertTrue(all(row["area"] == "Hay Al Andalus Zone 2" for row in refs))
        self.assertEqual(main.rollout_area_key("Hay Al Andalus"), main.rollout_area_key("Hay Al Andalus Zone 2"))

    def test_hub_codes_are_available_from_map_parent_hub_field(self):
        refs = main.rollout_code_reference_rows()
        hubs = [
            row for row in refs
            if row["type"] == "box" and row["box_type"] == "HUB BOX"
        ]
        self.assertTrue(hubs)
        self.assertTrue(any(row["code"] == "H7" for row in hubs))
        self.assertTrue(any(row["code"] == "H10" for row in hubs))

    def test_pending_mr_and_transfer_reserve_stock_before_confirmation(self):
        db = SessionLocal()
        warehouse = Warehouse(name="Reservation Test WH")
        product = Product(sku="RESERVATION-TEST", name="Reservation test material")
        db.add_all([warehouse, product])
        db.flush()
        db.add(StockBalance(warehouse_id=warehouse.id, product_id=product.id, quantity=50))

        requisition = MaterialRequisition(
            order_number="MR-RESERVATION-TEST",
            warehouse_id=warehouse.id,
            status="pending_approval",
        )
        transfer = MaterialTransfer(
            transfer_number="TR-RESERVATION-TEST",
            from_warehouse_id=warehouse.id,
            to_warehouse_id=warehouse.id,
            status="pending_approval",
        )
        db.add_all([requisition, transfer])
        db.flush()
        db.add_all([
            MaterialRequisitionItem(requisition_id=requisition.id, product_id=product.id, quantity=40),
            MaterialTransferItem(transfer_id=transfer.id, product_id=product.id, quantity=5),
        ])
        db.commit()

        reserved = main.reserved_stock_quantities(db)
        self.assertEqual(reserved[(warehouse.id, product.id)], 45)
        main.validate_reservable_stock(
            db,
            warehouse.id,
            [SimpleNamespace(product_id=product.id, quantity=5)],
        )
        with self.assertRaises(HTTPException) as error:
            main.validate_reservable_stock(
                db,
                warehouse.id,
                [SimpleNamespace(product_id=product.id, quantity=6)],
            )
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("available 5", error.exception.detail)
        db.close()
