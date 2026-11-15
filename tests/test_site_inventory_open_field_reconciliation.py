import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ["APP_ENV"] = "test"
os.environ["TEST_DATABASE_URL"] = "sqlite://"
os.environ["SESSION_SECRET"] = "open-field-reconciliation-test-secret-32-chars"

import main
import site_inventory_service as inventory
from database import Base
from migrations.site_inventory_foundation import migrate
from models import (
    AuditLog,
    MaterialRequisition,
    MaterialRequisitionItem,
    MaterialReturn,
    MaterialReturnItem,
    Product,
    RolloutRecord,
    Site,
    SiteBalance,
    SiteMaterialMovement,
    Warehouse,
)


class SiteInventoryOpenFieldReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.test_engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=self.test_engine, tables=[
            AuditLog.__table__, Site.__table__, Product.__table__, Warehouse.__table__,
            MaterialRequisition.__table__, MaterialRequisitionItem.__table__,
            MaterialReturn.__table__, MaterialReturnItem.__table__, RolloutRecord.__table__,
        ])
        migrate(self.test_engine, schema=None)
        self.Session = sessionmaker(bind=self.test_engine, expire_on_commit=False)
        self.suffix = uuid4().hex[:12]
        self.actor = f"Open Field Admin {self.suffix}"
        with self.Session() as db:
            with db.begin():
                site = Site(program="FTTH", name=f"Reconcile Site {self.suffix}", site_inventory_enabled=True)
                product = Product(
                    program="FTTH", sku=f"OFR-{self.suffix}",
                    name="Single-Core Distribution Cable_5m", unit="PCS", status="active",
                )
                warehouse = Warehouse(program="FTTH", name=f"Reconcile Warehouse {self.suffix}")
                db.add_all([site, product, warehouse])
                db.flush()
                self.site_id, self.site_name = site.id, site.name
                self.product_id, self.warehouse_id = product.id, warehouse.id
        self.request = Request({"type": "http", "headers": [], "query_string": b""})
        self.request.state.current_user = SimpleNamespace(
            role="Admin", name=self.actor, username=self.actor, program="FTTH",
        )

    def tearDown(self):
        self.test_engine.dispose()

    def _add_sources(self, *, mr_qty=0, rollout_qty=0, return_qty=0, product_id=None, uom="PCS"):
        with self.Session() as db:
            with db.begin():
                if mr_qty:
                    requisition = MaterialRequisition(
                        program="FTTH", order_number=f"OFR-{self.suffix}-MR",
                        warehouse_id=self.warehouse_id, site_id=self.site_name, status="issued",
                    )
                    db.add(requisition)
                    db.flush()
                    db.add(MaterialRequisitionItem(
                        requisition_id=requisition.id, product_id=product_id or self.product_id,
                        description="Single-Core Distribution Cable_5m", quantity=mr_qty, uom=uom,
                    ))
                if return_qty:
                    returned = MaterialReturn(
                        program="FTTH", return_number=f"OFR-{self.suffix}-RN",
                        warehouse_id=self.warehouse_id, site_id=self.site_name, status="confirmed",
                    )
                    db.add(returned)
                    db.flush()
                    db.add(MaterialReturnItem(
                        return_id=returned.id, product_id=product_id or self.product_id,
                        description="Single-Core Distribution Cable_5m", quantity=return_qty, uom=uom,
                    ))
                if rollout_qty:
                    db.add(RolloutRecord(
                        record_id=f"OFR-{self.suffix}-RO",
                        area=self.site_name, material_type="Single-Core Distribution Cable_5m",
                        actual=rollout_qty, status="Done",
                    ))

    def _seed_ledger(self, quantity):
        token = f"open-field-seed-{self.suffix}"
        with self.Session() as db:
            with db.begin():
                return inventory.post_site_movement(
                    db,
                    program="FTTH", site_id=self.site_id, product_id=self.product_id,
                    movement_type="incoming", event_type="test_seed",
                    quantity_delta=Decimal(str(quantity)), uom="PCS",
                    document_type="open_field_test", document_id=self.suffix,
                    document_line_id="1", reference=f"OFR-SEED-{self.suffix}",
                    note="Synthetic test balance", created_by=self.actor, approved_by=self.actor,
                    effective_date="2026-11-14", request_token=token,
                )

    def _snapshot(self):
        with self.Session() as db:
            return main.build_open_field_site_inventory_snapshot(db, self.request, "FTTH")

    def _apply(self, digest):
        endpoint = main.site_inventory_open_field_reconciliation
        with self.Session() as db:
            return endpoint(
                main.SiteInventoryOpenFieldReconciliationRequest(
                    program="FTTH", apply=True, expected_digest=digest,
                ),
                self.request,
                db,
            )

    def test_formula_preview_and_apply_reconcile_once(self):
        self._add_sources(mr_qty=12, rollout_qty=5, return_qty=2)
        self._seed_ledger(4)

        preview = self._snapshot()
        row = next(item for item in preview["items"] if item["site_id"] == self.site_id)
        self.assertEqual(row["open_in_field_qty"], Decimal("5.000000"))
        self.assertEqual(row["delta"], Decimal("1.000000"))
        self.assertEqual(preview["blockers"], [])
        with self.Session() as db:
            report = main.list_rollout_material_usage(self.request, db, False, "FTTH")
        report_row = next(item for item in report["usage"] if item["area"] == self.site_name)
        self.assertEqual(
            row["open_in_field_qty"],
            Decimal(str(report_row["remaining_after_rollout"])).quantize(Decimal("0.000001")),
        )

        with patch.dict(os.environ, {"SITE_INVENTORY_ADMIN_WRITE_ENABLED": "true"}):
            result = self._apply(preview["digest"])
            self.assertEqual(result["movement_count"], 1)
            with self.assertRaises(HTTPException) as replay:
                self._apply(preview["digest"])
        self.assertEqual(replay.exception.status_code, 409)
        verified = self._snapshot()
        final_row = next(item for item in verified["items"] if item["site_id"] == self.site_id)
        self.assertEqual(final_row["ledger_qty"], Decimal("5.000000"))
        self.assertEqual(final_row["balance_qty"], Decimal("5.000000"))
        self.assertEqual(final_row["delta"], Decimal("0.000000"))
        with self.Session() as db:
            movement_count = db.query(SiteMaterialMovement).filter_by(
                site_id=self.site_id, event_type="open_field_reconciliation",
            ).count()
            audit_count = db.query(AuditLog).filter_by(
                action="site_inventory_open_field_reconciliation_applied", actor=self.actor,
            ).count()
        self.assertEqual(movement_count, 1)
        self.assertEqual(audit_count, 1)

    def test_decimal_zero_rollout_cable_length_maps_to_catalog_product(self):
        rollout_product = Product(
            program="FTTH", sku=f"OFR-150-{self.suffix}",
            name="Single-Core Distribution Cable_150m", unit="PCS", status="active",
        )
        with self.Session() as db:
            with db.begin():
                db.add(rollout_product)
                db.flush()
                product_id = rollout_product.id
                db.add(RolloutRecord(
                    record_id=f"OFR-{self.suffix}-RO-DECIMAL",
                    area=self.site_name,
                    material_type="Single-Core Distribution Cable_150.0m",
                    actual=1,
                    status="Done",
                ))

        preview = self._snapshot()
        row = next(item for item in preview["items"] if item["product_id"] == product_id)
        self.assertEqual(row["rollout_actual_qty"], Decimal("1.000000"))
        self.assertEqual(row["open_in_field_qty"], Decimal("-1.000000"))
        self.assertEqual(preview["blockers"], [])

    def test_admin_write_gate_and_authorization_block_without_writes(self):
        self._add_sources(mr_qty=3)
        preview = self._snapshot()
        endpoint = main.site_inventory_open_field_reconciliation
        payload = main.SiteInventoryOpenFieldReconciliationRequest(
            program="FTTH", apply=True, expected_digest=preview["digest"],
        )
        with patch.dict(os.environ, {"SITE_INVENTORY_ADMIN_WRITE_ENABLED": "false"}):
            with self.Session() as db, self.assertRaises(HTTPException) as disabled:
                endpoint(payload, self.request, db)
        self.assertEqual(disabled.exception.status_code, 503)

        self.request.state.current_user.role = "Approval"
        with patch.dict(os.environ, {"SITE_INVENTORY_ADMIN_WRITE_ENABLED": "true"}):
            with self.Session() as db, self.assertRaises(HTTPException) as forbidden:
                endpoint(payload, self.request, db)
        self.assertEqual(forbidden.exception.status_code, 403)
        with self.Session() as db:
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(site_id=self.site_id).count(), 0)
            self.assertEqual(db.query(SiteBalance).filter_by(site_id=self.site_id).count(), 0)

    def test_source_change_after_preview_requires_a_new_digest(self):
        self._add_sources(mr_qty=3)
        preview = self._snapshot()
        with self.Session() as db:
            with db.begin():
                item = db.query(MaterialRequisitionItem).filter_by(product_id=self.product_id).one()
                item.quantity = 4
        with patch.dict(os.environ, {"SITE_INVENTORY_ADMIN_WRITE_ENABLED": "true"}):
            with self.assertRaises(HTTPException) as changed:
                self._apply(preview["digest"])
        self.assertEqual(changed.exception.status_code, 409)
        with self.Session() as db:
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(site_id=self.site_id).count(), 0)

    def test_uom_and_ambiguous_product_mappings_fail_closed(self):
        self._add_sources(mr_qty=3, uom="M")
        mismatch = self._snapshot()
        self.assertTrue(any(item["type"] == "uom_conflict" for item in mismatch["blockers"]))
        with patch.dict(os.environ, {"SITE_INVENTORY_ADMIN_WRITE_ENABLED": "true"}):
            with self.assertRaises(HTTPException) as uom_error:
                self._apply(mismatch["digest"])
        self.assertEqual(uom_error.exception.status_code, 409)

        duplicate = Product(
            program="FTTH", sku=f"OFR-DUP-{self.suffix}",
            name="Single-Core Distribution Cable_5m", unit="PCS", status="active",
        )
        with self.Session() as db:
            with db.begin():
                db.add(duplicate)
                db.add(RolloutRecord(
                    record_id=f"OFR-{self.suffix}-RO-DUP",
                    area=self.site_name, material_type="Single-Core Distribution Cable_5m",
                    actual=1, status="Done",
                ))
        ambiguous = self._snapshot()
        self.assertTrue(any(
            item["type"] == "unmapped_or_ambiguous_product"
            for item in ambiguous["blockers"]
        ))
        with patch.dict(os.environ, {"SITE_INVENTORY_ADMIN_WRITE_ENABLED": "true"}):
            with self.assertRaises(HTTPException) as product_error:
                self._apply(ambiguous["digest"])
        self.assertEqual(product_error.exception.status_code, 409)
        with self.Session() as db:
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(site_id=self.site_id).count(), 0)

    def test_reconciliation_can_preserve_negative_target_but_normal_outbound_still_fails(self):
        self._add_sources(rollout_qty=2)
        preview = self._snapshot()
        row = next(item for item in preview["items"] if item["site_id"] == self.site_id)
        self.assertEqual(row["open_in_field_qty"], Decimal("-2.000000"))
        with patch.dict(os.environ, {"SITE_INVENTORY_ADMIN_WRITE_ENABLED": "true"}):
            self._apply(preview["digest"])

        with self.Session() as db:
            balance = db.query(SiteBalance).filter_by(
                program="FTTH", site_id=self.site_id, product_id=self.product_id,
            ).one()
            self.assertEqual(balance.on_hand_qty, Decimal("-2.000000"))
            with self.assertRaises(HTTPException) as blocked:
                with db.begin_nested():
                    inventory.post_site_movement(
                        db,
                        program="FTTH", site_id=self.site_id, product_id=self.product_id,
                        movement_type="outgoing", event_type="normal_outbound",
                        quantity_delta=Decimal("-1"), uom="PCS",
                        document_type="test_outbound", document_id=self.suffix,
                        document_line_id="1", reference=f"OUT-{self.suffix}",
                        note="Synthetic outbound test", created_by=self.actor, approved_by=self.actor,
                        effective_date="2026-11-14", request_token=f"normal-outbound-{self.suffix}",
                    )
            self.assertEqual(blocked.exception.status_code, 409)

    def test_apply_rolls_back_every_row_if_any_movement_fails(self):
        second_product = Product(
            program="FTTH", sku=f"OFR2-{self.suffix}",
            name="HubBox", unit="PCS", status="active",
        )
        with self.Session() as db:
            with db.begin():
                db.add(second_product)
                db.flush()
                second_product_id = second_product.id
                for index, product_id in enumerate((self.product_id, second_product_id), start=1):
                    requisition = MaterialRequisition(
                        program="FTTH", order_number=f"OFR-{self.suffix}-MR{index}",
                        warehouse_id=self.warehouse_id, site_id=self.site_name, status="issued",
                    )
                    db.add(requisition)
                    db.flush()
                    db.add(MaterialRequisitionItem(
                        requisition_id=requisition.id, product_id=product_id,
                        description="Synthetic material", quantity=4, uom="PCS",
                    ))
        preview = self._snapshot()
        self.assertEqual(len([row for row in preview["items"] if row["delta"]]), 2)
        original = main.post_site_movement
        calls = 0

        def fail_after_second(*args, **kwargs):
            nonlocal calls
            movement = original(*args, **kwargs)
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic post-movement failure")
            return movement

        with patch.dict(os.environ, {"SITE_INVENTORY_ADMIN_WRITE_ENABLED": "true"}), patch.object(
            main, "post_site_movement", side_effect=fail_after_second,
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic post-movement failure"):
                self._apply(preview["digest"])
        with self.Session() as db:
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(
                site_id=self.site_id, event_type="open_field_reconciliation",
            ).count(), 0)
            self.assertEqual(db.query(SiteBalance).filter_by(site_id=self.site_id).count(), 0)
            db.query(Product).filter_by(id=second_product_id).delete(synchronize_session=False)


if __name__ == "__main__":
    unittest.main()
