import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from openpyxl import Workbook
from sqlalchemy.orm import close_all_sessions

TEST_DB_PATH = Path(tempfile.gettempdir()) / f"warehouse-mr-import-auth-{os.getpid()}.db"
try:
    TEST_DB_PATH.unlink()
except FileNotFoundError:
    pass

os.environ["FTTH_DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["SR_DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["SESSION_SECRET"] = "isolated-mr-import-test-session-secret-32chars"

import main
from database import SessionLocal
from models import (
    IssueOrder,
    MaterialRequisition,
    Product,
    StockBalance,
    StockMovement,
    TechnicianBalance,
    Warehouse,
)


class MaterialRequisitionExcelWarehouseAuthorizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if main.engine.dialect.name != "sqlite" or str(main.engine.url.database) != str(TEST_DB_PATH):
            raise RuntimeError("Tests must use their isolated SQLite database, never a production database.")

    @classmethod
    def tearDownClass(cls):
        close_all_sessions()
        main.engine.dispose()
        try:
            TEST_DB_PATH.unlink()
        except FileNotFoundError:
            pass

    def setUp(self):
        self.db = SessionLocal()
        self.test_key = self.id().rsplit(".", 1)[-1]
        self.tripoli = Warehouse(program="FTTH", name=f"Tripoli {self.test_key}")
        self.misurata = Warehouse(program="FTTH", name=f"Misurata {self.test_key}")
        self.product = Product(
            program="FTTH",
            sku=f"MR-AUTH-{self.test_key}",
            part_number=f"MR-AUTH-{self.test_key}",
            name="Authorization test material",
            item_detail="Authorization test material",
            status="active",
        )
        self.db.add_all([self.tripoli, self.misurata, self.product])
        self.db.flush()
        self.db.add_all([
            StockBalance(program="FTTH", warehouse_id=self.tripoli.id, product_id=self.product.id, quantity=10),
            StockBalance(program="FTTH", warehouse_id=self.misurata.id, product_id=self.product.id, quantity=10),
        ])
        self.db.commit()
        self.request = SimpleNamespace(state=SimpleNamespace(current_user=SimpleNamespace(
            role="Warehouse Manager",
            name="Tripoli manager",
            username="tripoli-manager",
            warehouse_name="Tripoli",
        )))

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    def make_sheet(self, warehouse_name):
        sheet = Workbook().active
        sheet.append(["Warehouse name", warehouse_name])
        sheet.append(["Part #", "Model", "Description", "Quantity", "UOM"])
        sheet.append([self.product.sku, self.product.sku, self.product.name, 2, "PCS"])
        return sheet

    def test_manager_cannot_import_and_issue_from_workbook_warehouse_outside_scope(self):
        with self.assertRaises(HTTPException) as raised:
            main.import_mr_sheet(
                self.db,
                self.make_sheet(self.misurata.name),
                "materials.xlsx",
                self.tripoli.id,
                "Tripoli manager",
                self.request,
            )

        self.assertEqual(raised.exception.status_code, 403)
        self.db.expire_all()
        self.assertEqual(
            self.db.query(StockBalance).filter_by(warehouse_id=self.tripoli.id, product_id=self.product.id).one().quantity,
            10,
        )
        self.assertEqual(
            self.db.query(StockBalance).filter_by(warehouse_id=self.misurata.id, product_id=self.product.id).one().quantity,
            10,
        )
        self.assertEqual(self.db.query(MaterialRequisition).filter_by(warehouse_id=self.misurata.id).count(), 0)
        self.assertEqual(self.db.query(IssueOrder).filter_by(warehouse_id=self.misurata.id).count(), 0)
        self.assertEqual(
            self.db.query(StockMovement).filter_by(warehouse_id=self.misurata.id, product_id=self.product.id).count(),
            0,
        )

    def test_import_rejects_form_program_that_differs_from_session_scope(self):
        self.request.state.session_program = "FTTH"
        self.request.state.program = "FTTH"

        with self.assertRaises(HTTPException) as raised:
            main.material_requisition_import_program(self.request, "SINGLE_RAN")

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(self.db.query(MaterialRequisition).count(), 0)
        self.assertEqual(self.db.query(IssueOrder).count(), 0)
        self.assertEqual(self.db.query(StockMovement).count(), 0)

    def test_management_can_still_import_from_another_warehouse(self):
        self.request.state.current_user.role = "Management"
        row = main.import_mr_sheet(
            self.db,
            self.make_sheet(self.misurata.name),
            "materials.xlsx",
            self.tripoli.id,
            "Management user",
            self.request,
        )
        self.db.commit()
        self.db.refresh(row)

        self.assertEqual(row.warehouse_id, self.misurata.id)
        self.assertEqual(row.status, "issued")
        self.assertEqual(
            self.db.query(StockBalance).filter_by(warehouse_id=self.misurata.id, product_id=self.product.id).one().quantity,
            8,
        )
        self.assertEqual(
            self.db.query(StockBalance).filter_by(warehouse_id=self.tripoli.id, product_id=self.product.id).one().quantity,
            10,
        )

    def test_manager_can_still_import_and_issue_from_their_authorized_warehouse(self):
        row = main.import_mr_sheet(
            self.db,
            self.make_sheet(self.tripoli.name),
            "materials.xlsx",
            self.tripoli.id,
            "Tripoli manager",
            self.request,
        )
        self.db.commit()
        self.db.refresh(row)

        self.assertEqual(row.warehouse_id, self.tripoli.id)
        self.assertEqual(row.status, "issued")
        self.assertEqual(
            self.db.query(StockBalance).filter_by(warehouse_id=self.tripoli.id, product_id=self.product.id).one().quantity,
            8,
        )
        self.assertEqual(
            self.db.query(StockBalance).filter_by(warehouse_id=self.misurata.id, product_id=self.product.id).one().quantity,
            10,
        )
        self.assertEqual(self.db.query(TechnicianBalance).filter_by(product_id=self.product.id).count(), 1)
        self.assertEqual(self.db.query(IssueOrder).filter_by(warehouse_id=self.tripoli.id).count(), 1)
        self.assertEqual(
            self.db.query(StockMovement).filter_by(warehouse_id=self.tripoli.id, product_id=self.product.id).count(),
            1,
        )


if __name__ == "__main__":
    unittest.main()
