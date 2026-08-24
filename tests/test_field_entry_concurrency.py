import json
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

    def test_receiving_warehouse_can_return_approved_transfer_without_stock_movement(self):
        db = SessionLocal()
        source = Warehouse(name="Transfer Source WH")
        destination = Warehouse(name="Transfer Destination WH")
        product = Product(sku="TRANSFER-RETURN-TEST", name="Transfer return test material")
        db.add_all([source, destination, product])
        db.flush()
        db.add(StockBalance(warehouse_id=source.id, product_id=product.id, quantity=10))
        transfer = MaterialTransfer(
            transfer_number="TR-RETURN-TEST",
            from_warehouse_id=source.id,
            to_warehouse_id=destination.id,
            requester_name="Transfer Requester",
            status="approved",
            created_by="transfer-requester",
        )
        db.add(transfer)
        db.flush()
        db.add(MaterialTransferItem(transfer_id=transfer.id, product_id=product.id, quantity=4))
        db.commit()

        receiving_manager = SimpleNamespace(
            state=SimpleNamespace(
                current_user=SimpleNamespace(
                    role="Warehouse Manager",
                    name="Receiving Manager",
                    username="receiving-manager",
                    warehouse_name=destination.name,
                )
            )
        )
        returned = main.return_material_transfer_by_destination(
            transfer.id,
            main.MaterialRequisitionActionIn(actor="Receiving Manager", comment="Quantity needs correction"),
            receiving_manager,
            db,
        )["transfer"]
        self.assertEqual(returned["status"], "returned_for_edit")
        self.assertEqual(returned["receiver_name"], "Receiving Manager")
        self.assertEqual(returned["receiver_comment"], "Quantity needs correction")
        self.assertEqual(db.query(StockBalance).filter_by(warehouse_id=source.id, product_id=product.id).one().quantity, 10)
        self.assertEqual(db.query(StockBalance).filter_by(warehouse_id=destination.id, product_id=product.id).count(), 0)
        self.assertEqual(db.query(StockMovement).filter_by(reference=transfer.transfer_number).count(), 0)

        with self.assertRaises(HTTPException) as error:
            main.return_material_transfer_by_destination(
                transfer.id,
                main.MaterialRequisitionActionIn(actor="Receiving Manager", comment="Second attempt"),
                receiving_manager,
                db,
            )
        self.assertEqual(error.exception.status_code, 400)
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

    def test_rollout_dashboard_summary_groups_records_without_loading_full_rows(self):
        db = SessionLocal()
        db.query(RolloutRecord).delete()
        db.add_all([
            RolloutRecord(
                record_id="RDP-SUMMARY-1",
                date="2026-08-23",
                city="Misurata",
                area="Maqawba",
                item="Cable",
                material_type="Single-Core Distribution Cable_80m",
                team_leader="Team A",
                related_to_xbox="X1",
                cable_code="H1-L1-S1",
                actual=1,
            ),
            RolloutRecord(
                record_id="RDP-SUMMARY-2",
                date="2026-08-23",
                city="Misurata",
                area="Maqawba",
                item="Cable",
                material_type="Single-Core Distribution Cable_80m",
                team_leader="Team A",
                related_to_xbox="X1",
                cable_code="H1-L1-S1",
                actual=2,
            ),
            RolloutRecord(
                record_id="RDP-SUMMARY-3",
                date="2026-08-24",
                city="Misurata",
                area="Ras A Tota",
                item="SUB BOX",
                material_type="SUB BOX",
                team_leader="Team B",
                related_to_xbox="X2",
                actual=1,
            ),
        ])
        db.commit()
        request = SimpleNamespace(
            state=SimpleNamespace(
                current_user=SimpleNamespace(role="Admin", name="Admin", username="admin", warehouse_name=""),
                program="FTTH",
            )
        )

        result = main.rollout_dashboard_summary(request, program="FTTH", db=db)

        self.assertEqual(result["count"], 3)
        self.assertEqual(result["metrics"]["database_rows_loaded"], 2)
        self.assertEqual(result["metrics"]["rows_returned"], 2)
        self.assertEqual(result["metrics"]["full_record_rows_avoided"], 3)
        self.assertEqual(result["records"][0]["actual"], 3)
        self.assertEqual(result["records"][0]["cable code"], "H1-L1-S1")
        self.assertGreater(result["metrics"]["estimated_payload_bytes"], 0)
        full_payload_bytes = len(json.dumps(
            {"records": [main.row_to_record(row) for row in db.query(RolloutRecord).all()]},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"))
        self.assertLess(result["metrics"]["estimated_payload_bytes"], full_payload_bytes)
        db.close()

    def test_rollout_dashboard_summary_filters_maqawba_by_active_map_codes(self):
        """Maqawba KPI rows must match the active fiber-map design only."""
        db = SessionLocal()
        db.query(RolloutRecord).delete()
        db.add_all([
            RolloutRecord(
                record_id="RDP-MAQ-ACTIVE",
                date="2026-08-24",
                city="Misurata",
                area="Maqawba",
                item="SUB BOX",
                material_type="SUB BOX",
                related_to_xbox="X1",
                box_code="H1-L1-S1",
                actual=1,
            ),
            RolloutRecord(
                record_id="RDP-MAQ-REMOVED",
                date="2026-08-24",
                city="Misurata",
                area="Maqawba",
                item="SUB BOX",
                material_type="SUB BOX",
                related_to_xbox="X1",
                box_code="H9-L3-S4",
                actual=1,
            ),
            RolloutRecord(
                record_id="RDP-OTHER-AREA",
                date="2026-08-24",
                city="Tripoli",
                area="Hay Demashq",
                item="SUB BOX",
                material_type="SUB BOX",
                related_to_xbox="X1",
                box_code="H9-L3-S4",
                actual=1,
            ),
        ])
        db.commit()
        request = SimpleNamespace(
            state=SimpleNamespace(
                current_user=SimpleNamespace(role="Admin", name="Admin", username="admin", warehouse_name=""),
                program="FTTH",
            )
        )
        original_reference = main.rollout_code_reference_rows
        main.ROLLOUT_CODE_REFERENCE_CACHE.clear()
        main.rollout_code_reference_rows = lambda db=None, program="FTTH": [
            {"area": "Maqawba", "xbox": "X1", "code": "H1-L1-S1", "type": "box", "source": "box"}
        ]
        try:
            result = main.rollout_dashboard_summary(request, program="FTTH", db=db)
        finally:
            main.rollout_code_reference_rows = original_reference
            main.ROLLOUT_CODE_REFERENCE_CACHE.clear()
            db.close()

        codes = {(row["Area"], row["box code"]) for row in result["records"]}
        self.assertIn(("Maqawba", "H1-L1-S1"), codes)
        self.assertNotIn(("Maqawba", "H9-L3-S4"), codes)
        self.assertIn(("Hay Demashq", "H9-L3-S4"), codes)

    def test_warehouse_bootstrap_excludes_rollout_payloads(self):
        """Warehouse startup must not transfer the Rollout record dataset."""
        db = SessionLocal()
        main.WAREHOUSE_CACHE.clear()
        request = SimpleNamespace(
            state=SimpleNamespace(
                current_user=SimpleNamespace(role="Admin", name="Admin", username="admin", warehouse_name=""),
                program="FTTH",
            )
        )

        result = main.warehouse_bootstrap(request, light=True, program="FTTH", db=db)

        self.assertFalse({"rolloutRecords", "rolloutUsage", "rolloutDailyProgress", "rolloutSource"} & set(result))
        db.close()

    def test_rollout_dashboard_summary_enforces_warehouse_scope_and_roles(self):
        db = SessionLocal()
        db.query(RolloutRecord).delete()
        db.add_all([
            RolloutRecord(
                record_id="RDP-SCOPE-MAQAWBA",
                date="2026-08-23",
                city="Misurata",
                area="Maqawba",
                item="Cable",
                material_type="Single-Core Distribution Cable_80m",
                related_to_xbox="X1",
                cable_code="H1-L1-S1",
                actual=1,
            ),
            RolloutRecord(
                record_id="RDP-SCOPE-OTHER",
                date="2026-08-23",
                city="Tripoli",
                area="Hay Al Andalus Zone 3",
                item="Cable",
                material_type="Single-Core Distribution Cable_80m",
                related_to_xbox="X1",
                cable_code="H1-L1-S1",
                actual=1,
            ),
        ])
        db.commit()

        manager_request = SimpleNamespace(
            state=SimpleNamespace(
                current_user=SimpleNamespace(
                    role="Warehouse Manager",
                    name="Maqawba Manager",
                    username="maqawba-manager",
                    warehouse_name="Maqawba",
                ),
                program="FTTH",
            )
        )
        result = main.rollout_dashboard_summary(manager_request, program="FTTH", db=db)
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"][0]["Area"], "Maqawba")

        technician_request = SimpleNamespace(
            state=SimpleNamespace(
                current_user=SimpleNamespace(role="Technician", name="Technician", username="tech", warehouse_name="Maqawba"),
                program="FTTH",
            )
        )
        with self.assertRaises(HTTPException) as error:
            main.rollout_dashboard_summary(technician_request, program="FTTH", db=db)
        self.assertEqual(error.exception.status_code, 403)
        db.close()

    def test_admin_can_edit_all_field_entry_columns_without_changing_audit_identity(self):
        db = SessionLocal()
        record = RolloutRecord(
            record_id="RDP-FULL-EDIT-TEST",
            date="2026-08-18",
            supervisor_name="Before supervisor",
            team_leader="Before leader",
            city="Misurata",
            area="Maqawba",
            activity="Installation",
            related_to_xbox="X1",
            item="Accessories",
            material_type="Plum ring hook",
            mount_type="Pole",
            item_serial="OLD",
            planned_quantity=1,
            actual=1,
            stock_remaining=10,
            status="Done",
            laser="No",
            acceptance="No",
            scan="No",
            labeling="No",
            olt="OLD-OLT",
            cable_route="Aerial",
            notes="Before notes",
            entry_time="2026-08-18 08:00:00",
        )
        db.add(record)
        db.commit()
        request = SimpleNamespace(
            state=SimpleNamespace(
                current_user=SimpleNamespace(role="Admin", name="Server Admin", username="admin", warehouse_name=""),
                program="FTTH",
            )
        )
        result = main.edit_rollout_field_entry(
            record.record_id,
            {
                "Date": "2026-08-19",
                "supervisor_name": "After supervisor",
                "team_leader": "After leader",
                "city": "Tripoli",
                "Area": "Maqawba",
                "Activity": "Testing",
                "related_to_xbox": "X1",
                "item": "Accessories",
                "material_type": "Metal wedge clamping",
                "mount_type": "Wall",
                "item_serial": "NEW",
                "planned_quantity": 8,
                "actual": 7,
                "stock_remaining": 3,
                "status": "In Progress",
                "laser": "Yes",
                "acceptance": "Yes",
                "scan": "Yes",
                "labeling": "Yes",
                "olt": "NEW-OLT",
                "cable_route": "Underground",
                "notes": "After notes",
                "code_type": "accessory",
                "code": "",
                "actor": "Spoofed actor",
            },
            request,
            db,
        )
        updated = db.query(RolloutRecord).filter_by(record_id=record.record_id).one()
        self.assertTrue(result["success"])
        self.assertEqual(updated.entry_time, "2026-08-18 08:00:00")
        self.assertEqual(updated.supervisor_name, "After supervisor")
        self.assertEqual(updated.team_leader, "After leader")
        self.assertEqual(updated.city, "Tripoli")
        self.assertEqual(updated.activity, "Testing")
        self.assertEqual(updated.mount_type, "Wall")
        self.assertEqual(updated.item_serial, "NEW")
        self.assertEqual(updated.actual, 7)
        self.assertEqual(updated.stock_remaining, 3)
        self.assertEqual(updated.notes, "After notes")
        main.clear_rollout_db_cache()
        db.close()
