import os
import unittest
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException, Request
from types import SimpleNamespace
from sqlalchemy.exc import IntegrityError


os.environ["APP_ENV"] = "test"
os.environ["TEST_DATABASE_URL"] = "sqlite://"
os.environ["SESSION_SECRET"] = "foundation-local-test-secret-at-least-32-chars"

import main
import site_inventory_service as inventory
from database import SessionLocal, engine
from migrations.site_inventory_foundation import _validate_schema, migrate
from models import AuditLog, Product, Site, SiteAlias, SiteBalance, SiteMaterialMovement, SiteOpeningBalanceRequest


class SiteInventoryFoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if engine.dialect.name != "sqlite" or os.getenv("APP_ENV") != "test":
            raise RuntimeError("Local Foundation tests require an explicit isolated SQLite TEST_DATABASE_URL")
        with engine.connect() as connection:
            before = set(engine.dialect.get_table_names(connection))
        cls.foundation_tables = {
            "site_aliases", "site_balances", "site_material_movements", "site_opening_balance_requests"
        }
        if before & cls.foundation_tables:
            raise RuntimeError("Foundation tables were created by startup instead of the explicit migration")
        migrate(engine, schema=None)
        cls.second_migration = migrate(engine, schema=None)

    @classmethod
    def tearDownClass(cls):
        engine.dispose(close=True)

    def setUp(self):
        self.program = "FTTH"
        suffix = uuid4().hex[:10]
        with SessionLocal() as db:
            self.site = Site(program=self.program, name=f"Foundation Test {suffix}", site_inventory_enabled=True)
            self.product = Product(program=self.program, sku=f"FOUND-{suffix}", name="Fiber cable", unit="M")
            db.add_all([self.site, self.product])
            db.commit()
            self.site_id, self.product_id = self.site.id, self.product.id

    def _post(self, *, quantity, token=None, event_id=None, movement_type="incoming", uom="M", program=None,
              site_id=None, product_id=None, event_type=None, document_type="manual_test", line_id="1"):
        program_key = program or self.program
        amount = Decimal(str(quantity))
        delta = amount if movement_type != "outgoing" else -amount
        token = token or uuid4().hex
        with SessionLocal() as db:
            with db.begin():
                row = inventory.post_site_movement(
                    db,
                    program=program_key,
                    site_id=site_id or self.site_id,
                    product_id=product_id or self.product_id,
                    movement_type=movement_type,
                    event_type=event_type or movement_type,
                    quantity_delta=delta,
                    uom=uom,
                    document_type=document_type,
                    document_id=event_id or token,
                    document_line_id=line_id,
                    reference=f"REF-{event_id or token}",
                    note="foundation test movement",
                    created_by="tester",
                    approved_by="approver",
                    effective_date="2026-11-06",
                    request_token=token,
                )
                return row.id, row.source_event_key

    def _balance(self):
        with SessionLocal() as db:
            row = db.query(SiteBalance).filter_by(
                program=self.program, site_id=self.site_id, product_id=self.product_id
            ).first()
            return None if row is None else row.on_hand_qty

    def _opening(self, *, quantity="20", token=None):
        with SessionLocal() as db:
            with db.begin():
                row = inventory.create_opening_balance_request(
                    db, program=self.program, site_id=self.site_id, product_id=self.product_id,
                    quantity=quantity, uom="M", effective_date="2026-11-06",
                    reference="PHYSICAL-COUNT", note="Approved physical count", entered_by="Aysar",
                    request_token=token or uuid4().hex,
                )
                return row.id

    def test_startup_excludes_foundation_and_migration_is_versioned(self):
        with engine.connect() as connection:
            tables = set(engine.dialect.get_table_names(connection))
        self.assertTrue(self.foundation_tables.issubset(tables))
        self.assertTrue(self.second_migration["already_applied"])
        self.assertEqual(_validate_schema("warehouse_app", "postgresql"), "warehouse_app")
        with self.assertRaises(ValueError):
            _validate_schema("bad-schema", "postgresql")

    def test_site_inventory_activation_defaults_off_and_admin_toggle_is_audited(self):
        suffix = uuid4().hex[:10]
        with SessionLocal() as db:
            with db.begin():
                site = Site(program=self.program, name=f"Activation Test {suffix}")
                db.add(site)
                db.flush()
                site_id = site.id
                self.assertFalse(site.site_inventory_enabled)

        with SessionLocal() as db:
            with db.begin():
                enabled = inventory.set_site_inventory_enabled(
                    db, program=self.program, site_id=site_id, enabled=True, actor="Admin Test",
                )
                self.assertTrue(enabled.site_inventory_enabled)
        with SessionLocal() as db:
            with db.begin():
                disabled = inventory.set_site_inventory_enabled(
                    db, program=self.program, site_id=site_id, enabled=False, actor="Admin Test",
                )
                self.assertFalse(disabled.site_inventory_enabled)
        with SessionLocal() as db:
            actions = db.query(AuditLog.action).filter(
                AuditLog.entity_type == "site_inventory", AuditLog.entity_id == str(site_id),
            ).all()
        self.assertEqual({action for (action,) in actions}, {
            "site_inventory_site_activated", "site_inventory_site_deactivated",
        })

    def test_default_site_bootstrap_never_activates_sites_and_admin_can_activate_later(self):
        suffix = uuid4().hex[:10]
        existing_inactive_name = f"Bootstrap inactive {suffix}"
        existing_active_name = f"Bootstrap active {suffix}"
        missing_name = f"Bootstrap missing {suffix}"
        site_ids = []

        try:
            with SessionLocal() as db:
                existing_inactive = Site(
                    program=self.program, name=existing_inactive_name, site_inventory_enabled=False,
                )
                existing_active = Site(
                    program=self.program, name=existing_active_name, site_inventory_enabled=True,
                )
                db.add_all([existing_inactive, existing_active])
                db.commit()
                site_ids = [existing_inactive.id, existing_active.id]

            with patch.object(
                main, "DEFAULT_SITE_IDS",
                (existing_inactive_name, existing_active_name, missing_name),
            ), patch.object(main, "get_sessionmaker", return_value=SessionLocal):
                main.ensure_default_site_ids()

            with SessionLocal() as db:
                inactive = db.query(Site).filter_by(
                    program=self.program, name=existing_inactive_name,
                ).one()
                active = db.query(Site).filter_by(
                    program=self.program, name=existing_active_name,
                ).one()
                bootstrapped = db.query(Site).filter_by(
                    program=self.program, name=missing_name,
                ).one()
                site_ids.append(bootstrapped.id)
                self.assertFalse(inactive.site_inventory_enabled)
                self.assertTrue(active.site_inventory_enabled)
                self.assertFalse(bootstrapped.site_inventory_enabled)
                bootstrapped_id = bootstrapped.id

            from site_inventory_api import SiteInventoryActivationUpdate

            activation_endpoint = next(
                route.endpoint for route in main.app.routes
                if getattr(route, "path", None) == "/api/warehouse/site-inventory/sites/{site_id}"
                and "PATCH" in getattr(route, "methods", set())
            )
            request = Request({"type": "http", "headers": [], "query_string": b""})
            request.state.current_user = SimpleNamespace(
                role="Admin", name="Admin Test", username="admin",
            )
            with patch.dict(os.environ, {"SITE_INVENTORY_ADMIN_WRITE_ENABLED": "true"}):
                with SessionLocal() as db:
                    activation_endpoint(
                        bootstrapped_id,
                        SiteInventoryActivationUpdate(program=self.program, enabled=True),
                        request,
                        db,
                    )
            with SessionLocal() as db:
                self.assertTrue(db.get(Site, bootstrapped_id).site_inventory_enabled)
        finally:
            if site_ids:
                with SessionLocal() as db:
                    with db.begin():
                        db.query(AuditLog).filter(
                            AuditLog.entity_type == "site_inventory",
                            AuditLog.entity_id.in_([str(site_id) for site_id in site_ids]),
                        ).delete(synchronize_session=False)
                        db.query(Site).filter(Site.id.in_(site_ids)).delete(synchronize_session=False)

    def test_new_ftth_site_created_through_admin_api_is_active_by_default(self):
        site_name = f"New FTTH Area {uuid4().hex[:10]}"
        request = Request({"type": "http", "headers": [], "query_string": b""})
        request.state.current_user = SimpleNamespace(role="Admin", name="Admin", username="admin")
        with SessionLocal() as db:
            response = main.create_site(main.SiteIn(name=site_name, program="FTTH"), request, db)
            row = db.get(Site, response["site"]["id"])
            self.assertTrue(row.site_inventory_enabled)
            db.delete(row)
            db.commit()

    def test_new_non_ftth_site_created_through_admin_api_is_inactive_by_default(self):
        site_name = f"New Single RAN Area {uuid4().hex[:10]}"
        request = Request({"type": "http", "headers": [], "query_string": b""})
        request.state.current_user = SimpleNamespace(role="Admin", name="Admin", username="admin")
        with SessionLocal() as db:
            response = main.create_site(main.SiteIn(name=site_name, program="SINGLE_RAN"), request, db)
            row = db.get(Site, response["site"]["id"])
            self.assertFalse(row.site_inventory_enabled)
            db.delete(row)
            db.commit()

    def test_inactive_site_rejects_opening_and_manual_movement_without_state_changes(self):
        suffix = uuid4().hex[:10]
        with SessionLocal() as db:
            with db.begin():
                site = Site(program=self.program, name=f"Inactive Site {suffix}")
                db.add(site)
                db.flush()
                inactive_site_id = site.id

        with self.assertRaises(HTTPException) as movement_error:
            self._post(quantity="3", site_id=inactive_site_id, event_id=f"INACTIVE-{suffix}")
        self.assertEqual(movement_error.exception.status_code, 409)
        self.assertEqual(movement_error.exception.detail, "Site Inventory is not active for this Site.")

        with SessionLocal() as db:
            with self.assertRaises(HTTPException) as opening_error:
                with db.begin():
                    inventory.create_opening_balance_request(
                        db, program=self.program, site_id=inactive_site_id, product_id=self.product_id,
                        quantity="3", uom="M", effective_date="2026-11-06",
                        reference=f"INACTIVE-{suffix}", note="Should be blocked", entered_by="tester",
                        request_token=f"inactive-opening-{suffix}-token",
                    )
            self.assertEqual(opening_error.exception.status_code, 409)
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(site_id=inactive_site_id).count(), 0)
            self.assertEqual(db.query(SiteBalance).filter_by(site_id=inactive_site_id).count(), 0)
            self.assertEqual(db.query(SiteOpeningBalanceRequest).filter_by(site_id=inactive_site_id).count(), 0)

    def test_alias_normalization_and_collision_rejection(self):
        with SessionLocal() as db:
            with db.begin():
                alias = inventory.create_site_alias(
                    db, program=self.program, site_id=self.site_id, display_alias="North-East / A", actor="admin"
                )
                self.assertEqual(alias.normalized_alias, "north east a")
        with SessionLocal() as db:
            with db.begin():
                resolved = inventory.resolve_site_alias(db, self.program, "NORTH east-a")
                self.assertEqual(resolved.id, self.site_id)
        with SessionLocal() as db:
            with self.assertRaises(HTTPException) as collision:
                with db.begin():
                    inventory.create_site_alias(
                        db, program=self.program, site_id=self.site_id, display_alias="north east a", actor="admin"
                    )
            self.assertEqual(collision.exception.status_code, 409)

    def test_alias_colliding_with_canonical_site_is_rejected(self):
        with SessionLocal() as db:
            with db.begin():
                with self.assertRaises(HTTPException) as error:
                    inventory.create_site_alias(
                        db, program=self.program, site_id=self.site_id,
                        display_alias=self.site.name.upper(), actor="admin",
                    )
            self.assertEqual(error.exception.status_code, 409)

    def test_alias_becoming_ambiguous_after_site_creation_is_rejected(self):
        with SessionLocal() as db:
            with db.begin():
                inventory.create_site_alias(
                    db, program=self.program, site_id=self.site_id,
                    display_alias="Future Site Name", actor="admin",
                )
        with SessionLocal() as db:
            with db.begin():
                db.add(Site(program=self.program, name="Future-Site-Name"))
        with SessionLocal() as db:
            with self.assertRaises(HTTPException) as error:
                inventory.resolve_site_alias(db, self.program, "future site name")
            self.assertEqual(error.exception.status_code, 409)

    def test_uom_must_match_product_unit(self):
        with self.assertRaises(HTTPException) as error:
            self._post(quantity="3", uom="PCS")
        self.assertEqual(error.exception.status_code, 400)
        self.assertIsNone(self._balance())

    def test_opening_balance_posts_only_on_separate_approval(self):
        request_id = self._opening()
        self.assertIsNone(self._balance())
        with SessionLocal() as db:
            with self.assertRaises(HTTPException) as same_actor:
                with db.begin():
                    inventory.approve_opening_balance(db, request_id, self.program, "Aysar")
            self.assertEqual(same_actor.exception.status_code, 403)
        with SessionLocal() as db:
            with db.begin():
                movement = inventory.approve_opening_balance(db, request_id, self.program, "Approver")
                movement_id = movement.id
        self.assertEqual(self._balance(), 20)
        with SessionLocal() as db:
            movement = db.get(SiteMaterialMovement, movement_id)
            opening = db.get(SiteOpeningBalanceRequest, request_id)
            self.assertEqual(movement.movement_type, "opening_balance")
            self.assertEqual(movement.event_type, "opening_balance_approved")
            self.assertEqual(opening.status, "approved")
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(site_id=self.site_id).count(), 1)

    def test_source_event_is_deterministic_and_deduplicated(self):
        event_key_1 = inventory.source_event_key("ftth", "field_issue", "manual", "DOC-1", "LINE-1")
        event_key_2 = inventory.source_event_key("FTTH", "field_issue", "manual", "DOC-1", "LINE-1")
        self.assertEqual(event_key_1, event_key_2)
        first_id, first_key = self._post(quantity="7", event_id="DOC-1", token="a" * 32)
        retry_id, retry_key = self._post(quantity="7", event_id="DOC-1", token="b" * 32)
        self.assertEqual((first_id, first_key), (retry_id, retry_key))
        self.assertEqual(self._balance(), 7)
        with self.assertRaises(HTTPException) as conflict:
            self._post(quantity="8", event_id="DOC-1", token="c" * 32)
        self.assertEqual(conflict.exception.status_code, 409)

    def test_request_idempotency_is_program_scoped_and_legacy_retry_safe(self):
        token = "shared-idempotency-token-1234"
        first_id, _ = self._post(quantity="4", token=token, event_id="FTTH-DOC")
        with SessionLocal() as db:
            with db.begin():
                sr_site = Site(program="SINGLE_RAN", name=f"SR {uuid4().hex[:8]}", site_inventory_enabled=True)
                sr_product = Product(program="SINGLE_RAN", sku=f"SR-{uuid4().hex[:8]}", name="SR material", unit="M")
                db.add_all([sr_site, sr_product])
                db.flush()
                sr_ids = (sr_site.id, sr_product.id)
        sr_id, _ = self._post(
            quantity="6", token=token, event_id="SR-DOC", program="SINGLE_RAN",
            site_id=sr_ids[0], product_id=sr_ids[1],
        )
        self.assertNotEqual(first_id, sr_id)

        legacy_token = "legacy-phase-one-token-12345"
        legacy_hash = inventory.legacy_request_token_hash(legacy_token)
        legacy_event = inventory.source_event_key(self.program, "incoming", "manual_test", "LEGACY-DOC", "1")
        with SessionLocal() as db:
            with db.begin():
                legacy_site = Site(program=self.program, name=f"Legacy {uuid4().hex[:8]}", site_inventory_enabled=True)
                legacy_product = Product(program=self.program, sku=f"LEGACY-{uuid4().hex[:8]}", name="Legacy material", unit="M")
                db.add_all([legacy_site, legacy_product])
                db.flush()
                balance = SiteBalance(program=self.program, site_id=legacy_site.id, product_id=legacy_product.id, on_hand_qty=5)
                db.add(balance)
                db.flush()
                legacy = SiteMaterialMovement(
                    program=self.program, site_id=legacy_site.id, product_id=legacy_product.id,
                    movement_type="incoming", event_type="incoming", quantity_delta=5, uom="M",
                    document_type="manual_test", document_id="LEGACY-DOC", document_line_id="1",
                    source_event_key=legacy_event, reference="REF-LEGACY-DOC", note="foundation test movement",
                    created_by="tester", approved_by="approver", effective_date="2026-11-06",
                    status="approved", idempotency_key=legacy_hash, request_token_hash=legacy_hash,
                )
                db.add(legacy)
                db.flush()
                legacy_id, legacy_site_id, legacy_product_id = legacy.id, legacy_site.id, legacy_product.id
        retried_id, _ = self._post(
            quantity="5", token=legacy_token, event_id="LEGACY-DOC",
            site_id=legacy_site_id, product_id=legacy_product_id,
        )
        self.assertEqual(retried_id, legacy_id)
        with SessionLocal() as db:
            balance = db.query(SiteBalance).filter_by(
                program=self.program, site_id=legacy_site_id, product_id=legacy_product_id
            ).one()
            self.assertEqual(balance.on_hand_qty, 5)

    def test_negative_balance_is_rejected(self):
        with self.assertRaises(HTTPException) as error:
            self._post(quantity="1", movement_type="outgoing")
        self.assertEqual(error.exception.status_code, 409)
        self.assertIsNone(self._balance())

    def test_negative_initial_opening_can_be_recovered_but_not_worsened(self):
        request_id = self._opening(quantity="-5")
        with SessionLocal() as db:
            with db.begin():
                inventory.approve_opening_balance(db, request_id, self.program, "Approver")
        self.assertEqual(self._balance(), Decimal("-5"))

        with self.assertRaises(HTTPException) as outgoing:
            self._post(quantity="1", movement_type="outgoing")
        self.assertEqual(outgoing.exception.status_code, 409)
        self.assertEqual(self._balance(), Decimal("-5"))

        self._post(quantity="3", event_id="NEGATIVE-RECOVERY-1")
        self.assertEqual(self._balance(), Decimal("-2"))
        self._post(quantity="20", event_id="NEGATIVE-RECOVERY-2")
        self.assertEqual(self._balance(), Decimal("18"))
        with SessionLocal() as db:
            rows = inventory.reconciliation_rows(db, self.program, self.site_id, self.product_id)
            self.assertEqual(rows[0]["status"], "MATCH")

    def test_negative_opening_is_rejected_after_prior_site_history(self):
        self._post(quantity="2", event_id="PRIOR-HISTORY")
        with self.assertRaises(HTTPException) as opening:
            self._opening(quantity="-1")
        self.assertEqual(opening.exception.status_code, 409)
        self.assertEqual(self._balance(), Decimal("2"))

    def test_failure_rolls_back_movement_balance_and_audit(self):
        with SessionLocal() as db:
            before = db.query(AuditLog).filter_by(entity_type="site_inventory").count()
        with patch.object(inventory, "_stage", side_effect=lambda stage: (_ for _ in ()).throw(RuntimeError("injected")) if stage == "after_movement" else None):
            with self.assertRaises(RuntimeError):
                self._post(quantity="9")
        self.assertIsNone(self._balance())
        with SessionLocal() as db:
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(site_id=self.site_id).count(), 0)
            self.assertEqual(db.query(AuditLog).filter_by(entity_type="site_inventory").count(), before)

    def test_movements_are_append_only_and_corrections_are_reversals(self):
        movement_id, _ = self._post(quantity="12", event_id="REVERSAL-BASE")
        with SessionLocal() as db:
            with self.assertRaises(IntegrityError):
                with db.begin():
                    row = db.get(SiteMaterialMovement, movement_id)
                    row.note = "tampered"
        with SessionLocal() as db:
            with self.assertRaises(IntegrityError):
                with db.begin():
                    db.delete(db.get(SiteMaterialMovement, movement_id))
        with SessionLocal() as db:
            with db.begin():
                reversal = inventory.reverse_site_movement(
                    db, movement_id, self.program, "approver", "CORRECTION", "Reverse test posting", "reversal-token-123456"
                )
                reversal_id = reversal.id
        self.assertEqual(self._balance(), 0)
        with SessionLocal() as db:
            original = db.get(SiteMaterialMovement, movement_id)
            reversal = db.get(SiteMaterialMovement, reversal_id)
            self.assertEqual(original.note, "foundation test movement")
            self.assertEqual(reversal.reverses_movement_id, movement_id)
            self.assertEqual(reversal.quantity_delta, -original.quantity_delta)
        with SessionLocal() as db:
            with self.assertRaises(HTTPException) as duplicate:
                with db.begin():
                    inventory.reverse_site_movement(
                        db, movement_id, self.program, "approver", "CORRECTION-2", "Second reverse", "reversal-token-654321"
                    )
            self.assertEqual(duplicate.exception.status_code, 409)

    def test_reconciliation_is_read_only_and_rebuild_requires_preview_digest(self):
        self._post(quantity="10", event_id="REBUILD-BASE")
        with SessionLocal() as db:
            with db.begin():
                row = db.query(SiteBalance).filter_by(site_id=self.site_id, product_id=self.product_id).one()
                row.on_hand_qty = 8
        with SessionLocal() as db:
            rows = inventory.reconciliation_rows(db, self.program, self.site_id, self.product_id)
            self.assertEqual(rows[0]["status"], "MISMATCH")
            self.assertEqual(rows[0]["balance_qty"], 8)
            preview = inventory.rebuild_site_balances(db, self.program)
        self.assertEqual(preview["mode"], "preview")
        self.assertEqual(preview["mismatch_count"], 1)
        self.assertEqual(self._balance(), 8)
        with SessionLocal() as db:
            with self.assertRaises(HTTPException) as stale:
                with db.begin():
                    inventory.rebuild_site_balances(db, self.program, apply=True, expected_digest="stale", actor="admin")
            self.assertEqual(stale.exception.status_code, 409)
        movement_count_before = None
        with SessionLocal() as db:
            movement_count_before = db.query(SiteMaterialMovement).filter_by(site_id=self.site_id).count()
        with SessionLocal() as db:
            with db.begin():
                result = inventory.rebuild_site_balances(
                    db, self.program, apply=True, expected_digest=preview["preview_digest"], actor="admin"
                )
        self.assertEqual(result["mode"], "applied")
        self.assertEqual(self._balance(), 10)
        with SessionLocal() as db:
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(site_id=self.site_id).count(), movement_count_before)
            audit = db.query(AuditLog).filter_by(action="site_inventory_projection_rebuilt", entity_id=self.program).one()
            self.assertIn('"movement_history_changed": false', audit.details)
            rows = inventory.reconciliation_rows(db, self.program, self.site_id, self.product_id)
            self.assertEqual(rows[0]["status"], "MATCH")

    def test_opening_request_program_scoped_token(self):
        token = "opening-scope-token-123456"
        first = self._opening(quantity="2", token=token)
        same = self._opening(quantity="2", token=token)
        self.assertEqual(first, same)
        with self.assertRaises(HTTPException) as conflict:
            self._opening(quantity="3", token=token)
        self.assertEqual(conflict.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main(verbosity=2)
