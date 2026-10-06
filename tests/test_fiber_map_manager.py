import os
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from starlette.requests import Request

TEST_DB_PATH = Path(tempfile.gettempdir()) / f"fiber-map-manager-{os.getpid()}.db"
os.environ["FTTH_DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH.as_posix()}"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH.as_posix()}"
os.environ.pop("SR_DATABASE_URL", None)
os.environ["SESSION_SECRET"] = "isolated-test-session-secret-at-least-thirty-two-characters-long"

import main
from database import SessionLocal


class FiberMapManagerTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        main.clear_rollout_db_cache()
        main.engine.dispose()
        try:
            TEST_DB_PATH.unlink()
        except FileNotFoundError:
            pass

    def setUp(self):
        if main.engine.dialect.name != "sqlite":
            raise RuntimeError("Tests must use an isolated SQLite database, never production.")
        self.db = SessionLocal()
        self.db.query(main.AuditLog).delete()
        self.db.query(main.FiberMapArea).delete()
        boxes = []
        for splitter in range(1, 4):
            boxes.append({
                "Area": "Test Map Manager",
                "Zone": "Test Map Manager",
                "City": "Tripoli",
                "Related to XBOX": "X1",
                "XBOX": "X-BOX01",
                "Part": "Part01",
                "Hub": "H1",
                "Line": 1,
                "Splitter": splitter,
                "Box code": f"H1-L1-S{splitter}",
                "Box type": "SUB BOX" if splitter < 3 else "END BOX",
                "Real length m": "",
                "Cable length m": 50,
                "Material type": "Single-Core Distribution Cable_50m",
                "dB": "",
            })
        self.db.add(main.FiberMapArea(
            program="FTTH",
            area="Test Map Manager",
            city="Tripoli",
            start_date="",
            end_date="",
            target_users=0,
            design_data=json.dumps({"boxes": boxes, "routes": []}),
            created_by="test",
        ))
        self.db.commit()
        main.clear_rollout_db_cache()
        main.ROLLOUT_CODE_REFERENCE_CACHE.clear()
        self.request = self.make_request("Admin")

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    @staticmethod
    def make_request(role, program="FTTH"):
        return Request({
            "type": "http",
            "method": "POST",
            "path": "/api/warehouse/fiber-map-manager/change",
            "headers": [],
            "query_string": b"",
            "state": {
                "program": program,
                "current_user": SimpleNamespace(role=role, name="Test Admin", username="testadmin"),
            },
        })

    def test_delete_and_readd_multiple_codes_round_trip_through_database(self):
        area = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["name"] == "Test Map Manager")
        self.assertIn("X1", [row["xbox"] for row in area["topology"]])
        self.assertIn(50, area["cable_lengths"])
        selected = area["active"][:2]
        payload = main.FiberMapChangeIn(
            area=area["name"],
            action="delete",
            selections=[{"xbox": row["xbox"], "code": row["code"]} for row in selected],
            expected_revision=area["revision"],
        )
        result = main.fiber_map_manager_change(payload, self.request, self.db)
        self.assertEqual(result["changed"], len(selected))

        updated_area = next(
            row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"]
            if row["name"] == area["name"]
        )
        active_keys = {(row["xbox"], main.rollout_code_key(row["code"])) for row in updated_area["active"]}
        for row in selected:
            self.assertNotIn((row["xbox"], main.rollout_code_key(row["code"])), active_keys)

        stale = main.FiberMapChangeIn(
            area=area["name"],
            action="add",
            selections=[{"xbox": row["xbox"], "code": row["code"], "box_type": row["box_type"], "cable_length_m": row["cable_length_m"]} for row in selected],
            expected_revision=area["revision"],
        )
        with self.assertRaises(HTTPException) as conflict:
            main.fiber_map_manager_change(stale, self.request, self.db)
        self.assertEqual(conflict.exception.status_code, 409)

        delete_payload = main.FiberMapChangeIn(
            area=area["name"],
            action="add",
            selections=[{"xbox": row["xbox"], "code": row["code"], "box_type": row["box_type"], "cable_length_m": row["cable_length_m"]} for row in selected],
            expected_revision=updated_area["revision"],
        )
        added = main.fiber_map_manager_change(delete_payload, self.request, self.db)
        self.assertEqual(added["changed"], len(selected))
        final_area = next(
            row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"]
            if row["name"] == area["name"]
        )
        final_keys = {(row["xbox"], main.rollout_code_key(row["code"])) for row in final_area["active"]}
        for row in selected:
            self.assertIn((row["xbox"], main.rollout_code_key(row["code"])), final_keys)
        field_codes = main.rollout_code_reference_rows(self.db, "FTTH")
        for row in selected:
            related = [item for item in field_codes if item["area"] == "Test Map Manager" and item["xbox"] == row["xbox"] and main.rollout_code_key(item["code"]) == main.rollout_code_key(row["code"])]
            self.assertEqual({item["type"] for item in related}, {"box", "cable"})

    def test_requester_can_add_and_delete_map_boxes(self):
        request = self.make_request("Requester")
        options = main.fiber_map_manager_get_options(request, self.db)
        self.assertEqual(options.status_code, 200)
        area = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["name"] == "Test Map Manager")
        selected = area["active"][0]
        removed = main.fiber_map_manager_change(main.FiberMapChangeIn(
            area=area["name"],
            action="delete",
            selections=[{"xbox": selected["xbox"], "code": selected["code"]}],
            expected_revision=area["revision"],
        ), request, self.db)
        self.assertEqual(removed["changed"], 1)
        updated = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["name"] == area["name"])
        restored = main.fiber_map_manager_change(main.FiberMapChangeIn(
            area=area["name"],
            action="add",
            selections=[{"xbox": selected["xbox"], "code": selected["code"], "box_type": selected["box_type"], "cable_length_m": selected["cable_length_m"]}],
            expected_revision=updated["revision"],
        ), request, self.db)
        self.assertEqual(restored["changed"], 1)

    def test_management_role_cannot_change_map(self):
        area = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["active"])
        payload = main.FiberMapChangeIn(
            area=area["name"],
            action="delete",
            selections=[{"xbox": area["active"][0]["xbox"], "code": area["active"][0]["code"]}],
            expected_revision=area["revision"],
        )
        with self.assertRaises(HTTPException) as denied:
            main.fiber_map_manager_change(payload, self.make_request("Management"), self.db)
        self.assertEqual(denied.exception.status_code, 403)

    def test_requester_map_access_is_limited_to_ftth(self):
        with self.assertRaises(HTTPException) as denied:
            main.fiber_map_manager_get_options(self.make_request("Requester", "SINGLE_RAN"), self.db)
        self.assertEqual(denied.exception.status_code, 403)

    def test_generated_code_adds_to_map_and_field_entry_without_catalog_row(self):
        area = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["name"] == "Test Map Manager")
        payload = main.FiberMapChangeIn(
            area=area["name"],
            action="add",
            selections=[{"xbox": "X1", "code": "H1-L1-S4", "box_type": "END BOX", "cable_length_m": 37.5}],
            expected_revision=area["revision"],
        )
        result = main.fiber_map_manager_change(payload, self.request, self.db)
        self.assertEqual(result["changed"], 1)
        updated = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["name"] == "Test Map Manager")
        self.assertIn(("X1", "H1L1S4"), {(row["xbox"], main.rollout_code_key(row["code"])) for row in updated["active"]})
        field_rows = main.rollout_code_reference_rows(self.db, "FTTH")
        selected = [row for row in field_rows if row["area"] == "Test Map Manager" and row["xbox"] == "X1" and main.rollout_code_key(row["code"]) == "H1L1S4"]
        self.assertEqual({row["type"] for row in selected}, {"box", "cable"})
        self.assertTrue(all(row["cable_length_m"] == 37.5 for row in selected))

    def test_hay_andalus_z1_can_add_h3_line_four_before_it_exists(self):
        boxes = []
        for line in range(1, 4):
            boxes.append({
                "Area": "Hay Al Andalus Z1",
                "Zone": "Hay Al Andalus Z1",
                "City": "Tripoli",
                "Related to XBOX": "X4",
                "XBOX": "X-BOX4",
                "Part": "X4-Part01",
                "Hub": "H3",
                "Line": line,
                "Splitter": 1,
                "Box code": f"H3-L{line}-S1",
                "Box type": "SUB BOX",
                "Real length m": "",
                "Cable length m": 50,
                "Material type": "Single-Core Distribution Cable_50m",
                "dB": "",
            })
        self.db.add(main.FiberMapArea(
            program="FTTH",
            area="Hay Al Andalus Z1",
            city="Tripoli",
            start_date="",
            end_date="",
            target_users=0,
            design_data=json.dumps({"boxes": boxes, "routes": []}),
            created_by="test",
        ))
        self.db.commit()

        area = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["name"] == "Hay Al Andalus Z1")
        h3 = next(row for row in next(row for row in area["topology"] if row["xbox"] == "X4")["hubs"] if row["hub"] == "H3")
        self.assertIn("L4", h3["lines"])

        result = main.fiber_map_manager_change(main.FiberMapChangeIn(
            area=area["name"],
            action="add",
            selections=[{"xbox": "X4", "code": "H3-L4-S1", "box_type": "SUB BOX", "cable_length_m": 80}],
            expected_revision=area["revision"],
        ), self.request, self.db)
        self.assertEqual(result["changed"], 1)
        updated = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["name"] == area["name"])
        self.assertIn(("X4", "H3L4S1"), {(row["xbox"], main.rollout_code_key(row["code"])) for row in updated["active"]})
        field_rows = main.rollout_code_reference_rows(self.db, "FTTH")
        selected = [row for row in field_rows if row["area"] == area["name"] and row["xbox"] == "X4" and main.rollout_code_key(row["code"]) == "H3L4S1"]
        self.assertEqual({row["type"] for row in selected}, {"box", "cable"})


if __name__ == "__main__":
    unittest.main()
