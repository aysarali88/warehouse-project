"""PostgreSQL MR issue integration tests confined to a disposable Warehouse-Test schema."""

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, Event
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import patch
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv(".env.test", override=False)

import database
from database import APP_ENV, TEST_DATABASE_URL, create_database_engine, test_database_identity as _test_database_identity

EXPECTED_TEST_REF = "kdzhcizesmjseadwegvd"
PRODUCTION_REF = "ndhccmrxpujpqhyewoac"
IDENTITY = _test_database_identity(TEST_DATABASE_URL)
if (
    APP_ENV != "test"
    or not IDENTITY["safe"]
    or not IDENTITY["test_ref_match"]
    or IDENTITY["production_ref_detected"]
    or IDENTITY["database"] != "postgres"
    or IDENTITY["port"] != 5432
    or IDENTITY["mode"] != "Supabase Shared Session Pooler"
    or database.WAREHOUSE_TEST_PROJECT_REF != EXPECTED_TEST_REF
    or database.PRODUCTION_PROJECT_REF != PRODUCTION_REF
):
    raise RuntimeError("PostgreSQL MR issue validation refused: Warehouse-Test identity was not verified")

from fastapi import HTTPException, Request
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from database import Base
from migrations.site_inventory_foundation import migrate
from models import (
    IssueOrder,
    IssueOrderItem,
    MaterialRequisition,
    MaterialRequisitionItem,
    MaterialScanLog,
    Product,
    Site,
    SiteBalance,
    SiteMaterialMovement,
    StockBalance,
    StockMovement,
    Technician,
    TechnicianBalance,
    Warehouse,
)
import site_inventory_service as inventory

class _EmptyQuery:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: self

    def all(self):
        return []

    def first(self):
        return None

    def count(self):
        return 0

    def __iter__(self):
        return iter(())


class _NoopSession:
    """Prevents unrelated app import-time startup helpers from touching a database."""

    def query(self, *_args, **_kwargs):
        return _EmptyQuery()

    def add(self, *_args, **_kwargs):
        pass

    def add_all(self, *_args, **_kwargs):
        pass

    def flush(self, *_args, **_kwargs):
        pass

    def commit(self, *_args, **_kwargs):
        pass

    def rollback(self, *_args, **_kwargs):
        pass

    def close(self, *_args, **_kwargs):
        pass


