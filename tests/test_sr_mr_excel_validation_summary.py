import os
import unittest
from io import BytesIO
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["FTTH_DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SR_DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SESSION_SECRET"] = "isolated-sr-mr-validation-test-secret-32-chars"

from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import main
from models import Product, StockBalance


class SingleRanMrExcelValidationSummaryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Product.__table__.create(self.engine)
        StockBalance.__table__.create(self.engine)
        self.db = Session(self.engine)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_mr_upload_lists_row_errors_and_stock_shortages_together(self):
        product = Product(
            program="SINGLE_RAN",
            sku="PART-1",
            part_number="PART-1",
            name="Material one",
            vendor="Huawei",
            status="active",
        )
        self.db.add(product)
        self.db.flush()
        self.db.add(
            StockBalance(
                program="SINGLE_RAN",
                warehouse_id=1,
                product_id=product.id,
                quantity=2,
            )
        )
        self.db.commit()

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Part #", "Vendor", "Description", "Quantity", "Remark"])
        sheet.append(["PART-1", "Huawei", "Material one", 3, ""])
        sheet.append(["UNKNOWN", "Huawei", "Unknown material", 1, ""])
        sheet.append(["PART-1", "Huawei", "Material one", 2, ""])
        sheet.append(["PART-1", "Huawei", "Material one", "not a number", ""])
        stream = BytesIO()
        workbook.save(stream)

        with (
            patch("main.require_warehouse"),
            patch("main.reserved_stock_quantities", return_value={}),
        ):
            with self.assertRaises(ValueError) as raised:
                main.parse_sr_request_upload(
                    self.db,
                    stream.getvalue(),
                    1,
                    "items.xlsx",
                    request_kind="mr",
                )

        message = str(raised.exception)
        self.assertIn("Excel validation found 3 issue(s)", message)
        self.assertIn("Row 3: Part # not found or ambiguous: UNKNOWN", message)
        self.assertIn("Row 5: Quantity must be a number", message)
        self.assertIn("Rows 2, 4: Insufficient available stock", message)
        self.assertIn("Requested 5, stock 2, reserved 0, available 2", message)


if __name__ == "__main__":
    unittest.main()
