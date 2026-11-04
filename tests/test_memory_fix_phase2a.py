import ctypes
import gc
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.orm import close_all_sessions


TEST_DB_PATH = Path(tempfile.gettempdir()) / f"warehouse-memory-phase2a-{os.getpid()}.db"
for suffix in ("", "-wal", "-shm"):
    try:
        Path(f"{TEST_DB_PATH}{suffix}").unlink()
    except FileNotFoundError:
        pass

os.environ["APP_ENV"] = "test"
TEST_DB_URL = f"sqlite:///{TEST_DB_PATH.as_posix()}"
os.environ["FTTH_DATABASE_URL"] = TEST_DB_URL
os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ.pop("SR_DATABASE_URL", None)
os.environ["SESSION_SECRET"] = "isolated-phase2a-test-session-secret-32chars"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from database import SessionLocal
from models import Product, RolloutRecord, StockBalance, Warehouse


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
        if get_process_memory_info(get_current_process(), ctypes.byref(counters), counters.cb):
            return counters.WorkingSetSize
        return None
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return value if os.name == "nt" else value * 1024
    except ImportError:
        return None


def measure_rss(label, operation):
    gc.collect()
    before = process_rss_bytes()
    samples = [before] if before is not None else []
    stop = threading.Event()

    def sample():
        while not stop.wait(0.005):
            value = process_rss_bytes()
            if value is not None:
                samples.append(value)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    try:
        operation()
    finally:
        stop.set()
        thread.join()
    after = process_rss_bytes()
    gc.collect()
    after_gc = process_rss_bytes()
    peak = max(samples + [after or 0, after_gc or 0]) if samples or after or after_gc else None
    return {
        "name": label,
        "before": before,
        "peak": peak,
        "peak_delta": peak - before if peak is not None and before is not None else None,
        "after": after,
        "after_gc": after_gc,
        "retained": after_gc - before if after_gc is not None and before is not None else None,
    }


