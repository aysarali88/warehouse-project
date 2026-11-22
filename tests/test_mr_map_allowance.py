import os
import unittest
from unittest.mock import patch
from uuid import uuid4


os.environ["APP_ENV"] = "test"
os.environ["TEST_DATABASE_URL"] = "sqlite://"
os.environ["SESSION_SECRET"] = "mr-map-allowance-test-secret-at-least-32"

import main
from database import SessionLocal, engine
from models import (
    MaterialRequisition,
    MaterialRequisitionItem,
    MaterialReturn,
    MaterialReturnItem,
    Product,
    Warehouse,
)


class MaterialRequisitionMapAllowanceTests(unittest.TestCase):
    def test_design_summary_aggregates_area_aliases_and_exact_materials(self):
        result = main.mr_map_design_summary({
            "boxes": [
                {
                    "Area": "Mansoura",
                    "Box type": "SUB BOX",
                    "Material type": "Single-Core Distribution Cable_200m",
                    "Related to XBOX": "X1",
                    "Hub": "H1",
                },
                {
                    "Area": "Al-Mansoura",
                    "Box type": "END BOX",
                    "Material type": "",
                    "Related to XBOX": "X1",
                    "Hub": "H1",
                },
            ],
            "routes": [
                {"Area": "Al-Mansoura", "Material type": "4-coreCable_150m"},
            ],
        })

        area = result[main.mr_map_area_key("Mansoura")]
        self.assertEqual(area["area"], "Al-Mansoura")
        self.assertEqual(area["limits"], {
            "subbox": 1,
            "endbox": 1,
            "distributioncable200m": 1,
            "4corecable150m": 1,
            "xbox": 1,
            "hubbox": 1,
        })
        self.assertEqual(area["incomplete_box_rows"], 0)
        self.assertEqual(area["incomplete_cable_rows"], 1)

    def test_detail_projects_prior_mrs_returns_and_current_request(self):
        if engine.dialect.name != "sqlite" or os.getenv("APP_ENV") != "test":
            raise RuntimeError("Map allowance tests require an isolated SQLite test database")

        suffix = uuid4().hex[:10]
        area = "Al-Mansoura"
        design_key = main.mr_map_area_key(area)
        design = {
            "area": area,
            "limits": {"distributioncable200m": 10},
            "incomplete_cable_rows": 0,
        }

        with SessionLocal() as db:
            warehouse = Warehouse(program="FTTH", name=f"MR map test {suffix}")
            product = Product(
                program="FTTH",
                sku=f"MR-MAP-{suffix}",
                name="Single-Core Distribution Cable_200m",
                unit="PCS",
            )
            db.add_all([warehouse, product])
            db.flush()

            prior = MaterialRequisition(
                program="FTTH", order_number=f"MAP-PRIOR-{suffix}",
                warehouse_id=warehouse.id, site_id="Mansoura", status="approved",
            )
            current = MaterialRequisition(
                program="FTTH", order_number=f"MAP-CURRENT-{suffix}",
                warehouse_id=warehouse.id, site_id=area, status="pending_approval",
            )
            prior.items.append(MaterialRequisitionItem(
                product_id=product.id, quantity=8, uom="PCS",
                description=product.name, model=product.sku,
            ))
            current.items.append(MaterialRequisitionItem(
                product_id=product.id, quantity=5, uom="PCS",
                description=product.name, model=product.sku,
            ))
            returned = MaterialReturn(
                program="FTTH", return_number=f"MAP-RETURN-{suffix}",
                warehouse_id=warehouse.id, site_id="", site_address="Mansoura", status="confirmed",
            )
            returned.items.append(MaterialReturnItem(
                product_id=product.id, quantity=2, uom="PCS",
                description=product.name, part_nbr=product.sku,
            ))
            db.add_all([prior, current, returned])
            db.flush()

            with patch.object(main, "mr_map_design_for_program", return_value={design_key: design}), \
                    patch.object(main, "mr_map_active_site_ids", return_value=set()):
                summary = main.mr_map_allowance_summary(db, current, "FTTH")

        self.assertTrue(summary["available"])
        self.assertEqual(len(summary["items"]), 1)
        row = summary["items"][0]
        self.assertEqual(row["prior_mr_qty"], 8)
        self.assertEqual(row["confirmed_return_qty"], 2)
        self.assertEqual(row["request_qty"], 5)
        self.assertEqual(row["projected_qty"], 11)
        self.assertEqual(row["remaining_qty"], -1)
        self.assertEqual(row["status"], "exceeds")


if __name__ == "__main__":
    unittest.main()
