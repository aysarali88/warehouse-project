import os
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

os.environ["APP_ENV"] = "test"
os.environ["TEST_DATABASE_URL"] = "sqlite://"
os.environ["SESSION_SECRET"] = "site-inventory-authorization-test-secret-32"

from fastapi import HTTPException, Request

import main
import site_inventory_api
from models import Product, Site, SiteBalance, SiteOpeningBalanceRequest


class DummySession:
    def __init__(self):
        self.begin_calls = 0

    def begin(self):
        self.begin_calls += 1
        return nullcontext()

    def query(self, _model):
        return EmptyQuery()


class EmptyQuery:
    def filter_by(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return []

    def first(self):
        return None

    def count(self):
        return 0

    def offset(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self


class SiteInventoryAuthorizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        router = site_inventory_api.build_site_inventory_router(
            lambda: None, main.normalize_program, main.require_roles, main.request_actor
        )
        cls.endpoints = {route.path: route.endpoint for route in router.routes}
        cls.get_endpoints = {
            route.path: route.endpoint for route in router.routes if "GET" in route.methods
        }

    def make_request(self, role=None):
        request = Request({"type": "http", "headers": [], "query_string": b""})
        if role is not None:
            request.state.current_user = SimpleNamespace(role=role, name=role, username=role.lower())
        return request

    def movement_payload(self):
        return site_inventory_api.SiteMovementCreate(
            program="FTTH", site_id=1, product_id=1, movement_type="incoming", quantity="1",
            direction="increase", uom="M", event_type="manual_adjustment", document_type="manual",
            document_id="AUTH-TEST-1", document_line_id="1", reference="AUTH-TEST-1",
            note="Synthetic authorization test", request_token="authorization-test-token-0001",
        )

    def test_manual_movement_is_admin_only(self):
        endpoint = self.endpoints["/api/warehouse/site-inventory/movements"]
        denied_roles = [None, "Requester", "Technician", "Warehouse Manager", "Management", "Approval"]
        for role in denied_roles:
            with self.subTest(role=role):
                with patch.object(site_inventory_api, "post_site_movement") as post:
                    with self.assertRaises(HTTPException) as error:
                        endpoint(self.movement_payload(), self.make_request(role), DummySession())
                    self.assertEqual(error.exception.status_code, 401 if role is None else 403)
                    post.assert_not_called()

        movement = SimpleNamespace(id=11, source_event_key="synthetic-event-key")
        with patch.dict(os.environ, {"SITE_INVENTORY_ADMIN_WRITE_ENABLED": "true"}), patch.object(
            site_inventory_api, "post_site_movement", return_value=movement
        ) as post:
            result = endpoint(self.movement_payload(), self.make_request("Admin"), DummySession())
        self.assertTrue(result["success"])
        self.assertEqual(result["movement_id"], 11)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["created_by"], "Admin")

    def test_management_and_approval_keep_read_only_inventory_access(self):
        endpoint = self.endpoints["/api/warehouse/site-inventory"]
        for role in ("Management", "Approval"):
            with self.subTest(role=role):
                result = endpoint(self.make_request(role), db=DummySession())
                self.assertEqual(result, {"success": True, "items": []})

    def test_requester_can_read_transfer_options_without_opening_balance_details(self):
        endpoint = self.endpoints["/api/warehouse/site-inventory/options"]
        active_site = SimpleNamespace(id=8, name="Active FTTH", program="FTTH", site_inventory_enabled=True)
        inactive_site = SimpleNamespace(id=9, name="Historical FTTH", program="FTTH", site_inventory_enabled=False)
        product = SimpleNamespace(id=21, sku="TEST-21", name="Synthetic cable", program="FTTH", status="active", unit="M")
        pending = SimpleNamespace(
            id=31, program="FTTH", status="pending_approval", site_id=8,
            site=SimpleNamespace(name=active_site.name), product_id=21,
            product=SimpleNamespace(name=product.name), quantity=4, uom="M",
            effective_date="2026-11-14", reference="OPEN-TEST", note="private detail",
            entered_by="Admin",
        )
        queried_models = []

        class OptionsQuery:
            def __init__(self, model):
                self.model = model
                self.filters = {}
                queried_models.append(model)

            def filter_by(self, **kwargs):
                self.filters.update(kwargs)
                return self

            def order_by(self, *_args):
                return self

            def all(self):
                rows = {
                    Site: [active_site, inactive_site],
                    Product: [product],
                    SiteOpeningBalanceRequest: [pending],
                }[self.model]
                return [row for row in rows if all(getattr(row, key) == value for key, value in self.filters.items())]

        class OptionsDB:
            def query(self, model):
                return OptionsQuery(model)

        requester = self.make_request("Requester")
        requester.state.current_user.program = "FTTH"
        requester.state.session_program = "FTTH"
        result = endpoint(requester, program="FTTH", db=OptionsDB())
        self.assertEqual([row["id"] for row in result["sites"]], [8])
        self.assertEqual([row["id"] for row in result["products"]], [21])
        self.assertEqual(result["pending_openings"], [])
        self.assertNotIn(SiteOpeningBalanceRequest, queried_models)

        requester.state.session_program = "SINGLE_RAN"
        with self.assertRaises(HTTPException) as wrong_program:
            endpoint(requester, program="SINGLE_RAN", db=OptionsDB())
        self.assertEqual(wrong_program.exception.status_code, 403)

        admin = self.make_request("Admin")
        result = endpoint(admin, program="FTTH", db=OptionsDB())
        self.assertEqual(len(result["pending_openings"]), 1)

    def test_requester_balance_read_is_limited_to_one_active_site_and_product(self):
        endpoint = self.endpoints["/api/warehouse/site-inventory"]
        site = SimpleNamespace(id=8, program="FTTH", name="Active FTTH", site_inventory_enabled=True)
        product = SimpleNamespace(id=21, program="FTTH", sku="TEST-21", name="Synthetic cable", status="active", unit="M")
        balance = SimpleNamespace(
            program="FTTH", site_id=8, site=site, product_id=21, product=product,
            on_hand_qty=7, updated_at=None,
        )
        other_balance = SimpleNamespace(
            program="FTTH", site_id=14,
            site=SimpleNamespace(id=14, program="FTTH", name="Other FTTH", site_inventory_enabled=True),
            product_id=22,
            product=SimpleNamespace(id=22, program="FTTH", sku="TEST-22", name="Other material", unit="PCS"),
            on_hand_qty=99, updated_at=None,
        )
        queried_models = []

        class BalanceQuery:
            def __init__(self, model, site_row=site):
                self.model = model
                self.site_row = site_row
                self.criteria = {}
                queried_models.append(model)

            def filter_by(self, **kwargs):
                if self.model is Site and kwargs.get("site_inventory_enabled") is True:
                    if not self.site_row.site_inventory_enabled:
                        return EmptyQuery(self.model)
                if self.model is Product and kwargs.get("status") == "active":
                    if product.status != "active":
                        return EmptyQuery(self.model)
                return self

            def filter(self, *expressions):
                for expression in expressions:
                    self.criteria[expression.left.key] = expression.right.value
                return self

            def order_by(self, *_args):
                return self

            def first(self):
                if self.model is Site:
                    return self.site_row
                if self.model is Product:
                    return product
                return None

            def all(self):
                if self.model is not SiteBalance:
                    return []
                rows = [balance, other_balance]
                return [
                    row for row in rows
                    if all(getattr(row, key) == value for key, value in self.criteria.items())
                ]

        class BalanceDB:
            def __init__(self, site_row=site):
                self.site_row = site_row

            def query(self, model):
                return BalanceQuery(model, self.site_row)

        class EmptyQuery:
            def __init__(self, model):
                self.model = model

            def first(self):
                return None

        requester = self.make_request("Requester")
        requester.state.current_user.program = "FTTH"
        requester.state.session_program = "FTTH"
        with self.assertRaises(HTTPException) as broad_read:
            endpoint(requester, program="FTTH", site_id=None, product_id=None, db=BalanceDB())
        self.assertEqual(broad_read.exception.status_code, 400)

        result = endpoint(requester, program="FTTH", site_id=8, product_id=21, db=BalanceDB())
        self.assertEqual(result["items"][0]["on_hand_qty"], "7")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(queried_models, [Site, Product, SiteBalance])

        inactive_site = SimpleNamespace(id=8, program="FTTH", name="Inactive", site_inventory_enabled=False)
        with self.assertRaises(HTTPException) as inactive:
            endpoint(requester, program="FTTH", site_id=8, product_id=21, db=BalanceDB(inactive_site))
        self.assertEqual(inactive.exception.status_code, 404)

    def test_site_inventory_activation_is_admin_only_and_uses_the_existing_write_gate(self):
        update = self.endpoints["/api/warehouse/site-inventory/sites/{site_id}"]
        payload = site_inventory_api.SiteInventoryActivationUpdate(program="FTTH", enabled=True)
        for role in (None, "Requester", "Technician", "Warehouse Manager", "Management", "Approval"):
            db = DummySession()
            with self.subTest(role=role), patch.dict(os.environ, {}, clear=True), patch.object(
                site_inventory_api, "set_site_inventory_enabled"
            ) as activate:
                with self.assertRaises(HTTPException) as error:
                    update(5, payload, self.make_request(role), db)
                self.assertEqual(error.exception.status_code, 401 if role is None else 403)
                activate.assert_not_called()
                self.assertEqual(db.begin_calls, 0)

        db = DummySession()
        with patch.dict(os.environ, {}, clear=True), patch.object(
            site_inventory_api, "set_site_inventory_enabled"
        ) as activate:
            with self.assertRaises(HTTPException) as error:
                update(5, payload, self.make_request("Admin"), db)
            self.assertEqual(error.exception.status_code, 503)
            activate.assert_not_called()
            self.assertEqual(db.begin_calls, 0)

        site = SimpleNamespace(id=5, program="FTTH", name="Synthetic Site", site_inventory_enabled=True)
        with patch.dict(os.environ, {"SITE_INVENTORY_ADMIN_WRITE_ENABLED": "true"}), patch.object(
            site_inventory_api, "set_site_inventory_enabled", return_value=site
        ) as activate:
            result = update(5, payload, self.make_request("Admin"), DummySession())
        self.assertEqual(result["site"]["site_inventory_enabled"], True)
        activate.assert_called_once()

    def test_site_activation_status_list_is_read_only_while_writes_are_disabled(self):
        endpoint = self.endpoints["/api/warehouse/site-inventory/sites"]
        for role in (None, "Requester", "Technician", "Warehouse Manager", "Management", "Approval"):
            with self.subTest(role=role), patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(HTTPException) as error:
                    endpoint(self.make_request(role), db=DummySession())
                self.assertEqual(error.exception.status_code, 401 if role is None else 403)
        with patch.dict(os.environ, {}, clear=True):
            result = endpoint(self.make_request("Admin"), db=DummySession())
        self.assertEqual(result, {"success": True, "sites": []})

    def test_opening_approval_roles_remain_admin_and_approval(self):
        endpoint = self.endpoints["/api/warehouse/site-inventory/opening-balances/{request_id}/approve"]
        movement = SimpleNamespace(id=12)
        for role in ("Admin", "Approval"):
            with self.subTest(role=role), patch.dict(
                os.environ, {"SITE_INVENTORY_ADMIN_WRITE_ENABLED": "true"}
            ), patch.object(
                site_inventory_api, "approve_opening_balance", return_value=movement
            ) as approve:
                result = endpoint(3, self.make_request(role), db=DummySession())
                self.assertEqual(result["movement_id"], 12)
                approve.assert_called_once()
        with self.assertRaises(HTTPException) as error:
            endpoint(3, self.make_request("Management"), db=DummySession())
        self.assertEqual(error.exception.status_code, 403)

    def test_rebuild_apply_remains_admin_only(self):
        endpoint = self.endpoints["/api/warehouse/site-inventory/rebuild"]
        payload = site_inventory_api.ProjectionRebuildRequest(program="FTTH", apply=True, expected_digest="digest")
        for role in (None, "Requester", "Technician", "Warehouse Manager", "Management", "Approval"):
            with self.subTest(role=role), patch.object(site_inventory_api, "rebuild_site_balances") as rebuild:
                with self.assertRaises(HTTPException) as error:
                    endpoint(payload, self.make_request(role), DummySession())
                self.assertEqual(error.exception.status_code, 401 if role is None else 403)
                rebuild.assert_not_called()

        with patch.dict(os.environ, {"SITE_INVENTORY_ADMIN_WRITE_ENABLED": "true"}), patch.object(
            site_inventory_api, "rebuild_site_balances", return_value={
                "mode": "applied", "items": [],
            }
        ) as rebuild:
            result = endpoint(payload, self.make_request("Admin"), DummySession())
        self.assertEqual(result["mode"], "applied")
        rebuild.assert_called_once()

    def test_reversal_is_admin_only(self):
        endpoint = self.endpoints["/api/warehouse/site-inventory/movements/{movement_id}/reverse"]
        reversal = SimpleNamespace(id=13, reverses_movement_id=10)
        payload = site_inventory_api.SiteMovementReverse(
            program="FTTH", reference="REV-TEST", note="Synthetic reversal authorization test",
            request_token="reversal-authorization-test-001",
        )
        with patch.dict(os.environ, {"SITE_INVENTORY_ADMIN_WRITE_ENABLED": "true"}), patch.object(
            site_inventory_api, "reverse_site_movement", return_value=reversal
        ) as reverse:
            result = endpoint(10, payload, self.make_request("Admin"), DummySession())
            self.assertEqual(result["reverses_movement_id"], 10)
            reverse.assert_called_once()
        for role in (None, "Approval", "Management", "Warehouse Manager", "Technician", "Requester"):
            with self.subTest(role=role), patch.object(site_inventory_api, "reverse_site_movement") as reverse:
                with self.assertRaises(HTTPException) as error:
                    endpoint(10, payload, self.make_request(role), DummySession())
                self.assertEqual(error.exception.status_code, 401 if role is None else 403)
                reverse.assert_not_called()

    def test_cutover_readiness_is_admin_only_and_read_only(self):
        endpoint = self.endpoints["/api/warehouse/site-inventory/readiness"]
        for role in (None, "Requester", "Technician", "Warehouse Manager", "Management", "Approval"):
            with self.subTest(role=role):
                with self.assertRaises(HTTPException) as error:
                    endpoint(self.make_request(role), program="FTTH")
                self.assertEqual(error.exception.status_code, 401 if role is None else 403)

        with patch.dict(os.environ, {}, clear=True):
            result = endpoint(self.make_request("Admin"), program="FTTH")
        self.assertTrue(result["success"])
        self.assertFalse(result["configuration_ready"])
        self.assertFalse(result["admin_write_enabled"])
        self.assertTrue(all(not item["enabled"] for item in result["integrations"].values()))

    def test_all_admin_write_endpoints_fail_closed_before_database_mutation(self):
        admin = self.make_request("Admin")
        cases = [
            (
                "/api/warehouse/site-inventory/aliases",
                site_inventory_api.SiteAliasCreate(program="FTTH", site_id=1, display_alias="Synthetic alias"),
                "create_site_alias",
                lambda db, endpoint, payload: endpoint(payload, admin, db),
            ),
            (
                "/api/warehouse/site-inventory/opening-balances",
                site_inventory_api.OpeningBalanceCreate(
                    program="FTTH", site_id=1, product_id=1, quantity="10", uom="M",
                    effective_date="2026-11-09", reference="OPEN-TEST", note="test",
                    request_token="admin-write-opening-token-0001",
                ),
                "create_opening_balance_request",
                lambda db, endpoint, payload: endpoint(payload, admin, db),
            ),
            (
                "/api/warehouse/site-inventory/opening-balances/{request_id}/approve",
                10, "approve_opening_balance",
                lambda db, endpoint, payload: endpoint(payload, admin, db=db),
            ),
            (
                "/api/warehouse/site-inventory/opening-balances/{request_id}/reject",
                site_inventory_api.OpeningBalanceReject(program="FTTH", note="Reject synthetic request"),
                "reject_opening_balance",
                lambda db, endpoint, payload: endpoint(10, payload, admin, db=db),
            ),
            (
                "/api/warehouse/site-inventory/movements", self.movement_payload(),
                "post_site_movement",
                lambda db, endpoint, payload: endpoint(payload, admin, db),
            ),
            (
                "/api/warehouse/site-inventory/movements/{movement_id}/reverse",
                site_inventory_api.SiteMovementReverse(
                    program="FTTH", reference="REV-TEST", note="Synthetic reversal",
                    request_token="admin-write-reversal-token-0001",
                ),
                "reverse_site_movement",
                lambda db, endpoint, payload: endpoint(10, payload, admin, db=db),
            ),
            (
                "/api/warehouse/site-inventory/rebuild",
                site_inventory_api.ProjectionRebuildRequest(
                    program="FTTH", apply=True, expected_digest="preview-digest"
                ),
                "rebuild_site_balances",
                lambda db, endpoint, payload: endpoint(payload, admin, db),
            ),
        ]
        for path, payload, mutation_name, invoke in cases:
            db = DummySession()
            with self.subTest(path=path), patch.dict(os.environ, {}, clear=True), patch.object(
                site_inventory_api, mutation_name
            ) as mutation:
                with self.assertRaises(HTTPException) as error:
                    invoke(db, self.endpoints[path], payload)
                self.assertEqual(error.exception.status_code, 503)
                mutation.assert_not_called()
                self.assertEqual(db.begin_calls, 0)

    def test_read_only_endpoints_remain_available_when_admin_writes_are_off(self):
        db = DummySession()
        admin = self.make_request("Admin")
        with patch.dict(os.environ, {}, clear=True), patch.object(
            site_inventory_api, "resolve_site_alias",
            return_value=SimpleNamespace(id=1, name="Synthetic site", program="FTTH"),
        ), patch.object(site_inventory_api, "reconciliation_rows", return_value=[]), patch.object(
            site_inventory_api, "rebuild_site_balances", return_value={"mode": "preview", "items": []},
        ):
            self.assertTrue(self.endpoints["/api/warehouse/site-inventory/readiness"](admin)["success"])
            self.assertEqual(self.endpoints["/api/warehouse/site-inventory"](admin, db=db)["items"], [])
            self.assertEqual(self.get_endpoints["/api/warehouse/site-inventory/aliases"](admin, db=db)["items"], [])
            self.assertEqual(self.endpoints["/api/warehouse/site-inventory/aliases/resolve"](
                admin, alias="Synthetic", db=db
            )["site_id"], 1)
            self.assertEqual(self.endpoints["/api/warehouse/site-inventory/ledger"](
                admin, site_id=1, product_id=1, page=1, page_size=100, db=db
            )["items"], [])
            self.assertEqual(self.endpoints["/api/warehouse/site-inventory/reconciliation"](
                admin, db=db
            )["items"], [])
            self.assertEqual(self.endpoints["/api/warehouse/site-inventory/rebuild"](
                site_inventory_api.ProjectionRebuildRequest(program="FTTH", apply=False), admin, db
            )["mode"], "preview")
        self.assertEqual(db.begin_calls, 0)


if __name__ == "__main__":
    unittest.main()