class MemoryFixPhase2ATests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if (
            os.environ.get("APP_ENV") != "test"
            or main.engine.dialect.name != "sqlite"
            or Path(main.engine.url.database).resolve() != TEST_DB_PATH.resolve()
        ):
            raise RuntimeError("Phase 2A tests require the isolated temporary SQLite test database.")
        if hasattr(main, "ROLLOUT_DB_CACHE"):
            raise RuntimeError("Full Rollout rows must not be held in ROLLOUT_DB_CACHE.")
        main.Base.metadata.create_all(bind=main.engine)
        cls.seeded = False

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
        main.clear_rollout_db_cache()
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
        if not type(self).seeded:
            self._seed_test_dataset()
            type(self).seeded = True

    def tearDown(self):
        self.db.close()
        gc.collect()

    def _seed_test_dataset(self):
        warehouse = Warehouse(program="FTTH", name="Misurata Lnet", location="Misurata", status="active")
        product = Product(
            program="FTTH",
            sku="P2A-DIST-80",
            name="Single-Core Distribution Cable_80m",
            part_number="P2A-DIST-80",
            unit="m",
            status="active",
        )
        self.db.add_all([warehouse, product])
        self.db.flush()
        self.db.add(StockBalance(program="FTTH", warehouse_id=warehouse.id, product_id=product.id, quantity=50000))

        today = date.today()
        records = []
        material_types = (
            "Single-Core Distribution Cable_80m",
            "4-coreCable_500m",
            "SUB BOX",
            "",
            "END BOX",
        )
        items = ("Cable", "Cable", "SUB BOX", "Cable", "END BOX")
        statuses = ("Done", "In Progress", "completed", "", "Installed")
        areas = ("Maqawba", "Ras Ali", "Bera W Taleem", "Hay Demashq")
        for index in range(1, 6001):
            record_day = (today - timedelta(days=index % 40)).isoformat()
            if index % 997 == 0:
                record_day = "not-a-date"
            elif index % 31 == 0:
                record_day = ""
            actual = 0.0 if index % 13 == 0 else float(index % 5 + 1)
            if index % 997 == 0:
                actual = -1.0
            city = "Misurata" if index % 2 == 0 else "Tripoli"
            area = areas[index % len(areas)]
            records.append({
                "record_id": f"RDP-P2A-{index:05d}",
                "date": record_day,
                "supervisor_name": f"Supervisor {index % 9}",
                "team_leader": f"Team {index % 12:02d}",
                "area": area,
                "city": city,
                "activity": "Installation" if index % 2 else "Repair",
                "item": items[index % len(items)],
                "material_type": material_types[index % len(material_types)],
                "planned_quantity": float(index % 7 + 1),
                "actual": actual,
                "stock_remaining": 0.0,
                "status": statuses[index % len(statuses)],
                "laser": "Yes" if index % 2 else "No",
                "acceptance": "Yes" if index % 3 else "No",
                "scan": "Yes" if index % 4 else "No",
                "labeling": "Yes" if index % 5 else "No",
                "related_to_xbox": f"X{index % 4 + 1}",
                "entry_time": f"{today.isoformat()} 10:{index % 60:02d}:00",
                "cable_code": f"H{index % 20 + 1}-L{index % 4 + 1}-S1" if index % 5 < 2 else "",
                "box_code": f"H{index % 20 + 1}-L{index % 4 + 1}-S1" if index % 5 == 2 else "",
                "notes": f"hub: H{index % 20 + 1} | synthetic {index}",
            })
        self.db.bulk_insert_mappings(RolloutRecord, records)
        self.db.commit()
        self.db.expire_all()

    def _legacy_rollout_rows(self):
        return [main.row_to_record(row) for row in self.db.query(RolloutRecord).order_by(RolloutRecord.id.asc()).yield_per(200)]

    def test_pagination_search_filters_and_empty_results(self):
        first = main.list_records(self.request, page=1, page_size=500, db=self.db)
        middle = main.list_records(self.request, page=6, page_size=500, db=self.db)
        last = main.list_records(self.request, page=12, page_size=500, db=self.db)
        self.assertEqual((first["total"], first["page"], first["page_size"], first["total_pages"]), (6000, 1, 500, 12))
        self.assertEqual(len(first["records"]), 500)
        self.assertEqual(first["records"][0]["ID"], "RDP-P2A-06000")
        self.assertEqual(middle["page"], 6)
        self.assertEqual(len(middle["records"]), 500)
        self.assertEqual(last["page"], 12)
        self.assertEqual(len(last["records"]), 500)
        self.assertEqual(last["records"][-1]["ID"], "RDP-P2A-00001")

        searched = main.list_records(self.request, page=1, page_size=50, search="RDP-P2A-00321", db=self.db)
        filtered = main.list_records(self.request, page=1, page_size=500, area="Maqawba", city="Misurata", status="Done", db=self.db)
        empty = main.list_records(self.request, page=1, page_size=50, search="no-such-rollout-row", db=self.db)
        manager_request = SimpleNamespace(
            state=SimpleNamespace(
                current_user=SimpleNamespace(
                    id=2, role="Warehouse Manager", name="Misurata Lnet", username="manager", warehouse_name="Misurata Lnet"
                ),
                program="FTTH",
            ),
            query_params={},
        )
        manager_page = main.list_records(manager_request, page=1, page_size=500, db=self.db)
        legacy_scoped = main.rollout_records_for_session(manager_request, self._legacy_rollout_rows())
        self.assertEqual(searched["total"], 1)
        self.assertEqual(searched["records"][0]["ID"], "RDP-P2A-00321")
        self.assertTrue(all(row["Area"] == "Maqawba" and row["city"] == "Misurata" and row["staus"] == "Done" for row in filtered["records"]))
        self.assertEqual(empty["total"], 0)
        self.assertEqual(empty["records"], [])
        self.assertEqual(manager_page["total"], len(legacy_scoped))
        self.assertTrue(all(row["city"] == "Misurata" for row in manager_page["records"]))

        sql = []
        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            if "from rollout_records" in statement.lower():
                sql.append(statement.upper())
        event.listen(main.engine, "before_cursor_execute", capture)
        try:
            main.list_records(self.request, page=3, page_size=125, search="Maqawba", db=self.db)
        finally:
            event.remove(main.engine, "before_cursor_execute", capture)
        page_selects = [statement for statement in sql if "SELECT" in statement and "LIMIT" in statement]
        self.assertTrue(page_selects)
        self.assertIn("OFFSET", page_selects[-1])

        daily = main.list_rollout_daily_progress(self.request, limit=250, offset=2500, db=self.db)
        self.assertEqual((daily["total"], daily["page"], daily["page_size"], daily["total_pages"]), (6000, 11, 250, 24))
        self.assertEqual(len(daily["records"]), 250)
        searched_entry = main.rollout_entry_reference(self.request, query="RDP-P2A-00321", limit=50, records_only=True, db=self.db)
        self.assertEqual(searched_entry["count"], 1)
        self.assertEqual(searched_entry["latest"][0]["ID"], "RDP-P2A-00321")

    def test_dashboard_aggregates_match_legacy_python_results(self):
        legacy_rows = self._legacy_rollout_rows()
        old_stock_usage = None
        old_stock_coverage = None
        old_material_usage = None
        old_entry_summary = None
        try:
            old_entry_summary = main.rollout_entry_summary(legacy_rows)
            with patch.object(main, "rollout_stock_usage_aggregate_rows", return_value=legacy_rows):
                old_stock_usage = main.list_stock_usage(self.request, "FTTH", self.db)
            with patch.object(main, "rollout_stock_coverage_aggregate_rows", return_value=legacy_rows):
                old_stock_coverage = main.list_stock_coverage(self.request, "FTTH", self.db)
            with patch.object(main, "rollout_material_usage_aggregate_rows", return_value=(legacy_rows, len(legacy_rows))):
                old_material_usage = main.list_rollout_material_usage(self.request, self.db, program="FTTH")
        finally:
            del legacy_rows
            gc.collect()

        new_stock_usage = main.list_stock_usage(self.request, "FTTH", self.db)
        new_stock_coverage = main.list_stock_coverage(self.request, "FTTH", self.db)
        new_material_usage = main.list_rollout_material_usage(self.request, self.db, program="FTTH")
        new_entry_summary = main.rollout_entry_summary_from_db(self.request, self.db)
        self.assertEqual(new_stock_usage, old_stock_usage)
        self.assertEqual(new_stock_coverage, old_stock_coverage)
        self.assertEqual(new_material_usage, old_material_usage)
        self.assertEqual(new_entry_summary, old_entry_summary)
        self.assertGreater(new_stock_usage["usage"][0]["rollout_consumed_qty"], 0)
        self.assertEqual(new_material_usage["rollout_records"], 6000)

    def test_rollout_data_cache_is_removed(self):
        self.assertFalse(hasattr(main, "ROLLOUT_DB_CACHE"))
        rows = main.rollout_stock_usage_aggregate_rows(self.db)
        self.assertLess(len(rows), 100)

    def test_memory_profile_10_requests_and_combined_repeats(self):
        def legacy_rows(db):
            return [
                main.row_to_record(row)
                for row in db.query(RolloutRecord).order_by(RolloutRecord.id.asc()).all()
            ]

        def run_legacy_list_requests():
            for _ in range(10):
                db = SessionLocal()
                try:
                    rows = [main.row_to_record(row) for row in db.query(RolloutRecord).order_by(RolloutRecord.id.desc()).all()]
                    if len(rows) != 6000:
                        raise AssertionError("Legacy full-list baseline was incomplete")
                finally:
                    db.close()

        def run_legacy_stock_usage_requests():
            with patch.object(main, "rollout_stock_usage_aggregate_rows", side_effect=legacy_rows):
                for _ in range(10):
                    db = SessionLocal()
                    try:
                        main.list_stock_usage(self.request, "FTTH", db)
                    finally:
                        db.close()

        def run_list_requests():
            for page in (1, 3, 6, 9, 12, 2, 4, 7, 10, 11):
                db = SessionLocal()
                try:
                    result = main.list_records(self.request, page=page, page_size=500, db=db)
                    if len(result["records"]) != 500:
                        raise AssertionError("Rollout page size changed unexpectedly")
                finally:
                    db.close()

        def run_stock_usage_requests():
            for _ in range(10):
                db = SessionLocal()
                try:
                    main.list_stock_usage(self.request, "FTTH", db)
                finally:
                    db.close()

        def run_stock_coverage_requests():
            for _ in range(10):
                db = SessionLocal()
                try:
                    main.list_stock_coverage(self.request, "FTTH", db)
                finally:
                    db.close()

        def run_rollout_usage_requests():
            for _ in range(10):
                db = SessionLocal()
                try:
                    main.list_rollout_material_usage(self.request, db, program="FTTH")
                finally:
                    db.close()

        def run_combined_cycles():
            for cycle in range(4):
                db = SessionLocal()
                try:
                    main.list_stock_usage(self.request, "FTTH", db)
                    main.list_stock_coverage(self.request, "FTTH", db)
                    main.list_rollout_material_usage(self.request, db, program="FTTH")
                    main.list_records(self.request, page=(cycle * 3) + 1, page_size=500, db=db)
                finally:
                    db.close()

        metrics = [
            measure_rss("Legacy Rollout list / 10 full scans", run_legacy_list_requests),
            measure_rss("Rollout list / 10 pages", run_list_requests),
            measure_rss("Legacy stock-usage / 10 full scans", run_legacy_stock_usage_requests),
            measure_rss("Dashboard stock-usage / 10", run_stock_usage_requests),
            measure_rss("Dashboard stock-coverage / 10", run_stock_coverage_requests),
            measure_rss("Dashboard rollout usage / 10", run_rollout_usage_requests),
            measure_rss("Dashboard then list / 4 cycles", run_combined_cycles),
        ]
        for row in metrics:
            mib = lambda value: "n/a" if value is None else f"{value / (1024 * 1024):.1f} MiB"
            print(
                "PHASE2A_RSS",
                row["name"],
                "before=", mib(row["before"]),
                "peak=", mib(row["peak"]),
                "peak_delta=", mib(row["peak_delta"]),
                "after=", mib(row["after"]),
                "after_gc=", mib(row["after_gc"]),
                "retained=", mib(row["retained"]),
            )
        self.assertEqual(len(metrics), 7)


if __name__ == "__main__":
    unittest.main()
