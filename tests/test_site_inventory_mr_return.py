import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException


os.environ["APP_ENV"] = "test"
os.environ["TEST_DATABASE_URL"] = "sqlite://"
os.environ["SESSION_SECRET"] = "mr-return-local-test-secret-at-least-32-characters"

import main
import site_inventory_service as inventory
from database import SessionLocal, engine
from migrations.site_inventory_foundation import migrate
from models import (
    AuditLog,
    MaterialReturn,
    MaterialReturnItem,
    Product,
    Site,
    SiteBalance,
    SiteMaterialMovement,
    StockBalance,
    StockMovement,
    Warehouse,
)


class MaterialReturnSiteInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getenv("APP_ENV") != "test" or engine.dialect.name != "sqlite":
            raise RuntimeError("Local material return tests require an isolated SQLite test database")
        migrate(engine, schema=None)

    @classmethod
    def tearDownClass(cls):
        engine.dispose(close=True)

    def make_fixture(self, *, opening=50, products=None):
        suffix = uuid4().hex[:10]
        with SessionLocal() as db:
            with db.begin():
                site = Site(program="FTTH", name=f"MR Return Site {suffix}")
                warehouse = Warehouse(program="FTTH", name=f"MR Return WH {suffix}")
                db.add_all([site, warehouse])
                db.flush()
                product_rows = {}
                for key, unit in (products or {"main": "PCS"}).items():
                    product = Product(
                        program="FTTH", sku=f"MR-RETURN-{suffix}-{key}",
                        name=f"MR return material {key}", unit=unit,
                    )
                    db.add(product)
                    db.flush()
                    product_rows[key] = product
                    db.add(StockBalance(
                        program="FTTH", warehouse_id=warehouse.id,
                        product_id=product.id, quantity=10,
                    ))
                    inventory.post_site_movement(
                        db,
                        program="FTTH",
                        site_id=site.id,
                        product_id=product.id,
                        movement_type="incoming",
                        event_type="test_site_opening",
                        quantity_delta=opening,
                        uom=unit,
                        document_type="test_fixture",
                        document_id=suffix,
                        document_line_id=key,
                        reference=f"fixture-{suffix}",
                        note="Synthetic local test opening",
                        created_by="test",
                        approved_by="test",
                        effective_date="2026-11-07",
                    )
                return {
                    "site_id": site.id,
                    "site_name": site.name,
                    "warehouse_id": warehouse.id,
                    "products": {key: product.id for key, product in product_rows.items()},
                    "suffix": suffix,
                }

    def request(self, role, warehouse_name=""):
        return SimpleNamespace(state=SimpleNamespace(current_user=SimpleNamespace(
            role=role, name=f"{role} actor", username=f"{role.lower()}-actor",
            warehouse_name=warehouse_name, program="FTTH",
        )))

    def create_pending(self, db, fixture, lines):
        return main.create_material_return(
            main.MaterialReturnIn(
                program="FTTH", site_id=fixture["site_name"],
                warehouse_id=fixture["warehouse_id"], returned_by="Field team",
                items=[main.MaterialReturnItemIn(**line) for line in lines],
            ),
            self.request("Requester"), db,
        )["return"]

    def approve(self, db, fixture, return_id):
        return main.approve_material_return(
            return_id,
            main.MaterialRequisitionActionIn(program="FTTH", actor="Warehouse Manager"),
            self.request("Admin"), db,
        )

    def site_qty(self, db, fixture, product_id):
        balance = db.query(SiteBalance).filter_by(
            program="FTTH", site_id=fixture["site_id"], product_id=product_id,
        ).one()
        return Decimal(str(balance.on_hand_qty))

    def test_pending_return_does_not_change_site_or_warehouse_inventory(self):
        fixture = self.make_fixture()
        product_id = fixture["products"]["main"]
        with SessionLocal() as db:
            pending = self.create_pending(db, fixture, [
                {"product_id": product_id, "quantity": 4, "condition": "Good"},
            ])
            self.assertEqual(pending["status"], "pending_warehouse")
            self.assertEqual(self.site_qty(db, fixture, product_id), Decimal("50.000000"))
            self.assertEqual(db.query(StockMovement).filter_by(reference=pending["return_number"]).count(), 0)
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(
                document_type="material_return", document_id=str(pending["id"]),
            ).count(), 0)
            self.assertEqual(db.query(StockBalance).filter_by(
                warehouse_id=fixture["warehouse_id"], product_id=product_id,
            ).one().quantity, 10)

    def test_confirmed_return_decreases_site_and_increases_warehouse_atomically(self):
        fixture = self.make_fixture(products={"a": "PCS", "b": "M"})
        first, second = fixture["products"].values()
        with SessionLocal() as db:
            pending = self.create_pending(db, fixture, [
                {"product_id": first, "quantity": 3},
                {"product_id": second, "quantity": 2},
            ])
            result = self.approve(db, fixture, pending["id"])
            self.assertEqual(result["return"]["status"], "confirmed")
            self.assertEqual(self.site_qty(db, fixture, first), Decimal("47.000000"))
            self.assertEqual(self.site_qty(db, fixture, second), Decimal("48.000000"))
            self.assertEqual(db.query(StockBalance).filter_by(
                warehouse_id=fixture["warehouse_id"], product_id=first,
            ).one().quantity, 13)
            self.assertEqual(db.query(StockBalance).filter_by(
                warehouse_id=fixture["warehouse_id"], product_id=second,
            ).one().quantity, 12)
            movements = db.query(SiteMaterialMovement).filter_by(
                document_type="material_return", document_id=str(pending["id"]),
            ).order_by(SiteMaterialMovement.document_line_id).all()
            self.assertEqual(len(movements), 2)
            self.assertEqual({movement.event_type for movement in movements}, {"material_return_received"})
            self.assertEqual({movement.quantity_delta for movement in movements}, {Decimal("-3.000000"), Decimal("-2.000000")})
            self.assertEqual({movement.site_id for movement in movements}, {fixture["site_id"]})
            self.assertEqual({movement.uom for movement in movements}, {"PCS", "M"})
            self.assertTrue(all(
                movement.source_event_key == inventory.source_event_key(
                    "FTTH", "material_return_received", "material_return",
                    str(pending["id"]), movement.document_line_id,
                )
                for movement in movements
            ))
            self.assertEqual(db.query(StockMovement).filter_by(reference=pending["return_number"]).count(), 2)
            self.assertEqual(db.query(AuditLog).filter_by(
                action="approve_material_return", entity_id=pending["return_number"],
            ).count(), 1)

    def test_damaged_receipt_decreases_site_once_and_keeps_quarantine_behavior(self):
        fixture = self.make_fixture()
        product_id = fixture["products"]["main"]
        with SessionLocal() as db:
            pending = self.create_pending(db, fixture, [
                {"product_id": product_id, "quantity": 4, "condition": "Damaged"},
            ])
            result = self.approve(db, fixture, pending["id"])
            self.assertEqual(result["damaged_quantity_quarantined"], 4)
            self.assertEqual(self.site_qty(db, fixture, product_id), Decimal("46.000000"))
            self.assertEqual(db.query(StockBalance).filter_by(
                warehouse_id=fixture["warehouse_id"], product_id=product_id,
            ).one().quantity, 10)
            site_moves = db.query(SiteMaterialMovement).filter_by(
                document_type="material_return", document_id=str(pending["id"]),
            ).all()
            self.assertEqual(len(site_moves), 1)
            self.assertEqual(site_moves[0].quantity_delta, Decimal("-4.000000"))
            warehouse_move = db.query(StockMovement).filter_by(
                reference=pending["return_number"], movement_type="damage_in",
            ).one()
            self.assertNotEqual(warehouse_move.warehouse_id, fixture["warehouse_id"])
            self.assertEqual(db.query(StockBalance).filter_by(
                warehouse_id=warehouse_move.warehouse_id, product_id=product_id,
            ).one().quantity, 4)

    def test_manager_direct_confirmation_posts_site_movement(self):
        fixture = self.make_fixture()
        product_id = fixture["products"]["main"]
        with SessionLocal() as db:
            result = main.create_material_return(
                main.MaterialReturnIn(
                    program="FTTH", site_id=fixture["site_name"],
                    warehouse_id=fixture["warehouse_id"], returned_by="Field team",
                    items=[main.MaterialReturnItemIn(product_id=product_id, quantity=5)],
                ),
                self.request("Admin"), db,
            )
            return_row = result["return"]
            self.assertEqual(return_row["status"], "confirmed")
            self.assertEqual(self.site_qty(db, fixture, product_id), Decimal("45.000000"))
            self.assertEqual(db.query(StockBalance).filter_by(
                warehouse_id=fixture["warehouse_id"], product_id=product_id,
            ).one().quantity, 15)
            movement = db.query(SiteMaterialMovement).filter_by(
                document_type="material_return", document_id=str(return_row["id"]),
            ).one()
            self.assertEqual(movement.quantity_delta, Decimal("-5.000000"))

    def test_duplicate_confirmation_does_not_post_twice(self):
        fixture = self.make_fixture()
        product_id = fixture["products"]["main"]
        with SessionLocal() as db:
            pending = self.create_pending(db, fixture, [
                {"product_id": product_id, "quantity": 5},
            ])
            self.approve(db, fixture, pending["id"])
            with self.assertRaises(HTTPException) as error:
                self.approve(db, fixture, pending["id"])
            self.assertEqual(error.exception.status_code, 400)
            self.assertEqual(self.site_qty(db, fixture, product_id), Decimal("45.000000"))
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(
                document_type="material_return", document_id=str(pending["id"]),
            ).count(), 1)
            self.assertEqual(db.query(StockMovement).filter_by(reference=pending["return_number"]).count(), 1)

    def test_insufficient_site_balance_rolls_back_warehouse_confirmation(self):
        fixture = self.make_fixture(opening=2)
        product_id = fixture["products"]["main"]
        with SessionLocal() as db:
            pending = self.create_pending(db, fixture, [
                {"product_id": product_id, "quantity": 3},
            ])
            with self.assertRaises(HTTPException) as error:
                self.approve(db, fixture, pending["id"])
            self.assertEqual(error.exception.status_code, 409)
            db.rollback()
        with SessionLocal() as db:
            self.assertEqual(self.site_qty(db, fixture, product_id), Decimal("2.000000"))
            self.assertEqual(db.query(StockBalance).filter_by(
                warehouse_id=fixture["warehouse_id"], product_id=product_id,
            ).one().quantity, 10)
            self.assertEqual(db.query(StockMovement).filter_by(
                reference=pending["return_number"],
            ).count(), 0)
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(
                document_type="material_return", document_id=str(pending["id"]),
            ).count(), 0)
            self.assertEqual(db.get(MaterialReturn, pending["id"]).status, "pending_warehouse")

    def test_unresolved_site_fails_closed_before_confirming_or_receiving_stock(self):
        fixture = self.make_fixture()
        product_id = fixture["products"]["main"]
        with SessionLocal() as db:
            with self.assertRaises(HTTPException) as error:
                main.create_material_return(
                    main.MaterialReturnIn(
                        program="FTTH", site_id="Unknown Site", warehouse_id=fixture["warehouse_id"],
                        returned_by="Field team",
                        items=[main.MaterialReturnItemIn(product_id=product_id, quantity=2)],
                    ),
                    self.request("Admin"), db,
                )
            self.assertEqual(error.exception.status_code, 409)
            db.rollback()
        with SessionLocal() as db:
            self.assertEqual(db.query(StockBalance).filter_by(
                warehouse_id=fixture["warehouse_id"], product_id=product_id,
            ).one().quantity, 10)
            self.assertEqual(db.query(MaterialReturn).filter(
                MaterialReturn.site_id == "Unknown Site",
            ).count(), 0)

    def test_ambiguous_site_name_fails_closed(self):
        fixture = self.make_fixture()
        product_id = fixture["products"]["main"]
        with SessionLocal() as db:
            db.add_all([
                Site(program="FTTH", name=f"Ambiguous-Yard-{fixture['suffix']}"),
                Site(program="FTTH", name=f"Ambiguous Yard {fixture['suffix']}"),
            ])
            db.commit()
            with self.assertRaises(HTTPException) as error:
                main.create_material_return(
                    main.MaterialReturnIn(
                        program="FTTH", site_id=f"Ambiguous Yard {fixture['suffix']}",
                        warehouse_id=fixture["warehouse_id"], returned_by="Field team",
                        items=[main.MaterialReturnItemIn(product_id=product_id, quantity=1)],
                    ),
                    self.request("Admin"), db,
                )
            self.assertEqual(error.exception.status_code, 409)
            db.rollback()

    def test_immediate_confirmed_receipt_rolls_back_on_site_post_failure(self):
        fixture = self.make_fixture()
        product_id = fixture["products"]["main"]
        with SessionLocal() as db:
            stock_movement_count = db.query(StockMovement).count()
            with patch.object(main, "post_material_return_site_movement", side_effect=HTTPException(503, "injected")):
                with self.assertRaises(HTTPException):
                    main.create_material_return(
                        main.MaterialReturnIn(
                            program="FTTH", site_id=fixture["site_name"],
                            warehouse_id=fixture["warehouse_id"], returned_by="Field team",
                            items=[main.MaterialReturnItemIn(product_id=product_id, quantity=2)],
                        ),
                        self.request("Admin"), db,
                    )
            db.rollback()
        with SessionLocal() as db:
            self.assertEqual(self.site_qty(db, fixture, product_id), Decimal("50.000000"))
            self.assertEqual(db.query(StockBalance).filter_by(
                warehouse_id=fixture["warehouse_id"], product_id=product_id,
            ).one().quantity, 10)
            self.assertEqual(db.query(StockMovement).count(), stock_movement_count)
            self.assertEqual(db.query(MaterialReturn).filter_by(
                program="FTTH", site_id=fixture["site_name"],
            ).count(), 0)

    def test_uom_change_between_request_and_confirmation_is_rejected(self):
        fixture = self.make_fixture()
        product_id = fixture["products"]["main"]
        with SessionLocal() as db:
            pending = self.create_pending(db, fixture, [
                {"product_id": product_id, "quantity": 2},
            ])
            db.query(Product).filter_by(id=product_id).one().unit = "M"
            db.commit()
            with self.assertRaises(HTTPException) as error:
                self.approve(db, fixture, pending["id"])
            self.assertEqual(error.exception.status_code, 400)
            db.rollback()
        with SessionLocal() as db:
            self.assertEqual(db.query(StockBalance).filter_by(
                warehouse_id=fixture["warehouse_id"], product_id=product_id,
            ).one().quantity, 10)
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(
                document_type="material_return", document_id=str(pending["id"]),
            ).count(), 0)

    def test_site_post_failure_rolls_back_full_confirmed_receipt(self):
        fixture = self.make_fixture()
        product_id = fixture["products"]["main"]
        with SessionLocal() as db:
            pending = self.create_pending(db, fixture, [
                {"product_id": product_id, "quantity": 2},
            ])
            with patch.object(main, "post_material_return_site_movement", side_effect=HTTPException(503, "injected")):
                with self.assertRaises(HTTPException):
                    self.approve(db, fixture, pending["id"])
            db.rollback()
        with SessionLocal() as db:
            self.assertEqual(self.site_qty(db, fixture, product_id), Decimal("50.000000"))
            self.assertEqual(db.query(StockBalance).filter_by(
                warehouse_id=fixture["warehouse_id"], product_id=product_id,
            ).one().quantity, 10)
            self.assertEqual(db.query(StockMovement).filter_by(
                reference=pending["return_number"],
            ).count(), 0)
            self.assertEqual(db.get(MaterialReturn, pending["id"]).status, "pending_warehouse")


if __name__ == "__main__":
    unittest.main()
