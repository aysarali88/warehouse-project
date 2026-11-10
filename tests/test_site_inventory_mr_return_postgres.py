"""Material return → Site Inventory tests in an isolated Warehouse-Test schema."""

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from unittest import TestCase
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
    raise RuntimeError("PostgreSQL return validation refused: Warehouse-Test identity was not verified")

from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from database import Base
from migrations.site_inventory_foundation import migrate
from models import (
    AuditLog,
    MaterialReturn,
    MaterialReturnItem,
    Product,
    Site,
    SiteBalance,
    SiteMaterialMovement,
    StockBalance,
    StockMovement,
    Technician,
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


class MaterialReturnSiteInventoryPostgreSQLValidation(TestCase):
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
            "SITE_INVENTORY_RETURN_ENABLED": "true",
        })
        cls.site_inventory_env.start()

        cls.engine = create_database_engine(TEST_DATABASE_URL)
        cls.schema = f"mr_return_test_{uuid4().hex[:18]}"
        cls.translated_engine = cls.engine.execution_options(schema_translate_map={None: cls.schema})
        cls.Session = sessionmaker(bind=cls.translated_engine, expire_on_commit=False)
        cls.schema_created = False
        cls.allowed_schemas = {cls.schema}

        @event.listens_for(cls.engine, "before_cursor_execute")
        def _reject_writes_outside_schema(_connection, _cursor, statement, _parameters, _context, _executemany):
            command = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
            if command in {"INSERT", "UPDATE", "DELETE", "TRUNCATE", "CREATE", "ALTER", "DROP", "REVOKE", "GRANT", "LOCK"}:
                lowered = statement.casefold()
                if "public." in lowered or not any(schema.casefold() in lowered for schema in cls.allowed_schemas):
                    raise AssertionError("PostgreSQL validation attempted a write outside its temporary schema")

        with cls.engine.connect() as connection:
            cls.external_tables_before = cls._external_table_names(connection)
        try:
            with cls.engine.begin() as connection:
                connection.exec_driver_sql(f'CREATE SCHEMA "{cls.schema}"')
                cls.schema_created = True

            with cls.translated_engine.begin() as connection:
                for table in (
                    Site.__table__, Product.__table__, Warehouse.__table__, Technician.__table__, StockBalance.__table__,
                    StockMovement.__table__, MaterialReturn.__table__, MaterialReturnItem.__table__,
                    AuditLog.__table__,
                ):
                    table.create(bind=connection)
            cls.migration_result = migrate(cls.engine, cls.schema)

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
                f"_mr_return_test_main_{uuid4().hex}", os.path.join(os.getcwd(), "main.py")
            )
            cls.main_module = importlib.util.module_from_spec(spec)
            previous_session_secret = os.environ.get("SESSION_SECRET")
            os.environ["SESSION_SECRET"] = "postgres-mr-return-test-secret-not-a-credential"
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
            exists = connection.execute(text("SELECT 1 FROM pg_namespace WHERE nspname=:schema"), {"schema": cls.schema}).first()
            external_after = cls._external_table_names(connection)
        if exists:
            raise AssertionError("Temporary return-validation schema was not removed")
        if external_after != cls.external_tables_before:
            raise AssertionError("PostgreSQL objects outside the temporary schema changed")
        cls.engine.dispose()

    @classmethod
    def _external_table_names(cls, connection):
        return set(connection.execute(text(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema', :schema)"
        ), {"schema": cls.schema}).all())

    def make_fixture(self, *, opening=50, products=None):
        suffix = uuid4().hex[:10]
        with self.Session() as db:
            with db.begin():
                site = Site(program="FTTH", name=f"PG MR Return Site {suffix}", site_inventory_enabled=True)
                warehouse = Warehouse(program="FTTH", name=f"PG MR Return WH {suffix}")
                db.add_all([site, warehouse])
                db.flush()
                product_rows = {}
                for key, unit in (products or {"main": "PCS"}).items():
                    product = Product(
                        program="FTTH", sku=f"PG-MR-RETURN-{suffix}-{key}",
                        name=f"PG MR return material {key}", unit=unit,
                    )
                    db.add(product)
                    db.flush()
                    product_rows[key] = product
                    db.add(StockBalance(
                        program="FTTH", warehouse_id=warehouse.id, product_id=product.id, quantity=10,
                    ))
                    inventory.post_site_movement(
                        db,
                        program="FTTH", site_id=site.id, product_id=product.id,
                        movement_type="incoming", event_type="test_site_opening",
                        quantity_delta=opening, uom=unit, document_type="test_fixture",
                        document_id=suffix, document_line_id=key, reference=f"fixture-{suffix}",
                        note="Synthetic Warehouse-Test fixture", created_by="test", approved_by="test",
                        effective_date="2026-11-07",
                    )
                return {
                    "site_id": site.id,
                    "site_name": site.name,
                    "warehouse_id": warehouse.id,
                    "products": {key: product.id for key, product in product_rows.items()},
                    "suffix": suffix,
                }

    def request(self, role="Admin"):
        return SimpleNamespace(state=SimpleNamespace(current_user=SimpleNamespace(
            role=role, name=f"PG {role}", username=f"pg-{role.lower()}",
            warehouse_name="", program="FTTH",
        )))

    def create_pending(self, fixture, lines):
        with self.Session() as db:
            return self.main_module.create_material_return(
                self.main_module.MaterialReturnIn(
                    program="FTTH", site_id=fixture["site_name"],
                    warehouse_id=fixture["warehouse_id"], returned_by="PG field team",
                    items=[self.main_module.MaterialReturnItemIn(**line) for line in lines],
                ),
                self.request("Requester"), db,
            )["return"]

    def approve(self, return_id):
        with self.Session() as db:
            return self.main_module.approve_material_return(
                return_id,
                self.main_module.MaterialRequisitionActionIn(program="FTTH", actor="PG warehouse manager"),
                self.request(), db,
            )

    def values(self, fixture, return_id):
        with self.Session() as db:
            return {
                "site": {
                    product_id: db.query(SiteBalance).filter_by(
                        program="FTTH", site_id=fixture["site_id"], product_id=product_id,
                    ).one().on_hand_qty
                    for product_id in fixture["products"].values()
                },
                "warehouse": {
                    product_id: db.query(StockBalance).filter_by(
                        warehouse_id=fixture["warehouse_id"], product_id=product_id,
                    ).one().quantity
                    for product_id in fixture["products"].values()
                },
                "site_movements": db.query(SiteMaterialMovement).filter_by(
                    document_type="material_return", document_id=str(return_id),
                ).count(),
                "stock_movements": db.query(StockMovement).filter_by(
                    reference=db.query(MaterialReturn.return_number).filter_by(id=return_id).scalar(),
                ).count(),
                "status": db.get(MaterialReturn, return_id).status,
            }

    def test_pending_then_confirmed_multiline_good_and_quarantine(self):
        fixture = self.make_fixture(products={"good": "PCS", "damaged": "M"})
        good, damaged = fixture["products"].values()
        pending = self.create_pending(fixture, [
            {"product_id": good, "quantity": 3, "condition": "Good"},
            {"product_id": damaged, "quantity": 4, "condition": "Damaged"},
        ])
        before = self.values(fixture, pending["id"])
        self.assertEqual(before["status"], "pending_warehouse")
        self.assertEqual(before["site"], {good: Decimal("50.000000"), damaged: Decimal("50.000000")})
        self.assertEqual(before["site_movements"], 0)
        self.assertEqual(before["stock_movements"], 0)

        result = self.approve(pending["id"])
        after = self.values(fixture, pending["id"])
        self.assertEqual(result["return"]["status"], "confirmed")
        self.assertEqual(after["site"], {good: Decimal("47.000000"), damaged: Decimal("46.000000")})
        self.assertEqual(after["warehouse"][good], 13)
        self.assertEqual(after["warehouse"][damaged], 10)
        self.assertEqual(after["site_movements"], 2)
        self.assertEqual(after["stock_movements"], 2)
        with self.Session() as db:
            site_movements = db.query(SiteMaterialMovement).filter_by(
                document_type="material_return", document_id=str(pending["id"]),
            ).all()
            self.assertTrue(all(
                movement.source_event_key == inventory.source_event_key(
                    "FTTH", "material_return_received", "material_return",
                    str(pending["id"]), movement.document_line_id,
                )
                for movement in site_movements
            ))
            damage_move = db.query(StockMovement).filter_by(
                reference=pending["return_number"], movement_type="damage_in",
            ).one()
            self.assertNotEqual(damage_move.warehouse_id, fixture["warehouse_id"])
            self.assertEqual(db.query(StockBalance).filter_by(
                warehouse_id=damage_move.warehouse_id, product_id=damaged,
            ).one().quantity, 4)
            self.assertEqual(db.query(AuditLog).filter_by(
                action="approve_material_return", entity_id=pending["return_number"],
            ).count(), 1)

    def test_retry_and_insufficient_site_balance(self):
        fixture = self.make_fixture(opening=2)
        product_id = fixture["products"]["main"]
        pending = self.create_pending(fixture, [{"product_id": product_id, "quantity": 3}])
        with self.assertRaises(HTTPException) as error:
            self.approve(pending["id"])
        self.assertEqual(error.exception.status_code, 409)
        values = self.values(fixture, pending["id"])
        self.assertEqual(values["site"][product_id], Decimal("2.000000"))
        self.assertEqual(values["warehouse"][product_id], 10)
        self.assertEqual(values["site_movements"], 0)
        self.assertEqual(values["stock_movements"], 0)
        self.assertEqual(values["status"], "pending_warehouse")
        adequate = self.make_fixture(opening=20)
        adequate_product = adequate["products"]["main"]
        adequate_pending = self.create_pending(adequate, [{"product_id": adequate_product, "quantity": 4}])
        self.approve(adequate_pending["id"])
        with self.assertRaises(HTTPException) as retry:
            self.approve(adequate_pending["id"])
        self.assertEqual(retry.exception.status_code, 400)
        replay_values = self.values(adequate, adequate_pending["id"])
        self.assertEqual(replay_values["site"][adequate_product], Decimal("16.000000"))
        self.assertEqual(replay_values["site_movements"], 1)
        self.assertEqual(replay_values["stock_movements"], 1)

    def test_inactive_site_blocks_confirmed_return_without_warehouse_receipt(self):
        fixture = self.make_fixture()
        product_id = fixture["products"]["main"]
        pending = self.create_pending(fixture, [{"product_id": product_id, "quantity": 4}])
        with self.Session() as db:
            db.get(Site, fixture["site_id"]).site_inventory_enabled = False
            db.commit()
        with self.assertRaises(HTTPException) as error:
            self.approve(pending["id"])
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.detail, "Site Inventory is not active for this Site.")
        values = self.values(fixture, pending["id"])
        self.assertEqual(values["status"], "pending_warehouse")
        self.assertEqual(values["site"][product_id], Decimal("50.000000"))
        self.assertEqual(values["warehouse"][product_id], 10)
        self.assertEqual(values["site_movements"], 0)
        self.assertEqual(values["stock_movements"], 0)

    def test_direct_manager_confirmation_posts_site_and_warehouse_changes_together(self):
        fixture = self.make_fixture(opening=20)
        product_id = fixture["products"]["main"]
        with self.Session() as db:
            result = self.main_module.create_material_return(
                self.main_module.MaterialReturnIn(
                    program="FTTH", site_id=fixture["site_name"],
                    warehouse_id=fixture["warehouse_id"], returned_by="PG field team",
                    items=[self.main_module.MaterialReturnItemIn(product_id=product_id, quantity=5)],
                ),
                self.request(), db,
            )
            return_id = result["return"]["id"]
        values = self.values(fixture, return_id)
        self.assertEqual(values["status"], "confirmed")
        self.assertEqual(values["site"][product_id], Decimal("15.000000"))
        self.assertEqual(values["warehouse"][product_id], 15)
        self.assertEqual(values["site_movements"], 1)
        self.assertEqual(values["stock_movements"], 1)

    def test_unresolved_site_missing_product_and_uom_mismatch_fail_closed(self):
        fixture = self.make_fixture()
        product_id = fixture["products"]["main"]
        with self.Session() as db:
            with self.assertRaises(HTTPException) as missing_product:
                self.main_module.create_material_return(
                    self.main_module.MaterialReturnIn(
                        program="FTTH", site_id=fixture["site_name"], warehouse_id=fixture["warehouse_id"],
                        returned_by="PG field team",
                        items=[self.main_module.MaterialReturnItemIn(product_id=99999999, quantity=1)],
                    ),
                    self.request(), db,
                )
            self.assertEqual(missing_product.exception.status_code, 404)
            db.rollback()
        with self.Session() as db:
            with self.assertRaises(HTTPException) as unresolved:
                self.main_module.create_material_return(
                    self.main_module.MaterialReturnIn(
                        program="FTTH", site_id="Unmapped PG site", warehouse_id=fixture["warehouse_id"],
                        returned_by="PG field team",
                        items=[self.main_module.MaterialReturnItemIn(product_id=product_id, quantity=1)],
                    ),
                    self.request(), db,
                )
            self.assertEqual(unresolved.exception.status_code, 409)
            db.rollback()
        pending = self.create_pending(fixture, [{"product_id": product_id, "quantity": 2}])
        with self.Session() as db:
            db.query(Product).filter_by(id=product_id).one().unit = "M"
            db.commit()
        with self.assertRaises(HTTPException) as mismatch:
            self.approve(pending["id"])
        self.assertEqual(mismatch.exception.status_code, 400)
        final = self.values(fixture, pending["id"])
        self.assertEqual(final["site"][product_id], Decimal("50.000000"))
        self.assertEqual(final["warehouse"][product_id], 10)
        self.assertEqual(final["site_movements"], 0)
        self.assertEqual(final["status"], "pending_warehouse")

    def test_concurrent_receipts_serialize_and_do_not_overdraw_site_balance(self):
        fixture = self.make_fixture(opening=100)
        product_id = fixture["products"]["main"]
        returns = [self.create_pending(fixture, [{"product_id": product_id, "quantity": 60}]) for _ in range(2)]
        barrier = Barrier(2)

        def confirm(return_id):
            barrier.wait(timeout=10)
            try:
                self.approve(return_id)
                return "confirmed"
            except HTTPException as exc:
                return f"rejected:{exc.status_code}"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(confirm, [row["id"] for row in returns]))
        self.assertEqual(sorted(outcomes), ["confirmed", "rejected:409"])
        with self.Session() as db:
            balance = db.query(SiteBalance).filter_by(
                program="FTTH", site_id=fixture["site_id"], product_id=product_id,
            ).one()
            self.assertEqual(balance.on_hand_qty, Decimal("40.000000"))
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(
                program="FTTH", site_id=fixture["site_id"], product_id=product_id,
                document_type="material_return",
            ).count(), 1)
            self.assertEqual(db.query(StockBalance).filter_by(
                warehouse_id=fixture["warehouse_id"], product_id=product_id,
            ).one().quantity, 70)
            statuses = {row.id: row.status for row in db.query(MaterialReturn).filter(
                MaterialReturn.id.in_([row["id"] for row in returns])
            ).all()}
            self.assertEqual(sorted(statuses.values()), ["confirmed", "pending_warehouse"])

    def test_site_post_failure_rolls_back_warehouse_and_confirmation(self):
        fixture = self.make_fixture()
        product_id = fixture["products"]["main"]
        pending = self.create_pending(fixture, [{"product_id": product_id, "quantity": 2}])
        with patch.object(self.main_module, "post_material_return_site_movement", side_effect=HTTPException(503, "injected")):
            with self.assertRaises(HTTPException):
                self.approve(pending["id"])
        values = self.values(fixture, pending["id"])
        self.assertEqual(values["site"][product_id], Decimal("50.000000"))
        self.assertEqual(values["warehouse"][product_id], 10)
        self.assertEqual(values["site_movements"], 0)
        self.assertEqual(values["stock_movements"], 0)
        self.assertEqual(values["status"], "pending_warehouse")
