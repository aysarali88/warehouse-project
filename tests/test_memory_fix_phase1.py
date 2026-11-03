import ctypes
import gc
import os
import re
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import event, inspect as sqlalchemy_inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import close_all_sessions

TEST_DB_PATH = Path(tempfile.gettempdir()) / f"warehouse-memory-phase1-{os.getpid()}.db"
for suffix in ("", "-wal", "-shm"):
    try:
        Path(f"{TEST_DB_PATH}{suffix}").unlink()
    except FileNotFoundError:
        pass

os.environ["APP_ENV"] = "test"
os.environ["FTTH_DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH.as_posix()}"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH.as_posix()}"
os.environ.pop("SR_DATABASE_URL", None)
os.environ["SESSION_SECRET"] = "isolated-memory-test-session-secret-32-characters"

import main
from database import SessionLocal
from models import Product, ReceiveOrder, ReceiveOrderItem, Warehouse


def process_rss_bytes():
    if os.name == "nt":
        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.restype = ctypes.c_void_p
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        get_process_memory_info = psapi.GetProcessMemoryInfo
        get_process_memory_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessMemoryCounters), ctypes.c_ulong]
        get_process_memory_info.restype = ctypes.c_int
        process = get_current_process()
        if get_process_memory_info(process, ctypes.byref(counters), counters.cb):
            return counters.WorkingSetSize
        return None
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return value if os.name == "nt" else value * 1024
    except ImportError:
        return None


