import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException, Request


os.environ["APP_ENV"] = "test"
os.environ["TEST_DATABASE_URL"] = "sqlite://"
os.environ["SESSION_SECRET"] = "mr-issue-local-test-secret-at-least-32-characters"

import main
import site_inventory_service as inventory
from database import SessionLocal, engine
from migrations.site_inventory_foundation import migrate
from models import (
    IssueOrder,
    IssueOrderItem,
    MaterialRequisition,
    MaterialRequisitionItem,
    Product,
    Site,
    SiteBalance,
    SiteMaterialMovement,
    StockBalance,
    StockMovement,
    Technician,
    TechnicianBalance,
    Warehouse,
)


class MaterialRequisitionSiteIssueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site_inventory_env = patch.dict(os.environ, {
            "SITE_INVENTORY_CUTOVER_PROGRAMS": "FTTH",
            "SITE_INVENTORY_MR_ENABLED": "true",
        })
        cls.site_inventory_env.start()
        if os.getenv("APP_ENV") != "test" or engine.dialect.name != "sqlite":
            raise RuntimeError("Local MR issue tests require an isolated SQLite test database")
        migrate(engine, schema=None)

    @classmethod
    def tearDownClass(cls):
        engine.dispose(close=True)
        cls.site_inventory_env.stop()

    def make_mr(self, *, status="approved", site_label=None, stock=100, lines=None, ambiguous=False):
        suffix = uuid4().hex[:10]
        technician_name = f"MR Issue Tech {suffix}"
        actor = f"MR Issue Actor {suffix}"
        line_specs = lines or [("M", "M", 7, "main"), ("M", "M", 3, "main")]
        with SessionLocal() as db:
            with db.begin():
                site = Site(program="FTTH", name=f"MR Issue Site {suffix}", site_inventory_enabled=True)
                warehouse = Warehouse(program="FTTH", name=f"MR Issue WH {suffix}")
                db.add_all([site, warehouse])
                db.flush()
                if ambiguous:
                    db.add_all([
                        Site(program="FTTH", name="Ambiguous-Yard"),
                        Site(program="FTTH", name="Ambiguous Yard"),
                    ])
                products = {}
                for unit, _item_uom, _quantity, key in line_specs:
                    if key and key not in products:
                        product = Product(
                            program="FTTH", sku=f"MR-{suffix}-{key}", name=f"MR material {key}", unit=unit
                        )
                        db.add(product)
                        db.flush()
                        products[key] = product
                        db.add(StockBalance(
                            program="FTTH", warehouse_id=warehouse.id, product_id=product.id, quantity=stock
                        ))
                row = MaterialRequisition(
                    program="FTTH",
                    order_number=f"MR-TEST-{suffix}",
                    warehouse_id=warehouse.id,
                    site_id=site_label or ("Ambiguous Yard" if ambiguous else site.name),
                    team_leader=technician_name,
                    requester_name=technician_name,
                    receiver_name="Confirmed Receiver",
                    status=status,
                    created_by=actor,
                )
                db.add(row)
                db.flush()
                for index, (unit, item_uom, quantity, key) in enumerate(line_specs, start=1):
                    db.add(MaterialRequisitionItem(
                        requisition_id=row.id,
                        line_no=index,
                        product_id=products[key].id if key else None,
                        uom=item_uom,
                        quantity=quantity,
                    ))
                db.flush()
                result = {
                    "mr_id": row.id,
                    "order_number": row.order_number,
                    "site_id": site.id,
                    "warehouse_id": warehouse.id,
                    "product_ids": {key: product.id for key, product in products.items()},
                    "technician_name": technician_name,
                    "actor": actor,
                    "line_count": len(line_specs),
                }
        return result

    def issue(self, fixture):
        with SessionLocal() as db:
            row = db.get(MaterialRequisition, fixture["mr_id"])
            try:
                order_number = main.issue_material_requisition_row(db, row, fixture["actor"])
                db.commit()
                return order_number
            except Exception:
                db.rollback()
                raise

    def values(self, fixture):
        with SessionLocal() as db:
            row = db.get(MaterialRequisition, fixture["mr_id"])
            result = {
                "status": row.status,
                "stock": {},
                "site_balance": {},
                "site_movements": db.query(SiteMaterialMovement).filter_by(
                    program="FTTH", document_type="material_requisition", document_id=str(fixture["mr_id"])
                ).count(),
                "issue_orders": db.query(IssueOrder).filter_by(created_by=fixture["actor"]).count(),
                "issue_items": db.query(IssueOrderItem).join(IssueOrder).filter(
                    IssueOrder.created_by == fixture["actor"]
                ).count(),
                "stock_movements": db.query(StockMovement).filter_by(
                    reference=fixture["order_number"], movement_type="issue_to_technician"
                ).count(),
                "technician_balance": {},
            }
            for key, product_id in fixture["product_ids"].items():
                result["stock"][key] = db.query(StockBalance).filter_by(
                    warehouse_id=fixture["warehouse_id"], product_id=product_id
                ).one().quantity
                balance = db.query(SiteBalance).filter_by(
                    program="FTTH", site_id=fixture["site_id"], product_id=product_id
                ).first()
                result["site_balance"][key] = balance.on_hand_qty if balance else Decimal("0")
                tech = (
                    db.query(TechnicianBalance)
                    .join(Technician)
                    .filter(Technician.name == fixture["technician_name"], TechnicianBalance.product_id == product_id)
                    .first()
                )
                result["technician_balance"][key] = tech.quantity if tech else 0
            return result

    def test_actual_multiline_issue_posts_one_movement_per_line_and_preserves_warehouse_behavior(self):
        fixture = self.make_mr()
        self.issue(fixture)
        result = self.values(fixture)
        self.assertEqual(result["status"], "issued")
        self.assertEqual(result["stock"]["main"], 90)
        self.assertEqual(result["site_balance"]["main"], Decimal("10.000000"))
        self.assertEqual(result["technician_balance"]["main"], 10)
        self.assertEqual(result["site_movements"], 2)
        self.assertEqual(result["issue_orders"], 1)
        self.assertEqual(result["issue_items"], 2)
        self.assertEqual(result["stock_movements"], 2)
        with SessionLocal() as db:
            movements = db.query(SiteMaterialMovement).filter_by(
                program="FTTH", document_type="material_requisition", document_id=str(fixture["mr_id"])
            ).all()
            self.assertEqual({movement.event_type for movement in movements}, {"mr_issue"})
            self.assertEqual({movement.uom for movement in movements}, {"M"})
            self.assertEqual({movement.site_id for movement in movements}, {fixture["site_id"]})

    def test_signed_status_alone_does_not_create_site_inventory(self):
        fixture = self.make_mr(status="signed")
        result = self.values(fixture)
        self.assertEqual(result["stock"]["main"], 100)
        self.assertEqual(result["site_movements"], 0)
        self.assertEqual(result["site_balance"]["main"], Decimal("0"))

    def test_disabled_integration_keeps_legacy_issue_without_site_mapping(self):
        fixture = self.make_mr(site_label="Unmapped but valid legacy location", lines=[("M", "M", 4, "main")])
        with patch.dict(os.environ, {
            "SITE_INVENTORY_MR_ENABLED": "false",
            "SITE_INVENTORY_CUTOVER_PROGRAMS": "",
        }):
            self.issue(fixture)
        result = self.values(fixture)
        self.assertEqual(result["status"], "issued")
        self.assertEqual(result["stock"]["main"], 96)
        self.assertEqual(result["technician_balance"]["main"], 4)
        self.assertEqual(result["stock_movements"], 1)
        self.assertEqual(result["site_movements"], 0)

    def test_inactive_site_keeps_legacy_issue_without_site_inventory_posting(self):
        fixture = self.make_mr(lines=[("M", "PCS", 4, "main")])
        with SessionLocal() as db:
            site = db.get(Site, fixture["site_id"])
            site.site_inventory_enabled = False
            db.commit()

        self.issue(fixture)
        result = self.values(fixture)
        self.assertEqual(result["status"], "issued")
        self.assertEqual(result["stock"]["main"], 96)
        self.assertEqual(result["technician_balance"]["main"], 4)
        self.assertEqual(result["stock_movements"], 1)
        self.assertEqual(result["issue_orders"], 1)
        self.assertEqual(result["issue_items"], 1)
        self.assertEqual(result["site_movements"], 0)
        self.assertEqual(result["site_balance"]["main"], Decimal("0"))

    def test_issue_immediately_flushes_pending_approval_before_row_lock(self):
        fixture = self.make_mr(status="draft", lines=[("M", "M", 4, "main")])
        with SessionLocal() as db:
            row = db.get(MaterialRequisition, fixture["mr_id"])
            row.status = "approved"
            main.issue_material_requisition_row(db, row, fixture["actor"])
            db.commit()
        result = self.values(fixture)
        self.assertEqual(result["status"], "issued")
        self.assertEqual(result["stock"]["main"], 96)
        self.assertEqual(result["site_movements"], 1)

    def test_retry_after_commit_cannot_duplicate_issue_or_site_movement(self):
        fixture = self.make_mr(lines=[("M", "M", 4, "main")])
        self.issue(fixture)
        with self.assertRaises(HTTPException) as error:
            self.issue(fixture)
        self.assertEqual(error.exception.status_code, 400)
        result = self.values(fixture)
        self.assertEqual(result["stock"]["main"], 96)
        self.assertEqual(result["site_movements"], 1)
        self.assertEqual(result["issue_orders"], 1)
        self.assertEqual(result["stock_movements"], 1)

    def test_unissued_statuses_are_rejected_without_inventory_changes(self):
        for status in ("draft", "pending_approval", "cancelled"):
            with self.subTest(status=status):
                fixture = self.make_mr(status=status, lines=[("M", "M", 4, "main")])
                with self.assertRaises(HTTPException):
                    self.issue(fixture)
                result = self.values(fixture)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["stock"]["main"], 100)
                self.assertEqual(result["site_movements"], 0)
                self.assertEqual(result["issue_orders"], 0)

    def test_unresolved_site_fails_closed_before_warehouse_changes(self):
        fixture = self.make_mr(site_label="No configured site", lines=[("M", "M", 4, "main")])
        with self.assertRaises(HTTPException) as error:
            self.issue(fixture)
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("explicit Site alias", error.exception.detail)
        result = self.values(fixture)
        self.assertEqual(result["stock"]["main"], 100)
        self.assertEqual(result["site_movements"], 0)
        self.assertEqual(result["issue_orders"], 0)

    def test_ambiguous_site_fails_closed_before_warehouse_changes(self):
        fixture = self.make_mr(ambiguous=True, lines=[("M", "M", 4, "main")])
        with self.assertRaises(HTTPException) as error:
            self.issue(fixture)
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("ambiguous", error.exception.detail)
        result = self.values(fixture)
        self.assertEqual(result["stock"]["main"], 100)
        self.assertEqual(result["site_movements"], 0)
        self.assertEqual(result["issue_orders"], 0)

    def test_missing_product_id_fails_closed_before_warehouse_changes(self):
        fixture = self.make_mr(lines=[("M", "M", 4, "")])
        with self.assertRaises(HTTPException) as error:
            self.issue(fixture)
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("missing product_id", error.exception.detail)
        result = self.values(fixture)
        self.assertEqual(result["stock"], {})
        self.assertEqual(result["site_movements"], 0)
        self.assertEqual(result["issue_orders"], 0)

    def test_uom_mismatch_fails_closed_before_warehouse_changes(self):
        fixture = self.make_mr(lines=[("M", "PCS", 4, "main")])
        with self.assertRaises(HTTPException) as error:
            self.issue(fixture)
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("Unit of measure", error.exception.detail)
        result = self.values(fixture)
        self.assertEqual(result["stock"]["main"], 100)
        self.assertEqual(result["site_movements"], 0)
        self.assertEqual(result["issue_orders"], 0)

    def test_insufficient_warehouse_stock_leaves_all_ledgers_unchanged(self):
        fixture = self.make_mr(stock=3, lines=[("M", "M", 4, "main")])
        with self.assertRaises(HTTPException) as error:
            self.issue(fixture)
        self.assertEqual(error.exception.status_code, 400)
        result = self.values(fixture)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["stock"]["main"], 3)
        self.assertEqual(result["site_movements"], 0)
        self.assertEqual(result["issue_orders"], 0)
        self.assertEqual(result["stock_movements"], 0)

    def test_site_post_failure_rolls_back_warehouse_technician_and_stock_movement(self):
        fixture = self.make_mr(lines=[("M", "M", 4, "main")])
        def fail_after_movement(stage):
            if stage == "after_movement":
                raise RuntimeError("injected site failure")

        with patch.object(inventory, "_stage", side_effect=fail_after_movement):
            with self.assertRaisesRegex(RuntimeError, "injected site failure"):
                self.issue(fixture)
        result = self.values(fixture)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["stock"]["main"], 100)
        self.assertEqual(result["technician_balance"]["main"], 0)
        self.assertEqual(result["site_movements"], 0)
        self.assertEqual(result["issue_orders"], 0)
        self.assertEqual(result["issue_items"], 0)
        self.assertEqual(result["stock_movements"], 0)

    def test_issued_mr_hard_delete_is_rejected_without_changing_any_inventory_history(self):
        fixture = self.make_mr(lines=[("M", "M", 4, "main")])
        self.issue(fixture)
        before = self.values(fixture)
        with SessionLocal() as db:
            row = db.get(MaterialRequisition, fixture["mr_id"])
            with self.assertRaises(HTTPException) as error:
                main.delete_material_requisition_row(db, row, fixture["actor"])
            self.assertEqual(error.exception.status_code, 409)
            self.assertIn("controlled reversal/void workflow", error.exception.detail)
            db.rollback()
        after = self.values(fixture)
        self.assertEqual(after, before)
        with SessionLocal() as db:
            self.assertIsNotNone(db.get(MaterialRequisition, fixture["mr_id"]))

    def test_site_inventory_history_blocks_delete_even_after_mr_integration_is_disabled(self):
        fixture = self.make_mr(lines=[("M", "M", 4, "main")])
        self.issue(fixture)
        before = self.values(fixture)
        self.assertEqual(before["site_movements"], 1)
        with patch.dict(os.environ, {
            "SITE_INVENTORY_MR_ENABLED": "false",
            "SITE_INVENTORY_CUTOVER_PROGRAMS": "",
        }):
            with SessionLocal() as db:
                row = db.get(MaterialRequisition, fixture["mr_id"])
                with self.assertRaises(HTTPException) as error:
                    main.delete_material_requisition_row(db, row, fixture["actor"])
                self.assertEqual(error.exception.status_code, 409)
                db.rollback()
        self.assertEqual(self.values(fixture), before)

    def test_issued_mr_without_site_history_is_protected_when_mr_integration_is_enabled(self):
        fixture = self.make_mr(status="issued", lines=[("M", "M", 4, "main")])
        with SessionLocal() as db:
            row = db.get(MaterialRequisition, fixture["mr_id"])
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(
                program="FTTH", document_type="material_requisition", document_id=str(fixture["mr_id"])
            ).count(), 0)
            with self.assertRaises(HTTPException) as error:
                main.delete_material_requisition_row(db, row, fixture["actor"])
            self.assertEqual(error.exception.status_code, 409)
            db.rollback()
        with SessionLocal() as db:
            self.assertIsNotNone(db.get(MaterialRequisition, fixture["mr_id"]))

    def test_issued_mr_delete_keeps_legacy_behavior_when_mr_integration_is_dormant(self):
        fixture = self.make_mr(lines=[("M", "M", 4, "main")])
        with patch.dict(os.environ, {
            "SITE_INVENTORY_MR_ENABLED": "false",
            "SITE_INVENTORY_CUTOVER_PROGRAMS": "",
        }):
            self.issue(fixture)
            with SessionLocal() as db:
                self.assertEqual(db.query(SiteMaterialMovement).filter_by(
                    program="FTTH", document_type="material_requisition", document_id=str(fixture["mr_id"])
                ).count(), 0)
                row = db.get(MaterialRequisition, fixture["mr_id"])
                result = main.delete_material_requisition_row(db, row, fixture["actor"])
                db.commit()
                self.assertEqual(result["restored_quantity"], 4)
                self.assertIsNone(db.get(MaterialRequisition, fixture["mr_id"]))
                self.assertEqual(db.query(StockBalance).filter_by(
                    warehouse_id=fixture["warehouse_id"], product_id=fixture["product_ids"]["main"]
                ).one().quantity, 100)
                self.assertEqual(db.query(SiteMaterialMovement).filter_by(
                    program="FTTH", document_type="material_requisition", document_id=str(fixture["mr_id"])
                ).count(), 0)

    def test_product_purge_cannot_remove_an_issued_mr_line_or_its_history(self):
        fixture = self.make_mr(lines=[("M", "M", 4, "main")])
        self.issue(fixture)
        before = self.values(fixture)
        with SessionLocal() as db:
            request = Request({"type": "http", "headers": [], "query_string": b""})
            request.state.current_user = SimpleNamespace(role="Admin", name="Admin", username="admin")
            with self.assertRaises(HTTPException) as error:
                main.purge_product(
                    fixture["product_ids"]["main"], main.ProductPurgeIn(program="FTTH"), request, db
                )
            self.assertEqual(error.exception.status_code, 409)
            self.assertIn("issued material requisition", error.exception.detail)
            self.assertIsNotNone(db.get(Product, fixture["product_ids"]["main"]))
            self.assertIsNotNone(db.get(MaterialRequisition, fixture["mr_id"]))
        self.assertEqual(self.values(fixture), before)

    def test_resubmit_cannot_replace_lines_when_issue_history_exists(self):
        fixture = self.make_mr(lines=[("M", "M", 4, "main")])
        self.issue(fixture)
        with SessionLocal() as db:
            row = db.get(MaterialRequisition, fixture["mr_id"])
            site_name = db.get(Site, fixture["site_id"]).name
            row.status = "returned_for_edit"
            db.commit()
        before = self.values(fixture)
        with SessionLocal() as db:
            request = Request({"type": "http", "headers": [], "query_string": b""})
            request.state.current_user = SimpleNamespace(role="Admin", name="Admin", username="admin")
            data = main.MaterialRequisitionIn(
                warehouse_id=fixture["warehouse_id"],
                site_id=site_name,
                items=[main.MaterialRequisitionItemIn(
                    product_id=fixture["product_ids"]["main"], description="replacement", uom="M", quantity=4
                )],
            )
            with self.assertRaises(HTTPException) as error:
                main.resubmit_material_requisition(fixture["mr_id"], data, request, db)
            self.assertEqual(error.exception.status_code, 409)
            self.assertIn("inventory history must be preserved", error.exception.detail)
            self.assertEqual(db.query(MaterialRequisitionItem).filter_by(
                requisition_id=fixture["mr_id"]
            ).count(), 1)
        self.assertEqual(self.values(fixture), before)

    def test_unissued_draft_pending_and_signed_mrs_remain_deletable_without_site_movements(self):
        for status in ("draft", "pending_approval", "signed"):
            with self.subTest(status=status):
                fixture = self.make_mr(status=status, lines=[("M", "M", 4, "main")])
                with SessionLocal() as db:
                    self.assertEqual(db.query(SiteMaterialMovement).filter_by(
                        program="FTTH", document_type="material_requisition", document_id=str(fixture["mr_id"])
                    ).count(), 0)
                    row = db.get(MaterialRequisition, fixture["mr_id"])
                    result = main.delete_material_requisition_row(db, row, fixture["actor"])
                    db.commit()
                    self.assertEqual(result["restored_quantity"], 0)
                    self.assertIsNone(db.get(MaterialRequisition, fixture["mr_id"]))
                    self.assertEqual(db.query(SiteMaterialMovement).filter_by(
                        program="FTTH", document_type="material_requisition", document_id=str(fixture["mr_id"])
                    ).count(), 0)
                    self.assertEqual(db.query(StockBalance).filter_by(
                        warehouse_id=fixture["warehouse_id"], product_id=fixture["product_ids"]["main"]
                    ).one().quantity, 100)

    def test_mr_delete_route_remains_admin_only(self):
        fixture = self.make_mr(status="draft", lines=[("M", "M", 4, "main")])
        for role in (None, "Requester", "Technician", "Warehouse Manager", "Management", "Approval"):
            with self.subTest(role=role), SessionLocal() as db:
                request = Request({"type": "http", "headers": [], "query_string": b""})
                if role is not None:
                    request.state.current_user = SimpleNamespace(role=role, name=role, username=role.lower())
                with self.assertRaises(HTTPException) as error:
                    main.delete_material_requisition(
                        fixture["mr_id"], request,
                        main.MaterialRequisitionActionIn(program="FTTH"), db,
                    )
                self.assertEqual(error.exception.status_code, 401 if role is None else 403)
                self.assertIsNotNone(db.get(MaterialRequisition, fixture["mr_id"]))

        with SessionLocal() as db:
            request = Request({"type": "http", "headers": [], "query_string": b""})
            request.state.current_user = SimpleNamespace(role="Admin", name="Admin", username="admin")
            result = main.delete_material_requisition(
                fixture["mr_id"], request, main.MaterialRequisitionActionIn(program="FTTH"), db
            )
            self.assertTrue(result["success"])
            self.assertIsNone(db.get(MaterialRequisition, fixture["mr_id"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
