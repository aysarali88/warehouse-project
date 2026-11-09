import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4


os.environ["APP_ENV"] = "test"
os.environ["TEST_DATABASE_URL"] = "sqlite://"
os.environ["SESSION_SECRET"] = "rollout-consumption-local-test-secret-at-least-32-characters"

import main
import site_inventory_service as inventory
from database import SessionLocal, engine
from migrations.site_inventory_foundation import migrate
from migrations.site_inventory_rollout_cutover import migrate as migrate_rollout_cutover
from models import AuditLog, Product, RolloutRecord, Site, SiteBalance, SiteMaterialMovement


class RolloutConsumptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site_inventory_env = patch.dict(os.environ, {
            "SITE_INVENTORY_CUTOVER_PROGRAMS": "FTTH",
            "SITE_INVENTORY_ROLLOUT_ENABLED": "true",
            "SITE_INVENTORY_CUTOVER_AT": "2026-11-01T00:00:00+02:00",
        })
        cls.site_inventory_env.start()
        if os.getenv("APP_ENV") != "test" or engine.dialect.name != "sqlite":
            raise RuntimeError("Local rollout consumption tests require an isolated SQLite test database")
        migrate(engine, schema=None)
        migrate_rollout_cutover(engine, schema=None)
        cls.cutover_migration_repeat = migrate_rollout_cutover(engine, schema=None)

    @classmethod
    def tearDownClass(cls):
        engine.dispose(close=True)
        cls.site_inventory_env.stop()

    def make_fixture(self, *, opening=50, area=None, record_area=None, unit="M", status="Done", actual=9):
        suffix = uuid4().hex[:12]
        site_name = area or f"Rollout Consumption Site {suffix}"
        with SessionLocal() as db:
            with db.begin():
                site = Site(program="FTTH", name=site_name)
                product = Product(
                    program="FTTH", sku=f"ROLLOUT-CONSUME-{suffix}",
                    name=f"Rollout material {suffix}", unit=unit,
                )
                db.add_all([site, product])
                db.flush()
                inventory.post_site_movement(
                    db,
                    program="FTTH", site_id=site.id, product_id=product.id,
                    movement_type="incoming", event_type="test_site_opening",
                    quantity_delta=opening, uom=unit, document_type="test_fixture",
                    document_id=suffix, document_line_id="opening", reference=f"fixture-{suffix}",
                    note="Synthetic local test opening", created_by="test", approved_by="test",
                    effective_date="2026-11-07",
                )
                row = RolloutRecord(
                    record_id=f"RDP-CONSUME-{suffix}", date="2026-11-07", area=record_area or site_name,
                    city="Misurata", item="Cable", material_type="Single-Core Distribution Cable_50m",
                    actual=actual, status=status, related_to_xbox="X1",
                    site_inventory_recorded_at=datetime.now(timezone.utc),
                )
                db.add(row)
                db.flush()
                return {
                    "site_id": site.id, "site_name": site_name, "product_id": product.id,
                    "record_id": row.record_id, "record_pk": row.id, "suffix": suffix,
                }

    def request(self, role="Admin"):
        return SimpleNamespace(state=SimpleNamespace(
            current_user=SimpleNamespace(
                role=role, name=f"{role} actor", username=f"{role.lower()}-actor", warehouse_name="",
            ),
            program="FTTH",
        ))

    def confirm(self, fixture, *, role="Admin", product_id=None, quantity="6", uom="M", record_id=None):
        with SessionLocal() as db:
            return main.confirm_rollout_field_entry_consumption(
                record_id or fixture["record_id"],
                main.RolloutConsumptionConfirmationIn(
                    product_id=product_id or fixture["product_id"],
                    confirmed_consumed_quantity=Decimal(quantity),
                    uom=uom,
                    effective_date="2026-11-07",
                ),
                self.request(role),
                db,
            )

    def site_quantity(self, fixture):
        with SessionLocal() as db:
            return Decimal(str(db.query(SiteBalance).filter_by(
                program="FTTH", site_id=fixture["site_id"], product_id=fixture["product_id"],
            ).one().on_hand_qty))

    def consumption_movements(self, fixture):
        with SessionLocal() as db:
            return db.query(SiteMaterialMovement).filter_by(
                program="FTTH", document_type="rollout_record",
                document_id=str(fixture["record_pk"]), event_type="rollout_consumption_confirmed",
            ).all()

    def test_normal_done_field_entry_with_actual_does_not_consume_inventory(self):
        fixture = self.make_fixture()
        with SessionLocal() as db:
            result = main.save_rollout_field_entry(
                {
                    "submission_key": f"rollout-entry-{fixture['suffix']}",
                    "Area": fixture["site_name"], "Related to XBOX": "X1",
                    "code_type": "accessory", "item": "Accessories",
                    "material_type": "Plum ring hook", "actual": 8, "status": "Done",
                },
                self.request("Requester"), db,
            )
            self.assertEqual(result["record"]["staus"], "Done")
        self.assertEqual(self.site_quantity(fixture), Decimal("50.000000"))
        self.assertEqual(self.consumption_movements(fixture), [])

    def test_disabled_rollout_integration_rejects_confirmation_without_posting(self):
        fixture = self.make_fixture()
        with patch.dict(os.environ, {
            "SITE_INVENTORY_ROLLOUT_ENABLED": "false",
            "SITE_INVENTORY_CUTOVER_PROGRAMS": "",
        }):
            with self.assertRaises(main.HTTPException) as error:
                self.confirm(fixture)
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(self.site_quantity(fixture), Decimal("50.000000"))
        self.assertEqual(self.consumption_movements(fixture), [])

    def test_pre_cutover_and_legacy_records_are_ineligible(self):
        pre_cutover = self.make_fixture()
        with SessionLocal() as db:
            row = db.query(RolloutRecord).filter_by(record_id=pre_cutover["record_id"]).one()
            row.date = "2026-10-31"
            db.commit()
        with self.assertRaises(main.HTTPException) as activity_error:
            self.confirm(pre_cutover)
        self.assertEqual(activity_error.exception.status_code, 409)
        self.assertEqual(self.consumption_movements(pre_cutover), [])

        legacy = self.make_fixture()
        with SessionLocal() as db:
            row = db.query(RolloutRecord).filter_by(record_id=legacy["record_id"]).one()
            row.site_inventory_recorded_at = None
            db.commit()
        with self.assertRaises(main.HTTPException) as legacy_error:
            self.confirm(legacy)
        self.assertEqual(legacy_error.exception.status_code, 409)
        self.assertEqual(self.consumption_movements(legacy), [])

    def test_explicit_confirmation_decreases_site_balance_and_records_event(self):
        fixture = self.make_fixture(actual=20)
        result = self.confirm(fixture, quantity="6.25")
        self.assertTrue(result["consumption_confirmed"])
        self.assertFalse(result["replayed"])
        self.assertEqual(result["site_id"], fixture["site_id"])
        self.assertEqual(self.site_quantity(fixture), Decimal("43.750000"))
        movement = self.consumption_movements(fixture)[0]
        self.assertEqual(movement.product_id, fixture["product_id"])
        self.assertEqual(movement.quantity_delta, Decimal("-6.250000"))
        self.assertEqual(movement.uom, "M")
        self.assertEqual(movement.document_line_id, "consumption")
        self.assertEqual(movement.created_by, "Admin actor")

    def test_retry_is_idempotent_and_does_not_double_consume(self):
        fixture = self.make_fixture()
        first = self.confirm(fixture, quantity="6")
        retry = self.confirm(fixture, quantity="6")
        self.assertFalse(first["replayed"])
        self.assertTrue(retry["replayed"])
        self.assertEqual(first["movement_id"], retry["movement_id"])
        self.assertEqual(self.site_quantity(fixture), Decimal("44.000000"))
        self.assertEqual(len(self.consumption_movements(fixture)), 1)

    def test_reusing_event_with_different_quantity_is_rejected(self):
        fixture = self.make_fixture()
        self.confirm(fixture, quantity="6")
        with self.assertRaises(main.HTTPException) as error:
            self.confirm(fixture, quantity="7")
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.site_quantity(fixture), Decimal("44.000000"))
        self.assertEqual(len(self.consumption_movements(fixture)), 1)

    def test_non_admin_roles_cannot_confirm_consumption(self):
        fixture = self.make_fixture()
        for role in ("Requester", "Technician", "Warehouse Manager", "Management", "Approval"):
            with self.subTest(role=role), self.assertRaises(main.HTTPException) as error:
                self.confirm(fixture, role=role)
            self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(self.site_quantity(fixture), Decimal("50.000000"))
        self.assertEqual(self.consumption_movements(fixture), [])

    def test_unauthenticated_request_cannot_confirm_consumption(self):
        fixture = self.make_fixture()
        request = SimpleNamespace(state=SimpleNamespace(program="FTTH"))
        with SessionLocal() as db:
            with self.assertRaises(main.HTTPException) as error:
                main.confirm_rollout_field_entry_consumption(
                    fixture["record_id"],
                    main.RolloutConsumptionConfirmationIn(
                        product_id=fixture["product_id"],
                        confirmed_consumed_quantity=Decimal("1"), uom="M",
                        effective_date="2026-11-07",
                    ),
                    request,
                    db,
                )
        self.assertEqual(error.exception.status_code, 401)

    def test_insufficient_site_inventory_rejects_without_partial_movement(self):
        fixture = self.make_fixture(opening=2)
        with self.assertRaises(main.HTTPException) as error:
            self.confirm(fixture, quantity="3")
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.site_quantity(fixture), Decimal("2.000000"))
        self.assertEqual(self.consumption_movements(fixture), [])

    def test_unresolved_site_product_and_uom_fail_closed(self):
        unknown_area = self.make_fixture(record_area="Unmapped Rollout Area")
        with self.assertRaises(main.HTTPException) as unresolved:
            self.confirm(unknown_area)
        self.assertEqual(unresolved.exception.status_code, 409)

        fixture = self.make_fixture()
        with self.assertRaises(main.ValidationError):
            main.RolloutConsumptionConfirmationIn(
                confirmed_consumed_quantity=Decimal("1"), uom="M",
            )
        with self.assertRaises(main.HTTPException) as missing_product:
            self.confirm(fixture, product_id=99999999)
        self.assertEqual(missing_product.exception.status_code, 404)
        with self.assertRaises(main.HTTPException) as bad_uom:
            self.confirm(fixture, uom="PCS")
        self.assertEqual(bad_uom.exception.status_code, 400)
        self.assertEqual(self.site_quantity(fixture), Decimal("50.000000"))
        self.assertEqual(self.consumption_movements(fixture), [])

    def test_non_done_entry_cannot_be_confirmed_even_with_actual_quantity(self):
        fixture = self.make_fixture(status="In Progress", actual=30)
        with self.assertRaises(main.HTTPException) as error:
            self.confirm(fixture)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.site_quantity(fixture), Decimal("50.000000"))
        self.assertEqual(self.consumption_movements(fixture), [])

    def test_confirmed_entry_cannot_be_edited_deleted_or_upserted(self):
        fixture = self.make_fixture()
        self.confirm(fixture)
        for operation in ("edit", "delete", "upsert"):
            with self.subTest(operation=operation), SessionLocal() as db:
                with self.assertRaises(main.HTTPException) as error:
                    if operation == "edit":
                        main.edit_rollout_field_entry(fixture["record_id"], {}, self.request("Admin"), db)
                    elif operation == "delete":
                        main.delete_rollout_field_entry(fixture["record_id"], {}, self.request("Admin"), db)
                    else:
                        main.save_record({"ID": fixture["record_id"], "Area": "Changed"}, self.request("Admin"), db)
                self.assertEqual(error.exception.status_code, 409)
                db.rollback()
        with SessionLocal() as db:
            self.assertIsNotNone(db.query(RolloutRecord).filter_by(record_id=fixture["record_id"]).first())
        self.assertEqual(self.site_quantity(fixture), Decimal("44.000000"))
        self.assertEqual(len(self.consumption_movements(fixture)), 1)

    def test_reversal_restores_site_balance_without_removing_history(self):
        fixture = self.make_fixture()
        self.confirm(fixture, quantity="6")
        original = self.consumption_movements(fixture)[0]
        original_id = original.id
        reversal_id = None
        with SessionLocal() as db:
            with db.begin():
                reversal = inventory.reverse_site_movement(
                    db, original_id, "FTTH", "Admin actor", "CORRECTION-1",
                    "Reverse incorrect confirmed consumption", "rollout-reversal-token-0001",
                )
                reversal_id = reversal.id
                self.assertEqual(reversal.reverses_movement_id, original_id)
        self.assertEqual(self.site_quantity(fixture), Decimal("50.000000"))
        with SessionLocal() as db:
            rows = db.query(SiteMaterialMovement).filter(
                SiteMaterialMovement.id.in_([original_id, reversal_id]),
            ).all()
            self.assertEqual(len(rows), 2)
            self.assertEqual({row.quantity_delta for row in rows}, {Decimal("-6.000000"), Decimal("6.000000")})
        with self.assertRaises(main.HTTPException) as retried_after_reversal:
            self.confirm(fixture, quantity="6")
        self.assertEqual(retried_after_reversal.exception.status_code, 409)
        self.assertEqual(self.site_quantity(fixture), Decimal("50.000000"))

    def test_failure_after_balance_update_rolls_back_movement_balance_and_audit(self):
        fixture = self.make_fixture()
        with SessionLocal() as db:
            audit_count_before = db.query(AuditLog).filter_by(action="site_inventory_movement_posted").count()

        def fail_after_balance(stage):
            if stage == "after_balance":
                raise RuntimeError("injected rollout consumption failure")

        with patch.object(inventory, "_stage", side_effect=fail_after_balance):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.confirm(fixture)
        self.assertEqual(self.site_quantity(fixture), Decimal("50.000000"))
        self.assertEqual(self.consumption_movements(fixture), [])
        with SessionLocal() as db:
            self.assertEqual(
                db.query(AuditLog).filter_by(action="site_inventory_movement_posted").count(),
                audit_count_before,
            )


if __name__ == "__main__":
    unittest.main()
