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


class DummySession:
    def begin(self):
        return nullcontext()

    def query(self, _model):
        return EmptyQuery()


class EmptyQuery:
    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return []


class SiteInventoryAuthorizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        router = site_inventory_api.build_site_inventory_router(
            lambda: None, main.normalize_program, main.require_roles, main.request_actor
        )
        cls.endpoints = {route.path: route.endpoint for route in router.routes}

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
        with patch.object(site_inventory_api, "post_site_movement", return_value=movement) as post:
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

    def test_opening_approval_roles_remain_admin_and_approval(self):
        endpoint = self.endpoints["/api/warehouse/site-inventory/opening-balances/{request_id}/approve"]
        movement = SimpleNamespace(id=12)
        for role in ("Admin", "Approval"):
            with self.subTest(role=role), patch.object(
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

        with patch.object(site_inventory_api, "rebuild_site_balances", return_value={
            "mode": "applied", "items": [],
        }) as rebuild:
            result = endpoint(payload, self.make_request("Admin"), DummySession())
        self.assertEqual(result["mode"], "applied")
        rebuild.assert_called_once()

    def test_reversal_permissions_are_unchanged_admin_and_approval(self):
        endpoint = self.endpoints["/api/warehouse/site-inventory/movements/{movement_id}/reverse"]
        reversal = SimpleNamespace(id=13, reverses_movement_id=10)
        payload = site_inventory_api.SiteMovementReverse(
            program="FTTH", reference="REV-TEST", note="Synthetic reversal authorization test",
            request_token="reversal-authorization-test-001",
        )
        for role in ("Admin", "Approval"):
            with self.subTest(role=role), patch.object(
                site_inventory_api, "reverse_site_movement", return_value=reversal
            ) as reverse:
                result = endpoint(10, payload, self.make_request(role), DummySession())
                self.assertEqual(result["reverses_movement_id"], 10)
                reverse.assert_called_once()
        for role in ("Requester", "Technician", "Warehouse Manager", "Management"):
            with self.subTest(role=role), patch.object(site_inventory_api, "reverse_site_movement") as reverse:
                with self.assertRaises(HTTPException) as error:
                    endpoint(10, payload, self.make_request(role), DummySession())
                self.assertEqual(error.exception.status_code, 403)
                reverse.assert_not_called()


if __name__ == "__main__":
    unittest.main()
