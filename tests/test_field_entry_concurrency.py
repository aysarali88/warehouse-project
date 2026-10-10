import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import close_all_sessions
from fastapi import HTTPException

TEST_DB_PATH = Path(tempfile.gettempdir()) / f"warehouse-rollout-tests-{os.getpid()}.db"
try:
    TEST_DB_PATH.unlink()
except FileNotFoundError:
    pass
os.environ["FTTH_DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["SESSION_SECRET"] = "isolated-test-session-secret-at-least-thirty-two-characters-long"


class FieldEntryConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
        if main.engine.dialect.name != "sqlite":
            raise RuntimeError("Tests must use an isolated SQLite database, never the production database.")

    @classmethod
    def tearDownClass(cls):
        main.clear_rollout_db_cache()
        close_all_sessions()
        main.engine.dispose()
        try:
            TEST_DB_PATH.unlink()
        except FileNotFoundError:
            pass

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

    def test_canonical_material_key_groups_cable_lengths_with_or_without_meter_suffix(self):
        equivalent_pairs = [
            ("Single-Core Distribution Cable_50m", "Single-Core Distribution Cable_50"),
            ("Single-Core Drop Cable_100m", "Single-Core Drop Cable_100"),
            ("4-coreCable_300m", "4-coreCable_300"),
        ]
        for with_unit, without_unit in equivalent_pairs:
            with self.subTest(material=with_unit):
                self.assertEqual(
                    main.canonical_material_key(with_unit),
                    main.canonical_material_key(without_unit),
                )

        self.assertNotEqual(
            main.canonical_material_key("Single-Core Distribution Cable_50"),
            main.canonical_material_key("Single-Core Distribution Cable_80m"),
        )

    def test_rollout_usage_merges_cable_materials_differing_only_by_meter_suffix(self):
        db = SessionLocal()
        db.add_all([
            RolloutRecord(
                record_id="RDP-CABLE-SUFFIX-WITH-M",
                area="Hay Al Andalus Z1",
                material_type="Single-Core Distribution Cable_424242m",
                actual=3,
                status="Done",
            ),
            RolloutRecord(
                record_id="RDP-CABLE-SUFFIX-WITHOUT-M",
                area="Hay Al Andalus Z1",
                material_type="Single-Core Distribution Cable_424242",
                actual=2,
                status="Done",
            ),
        ])
        db.commit()
        main.clear_rollout_db_cache()

        admin_request = SimpleNamespace(
            state=SimpleNamespace(current_user=SimpleNamespace(role="Admin", name="Admin", username="admin"))
        )
        usage = main.list_rollout_material_usage(admin_request, db, program="FTTH")["usage"]
        material_key = main.canonical_material_key("Single-Core Distribution Cable_424242m")
        rows = [
            row for row in usage
            if row["area"] == "Hay Al Andalus Z1"
            and main.canonical_material_key(row["material"]) == material_key
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rollout_used_qty"], 5)
        db.close()

    def test_andalus_zone_two_reference_matches_legacy_area(self):
        refs = [
            row for row in main.rollout_code_reference_rows()
            if row["xbox"] == "X4" and row["area"] == "Hay Al Andalus Zone 2"
        ]
        self.assertTrue(refs)
        self.assertTrue(all(row["area"] == "Hay Al Andalus Zone 2" for row in refs))
        self.assertEqual(main.rollout_area_key("Hay Al Andalus"), main.rollout_area_key("Hay Al Andalus Zone 2"))

    def test_andalus_zone_one_aliases_collapse_to_the_existing_field_entry_name(self):
        names = ("Hay Al Andalus Z1", "Hay Andalus ZONE 1", "Hay Andalus Zone 1")
        keys = {main.rollout_area_key(name) for name in names}
        labels = {main.rollout_area_label(name) for name in names}
        self.assertEqual(keys, {"hayalandaluszone1"})
        self.assertEqual(labels, {"Hay Al Andalus Z1"})

    def test_awlad_baeoo_x1_box_changes_are_scoped_and_idempotent(self):
        def box(xbox, code, length):
            return {
                "Area": "Awlad Baeoo", "Zone": "Awlad Baeoo", "Related to XBOX": xbox,
                "Box code": code, "Cable length m": length,
            }

        reference = {
            "boxes": [
                box("X1", "H4-L1-S4", 30), box("X1", "H6-L3-S1", 20), box("X1", "H4-L2-S4", 10),
                box("X2", "H4-L1-S4", 30), box("X1", "H4-L1-S3", 25), box("X1", "H4-L4-S3", 20),
            ],
            "area_plans": [{"area": "Awlad Baeoo", "targetSubEndBox": 30, "targetCableMeters": 1000}],
        }
        main.apply_awlad_baeoo_x1_map_changes(reference)
        main.apply_awlad_baeoo_x1_map_changes(reference)

        x1_codes = {row["Box code"] for row in reference["boxes"] if row["Related to XBOX"] == "X1"}
        self.assertFalse(x1_codes & {"H4-L1-S4", "H6-L3-S1", "H4-L2-S4"})
        self.assertIn("H4-L1-S4", {row["Box code"] for row in reference["boxes"] if row["Related to XBOX"] == "X2"})
        expected = {"H4-L4-S3": 100, "H4-L4-S4": 50, "H4-L3-S3": 80, "H4-L3-S4": 50}
        actual = {row["Box code"]: row["Cable length m"] for row in reference["boxes"] if row["Related to XBOX"] == "X1" and row["Box code"] in expected}
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 4)
        self.assertEqual(reference["area_plans"][0]["targetSubEndBox"], 30)
        self.assertEqual(reference["area_plans"][0]["targetCableMeters"], 1200)

    def test_mr_site_aliases_use_albera_as_the_single_name(self):
        self.assertEqual(main.canonical_mr_history_area("Bera W Taleem"), "Albera")
        self.assertEqual(main.canonical_mr_history_area("Albera"), "Albera")
        self.assertEqual(main.canonical_mr_history_area("Hay Al Andalus Z2"), "Hay Al Andalus Z2")

    def test_retired_zone_one_boxes_are_scoped_to_requested_xboxes_and_codes(self):
        self.assertTrue(main.retired_hay_andalus_z1_box({
            "Area": "Hay Al Andalus", "Zone": "Hay Andalus ZONE 1",
            "XBOX": "X-BOX3", "Box code": "H8-L3-S1",
        }))
        self.assertTrue(main.retired_hay_andalus_z1_box({
            "Area": "Hay Al Andalus Z1", "XBOX": "X3", "Box code": "H9-L2-S4",
        }))
        self.assertTrue(main.retired_hay_andalus_z1_box({
            "Area": "Hay Andalus Zone 1", "XBOX": "X4", "Box code": "H2-L4-S3",
        }))
        self.assertTrue(main.retired_hay_andalus_z1_box({
            "Area": "Hay Al Andalus Z1", "XBOX": "X4", "Box code": "H8-L2-S4",
        }))
        self.assertFalse(main.retired_hay_andalus_z1_box({
            "Area": "Hay Al Andalus", "Zone": "Hay Andalus ZONE 2",
            "XBOX": "X4", "Box code": "H2-L4-S3",
        }))
        self.assertFalse(main.retired_hay_andalus_z1_box({
            "Area": "Hay Al Andalus Z1", "XBOX": "X3", "Box code": "H8-L2-S1",
        }))
        self.assertFalse(main.retired_hay_andalus_z1_box({
            "Area": "Hay Al Andalus Zone 2", "XBOX": "X4", "Box code": "H8-L2-S4",
        }))

    def test_hay_andalus_z1_x4_adds_requested_box_and_cable(self):
        reference = {"boxes": [], "area_plans": [{"area": "Hay Al Andalus Z1", "targetSubEndBox": 10, "targetCableMeters": 500}]}
        main.add_hay_andalus_z1_x4_box(reference)

        self.assertEqual(len(reference["boxes"]), 1)
        box = reference["boxes"][0]
        self.assertEqual(box["Related to XBOX"], "X4")
        self.assertEqual(box["Box code"], "H8-L1-S3")
        self.assertEqual(box["Cable length m"], 80)
        self.assertEqual(reference["area_plans"][0]["targetSubEndBox"], 11)
        self.assertEqual(reference["area_plans"][0]["targetCableMeters"], 580)

    def test_bera_w_taleem_x2_boxes_are_added_once_with_requested_cable_lengths(self):
        reference = {"boxes": [], "area_plans": [{"area": "Bera W Taleem", "targetSubEndBox": 10, "targetCableMeters": 500}]}
        main.add_bera_w_taleem_x2_boxes(reference)
        main.add_bera_w_taleem_x2_boxes(reference)

        boxes = {row["Box code"]: row for row in reference["boxes"]}
        self.assertEqual(boxes["H4-L1-S3"]["Cable length m"], 80)
        self.assertEqual(boxes["H2-L1-S3"]["Cable length m"], 100)
        self.assertEqual(boxes["H8-L1-S4"]["Cable length m"], 80)
        self.assertEqual(len(reference["boxes"]), 3)
        self.assertEqual(reference["area_plans"][0]["targetSubEndBox"], 13)
        self.assertEqual(reference["area_plans"][0]["targetCableMeters"], 760)

    def test_bera_w_taleem_x1_retired_boxes_are_scoped_to_requested_codes(self):
        for code in ("H1-L1-S4", "H5-L1-S4", "H5-L4-S1", "H5-L4-S2", "H5-L4-S3", "H5-L4-S4"):
            self.assertTrue(main.retired_bera_w_taleem_x1_box({
                "Area": "Bera W Taleem", "Related to XBOX": "X1", "Box code": code,
            }))
        self.assertFalse(main.retired_bera_w_taleem_x1_box({
            "Area": "Bera W Taleem", "Related to XBOX": "X2", "Box code": "H5-L4-S1",
        }))
        self.assertFalse(main.retired_bera_w_taleem_x1_box({
            "Area": "Another Area", "Related to XBOX": "X1", "Box code": "H5-L4-S1",
        }))
        self.assertFalse(main.retired_bera_w_taleem_x1_box({
            "Area": "Bera W Taleem", "Related to XBOX": "X2", "Box code": "H1-L1-S4",
        }))

    def test_bera_w_taleem_x1_h5_splitters_are_available_with_requested_cables(self):
        reference = {"boxes": [], "area_plans": [{"area": "Bera W Taleem", "targetSubEndBox": 10, "targetCableMeters": 500}]}
        main.add_bera_w_taleem_x1_box(reference)
        main.add_bera_w_taleem_x1_box(reference)

        boxes = {box["Box code"]: box for box in reference["boxes"]}
        self.assertEqual(len(boxes), 3)
        self.assertEqual(boxes["H5-L3-S4"]["Cable length m"], 50)
        self.assertEqual(boxes["H5-L2-S4"]["Cable length m"], 80)
        self.assertEqual(boxes["H2-L3-S4"]["Cable length m"], 80)
        self.assertTrue(all(box["Related to XBOX"] == "X1" for box in boxes.values()))
        self.assertEqual(reference["area_plans"][0]["targetSubEndBox"], 13)
        self.assertEqual(reference["area_plans"][0]["targetCableMeters"], 710)

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
        damaged_product = Product(sku="RETURN-DAMAGED-APPROVAL-TEST", name="Damaged approval test material")
        db.add_all([warehouse, product, damaged_product])
        db.flush()
        db.add_all([
            StockBalance(warehouse_id=warehouse.id, product_id=product.id, quantity=10),
            StockBalance(warehouse_id=warehouse.id, product_id=damaged_product.id, quantity=20),
        ])
        db.commit()

        requester_request = SimpleNamespace(
            state=SimpleNamespace(current_user=SimpleNamespace(role="Requester", name="Requester", username="requester", warehouse_name=""))
        )
        pending = main.create_material_return(
            main.MaterialReturnIn(
                warehouse_id=warehouse.id,
                returned_by="Requester",
                items=[
                    main.MaterialReturnItemIn(product_id=product.id, quantity=3, condition="Good"),
                    main.MaterialReturnItemIn(product_id=damaged_product.id, quantity=4, condition="Damaged"),
                ],
            ),
            requester_request,
            db,
        )["return"]
        self.assertEqual(pending["status"], "pending_warehouse")
        self.assertEqual(db.query(StockBalance).filter_by(warehouse_id=warehouse.id, product_id=product.id).one().quantity, 10)
        self.assertEqual(db.query(StockMovement).filter_by(reference=pending["return_number"]).count(), 0)
        self.assertEqual(db.query(Warehouse).filter_by(
            status=main.VIRTUAL_DAMAGE_WAREHOUSE_STATUS,
            location=f"source_warehouse_id:{warehouse.id}",
        ).count(), 0)

        manager_request = SimpleNamespace(
            state=SimpleNamespace(current_user=SimpleNamespace(role="Warehouse Manager", name="Warehouse Manager", username="manager", warehouse_name=warehouse.name))
        )
        approval_result = main.approve_material_return(
            pending["id"],
            main.MaterialRequisitionActionIn(actor="Warehouse Manager"),
            manager_request,
            db,
        )
        confirmed = approval_result["return"]
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(approval_result["stock_added_quantity"], 3)
        self.assertEqual(approval_result["damaged_quantity_quarantined"], 4)
        self.assertEqual(db.query(StockBalance).filter_by(warehouse_id=warehouse.id, product_id=product.id).one().quantity, 13)
        self.assertEqual(db.query(StockBalance).filter_by(warehouse_id=warehouse.id, product_id=damaged_product.id).one().quantity, 20)
        self.assertEqual(db.query(StockMovement).filter_by(reference=pending["return_number"], movement_type="return_in").count(), 1)
        damage_move = db.query(StockMovement).filter_by(reference=pending["return_number"], movement_type="damage_in").one()
        self.assertEqual(damage_move.source_item_id, next(item["id"] for item in confirmed["items"] if item["condition"] == "Damaged"))
        self.assertEqual(db.query(StockBalance).filter_by(warehouse_id=damage_move.warehouse_id, product_id=damaged_product.id).one().quantity, 4)
        self.assertNotIn(damage_move.warehouse_id, [item["id"] for item in main.list_warehouses(program="FTTH", db=db)["warehouses"]])
        with self.assertRaises(HTTPException):
            main.require_warehouse(db, damage_move.warehouse_id)
        db.close()

    def test_damaged_return_is_recorded_in_virtual_quarantine_not_usable_stock(self):
        route = next(
            route for route in main.app.routes
            if getattr(route, "path", None) == "/api/warehouse/material-returns"
            and "POST" in getattr(route, "methods", set())
        )
        self.assertIs(route.endpoint, main.create_material_return)
        db = SessionLocal()
        warehouse = Warehouse(name="Damaged Returns Test WH")
        good_product = Product(sku="RETURN-GOOD-TEST", name="Good return test material")
        damaged_product = Product(sku="RETURN-DAMAGED-TEST", name="Damaged return test material")
        db.add_all([warehouse, good_product, damaged_product])
        db.flush()
        db.add_all([
            StockBalance(warehouse_id=warehouse.id, product_id=good_product.id, quantity=10),
            StockBalance(warehouse_id=warehouse.id, product_id=damaged_product.id, quantity=20),
        ])
        db.commit()

        manager_request = SimpleNamespace(
            state=SimpleNamespace(current_user=SimpleNamespace(role="Warehouse Manager", name="Manager", username="manager", warehouse_name=warehouse.name))
        )
        result = main.create_material_return(
            main.MaterialReturnIn(
                warehouse_id=warehouse.id,
                returned_by="Field team",
                items=[
                    main.MaterialReturnItemIn(product_id=good_product.id, quantity=3, condition="Good"),
                    main.MaterialReturnItemIn(product_id=damaged_product.id, quantity=4, condition="Damaged"),
                ],
            ),
            manager_request,
            db,
        )
        return_row = result["return"]
        self.assertEqual(result["stock_added_quantity"], 3)
        self.assertEqual(result["damaged_quantity_quarantined"], 4)
        self.assertEqual(len(return_row["items"]), 2)
        self.assertEqual(db.query(StockBalance).filter_by(warehouse_id=warehouse.id, product_id=good_product.id).one().quantity, 13)
        self.assertEqual(db.query(StockBalance).filter_by(warehouse_id=warehouse.id, product_id=damaged_product.id).one().quantity, 20)
        self.assertEqual(db.query(StockMovement).filter_by(reference=return_row["return_number"], movement_type="return_in").count(), 1)
        damage_move = db.query(StockMovement).filter_by(reference=return_row["return_number"], movement_type="damage_in").one()
        self.assertEqual(damage_move.source_item_id, next(item["id"] for item in return_row["items"] if item["condition"] == "Damaged"))
        self.assertEqual(db.query(StockBalance).filter_by(warehouse_id=damage_move.warehouse_id, product_id=damaged_product.id).one().quantity, 4)
        balances = main.list_stock_balances(manager_request, program="FTTH", db=db)["balances"]
        usage = main.list_stock_usage(manager_request, program="FTTH", db=db)["usage"]
        movements = main.list_stock_movements(manager_request, program="FTTH", db=db)["movements"]
        main.clear_rollout_db_cache()
        self.assertTrue(all(balance["warehouse_id"] == warehouse.id for balance in balances))
        self.assertTrue(all(row["warehouse_id"] == warehouse.id for row in usage))
        self.assertNotIn("damage_in", [movement["type"] for movement in movements])
        self.assertNotIn(damage_move.warehouse_id, [item["id"] for item in main.list_warehouses(program="FTTH", db=db)["warehouses"]])
        report = main.virtual_damage_report(manager_request, db, "FTTH")
        self.assertEqual(report["total_quantity"], 4)
        self.assertEqual(report["items"][0]["warehouse"], "Damage")
        self.assertEqual(report["events"][0]["reference"], return_row["return_number"])
        db.close()

    def test_single_ran_damage_return_requires_and_tracks_one_serial_per_unit(self):
        from models import ProductSerial

        db = SessionLocal()
        warehouse = Warehouse(program="SINGLE_RAN", name="SR Serial Return WH")
        product = Product(program="SINGLE_RAN", sku="SR-DAMAGE-SERIAL-TEST", name="SR damaged serialized material")
        db.add_all([warehouse, product])
        db.commit()

        requester_request = SimpleNamespace(
            state=SimpleNamespace(current_user=SimpleNamespace(
                role="Requester", name="SR Requester", username="sr-requester", warehouse_name="", program="SINGLE_RAN",
            ))
        )
        with self.assertRaises(HTTPException) as missing_error:
            main.create_material_return(
                main.MaterialReturnIn(
                    program="SINGLE_RAN",
                    warehouse_id=warehouse.id,
                    returned_by="SR Requester",
                    items=[main.MaterialReturnItemIn(product_id=product.id, quantity=1, condition="Damaged")],
                ),
                requester_request,
                db,
            )
        self.assertIn("Serial Number is required", missing_error.exception.detail)
        db.rollback()

        pending = main.create_material_return(
            main.MaterialReturnIn(
                program="SINGLE_RAN",
                warehouse_id=warehouse.id,
                returned_by="SR Requester",
                items=[main.MaterialReturnItemIn(
                    product_id=product.id,
                    quantity=2,
                    condition="Damaged",
                    serial_numbers=["SR-SERIAL-001", "SR-SERIAL-002"],
                )],
            ),
            requester_request,
            db,
        )["return"]
        self.assertEqual(pending["status"], "pending_warehouse")
        self.assertEqual([item["serial_number"] for item in pending["items"]], ["SR-SERIAL-001", "SR-SERIAL-002"])
        self.assertTrue(all(item["quantity"] == 1 for item in pending["items"]))
        self.assertEqual(
            {row.status for row in db.query(ProductSerial).filter(ProductSerial.program == "SINGLE_RAN").all()},
            {main.SR_DAMAGE_PENDING_NEW},
        )

        manager_request = SimpleNamespace(
            state=SimpleNamespace(current_user=SimpleNamespace(
                role="Warehouse Manager", name="SR Manager", username="sr-manager",
                warehouse_name=warehouse.name, program="SINGLE_RAN",
            ))
        )
        result = main.approve_material_return(
            pending["id"],
            main.MaterialRequisitionActionIn(program="SINGLE_RAN", actor="SR Manager"),
            manager_request,
            db,
        )
        self.assertEqual(result["damaged_quantity_quarantined"], 2)
        serial_rows = db.query(ProductSerial).filter(ProductSerial.program == "SINGLE_RAN").order_by(ProductSerial.serial_number).all()
        self.assertEqual([row.status for row in serial_rows], ["damaged", "damaged"])
        self.assertTrue(all(row.warehouse_id is not None for row in serial_rows))
        movements = db.query(StockMovement).filter_by(program="SINGLE_RAN", movement_type="damage_in", reference=pending["return_number"]).all()
        self.assertEqual({row.serial_number for row in movements}, {"SR-SERIAL-001", "SR-SERIAL-002"})

        with self.assertRaises(HTTPException) as duplicate_error:
            main.create_material_return(
                main.MaterialReturnIn(
                    program="SINGLE_RAN",
                    warehouse_id=warehouse.id,
                    returned_by="SR Requester",
                    items=[main.MaterialReturnItemIn(
                        product_id=product.id,
                        quantity=1,
                        condition="Damaged",
                        serial_numbers=["SR-SERIAL-001"],
                    )],
                ),
                requester_request,
                db,
            )
        self.assertIn("not available for a Damage return", duplicate_error.exception.detail)
        db.rollback()
        db.close()

    def test_historical_damaged_returns_import_once_into_central_damage_warehouse(self):
        from models import MaterialReturn, MaterialReturnItem

        db = SessionLocal()
        warehouse = Warehouse(name="Historical Damage Source")
        product = Product(sku="HISTORICAL-DAMAGE-TEST", name="Historical damaged material", unit="PCS")
        db.add_all([warehouse, product])
        db.flush()
        return_row = MaterialReturn(
            program="FTTH",
            return_number="RN-HISTORICAL-DAMAGE-TEST",
            warehouse_id=warehouse.id,
            status="confirmed",
            created_by="test",
        )
        db.add(return_row)
        db.flush()
        line = MaterialReturnItem(
            return_id=return_row.id,
            product_id=product.id,
            quantity=5,
            condition="Damaged",
        )
        db.add(line)
        db.commit()

        admin_request = SimpleNamespace(
            state=SimpleNamespace(current_user=SimpleNamespace(role="Admin", name="Admin", username="admin"))
        )
        first = main.reconcile_damaged_returns(admin_request, program="FTTH", db=db)
        second = main.reconcile_damaged_returns(admin_request, program="FTTH", db=db)

        damage = db.query(Warehouse).filter_by(program="FTTH", name="Damage").one()
        balance = db.query(StockBalance).filter_by(
            program="FTTH", warehouse_id=damage.id, product_id=product.id,
        ).one()
        self.assertEqual(first["migrated_lines"], 1)
        self.assertEqual(first["migrated_quantity"], 5)
        self.assertEqual(second["migrated_lines"], 0)
        self.assertGreaterEqual(second["already_in_damage"], 1)
        self.assertEqual(balance.quantity, 5)
        self.assertEqual(db.query(StockMovement).filter_by(
            program="FTTH", movement_type="damage_in", source_item_id=line.id,
        ).count(), 1)
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
        self.assertEqual(result["records"][0]["staus"], "Done")
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

    def test_warehouse_manager_cannot_view_mr_until_approval(self):
        row = SimpleNamespace(status="pending_approval", warehouse=SimpleNamespace(name="Tripoli"))
        self.assertFalse(main.user_can_view_requisition(row, "Tripoli", "Warehouse Manager"))

        for status in ("approved", "signed", "issued"):
            row.status = status
            self.assertTrue(main.user_can_view_requisition(row, "Tripoli", "Warehouse Manager"))

        row.status = "rejected"
        self.assertFalse(main.user_can_view_requisition(row, "Tripoli", "Warehouse Manager"))
        row.status = "pending_approval"
        self.assertTrue(main.user_can_view_requisition(row, "Approver", "Approval"))
