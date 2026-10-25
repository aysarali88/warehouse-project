import os
import json
import tempfile
import unittest
from io import BytesIO
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
        self.db.query(main.RolloutRecord).filter(main.RolloutRecord.area == "Test Map Manager").delete(synchronize_session=False)
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

    def test_bera_w_taleem_x2_qlc_routes_are_added_once_with_planned_lengths(self):
        reference = main.load_fiber_map_reference(self.db, "FTTH")
        expected = {
            "X2-H1": 300,
            "X2-H3": 300,
            "X2-H5": 500,
            "X2-H7": 500,
            "X2-H9": 500,
        }

        def bera_x2_routes():
            return [
                row for row in reference["routes"]
                if main.rollout_area_key(main.first_value(row, "Area", "Zone", default="")) == main.rollout_area_key("Bera W Taleem")
                and main.rollout_xbox_key(main.first_value(row, "Related to XBOX", "XBOX", default="")) == "X2"
                and main.first_value(row, "Route code", default="") in expected
            ]

        routes = bera_x2_routes()
        self.assertEqual({row["Route code"]: row["Cable length m"] for row in routes}, expected)
        self.assertEqual(sum(row["Cable length m"] for row in routes), 2100)
        x2_parts = {
            row["Part"]
            for row in reference["boxes"]
            if main.rollout_area_key(main.first_value(row, "Area", "Zone", default="")) == main.rollout_area_key("Bera W Taleem")
            and main.rollout_xbox_key(main.first_value(row, "Related to XBOX", "XBOX", default="")) == "X2"
        }
        self.assertEqual(x2_parts, {"Part01"})
        self.assertTrue(all(row["Part"] in x2_parts for row in routes))

        main.add_bera_w_taleem_x2_qlc_routes(reference)
        self.assertEqual(len(bera_x2_routes()), len(expected))

    def test_area_builder_preserves_one_decimal_actual_cable_lengths(self):
        workbook = main.Workbook()
        hub_index = workbook.active
        hub_index.title = "Hub Index"
        hub_index.append(["X-Box", "Hub", "From", "To Hub", "QLC actual (m)", "QLC precon (m)"])
        hub_index.append(["X1", "X1-H1", "X1", "X1-H2", 432.171714, 500])
        hub_index.append(["X1", "X1-H2", "X1-H1", None, 156.383944, 200])

        cable_data = workbook.create_sheet("Cable Data")
        cable_data.append(["Sub box", "Hub", "X-Box", "Line", "Sequence", "Actual length (m)", "Precon length (m)", "Splitter"])
        cable_data.append(["H1-L1-S1", "X1-H1", "X1", "L1", 1, 38, 80, "1:9"])

        contents = BytesIO()
        workbook.save(contents)
        payload = main.build_area_map_from_workbook(contents.getvalue(), "Test Area", "Misurata")
        actual_by_route = {row["Route code"]: row["Real length m"] for row in payload["routes"]}

        self.assertEqual(actual_by_route, {"X1-H1": 432.2, "H1-H2": 156.4})
        self.assertEqual(payload["boxes"][0]["Real length m"], 38)

    def test_area_builder_derives_end_box_from_final_splitter_and_hub_subbox_count(self):
        workbook = main.Workbook()
        hub_index = workbook.active
        hub_index.title = "Hub Index"
        hub_index.append(["X-Box", "Hub", "From", "To Hub", "QLC actual (m)", "QLC precon (m)", "Sub boxes"])
        hub_index.append(["X1", "X1H1", "X1", "X1H2", 40, 50, 3])

        cable_data = workbook.create_sheet("Cable Data")
        cable_data.append(["Sub box", "Hub", "X-Box", "Line", "Sequence", "Actual length (m)", "Precon length (m)", "Splitter"])
        for sequence in range(1, 5):
            cable_data.append([f"X1H1-L1-S{sequence}", "X1H1", "X1", "L1", sequence, 40, 50, None])

        contents = BytesIO()
        workbook.save(contents)
        payload = main.build_area_map_from_workbook(contents.getvalue(), "Test Area", "Tripoli")

        self.assertEqual([row["Box type"] for row in payload["boxes"]], ["SUB BOX"] * 3 + ["END BOX"])

    def test_area_builder_applies_explicit_al_dhel_x4_h1_h2_length(self):
        workbook = main.Workbook()
        hub_index = workbook.active
        hub_index.title = "Hub Index"
        hub_index.append(["X-Box", "Hub", "From", "To Hub", "QLC actual (m)", "QLC precon (m)", "Sub boxes"])
        hub_index.append(["X4", "X4H2", "X4H1", "X4H3", 135.2925, None, 1])

        cable_data = workbook.create_sheet("Cable Data")
        cable_data.append(["Sub box", "Hub", "X-Box", "Line", "Sequence", "Actual length (m)", "Precon length (m)", "Splitter"])
        cable_data.append(["X4H2-L1-S1", "X4H2", "X4", "L1", 1, 20, 50, None])

        contents = BytesIO()
        workbook.save(contents)
        payload = main.build_area_map_from_workbook(contents.getvalue(), "Al-Dhel Street", "Tripoli")

        self.assertEqual(payload["routes"][0]["Route code"], "H1-H2")
        self.assertEqual(payload["routes"][0]["Cable length m"], 150)
        self.assertEqual(payload["routes"][0]["Real length m"], 135.3)

    def test_area_builder_imports_mansoura_structured_edge_workbook_and_end_codes(self):
        workbook = main.Workbook()
        hub_index = workbook.active
        hub_index.title = "Hub Index"
        hub_index.append([
            "X-Box", "Hub", "Parent node", "Cascade depth", "QLC actual (m)",
            "Selected QLC SKU (m)", "Source QLC SKU (m)", "Distribution lines",
            "SUB boxes", "END boxes", "Total boxes",
        ])
        hub_index.append(["X2", "H11", "X2", 1, 132.908, 150, 150, 1, 2, 2, 4])
        hub_index.append(["X2", "H12", "X2", 1, 479.346, 500, 500, 1, 2, 2, 4])
        workbook.create_sheet("X2 H10-H11")
        workbook.create_sheet("X2 H12-H13")

        cable_data = workbook.create_sheet("Cable Data")
        cable_data.append(["Cable type", "X-Box", "Hub", "Line", "From", "To", "Actual length (m)", "Source SKU (m)", "Selected SKU (m)"])
        cable_data.append(["QLC 4-core", "X2", "H11", None, "X2", "X2-H11", 132.908, 150, 150])
        cable_data.append(["QLC 4-core", "X2", "H12", None, "X2", "X2-H12", 479.346, 500, 500])
        for hub in ("H11", "H12"):
            previous = f"X2-{hub}"
            for sequence in range(1, 5):
                code = f"X2-{hub}-L1-S{sequence}"
                cable_data.append(["Distribution 1-core", "X2", hub, "L1", previous, code, 38.25, 50, 50])
                previous = code

        contents = BytesIO()
        workbook.save(contents)
        payload = main.build_area_map_from_workbook(contents.getvalue(), "Mansoura", "Tripoli")
        box_types = {row["Box code"]: row["Box type"] for row in payload["boxes"]}

        self.assertEqual(payload["summary"], {
            "xboxes": 1,
            "hubs": 2,
            "sub_end_boxes": 8,
            "distribution_meters": 400,
            "core_meters": 650,
            "total_cable_meters": 1050,
        })
        self.assertEqual(box_types["H11-L1-S3"], "END BOX")
        self.assertEqual(box_types["H11-L1-S4"], "END BOX")
        self.assertEqual(box_types["H12-L1-S1"], "END BOX")
        self.assertEqual(box_types["H12-L1-S4"], "END BOX")
        self.assertEqual(sum(value == "END BOX" for value in box_types.values()), 4)

    def test_area_builder_accepts_sub_end_box_header_for_interactive_map(self):
        workbook = main.Workbook()
        hub_index = workbook.active
        hub_index.title = "Hub Index"
        hub_index.append([
            "X-Box", "Hub", "From", "To Hub", "QLC actual (m)", "QLC precon (m)",
            "Lines", "SUB/END boxes", "Hub output (dBm)", "Best cumulative loss (dB)",
            "Worst cumulative loss (dB)",
        ])
        hub_index.append(["X1", "X1-H1", "X1", "X1-H2", 306.774, 500, 1, 4, -1.56, 21.0, 26.1])
        hub_index.append(["X1", "X1-H2", "X1-H1", None, 131.978, 150, 1, 1, -2.30, 21.7, 26.8])
        workbook.create_sheet("X1 H1-H2")

        cable_data = workbook.create_sheet("Cable Data")
        cable_data.append([
            "Cable FID", "SUB/END box", "Hub", "X-Box", "Line", "Sequence",
            "Actual length (m)", "Precon length (m)", "Splitter", "Cumulative loss (dB)",
        ])
        for sequence in range(1, 5):
            cable_data.append([
                400 + sequence, f"X1-H1-L1-S{sequence}", "X1-H1", "X1", "L1", sequence,
                30 + sequence, 50, "1:8" if sequence == 4 else "1:9", 20 + sequence,
            ])
        cable_data.append([405, "X1-H2-L1-S1", "X1-H2", "X1", "L1", 1, 25, 100, "1:8", 22])

        contents = BytesIO()
        workbook.save(contents)
        payload = main.build_area_map_from_workbook(contents.getvalue(), "Ras Ali", "Misurata")
        box_types = {row["Box code"]: row["Box type"] for row in payload["boxes"]}

        self.assertEqual(payload["summary"], {
            "xboxes": 1,
            "hubs": 2,
            "sub_end_boxes": 5,
            "distribution_meters": 300,
            "core_meters": 650,
            "total_cable_meters": 950,
        })
        self.assertEqual(box_types["H1-L1-S4"], "END BOX")
        self.assertEqual(box_types["H2-L1-S1"], "END BOX")
        self.assertEqual(sum(value == "END BOX" for value in box_types.values()), 2)
        self.assertEqual({row["Route code"] for row in payload["routes"]}, {"X1-H1", "H1-H2"})

    def test_area_builder_accepts_compact_cable_data_headers_and_preserves_optical_values(self):
        workbook = main.Workbook()
        hub_index = workbook.active
        hub_index.title = "Hub Index"
        hub_index.append(["X-Box", "Hub", "From", "To Hub", "QLC actual (m)", "QLC precon (m)", "Sub boxes"])
        hub_index.append(["X1", "X1-H1", "X1", None, 45, 50, 2])

        cable_data = workbook.create_sheet("Cable Data")
        cable_data.append([
            "SUB/END", "X-Box", "HUB", "Line", "Sequence", "Type", "Actual (m)",
            "Precon (m)", "Cumulative loss (dB)", "ATB output (dBm)",
        ])
        cable_data.append(["SUB", "X1", "X1-H1", "L1", 1, "1:8", 30, 50, 22.5, -5.1])
        cable_data.append(["SUB", "X1", "X1-H1", "L1", 2, "1:9", 32, 80, 23.2, -5.7])
        cable_data.append(["END", "X1", "X1-H1", "L1", 3, "1:8", 33, 100, 24.0, -6.2])

        contents = BytesIO()
        workbook.save(contents)
        payload = main.build_area_map_from_workbook(contents.getvalue(), "Test Area", "Misurata")

        self.assertEqual([row["Box type"] for row in payload["boxes"]], ["SUB BOX", "SUB BOX", "END BOX"])
        self.assertEqual([row["dB"] for row in payload["boxes"]], [22.5, 23.2, 24.0])
        self.assertEqual([row["ATB output (dBm)"] for row in payload["boxes"]], [-5.1, -5.7, -6.2])
        self.assertEqual(payload["summary"], {
            "xboxes": 1,
            "hubs": 1,
            "sub_end_boxes": 3,
            "distribution_meters": 230,
            "core_meters": 50,
            "total_cable_meters": 280,
        })

    def test_area_builder_imports_awlad_abushala_headers_summary_and_compact_parts(self):
        workbook = main.Workbook()
        hub_index = workbook.active
        hub_index.title = "Hub Index"
        hub_index.append(["Network Schematics"])
        hub_index.append([])
        hub_index.append(["Topology notes"])
        hub_index.append([])
        hub_index.append([
            "X-Box", "HUB", "From", "To HUB", "QLC actual (m)", "QLC precon (m)",
            "Lines", "SUB/END boxes", "HUB output (dBm)", "Loss to HUB (dB)", "HUB cascade order",
        ])
        hub_index.append(["X1", "X1-H1", "X1", "X1-H2", 45, 50, 1, 2, -2.1, 7.1, 1])
        hub_index.append(["X1", "X1-H2", "X1-H1", None, 80, 80, 1, 1, -2.9, 7.9, 2])
        hub_index.append([])
        hub_index.append(["X-Boxes", "HUBs", "SUB boxes", "END boxes", "Distribution lines"])
        hub_index.append([1, 2, 1, 2, 2])
        workbook.create_sheet("X1-H1-H2")

        cable_data = workbook.create_sheet("Cable Data")
        cable_data.append(["Distribution Cable and SUB Power Data"])
        cable_data.append([])
        cable_data.append(["Actual and preconnected lengths; optical values are included."])
        cable_data.append([])
        cable_data.append([
            "SUB/END", "X-Box", "HUB", "Line", "Sequence", "Type", "Actual (m)",
            "Precon (m)", "Cumulative loss (dB)", "ATB output (dBm)",
        ])
        cable_data.append(["H1-L1-S1", "X1", "X1-H1", "X1-H1-L1", 1, "SUB", 12.5, 50, 23.4, -20.0])
        cable_data.append(["H1-L1-S2", "X1", "X1-H1", "X1-H1-L1", 2, "END", 20, 50, 25.5, -22.0])
        cable_data.append(["H2-L1-S1", "X1", "X1-H2", "X1-H2-L1", 1, "END", 25, 80, 23.0, -20.0])

        contents = BytesIO()
        workbook.save(contents)
        payload = main.build_area_map_from_workbook(contents.getvalue(), "Awlad Abushala", "Misurata")

        self.assertEqual(payload["summary"], {
            "xboxes": 1,
            "hubs": 2,
            "sub_end_boxes": 3,
            "distribution_meters": 180,
            "core_meters": 130,
            "total_cable_meters": 310,
        })
        self.assertEqual({row["Part"] for row in payload["boxes"]}, {"X1 H1-H2"})
        self.assertEqual({row["Part"] for row in payload["routes"]}, {"X1 H1-H2"})
        self.assertEqual(
            {row["Box code"]: row["Box type"] for row in payload["boxes"]},
            {"H1-L1-S1": "SUB BOX", "H1-L1-S2": "END BOX", "H2-L1-S1": "END BOX"},
        )
        self.assertEqual([row["dB"] for row in payload["boxes"]], [23.4, 25.5, 23.0])
        self.assertEqual([row["ATB output (dBm)"] for row in payload["boxes"]], [-20.0, -22.0, -20.0])

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

    def test_requester_can_record_missing_box_and_cable_without_changing_map_design(self):
        request = self.make_request("Requester")
        area = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["name"] == "Test Map Manager")
        selected = area["active"][0]
        payload = main.FiberMapMissingInstallIn(
            area=area["name"],
            expected_revision=area["revision"],
            submission_key="test-install-submission-0001",
            entries=[{"xbox": selected["xbox"], "code": selected["code"], "cable_length_m": 37.5}],
        )

        result = main.fiber_map_manager_install_missing(payload, request, self.db)

        self.assertFalse(result["replayed"])
        self.assertEqual(result["changed"], 1)
        self.assertEqual(len(result["records"]), 2)
        box = next(row for row in result["records"] if row["box code"])
        cable = next(row for row in result["records"] if row["cable code"])
        self.assertEqual(box["box code"], selected["code"])
        self.assertEqual(box["actual"], 1)
        self.assertEqual(cable["cable code"], selected["code"])
        self.assertEqual(cable["material type"], "Single-Core Distribution Cable_37.5m")
        self.assertEqual(cable["actual"], 1)

        map_after = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["name"] == area["name"])
        self.assertEqual(map_after["revision"], area["revision"])
        self.assertEqual(map_after["active"], area["active"])

        replay = main.fiber_map_manager_install_missing(payload, request, self.db)
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(replay["records"]), 2)
        self.assertEqual(
            self.db.query(main.RolloutRecord).filter(main.RolloutRecord.area == area["name"]).count(),
            2,
        )

        duplicate = main.FiberMapMissingInstallIn(
            area=area["name"],
            expected_revision=area["revision"],
            submission_key="test-install-submission-0002",
            entries=[{"xbox": selected["xbox"], "code": selected["code"], "cable_length_m": 37.5}],
        )
        with self.assertRaises(HTTPException) as conflict:
            main.fiber_map_manager_install_missing(duplicate, request, self.db)
        self.assertEqual(conflict.exception.status_code, 409)

    def test_management_cannot_record_missing_box_installations(self):
        area = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["name"] == "Test Map Manager")
        selected = area["active"][0]
        payload = main.FiberMapMissingInstallIn(
            area=area["name"],
            expected_revision=area["revision"],
            submission_key="test-install-submission-0003",
            entries=[{"xbox": selected["xbox"], "code": selected["code"], "cable_length_m": 37.5}],
        )
        with self.assertRaises(HTTPException) as denied:
            main.fiber_map_manager_install_missing(payload, self.make_request("Management"), self.db)
        self.assertEqual(denied.exception.status_code, 403)

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

    def test_generated_code_supports_fifth_splitter_and_ras_ammar_h13_l4(self):
        area = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["name"] == "Test Map Manager")
        payload = main.FiberMapChangeIn(
            area=area["name"],
            action="add",
            selections=[{"xbox": "X1", "code": "H1-L1-S5", "box_type": "SUB BOX", "cable_length_m": 50}],
            expected_revision=area["revision"],
        )
        result = main.fiber_map_manager_change(payload, self.request, self.db)
        self.assertEqual(result["changed"], 1)
        self.assertTrue(main.fiber_map_manager_can_add_empty_line("Ras Ammar", "X1", "H13", "L4"))
        updated = next(row for row in main.fiber_map_manager_options(self.db, "FTTH")["areas"] if row["name"] == "Test Map Manager")
        self.assertIn(("X1", "H1L1S5"), {(row["xbox"], main.rollout_code_key(row["code"])) for row in updated["active"]})

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
            ("rasammar", "X1", "H13", "L4"),
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
