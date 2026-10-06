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

    def test_hay_andalus_z1_missing_lines_require_activation_before_add(self):
        boxes = []
        for xbox, hub in (("X4", "H3"), ("X1", "H1")):
            for line in range(1, 4):
                boxes.append({
                    "Area": "Hay Al Andalus Z1",
                    "Zone": "Hay Al Andalus Z1",
                    "City": "Tripoli",
                    "Related to XBOX": xbox,
                    "XBOX": f"X-BOX{xbox[1:]}",
                    "Part": f"{xbox}-Part01",
                    "Hub": hub,
                    "Line": line,
                    "Splitter": 1,
                    "Box code": f"{hub}-L{line}-S1",
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
        for xbox, hub in (("X4", "H3"), ("X1", "H1")):
            hub_options = next(row for row in next(row for row in area["topology"] if row["xbox"] == xbox)["hubs"] if row["hub"] == hub)
            self.assertNotIn("L4", hub_options["lines"])
            candidate = next(row for row in area["line_candidates"] if row["xbox"] == xbox and row["hub"] == hub and row["line"] == "L4")
            self.assertFalse(candidate["enabled"])

        with self.assertRaises(HTTPException) as not_enabled:
            main.fiber_map_manager_change(main.FiberMapChangeIn(
                area=area["name"],
                action="add",
                selections=[{"xbox": "X1", "code": "H1-L4-S1", "box_type": "SUB BOX", "cable_length_m": 80}],
                expected_revision=area["revision"],
            ), self.request, self.db)
        self.assertEqual(not_enabled.exception.status_code, 400)

        for xbox, hub in (("X4", "H3"), ("X1", "H1")):
            enabled = main.fiber_map_manager_toggle_line(main.FiberMapLineToggleIn(
                area=area["name"], xbox=xbox, hub=hub, line="L4", expected_revision=area["revision"],
            ), self.request, self.db)
            self.assertTrue(enabled["changed"])

        area = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["name"] == "Hay Al Andalus Z1")
        for xbox, hub in (("X4", "H3"), ("X1", "H1")):
            hub_options = next(row for row in next(row for row in area["topology"] if row["xbox"] == xbox)["hubs"] if row["hub"] == hub)
            self.assertIn("L4", hub_options["lines"])

        result = main.fiber_map_manager_change(main.FiberMapChangeIn(
            area=area["name"],
            action="add",
            selections=[
                {"xbox": "X4", "code": "H3-L4-S1", "box_type": "SUB BOX", "cable_length_m": 80},
                {"xbox": "X1", "code": "H1-L4-S1", "box_type": "SUB BOX", "cable_length_m": 80},
            ],
            expected_revision=area["revision"],
        ), self.request, self.db)
        self.assertEqual(result["changed"], 2)
        updated = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["name"] == area["name"])
        active = {(row["xbox"], main.rollout_code_key(row["code"])) for row in updated["active"]}
        self.assertIn(("X4", "H3L4S1"), active)
        self.assertIn(("X1", "H1L4S1"), active)
        field_rows = main.rollout_code_reference_rows(self.db, "FTTH")
        for xbox, code in (("X4", "H3L4S1"), ("X1", "H1L4S1")):
            selected = [row for row in field_rows if row["area"] == area["name"] and row["xbox"] == xbox and main.rollout_code_key(row["code"]) == code]
            self.assertEqual({row["type"] for row in selected}, {"box", "cable"})
        with self.assertRaises(HTTPException) as occupied:
            main.fiber_map_manager_toggle_line(main.FiberMapLineToggleIn(
                area=area["name"], xbox="X1", hub="H1", line="L4", enabled=False, expected_revision=area["revision"],
            ), self.request, self.db)
        self.assertEqual(occupied.exception.status_code, 409)

    def test_confirmed_line_candidates_are_exact_and_activation_creates_no_box_rows(self):
        expected = {
            ("hayalandaluszone1", "X1", "H1", "L4"),
            ("hayalandaluszone1", "X3", "H4", "L3"),
            ("hayalandaluszone1", "X3", "H4", "L4"),
            ("hayalandaluszone1", "X4", "H3", "L4"),
            ("hayalandaluszone1", "X4", "H8", "L4"),
            ("awladbaeoo", "X1", "H3", "L4"),
            ("awladbaeoo", "X1", "H7", "L4"),
            ("awladbaeoo", "X1", "H9", "L4"),
            ("awladbaeoo", "X1", "H11", "L3"),
            ("awladbaeoo", "X1", "H11", "L4"),
            ("awladbaeoo", "X2", "H9", "L4"),
            ("berawtaleem", "X1", "H1", "L4"),
            ("berawtaleem", "X1", "H3", "L4"),
            ("berawtaleem", "X1", "H5", "L4"),
            ("berawtaleem", "X1", "H7", "L4"),
            ("berawtaleem", "X2", "H7", "L4"),
            ("berawtaleem", "X2", "H8", "L4"),
            ("berawtaleem", "X2", "H9", "L4"),
        }
        actual = {
            (area, xbox, hub, line)
            for area, xboxes in main.FIBER_MAP_MANAGER_LINE_CANDIDATES.items()
            for xbox, hubs in xboxes.items()
            for hub, lines in hubs.items()
            for line in lines
        }
        self.assertEqual(actual, expected)

        area = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["name"] == "Hay Al Andalus Z1")
        candidates = {
            (row["xbox"], row["hub"], row["line"]): row
            for row in area["line_candidates"]
        }
        self.assertEqual(len(candidates), 5)
        self.assertTrue(all(not row["enabled"] for row in candidates.values()))
        saved_area_count = self.db.query(main.FiberMapArea).count()

        result = main.fiber_map_manager_toggle_line(main.FiberMapLineToggleIn(
            area=area["name"], xbox="X1", hub="H1", line="L4", expected_revision=area["revision"],
        ), self.request, self.db)
        self.assertTrue(result["changed"])
        self.assertEqual(self.db.query(main.FiberMapArea).count(), saved_area_count)
        updated = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["name"] == area["name"])
        self.assertTrue(next(row for row in updated["line_candidates"] if row["xbox"] == "X1" and row["hub"] == "H1" and row["line"] == "L4")["enabled"])
        self.assertFalse(any(row["xbox"] == "X1" and main.rollout_code_key(row["code"]).startswith("H1L4") for row in updated["active"]))
        self.assertFalse(any(
            row["area"] == area["name"] and row["xbox"] == "X1" and main.rollout_code_key(row["code"]).startswith("H1L4")
            for row in main.rollout_code_reference_rows(self.db, "FTTH")
        ))

        disabled = main.fiber_map_manager_toggle_line(main.FiberMapLineToggleIn(
            area=area["name"], xbox="X1", hub="H1", line="L4", enabled=False, expected_revision=result["revision"],
        ), self.request, self.db)
        self.assertTrue(disabled["changed"])
        updated = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["name"] == area["name"])
        self.assertFalse(next(row for row in updated["line_candidates"] if row["xbox"] == "X1" and row["hub"] == "H1" and row["line"] == "L4")["enabled"])


if __name__ == "__main__":
    unittest.main()
