import os
import tempfile
import unittest
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import close_all_sessions
from fastapi import HTTPException


class FieldEntryConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.db_file.close()
        os.environ["FTTH_DATABASE_URL"] = f"sqlite:///{cls.db_file.name}"
        os.environ["SESSION_SECRET"] = "isolated-test-session-secret-at-least-thirty-two-characters-long"
        global main, SessionLocal, RolloutRecord, Warehouse, Product, StockBalance, StockMovement, MaterialRequisition, MaterialRequisitionItem, MaterialTransfer, MaterialTransferItem
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
            StockMovement,
            Warehouse,
        )

    @classmethod
    def tearDownClass(cls):
        main.clear_rollout_db_cache()
        close_all_sessions()
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

    def test_requester_return_waits_for_warehouse_manager_confirmation(self):
        db = SessionLocal()
        warehouse = Warehouse(name="Returns Test WH")
        product = Product(sku="RETURN-TEST", name="Return test material")
        db.add_all([warehouse, product])
        db.flush()
        db.add(StockBalance(warehouse_id=warehouse.id, product_id=product.id, quantity=10))
        db.commit()

        requester_request = SimpleNamespace(
            state=SimpleNamespace(current_user=SimpleNamespace(role="Requester", name="Requester", username="requester", warehouse_name=""))
        )
        pending = main.create_material_return(
            main.MaterialReturnIn(
                warehouse_id=warehouse.id,
                returned_by="Requester",
                items=[main.MaterialReturnItemIn(product_id=product.id, quantity=3)],
            ),
            requester_request,
            db,
        )["return"]
        self.assertEqual(pending["status"], "pending_warehouse")
        self.assertEqual(db.query(StockBalance).filter_by(warehouse_id=warehouse.id, product_id=product.id).one().quantity, 10)
        self.assertEqual(db.query(StockMovement).filter_by(reference=pending["return_number"]).count(), 0)

        manager_request = SimpleNamespace(
            state=SimpleNamespace(current_user=SimpleNamespace(role="Warehouse Manager", name="Warehouse Manager", username="manager", warehouse_name=warehouse.name))
        )
        confirmed = main.approve_material_return(
            pending["id"],
            main.MaterialRequisitionActionIn(actor="Warehouse Manager"),
            manager_request,
            db,
        )["return"]
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(db.query(StockBalance).filter_by(warehouse_id=warehouse.id, product_id=product.id).one().quantity, 13)
        self.assertEqual(db.query(StockMovement).filter_by(reference=pending["return_number"], movement_type="return_in").count(), 1)
        db.close()

    def test_rollout_difference_excludes_confirmed_returns(self):
        db = SessionLocal()
        warehouse = Warehouse(name="Usage Return Test WH")
        product = Product(sku="USAGE-RETURN-TEST", name="Metal wedge clamping")
        db.add_all([warehouse, product])
        db.flush()
        requisition = MaterialRequisition(
            order_number="MR-USAGE-RETURN-TEST",
            warehouse_id=warehouse.id,
            site_id="Maqawba",
            status="issued",
        )
        db.add(requisition)
        db.flush()
        db.add(MaterialRequisitionItem(requisition_id=requisition.id, product_id=product.id, quantity=10))
        db.add(RolloutRecord(record_id="RDP-USAGE-RETURN-TEST", area="Maqawba", material_type=product.name, actual=3, status="Done"))
        db.commit()

        requester_request = SimpleNamespace(
            state=SimpleNamespace(current_user=SimpleNamespace(role="Requester", name="Usage Requester", username="usage-requester", warehouse_name=""))
        )
        pending = main.create_material_return(
            main.MaterialReturnIn(
                warehouse_id=warehouse.id,
                site_id="Maqawba",
                returned_by="Usage Requester",
                items=[main.MaterialReturnItemIn(product_id=product.id, quantity=2)],
            ),
            requester_request,
            db,
        )["return"]
        manager_request = SimpleNamespace(
            state=SimpleNamespace(current_user=SimpleNamespace(role="Warehouse Manager", name="Usage Manager", username="usage-manager", warehouse_name=warehouse.name))
        )
        main.approve_material_return(pending["id"], main.MaterialRequisitionActionIn(actor="Usage Manager"), manager_request, db)

        admin_request = SimpleNamespace(
            state=SimpleNamespace(current_user=SimpleNamespace(role="Admin", name="Admin", username="admin", warehouse_name=""))
        )
        usage = main.list_rollout_material_usage(admin_request, db, program="FTTH")["usage"]
        row = next(row for row in usage if row["sku"] == product.sku and row["area"] == "Maqawba")
        self.assertEqual(row["mr_issued_qty"], 10)
        self.assertEqual(row["rollout_used_qty"], 3)
        self.assertEqual(row["returned_qty"], 2)
        self.assertEqual(row["remaining_after_rollout"], 5)
        db.close()

    def test_rollout_usage_sources_match_the_reported_area_and_material_total(self):
        db = SessionLocal()
        db.query(RolloutRecord).delete()
        db.commit()
        db.add_all([
            RolloutRecord(
                record_id="RDP-SOURCE-1",
                area="Maqawba",
                related_to_xbox="X1",
                material_type="Metal wedge clamping",
                actual=7,
                status="Done",
                notes="Hub: H1",
            ),
            RolloutRecord(
                record_id="RDP-SOURCE-2",
                area="Maqawba",
                related_to_xbox="X1",
                material_type="Metal wedge clamping",
                actual=5,
                status="Done",
                notes="Hub: H2",
            ),
            RolloutRecord(
                record_id="RDP-SOURCE-OTHER-AREA",
                area="Hay Demashq",
                related_to_xbox="X1",
                material_type="Metal wedge clamping",
                actual=99,
                status="Done",
                notes="Hub: H1",
            ),
        ])
        db.commit()
        main.clear_rollout_db_cache()

        admin_request = SimpleNamespace(
            state=SimpleNamespace(current_user=SimpleNamespace(role="Admin", name="Admin", username="admin", warehouse_name=""))
        )
        details = main.list_rollout_material_usage_details(
            admin_request,
            area="Maqawba",
            material="ITC3301-P1_03",
            db=db,
            program="FTTH",
        )
        self.assertEqual(details["total"], 12)
        self.assertEqual([row["id"] for row in details["records"]], ["RDP-SOURCE-1", "RDP-SOURCE-2"])
        self.assertEqual([row["hub"] for row in details["records"]], ["H1", "H2"])
        db.close()