class SiteInventoryMRIssuePostgreSQLValidation(TestCase):
    @classmethod
    def setUpClass(cls):
        identity = _test_database_identity(TEST_DATABASE_URL)
        if not (
            APP_ENV == "test"
            and identity["safe"]
            and identity["test_ref_match"]
            and not identity["production_ref_detected"]
            and identity["database"] == "postgres"
            and identity["port"] == 5432
            and identity["mode"] == "Supabase Shared Session Pooler"
        ):
            raise RuntimeError("Refusing PostgreSQL connection because Warehouse-Test identity failed")

        cls.site_inventory_env = patch.dict(os.environ, {
            "SITE_INVENTORY_CUTOVER_PROGRAMS": "FTTH",
            "SITE_INVENTORY_MR_ENABLED": "true",
        })
        cls.site_inventory_env.start()

        cls.engine = create_database_engine(TEST_DATABASE_URL)
        cls.schema = f"mr_issue_test_{uuid4().hex[:18]}"
        cls.translated_engine = cls.engine.execution_options(schema_translate_map={None: cls.schema})
        cls.Session = sessionmaker(bind=cls.translated_engine, expire_on_commit=False)
        cls.schema_created = False
        cls.allowed_schemas = {cls.schema}

        @event.listens_for(cls.engine, "before_cursor_execute")
        def _reject_writes_outside_schema(_connection, _cursor, statement, _parameters, _context, _executemany):
            command = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
            if command in {"INSERT", "UPDATE", "DELETE", "TRUNCATE", "CREATE", "ALTER", "DROP", "REVOKE", "GRANT", "LOCK"}:
                if not any(schema.casefold() in statement.casefold() for schema in cls.allowed_schemas):
                    raise AssertionError("PostgreSQL validation attempted a write outside its temporary schema")

        with cls.engine.connect() as connection:
            cls.external_tables_before = cls._external_table_names(connection)
        try:
            with cls.engine.begin() as connection:
                connection.exec_driver_sql(f'CREATE SCHEMA "{cls.schema}"')
                cls.schema_created = True

            # Only the pre-existing app tables needed by the real warehouse issue path are created here.
            with cls.translated_engine.begin() as connection:
                for table in (
                    Site.__table__, Product.__table__, Warehouse.__table__, StockBalance.__table__,
                    Technician.__table__, TechnicianBalance.__table__, StockMovement.__table__,
                    IssueOrder.__table__, IssueOrderItem.__table__, MaterialRequisition.__table__,
                    MaterialRequisitionItem.__table__, MaterialScanLog.__table__,
                ):
                    table.create(bind=connection)
                from models import AuditLog
                AuditLog.__table__.create(bind=connection)
            cls.migration_result = migrate(cls.engine, cls.schema)

            # main.py has legacy import-time DB maintenance. Keep those unrelated hooks inert
            # while loading the exact issue function; all actual issue queries use test sessions.
            cls.database_originals = {
                name: getattr(database, name)
                for name in ("engine", "SessionLocal", "SRSessionLocal", "sr_engine", "all_engines", "all_sessionmakers", "get_sessionmaker")
            }
            database.engine = cls.translated_engine
            database.SessionLocal = _NoopSession
            database.SRSessionLocal = None
            database.sr_engine = None
            database.all_engines = lambda: []
            database.all_sessionmakers = lambda: []
            database.get_sessionmaker = lambda _program="FTTH": _NoopSession
            spec = importlib.util.spec_from_file_location(
                f"_mr_issue_test_main_{uuid4().hex}", os.path.join(os.getcwd(), "main.py")
            )
            cls.main_module = importlib.util.module_from_spec(spec)
            previous_session_secret = os.environ.get("SESSION_SECRET")
            os.environ["SESSION_SECRET"] = "postgres-mr-issue-test-secret-not-a-credential"
            try:
                spec.loader.exec_module(cls.main_module)
            finally:
                if previous_session_secret is None:
                    os.environ.pop("SESSION_SECRET", None)
                else:
                    os.environ["SESSION_SECRET"] = previous_session_secret
            for name, value in cls.database_originals.items():
                setattr(database, name, value)
        except Exception:
            if hasattr(cls, "database_originals"):
                for name, value in cls.database_originals.items():
                    setattr(database, name, value)
            if cls.schema_created:
                with cls.engine.begin() as connection:
                    connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE')
                cls.schema_created = False
            cls.engine.dispose()
            cls.site_inventory_env.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "engine"):
            return
        cls.engine.dispose()
        cls.site_inventory_env.stop()
        if cls.schema_created:
            with cls.engine.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE')
            cls.schema_created = False
        with cls.engine.connect() as connection:
            exists = connection.execute(text(
                "SELECT 1 FROM pg_namespace WHERE nspname=:schema"
            ), {"schema": cls.schema}).first()
            external_after = cls._external_table_names(connection)
        if exists:
            raise AssertionError("Temporary MR issue schema was not removed")
        if external_after != cls.external_tables_before:
            raise AssertionError("PostgreSQL objects outside the temporary schema changed")
        cls.engine.dispose()

    @classmethod
    def _external_table_names(cls, connection):
        return set(connection.execute(text(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema', :schema)"
        ), {"schema": cls.schema}).all())

    def make_mr(self, *, status="approved", site_label=None, stock=100, lines=None, ambiguous=False):
        suffix = uuid4().hex[:10]
        technician_name = f"PG MR Tech {suffix}"
        actor = f"PG MR Actor {suffix}"
        line_specs = lines or [("M", "M", 7, "main"), ("M", "M", 3, "main")]
        with self.Session() as db:
            with db.begin():
                site = Site(program="FTTH", name=f"PG MR Site {suffix}")
                warehouse = Warehouse(program="FTTH", name=f"PG MR WH {suffix}")
                db.add_all([site, warehouse])
                db.flush()
                if ambiguous:
                    db.add_all([
                        Site(program="FTTH", name="PG Ambiguous-Yard"),
                        Site(program="FTTH", name="PG Ambiguous Yard"),
                    ])
                products = {}
                for unit, _item_uom, _quantity, key in line_specs:
                    if key and key not in products:
                        product = Product(
                            program="FTTH", sku=f"PG-MR-{suffix}-{key}", name=f"PG MR material {key}", unit=unit
                        )
                        db.add(product)
                        db.flush()
                        products[key] = product
                        db.add(StockBalance(
                            program="FTTH", warehouse_id=warehouse.id, product_id=product.id, quantity=stock
                        ))
                row = MaterialRequisition(
                    program="FTTH",
                    order_number=f"PG-MR-{suffix}",
                    warehouse_id=warehouse.id,
                    site_id=site_label or ("PG Ambiguous Yard" if ambiguous else site.name),
                    team_leader=technician_name,
                    requester_name=technician_name,
                    receiver_name="Confirmed PG Receiver",
                    status=status,
                    created_by=actor,
                )
                db.add(row)
                db.flush()
                for index, (_unit, item_uom, quantity, key) in enumerate(line_specs, start=1):
                    db.add(MaterialRequisitionItem(
                        requisition_id=row.id,
                        line_no=index,
                        product_id=products[key].id if key else None,
                        uom=item_uom,
                        quantity=quantity,
                    ))
                db.flush()
                return {
                    "mr_id": row.id,
                    "order_number": row.order_number,
                    "site_id": site.id,
                    "warehouse_id": warehouse.id,
                    "product_ids": {key: product.id for key, product in products.items()},
                    "technician_name": technician_name,
                    "actor": actor,
                    "line_count": len(line_specs),
                }

    def issue(self, fixture):
        with self.Session() as db:
            row = db.get(MaterialRequisition, fixture["mr_id"])
            try:
                value = self.main_module.issue_material_requisition_row(db, row, fixture["actor"])
                db.commit()
                return value
            except Exception:
                db.rollback()
                raise

    def values(self, fixture):
        with self.Session() as db:
            row = db.get(MaterialRequisition, fixture["mr_id"])
            result = {
                "status": row.status,
                "stock": {},
                "site_balance": {},
                "site_movements": db.query(SiteMaterialMovement).filter_by(
                    program="FTTH", document_type="material_requisition", document_id=str(fixture["mr_id"])
                ).count(),
                "issue_orders": db.query(IssueOrder).filter_by(created_by=fixture["actor"]).count(),
                "issue_items": db.query(IssueOrderItem).join(IssueOrder).filter(
                    IssueOrder.created_by == fixture["actor"]
                ).count(),
                "stock_movements": db.query(StockMovement).filter_by(
                    reference=fixture["order_number"], movement_type="issue_to_technician"
                ).count(),
                "technician_balance": {},
            }
            for key, product_id in fixture["product_ids"].items():
                result["stock"][key] = db.query(StockBalance).filter_by(
                    warehouse_id=fixture["warehouse_id"], product_id=product_id
                ).one().quantity
                balance = db.query(SiteBalance).filter_by(
                    program="FTTH", site_id=fixture["site_id"], product_id=product_id
                ).first()
                result["site_balance"][key] = balance.on_hand_qty if balance else Decimal("0")
                tech = (
                    db.query(TechnicianBalance)
                    .join(Technician)
                    .filter(Technician.name == fixture["technician_name"], TechnicianBalance.product_id == product_id)
                    .first()
                )
                result["technician_balance"][key] = tech.quantity if tech else 0
            return result

    def test_actual_multiline_issue_is_atomic_and_preserves_existing_ledgers(self):
        fixture = self.make_mr()
        self.issue(fixture)
        result = self.values(fixture)
        self.assertEqual(result["status"], "issued")
        self.assertEqual(result["stock"]["main"], 90)
        self.assertEqual(result["site_balance"]["main"], Decimal("10.000000"))
        self.assertEqual(result["technician_balance"]["main"], 10)
        self.assertEqual(result["site_movements"], 2)
        self.assertEqual(result["issue_orders"], 1)
        self.assertEqual(result["issue_items"], 2)
        self.assertEqual(result["stock_movements"], 2)
        with self.Session() as db:
            rows = db.query(SiteMaterialMovement).filter_by(
                program="FTTH", document_type="material_requisition", document_id=str(fixture["mr_id"])
            ).all()
            self.assertEqual(len({row.source_event_key for row in rows}), 2)
            self.assertEqual({row.event_type for row in rows}, {"mr_issue"})
            self.assertEqual({row.uom for row in rows}, {"M"})
            self.assertEqual({row.site_id for row in rows}, {fixture["site_id"]})

    def test_signed_only_and_unissued_statuses_do_not_post_inventory(self):
        for status in ("signed", "draft", "pending_approval", "cancelled"):
            with self.subTest(status=status):
                fixture = self.make_mr(status=status, lines=[("M", "M", 4, "main")])
                if status == "signed":
                    # Signature is not an issue event; no call to the warehouse issue boundary occurs.
                    result = self.values(fixture)
                else:
                    with self.assertRaises(HTTPException):
                        self.issue(fixture)
                    result = self.values(fixture)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["stock"]["main"], 100)
                self.assertEqual(result["site_movements"], 0)
                self.assertEqual(result["issue_orders"], 0)

    def test_retry_is_rejected_without_duplicate_movement_or_warehouse_deduction(self):
        fixture = self.make_mr(lines=[("M", "M", 4, "main")])
        self.issue(fixture)
        with self.assertRaises(HTTPException) as error:
            self.issue(fixture)
        self.assertEqual(error.exception.status_code, 400)
        result = self.values(fixture)
        self.assertEqual(result["stock"]["main"], 96)
        self.assertEqual(result["site_movements"], 1)
        self.assertEqual(result["issue_orders"], 1)
        self.assertEqual(result["stock_movements"], 1)

    def test_issue_immediately_flushes_pending_approval_before_row_lock(self):
        fixture = self.make_mr(status="draft", lines=[("M", "M", 4, "main")])
        with self.Session() as db:
            row = db.get(MaterialRequisition, fixture["mr_id"])
            row.status = "approved"
            self.main_module.issue_material_requisition_row(db, row, fixture["actor"])
            db.commit()
        result = self.values(fixture)
        self.assertEqual(result["status"], "issued")
        self.assertEqual(result["stock"]["main"], 96)
        self.assertEqual(result["site_movements"], 1)

    def test_concurrent_attempts_for_same_mr_issue_once(self):
        fixture = self.make_mr(lines=[("M", "M", 10, "main")])
        barrier = Barrier(2)

        def attempt():
            with self.Session() as db:
                row = db.get(MaterialRequisition, fixture["mr_id"])
                barrier.wait(timeout=20)
                try:
                    self.main_module.issue_material_requisition_row(db, row, fixture["actor"])
                    db.commit()
                    return "issued"
                except HTTPException as exc:
                    db.rollback()
                    return f"http-{exc.status_code}"
                except Exception:
                    db.rollback()
                    raise

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: attempt(), range(2)))
        self.assertEqual(sorted(results), ["http-400", "issued"])
        result = self.values(fixture)
        self.assertEqual(result["status"], "issued")
        self.assertEqual(result["stock"]["main"], 90)
        self.assertEqual(result["site_movements"], 1)
        self.assertEqual(result["issue_orders"], 1)

    def test_concurrent_delete_waits_for_issue_and_preserves_history(self):
        fixture = self.make_mr(lines=[("M", "M", 10, "main")])
        issue_locked = Event()
        delete_started = Event()

        def issue_while_holding_lock():
            with self.Session() as db:
                row = db.query(MaterialRequisition).filter_by(id=fixture["mr_id"]).with_for_update().first()
                self.assertIsNotNone(row)
                issue_locked.set()
                if not delete_started.wait(timeout=20):
                    raise TimeoutError("Delete request did not start")
                result = self.main_module.issue_material_requisition_row(db, row, fixture["actor"])
                db.commit()
                return result

        def attempt_delete():
            if not issue_locked.wait(timeout=20):
                raise TimeoutError("Issue transaction did not acquire its lock")
            request = Request({"type": "http", "headers": [], "query_string": b""})
            request.state.current_user = SimpleNamespace(role="Admin", name="Admin", username="admin")
            delete_started.set()
            with self.Session() as db:
                try:
                    self.main_module.delete_material_requisition(
                        fixture["mr_id"], request,
                        self.main_module.MaterialRequisitionActionIn(program="FTTH"), db,
                    )
                    db.commit()
                    return "deleted"
                except HTTPException as exc:
                    db.rollback()
                    return f"http-{exc.status_code}"

        with ThreadPoolExecutor(max_workers=2) as pool:
            issue_future = pool.submit(issue_while_holding_lock)
            delete_future = pool.submit(attempt_delete)
            issue_future.result(timeout=30)
            self.assertEqual(delete_future.result(timeout=30), "http-409")

        result = self.values(fixture)
        self.assertEqual(result["status"], "issued")
        self.assertEqual(result["stock"]["main"], 90)
        self.assertEqual(result["site_movements"], 1)
        self.assertEqual(result["stock_movements"], 1)

    def test_site_product_uom_and_stock_validation_fail_before_issue(self):
        cases = (
            {"site_label": "Unmapped PG Site", "lines": [("M", "M", 4, "main")], "code": 409},
            {"ambiguous": True, "lines": [("M", "M", 4, "main")], "code": 409},
            {"lines": [("M", "M", 4, "")], "code": 400},
            {"lines": [("M", "PCS", 4, "main")], "code": 400},
            {"stock": 3, "lines": [("M", "M", 4, "main")], "code": 400},
        )
        for case in cases:
            with self.subTest(case=case):
                fixture = self.make_mr(**{key: value for key, value in case.items() if key != "code"})
                with self.assertRaises(HTTPException) as error:
                    self.issue(fixture)
                self.assertEqual(error.exception.status_code, case["code"])
                result = self.values(fixture)
                self.assertEqual(result["status"], "approved")
                self.assertEqual(result["site_movements"], 0)
                self.assertEqual(result["issue_orders"], 0)
                self.assertEqual(result["stock_movements"], 0)
                self.assertTrue(all(quantity in (100, 3) for quantity in result["stock"].values()))
                self.assertTrue(all(quantity == 0 for quantity in result["technician_balance"].values()))

    def test_site_posting_failure_rolls_back_every_warehouse_change(self):
        fixture = self.make_mr(lines=[("M", "M", 4, "main")])
        def fail_after_movement(stage):
            if stage == "after_movement":
                raise RuntimeError("injected site posting failure")

        with patch.object(inventory, "_stage", side_effect=fail_after_movement):
            with self.assertRaisesRegex(RuntimeError, "injected site posting failure"):
                self.issue(fixture)
        result = self.values(fixture)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["stock"]["main"], 100)
        self.assertEqual(result["technician_balance"]["main"], 0)
        self.assertEqual(result["site_movements"], 0)
        self.assertEqual(result["issue_orders"], 0)
        self.assertEqual(result["issue_items"], 0)
        self.assertEqual(result["stock_movements"], 0)

    def test_issued_mr_hard_delete_is_rejected_without_changing_any_inventory_history(self):
        fixture = self.make_mr(lines=[("M", "M", 4, "main")])
        self.issue(fixture)
        before = self.values(fixture)
        with self.Session() as db:
            row = db.get(MaterialRequisition, fixture["mr_id"])
            with self.assertRaises(HTTPException) as error:
                self.main_module.delete_material_requisition_row(db, row, fixture["actor"])
            self.assertEqual(error.exception.status_code, 409)
            self.assertIn("controlled reversal/void workflow", error.exception.detail)
            db.rollback()
        self.assertEqual(self.values(fixture), before)
        with self.Session() as db:
            self.assertIsNotNone(db.get(MaterialRequisition, fixture["mr_id"]))

    def test_product_purge_cannot_remove_an_issued_mr_line_or_its_history(self):
        fixture = self.make_mr(lines=[("M", "M", 4, "main")])
        self.issue(fixture)
        before = self.values(fixture)
        with self.Session() as db:
            request = Request({"type": "http", "headers": [], "query_string": b""})
            request.state.current_user = SimpleNamespace(role="Admin", name="Admin", username="admin")
            with self.assertRaises(HTTPException) as error:
                self.main_module.purge_product(
                    fixture["product_ids"]["main"],
                    self.main_module.ProductPurgeIn(program="FTTH"),
                    request,
                    db,
                )
            self.assertEqual(error.exception.status_code, 409)
            self.assertIn("issued material requisition", error.exception.detail)
            self.assertIsNotNone(db.get(Product, fixture["product_ids"]["main"]))
            self.assertIsNotNone(db.get(MaterialRequisition, fixture["mr_id"]))
        self.assertEqual(self.values(fixture), before)

    def test_resubmit_cannot_replace_lines_when_issue_history_exists(self):
        fixture = self.make_mr(lines=[("M", "M", 4, "main")])
        self.issue(fixture)
        with self.Session() as db:
            row = db.get(MaterialRequisition, fixture["mr_id"])
            site_name = db.get(Site, fixture["site_id"]).name
            row.status = "returned_for_edit"
            db.commit()
        before = self.values(fixture)
        with self.Session() as db:
            request = Request({"type": "http", "headers": [], "query_string": b""})
            request.state.current_user = SimpleNamespace(role="Admin", name="Admin", username="admin")
            data = self.main_module.MaterialRequisitionIn(
                warehouse_id=fixture["warehouse_id"],
                site_id=site_name,
                items=[self.main_module.MaterialRequisitionItemIn(
                    product_id=fixture["product_ids"]["main"], description="replacement", uom="M", quantity=4
                )],
            )
            with self.assertRaises(HTTPException) as error:
                self.main_module.resubmit_material_requisition(fixture["mr_id"], data, request, db)
            self.assertEqual(error.exception.status_code, 409)
            self.assertIn("inventory history must be preserved", error.exception.detail)
            self.assertEqual(db.query(MaterialRequisitionItem).filter_by(
                requisition_id=fixture["mr_id"]
            ).count(), 1)
        self.assertEqual(self.values(fixture), before)

    def test_unissued_draft_pending_and_signed_mrs_remain_deletable_without_site_movements(self):
        for status in ("draft", "pending_approval", "signed"):
            with self.subTest(status=status):
                fixture = self.make_mr(status=status, lines=[("M", "M", 4, "main")])
                with self.Session() as db:
                    self.assertEqual(db.query(SiteMaterialMovement).filter_by(
                        program="FTTH", document_type="material_requisition", document_id=str(fixture["mr_id"])
                    ).count(), 0)
                    row = db.get(MaterialRequisition, fixture["mr_id"])
                    result = self.main_module.delete_material_requisition_row(db, row, fixture["actor"])
                    db.commit()
                    self.assertEqual(result["restored_quantity"], 0)
                    self.assertIsNone(db.get(MaterialRequisition, fixture["mr_id"]))
                    self.assertEqual(db.query(SiteMaterialMovement).filter_by(
                        program="FTTH", document_type="material_requisition", document_id=str(fixture["mr_id"])
                    ).count(), 0)
                    self.assertEqual(db.query(StockBalance).filter_by(
                        warehouse_id=fixture["warehouse_id"], product_id=fixture["product_ids"]["main"]
                    ).one().quantity, 100)

    def test_mr_delete_route_remains_admin_only(self):
        fixture = self.make_mr(status="draft", lines=[("M", "M", 4, "main")])
        for role in (None, "Requester", "Technician", "Warehouse Manager", "Management", "Approval"):
            with self.subTest(role=role), self.Session() as db:
                request = Request({"type": "http", "headers": [], "query_string": b""})
                if role is not None:
                    request.state.current_user = SimpleNamespace(role=role, name=role, username=role.lower())
                with self.assertRaises(HTTPException) as error:
                    self.main_module.delete_material_requisition(
                        fixture["mr_id"], request,
                        self.main_module.MaterialRequisitionActionIn(program="FTTH"), db,
                    )
                self.assertEqual(error.exception.status_code, 401 if role is None else 403)
                self.assertIsNotNone(db.get(MaterialRequisition, fixture["mr_id"]))

        with self.Session() as db:
            request = Request({"type": "http", "headers": [], "query_string": b""})
            request.state.current_user = SimpleNamespace(role="Admin", name="Admin", username="admin")
            result = self.main_module.delete_material_requisition(
                fixture["mr_id"], request, self.main_module.MaterialRequisitionActionIn(program="FTTH"), db
            )
            self.assertTrue(result["success"])
            self.assertIsNone(db.get(MaterialRequisition, fixture["mr_id"]))


if __name__ == "__main__":
    main(verbosity=2)