class WarehouseMemoryPhase1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if (
            os.environ.get("APP_ENV") != "test"
            or main.engine.dialect.name != "sqlite"
            or Path(main.engine.url.database).resolve() != TEST_DB_PATH.resolve()
        ):
            raise RuntimeError("Memory Phase 1 tests require the isolated temporary SQLite test database.")
        if "has_invoice_file" in ReceiveOrder.__table__.columns:
            raise RuntimeError("Invoice availability must remain a query expression, not a database schema column.")

    @classmethod
    def tearDownClass(cls):
        close_all_sessions()
        main.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(f"{TEST_DB_PATH}{suffix}").unlink()
            except FileNotFoundError:
                pass

    def setUp(self):
        main.clear_warehouse_cache()
        self.db = SessionLocal()
        self.request = SimpleNamespace(
            state=SimpleNamespace(
                current_user=SimpleNamespace(
                    id=1, role="Admin", name="Test Admin", username="test-admin", warehouse_name=""
                ),
                program="FTTH",
            ),
            query_params={},
        )
        self.warehouse = Warehouse(program="FTTH", name=f"Memory test {uuid.uuid4().hex[:10]}")
        self.product = Product(
            program="FTTH",
            sku=f"MEM-{uuid.uuid4().hex[:12]}",
            name="Memory test product",
            part_number="MEM-PART",
            unit="PCS",
            status="active",
        )
        self.db.add_all([self.warehouse, self.product])
        self.db.flush()
        self.warehouse_id = self.warehouse.id
        self.order = ReceiveOrder(
            program="FTTH",
            order_number=f"MEM-{uuid.uuid4().hex[:12]}",
            supplier="Synthetic test supplier",
            receipt_date="2026-11-03",
            invoice_number="SYNTHETIC-1",
            invoice_file_name="synthetic-invoice.bin",
            invoice_file_content_type="application/octet-stream",
            invoice_file_data=b"I" * (8 * 1024 * 1024),
            warehouse_id=self.warehouse_id,
            status="confirmed",
            created_by="memory-test",
        )
        self.db.add(self.order)
        self.db.flush()
        self.db.add(
            ReceiveOrderItem(
                receive_order_id=self.order.id,
                product_id=self.product.id,
                quantity=14,
                serial_number="",
            )
        )
        self.order_id = self.order.id
        self.product_id = self.product.id
        self.db.commit()
        self.db.expunge_all()
        self.order = None

    def assert_invoice_blob_not_selected(self, statements):
        receipt_selects = [sql for sql in statements if "from receive_orders" in sql]
        self.assertTrue(receipt_selects)
        for sql in receipt_selects:
            projection = sql.split(" from receive_orders", 1)[0]
            projection = re.sub(r"length\(receive_orders\.invoice_file_data\)", "", projection)
            self.assertNotIn("receive_orders.invoice_file_data", projection)

    def tearDown(self):
        self.db.close()

    def capture_sql(self, callback):
        statements = []

        def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lower())

        event.listen(main.engine, "before_cursor_execute", record_sql)
        try:
            result = callback()
        finally:
            event.remove(main.engine, "before_cursor_execute", record_sql)
        return result, statements

    def test_receive_order_lists_and_dashboard_do_not_load_invoice_blob(self):
        def list_orders():
            return main.list_receive_orders(
                self.request, limit=50, offset=0, program="FTTH", db=self.db
            )

        result, statements = self.capture_sql(list_orders)
        self.assert_invoice_blob_not_selected(statements)
        self.assertTrue(result["receipts"][0]["has_invoice_file"])
        self.assertEqual(result["receipts"][0]["items"][0]["quantity"], 14)

        self.db.expire_all()
        row = self.db.query(ReceiveOrder).filter(ReceiveOrder.id == self.order_id).one()
        self.assertIn("invoice_file_data", sqlalchemy_inspect(row).unloaded)
        self.assertTrue(row.has_invoice_file)
        postgres_sql = str(self.db.query(ReceiveOrder).statement.compile(dialect=postgresql.dialect())).lower()
        self.assertIn("length(receive_orders.invoice_file_data)", postgres_sql)
        self.assert_invoice_blob_not_selected([postgres_sql])

        detail, detail_sql = self.capture_sql(
            lambda: main.get_receive_order(self.order_id, self.request, program="FTTH", db=self.db)
        )
        self.assertTrue(detail["receipt"]["has_invoice_file"])
        self.assert_invoice_blob_not_selected(detail_sql)

        def dashboard(light):
            with (
                patch.object(main, "list_stock_balances", return_value={"balances": []}),
                patch.object(main, "list_stock_usage", return_value={"usage": []}),
                patch.object(main, "list_stock_movements", return_value={"movements": []}),
                patch.object(main, "list_material_requisition_headers", return_value={"requisitions": []}),
                patch.object(main, "list_material_requisitions", return_value={"requisitions": []}),
                patch.object(main, "list_material_transfer_headers", return_value={"transfers": []}),
                patch.object(main, "list_material_transfers", return_value={"transfers": []}),
                patch.object(main, "list_material_return_headers", return_value={"returns": []}),
                patch.object(main, "list_material_returns", return_value={"returns": []}),
                patch.object(main, "list_material_scans", return_value={"scans": []}),
                patch.object(main, "warehouse_summary", return_value={}),
                patch.object(main, "list_warehouses", return_value={"warehouses": []}),
                patch.object(main, "list_sites", return_value={"sites": []}),
                patch.object(main, "list_technicians", return_value={"technicians": []}),
                patch.object(main, "list_products", return_value={"products": []}),
                patch.object(main, "list_technician_balances", return_value={"balances": []}),
                patch.object(main, "list_audit_logs", return_value={"logs": []}),
                patch.object(main, "virtual_damage_report", return_value={}),
            ):
                return main.warehouse_bootstrap(self.request, light=light, program="FTTH", db=self.db)

        for light in (True, False):
            dashboard_result, dashboard_sql = self.capture_sql(lambda: dashboard(light))
            self.assertTrue(dashboard_result["receipts"][0]["has_invoice_file"])
            self.assert_invoice_blob_not_selected(dashboard_sql)

    def test_invoice_download_explicitly_loads_and_returns_file(self):
        class CapturedStreamingResponse:
            def __init__(self, content, media_type, headers):
                self.content = content.getvalue()
                self.media_type = media_type
                self.headers = headers

        with patch.object(main, "StreamingResponse", CapturedStreamingResponse):
            response = main.download_receive_order_invoice(
                self.order_id, self.request, program="FTTH", db=self.db
            )
        self.assertEqual(len(response.content), 8 * 1024 * 1024)
        self.assertEqual(response.content[:4], b"IIII")
        self.assertEqual(response.media_type, "application/octet-stream")
        self.assertIn("synthetic-invoice.bin", response.headers["Content-Disposition"])

    def test_new_receipt_response_keeps_invoice_flag_without_reloading_blob(self):
        order = ReceiveOrder(
            program="FTTH",
            order_number=f"MEM-NEW-{uuid.uuid4().hex[:12]}",
            supplier="Synthetic test supplier",
            invoice_file_name="new-invoice.bin",
            invoice_file_content_type="application/octet-stream",
            invoice_file_data=b"synthetic-invoice-content",
            warehouse_id=self.warehouse_id,
            status="confirmed",
            created_by="memory-test",
        )
        self.db.add(order)
        self.db.commit()
        self.db.refresh(order)
        response = main.receive_order_to_dict(order)
        self.assertTrue(response["has_invoice_file"])
        self.assertIn("invoice_file_data", sqlalchemy_inspect(order).unloaded)

    def test_material_ledger_totals_are_unchanged_without_loading_invoice_blob(self):
        def get_ledger():
            with patch.object(main, "is_single_ran", return_value=True):
                return main.material_ledger(
                    self.request,
                    query="",
                    product_id=self.product_id,
                    force=True,
                    program="FTTH",
                    db=self.db,
                )

        result, statements = self.capture_sql(get_ledger)
        self.assert_invoice_blob_not_selected(statements)
        self.assertEqual(result["summary"]["total_received"], 14)
        self.assertEqual(result["events"][0]["type"], "GRN")
        self.assertEqual(result["events"][0]["quantity"], 14)

    def test_cache_is_bounded_lru_expires_and_reports_aggregate_metrics(self):
        main.clear_warehouse_cache()
        before = main.warehouse_cache_metrics()
        for index in range(100):
            main._warehouse_cache_set(f"synthetic-{index}", {"label": "x" * 2048})
        metrics = main.warehouse_cache_metrics()
        self.assertEqual(metrics["entries"], main.WAREHOUSE_CACHE_MAX_ENTRIES)
        self.assertLessEqual(metrics["estimated_bytes"], main.WAREHOUSE_CACHE_MAX_BYTES)
        self.assertIsNotNone(main._warehouse_cache_get("synthetic-96"))
        main._warehouse_cache_set("synthetic-100", {"label": "new"})
        self.assertIn("synthetic-96", main.WAREHOUSE_CACHE)
        self.assertNotIn("synthetic-97", main.WAREHOUSE_CACHE)
        self.assertIsNotNone(main._warehouse_cache_get("synthetic-99"))
        self.assertIsNone(main._warehouse_cache_get("not-present"))
        self.assertGreater(metrics["evictions"], before["evictions"])

        main.clear_warehouse_cache()
        now = main.time.monotonic()
        with patch.object(main.time, "monotonic", return_value=now):
            main._warehouse_cache_set("synthetic-expiring", {"label": "short-lived"})
        with patch.object(main.time, "monotonic", return_value=now + main.WAREHOUSE_CACHE_TTL + 1):
            self.assertIsNone(main._warehouse_cache_get("synthetic-expiring"))
        expired_metrics = main.warehouse_cache_metrics()
        self.assertEqual(expired_metrics["entries"], 0)
        self.assertGreater(expired_metrics["expired"], metrics["expired"])
        self.assertGreater(expired_metrics["hits"], metrics["hits"])
        self.assertGreater(expired_metrics["misses"], metrics["misses"])

        with patch.object(main, "WAREHOUSE_CACHE_MAX_BYTES", 128):
            main._warehouse_cache_set("oversize", {"payload": "x" * 1024})
        self.assertNotIn("oversize", main.WAREHOUSE_CACHE)
        self.assertGreater(main.warehouse_cache_metrics()["skipped"], metrics["skipped"])

    def test_repeated_list_requests_and_cache_expiry_report_rss(self):
        gc.collect()
        rss_before = process_rss_bytes()
        for _ in range(30):
            result = main.list_receive_orders(
                self.request, limit=50, offset=0, program="FTTH", db=self.db
            )
            self.assertTrue(result["receipts"][0]["has_invoice_file"])
        result = None
        gc.collect()
        rss_after_requests = process_rss_bytes()

        for index in range(100):
            main._warehouse_cache_set(f"rss-key-{index}", {"payload": "x" * 2048})
        rss_after_cache = process_rss_bytes()

        now = main.time.monotonic()
        with patch.object(main.time, "monotonic", return_value=now + main.WAREHOUSE_CACHE_TTL + 1):
            main._warehouse_cache_get("expired-rss-probe")
        main.clear_warehouse_cache()
        gc.collect()
        rss_after_cleanup = process_rss_bytes()
        print(
            "RSS bytes before_requests=%s after_requests=%s after_cache=%s after_expiry_cleanup=%s"
            % (rss_before, rss_after_requests, rss_after_cache, rss_after_cleanup)
        )
        metrics = main.warehouse_cache_metrics()
        self.assertEqual(metrics["entries"], 0)
        self.assertEqual(metrics["estimated_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
