"""Warehouse-Test PostgreSQL validation in a temporary non-public schema only."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest import TestCase, main
from unittest.mock import patch
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv(".env.test", override=False)

from database import (
    APP_ENV,
    TEST_DATABASE_URL,
    create_database_engine,
    test_database_identity,
)

IDENTITY = test_database_identity(TEST_DATABASE_URL)
if APP_ENV != "test" or not IDENTITY["safe"] or not IDENTITY["test_ref_match"] or IDENTITY["production_ref_detected"]:
    raise RuntimeError("PostgreSQL validation refused: Warehouse-Test identity was not verified")

from sqlalchemy import event, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException

from migrations.site_inventory_foundation import FOUNDATION_TABLES, migrate
from models import AuditLog, Product, Site, SiteAlias, SiteBalance, SiteMaterialMovement, SiteOpeningBalanceRequest
import site_inventory_service as inventory


class SiteInventoryPostgreSQLValidation(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.identity = test_database_identity(TEST_DATABASE_URL)
        if not (APP_ENV == "test" and cls.identity["safe"] and cls.identity["test_ref_match"] and not cls.identity["production_ref_detected"]):
            raise RuntimeError("Refusing PostgreSQL connection because test database identity failed")
        if cls.identity["database"] != "postgres" or cls.identity["port"] != 5432 or cls.identity["mode"] != "Supabase Shared Session Pooler":
            raise RuntimeError("Refusing PostgreSQL connection because the expected Warehouse-Test pooler was not identified")

        cls.engine = create_database_engine(TEST_DATABASE_URL)
        cls.schema = f"site_inv_test_{uuid4().hex[:18]}"
        cls.allowed_schemas = {cls.schema}
        @event.listens_for(cls.engine, "before_cursor_execute")
        def _reject_outside_schema_writes(_connection, _cursor, statement, _parameters, _context, _executemany):
            command = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
            if command in {"INSERT", "UPDATE", "DELETE", "TRUNCATE", "CREATE", "ALTER", "DROP", "REVOKE", "GRANT", "LOCK"}:
                if not any(schema.casefold() in statement.casefold() for schema in cls.allowed_schemas):
                    raise AssertionError(
                        f"PostgreSQL validation attempted a write outside its temporary schema: {statement}"
                    )
        cls.translated_engine = cls.engine.execution_options(schema_translate_map={None: cls.schema})
        cls.Session = sessionmaker(bind=cls.translated_engine, expire_on_commit=False)
        cls.dropped = False
        cls.schema_created = False
        try:
            with cls.engine.begin() as connection:
                cls.external_tables_before = cls._external_table_names(connection, cls.schema)
                connection.exec_driver_sql(f'CREATE SCHEMA "{cls.schema}"')
                cls.schema_created = True
                translated = connection.execution_options(schema_translate_map={None: cls.schema})
                Site.__table__.create(bind=translated)
                Product.__table__.create(bind=translated)
                AuditLog.__table__.create(bind=translated)

            cls.migration_result = migrate(cls.engine, cls.schema)
            cls.migration_repeat = migrate(cls.engine, cls.schema)
            cls.warehouse_snapshot = cls._schema_inventory_counts()
        except Exception:
            if cls.schema_created:
                try:
                    with cls.engine.begin() as connection:
                        connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE')
                finally:
                    cls.schema_created = False
            cls.engine.dispose()
            raise

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "engine"):
            return
        cls.engine.dispose()
        if cls.schema_created and not cls.dropped:
            with cls.engine.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE')
            cls.dropped = True
            cls.schema_created = False
        with cls.engine.connect() as connection:
            external_after = cls._external_table_names(connection, cls.schema)
            schema_exists = connection.execute(text(
                "SELECT 1 FROM pg_namespace WHERE nspname=:schema"
            ), {"schema": cls.schema}).first() is not None
        if external_after != cls.external_tables_before:
            raise AssertionError("PostgreSQL objects outside the temporary schema changed")
        if schema_exists:
            raise AssertionError("Temporary PostgreSQL validation schema was not removed")
        cls.engine.dispose()

    @classmethod
    def _external_table_names(cls, connection, test_schema):
        return set(connection.execute(text(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema', :test_schema)"
        ), {"test_schema": test_schema}).all())

    @classmethod
    def _schema_inventory_counts(cls):
        with cls.Session() as db:
            return {
                table: db.execute(text(f'SELECT count(*) FROM "{cls.schema}"."{table}"')).scalar_one()
                for table in ("stock_balances", "stock_movements", "technician_balances")
                if inspect(cls.translated_engine).has_table(table, schema=cls.schema)
            }

    def setUp(self):
        suffix = uuid4().hex[:12]
        with self.Session() as db:
            with db.begin():
                self.site = Site(program="FTTH", name=f"PG Test Site {suffix}", site_inventory_enabled=True)
                self.product = Product(program="FTTH", sku=f"PG-{suffix}", name="Postgres cable", unit="M")
                db.add_all([self.site, self.product])
                db.flush()
                self.site_id, self.product_id = self.site.id, self.product.id

    def _post(self, *, amount, movement_type="incoming", token=None, doc_id=None, program="FTTH", site_id=None,
              product_id=None, uom="M", event_type=None, document_type="pg_test", line_id="1"):
        token = token or uuid4().hex
        doc_id = doc_id or token
        quantity = Decimal(str(amount))
        delta = -quantity if movement_type == "outgoing" else quantity
        with self.Session() as db:
            with db.begin():
                movement = inventory.post_site_movement(
                    db,
                    program=program,
                    site_id=site_id or self.site_id,
                    product_id=product_id or self.product_id,
                    movement_type=movement_type,
                    event_type=event_type or movement_type,
                    quantity_delta=delta,
                    uom=uom,
                    document_type=document_type,
                    document_id=doc_id,
                    document_line_id=line_id,
                    reference=f"REF-{doc_id}",
                    note="Warehouse-Test synthetic movement",
                    created_by="test-user",
                    approved_by="test-approver",
                    effective_date="2026-11-06",
                    request_token=token,
                )
                return movement.id

    def _balance(self):
        with self.Session() as db:
            row = db.query(SiteBalance).filter_by(
                program="FTTH", site_id=self.site_id, product_id=self.product_id
            ).first()
            return None if row is None else row.on_hand_qty

    def test_site_activation_can_be_admin_toggled_and_audited(self):
        with self.Session() as db:
            with db.begin():
                site = inventory.set_site_inventory_enabled(
                    db, program="FTTH", site_id=self.site_id, enabled=False, actor="Admin Test",
                )
                self.assertFalse(site.site_inventory_enabled)
        with self.Session() as db:
            with db.begin():
                site = inventory.set_site_inventory_enabled(
                    db, program="FTTH", site_id=self.site_id, enabled=True, actor="Admin Test",
                )
                self.assertTrue(site.site_inventory_enabled)
        with self.Session() as db:
            actions = db.query(AuditLog.action).filter(
                AuditLog.entity_type == "site_inventory", AuditLog.entity_id == str(self.site_id),
                AuditLog.action.in_({"site_inventory_site_activated", "site_inventory_site_deactivated"}),
            ).all()
        self.assertEqual({action for (action,) in actions}, {
            "site_inventory_site_activated", "site_inventory_site_deactivated",
        })

    def test_inactive_site_blocks_opening_and_manual_movement_without_postgres_state_changes(self):
        with self.Session() as db:
            with db.begin():
                db.get(Site, self.site_id).site_inventory_enabled = False

        with self.Session() as db:
            before = (
                db.query(SiteMaterialMovement).count(),
                db.query(SiteBalance).count(),
                db.query(SiteOpeningBalanceRequest).count(),
            )

        with self.assertRaises(HTTPException) as opening_error:
            with self.Session() as db:
                with db.begin():
                    inventory.create_opening_balance_request(
                        db,
                        program="FTTH",
                        site_id=self.site_id,
                        product_id=self.product_id,
                        quantity="10",
                        uom="M",
                        effective_date="2026-11-06",
                        reference="INACTIVE-OPENING",
                        note="Synthetic inactive-site opening request",
                        entered_by="Admin Test",
                        request_token=uuid4().hex,
                    )
        self.assertEqual(opening_error.exception.status_code, 409)

        with self.assertRaises(HTTPException) as movement_error:
            self._post(
                amount="2",
                token=uuid4().hex,
                doc_id=f"INACTIVE-MANUAL-{uuid4().hex[:8]}",
            )
        self.assertEqual(movement_error.exception.status_code, 409)

        with self.Session() as db:
            after = (
                db.query(SiteMaterialMovement).count(),
                db.query(SiteBalance).count(),
                db.query(SiteOpeningBalanceRequest).count(),
            )
        self.assertEqual(after, before)

    def test_migration_is_versioned_and_confined_to_temporary_schema(self):
        self.assertEqual(self.migration_result["schema"], self.schema)
        self.assertFalse(self.migration_result["already_applied"])
        self.assertTrue(self.migration_repeat["already_applied"])
        names = set(inspect(self.translated_engine).get_table_names(schema=self.schema))
        self.assertTrue(set(FOUNDATION_TABLES).issubset(names))
        self.assertIn("site_inventory_schema_versions", names)
        outside_foundation = {
            table for _, table in self.external_tables_before
            if table in set(FOUNDATION_TABLES) | {"site_inventory_schema_versions"}
        }
        self.assertFalse(outside_foundation)

    def test_partial_unversioned_phase1_fails_closed_without_rewriting_history(self):
        schema = f"site_inv_legacy_{uuid4().hex[:18]}"
        self.allowed_schemas.add(schema)
        token = "legacy-phase1-retry-token-0001"
        legacy_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        try:
            with self.engine.begin() as connection:
                connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
                translated = connection.execution_options(schema_translate_map={None: schema})
                Site.__table__.create(bind=translated)
                Product.__table__.create(bind=translated)
                AuditLog.__table__.create(bind=translated)

            legacy_engine = self.engine.execution_options(schema_translate_map={None: schema})
            LegacySession = sessionmaker(bind=legacy_engine, expire_on_commit=False)
            with LegacySession() as db:
                with db.begin():
                    site = Site(program="FTTH", name="Legacy upgrade site")
                    product = Product(program="FTTH", sku=f"LEGACY-{uuid4().hex[:8]}", name="Legacy cable", unit="M")
                    db.add_all([site, product])
                    db.flush()
                    site_id, product_id = site.id, product.id

            q_schema = f'"{schema}"'
            with self.engine.begin() as connection:
                connection.exec_driver_sql(
                    f"CREATE TABLE {q_schema}.site_balances ("
                    "id BIGSERIAL PRIMARY KEY, program VARCHAR(32) NOT NULL, site_id INTEGER NOT NULL, "
                    "product_id INTEGER NOT NULL, on_hand_qty NUMERIC(18,6) NOT NULL DEFAULT 0, "
                    "updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                    "CONSTRAINT uq_site_balance_program_site_product UNIQUE (program, site_id, product_id), "
                    f"FOREIGN KEY (site_id) REFERENCES {q_schema}.sites(id), "
                    f"FOREIGN KEY (product_id) REFERENCES {q_schema}.products(id))"
                )
                connection.exec_driver_sql(
                    f"CREATE TABLE {q_schema}.site_material_movements ("
                    "id BIGSERIAL PRIMARY KEY, program VARCHAR(32) NOT NULL, site_id INTEGER NOT NULL, "
                    "product_id INTEGER NOT NULL, movement_type VARCHAR(32) NOT NULL, "
                    "quantity_delta NUMERIC(18,6) NOT NULL, document_type VARCHAR(80) NOT NULL, "
                    "document_id VARCHAR(120) NOT NULL, document_line_id VARCHAR(120) NOT NULL, "
                    "reference VARCHAR(200) NOT NULL, note TEXT NOT NULL, created_by VARCHAR(160) NOT NULL, "
                    "approved_by VARCHAR(160) NOT NULL, status VARCHAR(16) NOT NULL DEFAULT 'approved', "
                    "idempotency_key VARCHAR(64) NOT NULL, request_token_hash VARCHAR(64), "
                    "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                    "CONSTRAINT uq_site_movement_idempotency_key UNIQUE (idempotency_key), "
                    "CONSTRAINT uq_site_movement_request_token_hash UNIQUE (request_token_hash), "
                    f"FOREIGN KEY (site_id) REFERENCES {q_schema}.sites(id), "
                    f"FOREIGN KEY (product_id) REFERENCES {q_schema}.products(id))"
                )
                connection.exec_driver_sql(
                    f"CREATE TABLE {q_schema}.site_opening_balance_requests ("
                    "id BIGSERIAL PRIMARY KEY, program VARCHAR(32) NOT NULL, site_id INTEGER NOT NULL, "
                    "product_id INTEGER NOT NULL, quantity NUMERIC(18,6) NOT NULL, "
                    "effective_date VARCHAR(10) NOT NULL, reference VARCHAR(200) NOT NULL, note TEXT NOT NULL, "
                    "status VARCHAR(24) NOT NULL DEFAULT 'pending_approval', entered_by VARCHAR(160) NOT NULL, "
                    "approved_by VARCHAR(160) NOT NULL DEFAULT '', request_token_hash VARCHAR(64) NOT NULL, "
                    "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), approved_at TIMESTAMPTZ, "
                    "rejected_by VARCHAR(160) NOT NULL DEFAULT '', rejected_at TIMESTAMPTZ, "
                    "rejection_note TEXT NOT NULL DEFAULT '', "
                    "CONSTRAINT uq_site_opening_request_token_hash UNIQUE (request_token_hash), "
                    f"FOREIGN KEY (site_id) REFERENCES {q_schema}.sites(id), "
                    f"FOREIGN KEY (product_id) REFERENCES {q_schema}.products(id))"
                )
                connection.execute(
                    text(
                    f"INSERT INTO {q_schema}.site_balances (program, site_id, product_id, on_hand_qty) "
                    "VALUES ('FTTH', :site_id, :product_id, 12)"
                    ),
                    {"site_id": site_id, "product_id": product_id},
                )
                connection.execute(
                    text(
                        f"INSERT INTO {q_schema}.site_material_movements "
                        "(program, site_id, product_id, movement_type, quantity_delta, document_type, document_id, "
                        "document_line_id, reference, note, created_by, approved_by, status, idempotency_key, request_token_hash) "
                        "VALUES ('FTTH', :site_id, :product_id, 'incoming', 12, 'legacy_mr', 'MR-001', '1', "
                        "'LEGACY-REF', 'legacy synthetic movement', 'legacy-user', 'legacy-approver', 'approved', :token_hash, :token_hash)"
                    ),
                    {"site_id": site_id, "product_id": product_id, "token_hash": legacy_hash},
                )

            with self.assertRaisesRegex(RuntimeError, "Partial unversioned"):
                migrate(self.engine, schema)
            with self.engine.connect() as connection:
                row = connection.execute(text(
                    f"SELECT request_token_hash "
                    f"FROM {q_schema}.site_material_movements WHERE document_id='MR-001'"
                )).mappings().one()
                balance = connection.execute(text(
                    f"SELECT on_hand_qty FROM {q_schema}.site_balances WHERE site_id=:site_id AND product_id=:product_id"
                ), {"site_id": site_id, "product_id": product_id}).scalar_one()
            self.assertEqual(row["request_token_hash"], legacy_hash)
            self.assertEqual(balance, Decimal("12.000000"))
        finally:
            try:
                with self.engine.begin() as connection:
                    connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            finally:
                self.allowed_schemas.discard(schema)

    def test_schema_constraints_foreign_keys_and_rls(self):
        inspector = inspect(self.translated_engine)
        self.assertEqual(len(inspector.get_foreign_keys("site_balances", schema=self.schema)), 2)
        movement_fks = inspector.get_foreign_keys("site_material_movements", schema=self.schema)
        self.assertEqual(len(movement_fks), 3)
        unique = inspector.get_unique_constraints("site_balances", schema=self.schema)
        self.assertIn(("program", "site_id", "product_id"), [tuple(row["column_names"]) for row in unique])
        columns = {row["name"] for row in inspector.get_columns("site_material_movements", schema=self.schema)}
        self.assertTrue({"uom", "source_event_key", "event_type", "site_id", "product_id"}.issubset(columns))
        with self.engine.connect() as connection:
            rls_enabled = connection.execute(text(
                "SELECT c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=:schema AND c.relname='site_material_movements'"
            ), {"schema": self.schema}).scalar_one()
            self.assertTrue(rls_enabled)

    def test_alias_normalization_and_collision_rejection(self):
        with self.Session() as db:
            with db.begin():
                alias = inventory.create_site_alias(
                    db, program="FTTH", site_id=self.site_id, display_alias="Ras-Ali / North", actor="test-admin"
                )
                self.assertEqual(alias.normalized_alias, "ras ali north")
        with self.Session() as db:
            with db.begin():
                resolved = inventory.resolve_site_alias(db, "FTTH", "RAS ali north")
                self.assertEqual(resolved.id, self.site_id)
        with self.Session() as db:
            with self.assertRaises(HTTPException) as collision:
                with db.begin():
                    inventory.create_site_alias(
                        db, program="FTTH", site_id=self.site_id, display_alias="ras ali north", actor="test-admin"
                    )
            self.assertEqual(collision.exception.status_code, 409)

    def test_uom_validation_and_opening_approval(self):
        with self.assertRaises(HTTPException):
            self._post(amount="1", uom="PCS")
        token = "opening-request-postgres-001"
        with self.Session() as db:
            with db.begin():
                opening = inventory.create_opening_balance_request(
                    db, program="FTTH", site_id=self.site_id, product_id=self.product_id,
                    quantity=Decimal("125.5"), uom="M", effective_date="2026-11-06",
                    reference="PG-PHYSICAL-COUNT", note="Synthetic opening count",
                    entered_by="test-requester", request_token=token,
                )
                opening_id = opening.id
        self.assertIsNone(self._balance())
        with self.Session() as db:
            with self.assertRaises(HTTPException):
                with db.begin():
                    inventory.approve_opening_balance(db, opening_id, "FTTH", "test-requester")
        with self.Session() as db:
            with db.begin():
                movement = inventory.approve_opening_balance(db, opening_id, "FTTH", "test-approver")
                movement_id = movement.id
        self.assertEqual(self._balance(), Decimal("125.500000"))
        with self.Session() as db:
            movement = db.get(SiteMaterialMovement, movement_id)
            request = db.get(SiteOpeningBalanceRequest, opening_id)
            self.assertEqual(movement.movement_type, "opening_balance")
            self.assertEqual(movement.uom, "M")
            self.assertEqual(request.status, "approved")

    def test_source_event_uniqueness_and_program_scoped_request_identity(self):
        token = "shared-token-for-two-programs-001"
        movement_id = self._post(amount="9", token=token, doc_id="DOC-FTTH")
        retry_id = self._post(amount="9", token="another-token-for-same-event-001", doc_id="DOC-FTTH")
        self.assertEqual(movement_id, retry_id)
        with self.assertRaises(HTTPException):
            self._post(amount="10", token="different-token-for-same-event-01", doc_id="DOC-FTTH")
        with self.Session() as db:
            with db.begin():
                sr_site = Site(program="SINGLE_RAN", name=f"SR {uuid4().hex[:8]}", site_inventory_enabled=True)
                sr_product = Product(program="SINGLE_RAN", sku=f"SR-{uuid4().hex[:8]}", name="SR cable", unit="M")
                db.add_all([sr_site, sr_product])
                db.flush()
                sr_ids = sr_site.id, sr_product.id
        sr_movement_id = self._post(
            amount="3", token=token, doc_id="DOC-SR", program="SINGLE_RAN",
            site_id=sr_ids[0], product_id=sr_ids[1],
        )
        self.assertNotEqual(movement_id, sr_movement_id)
        with self.Session() as db:
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(program="FTTH", site_id=self.site_id).count(), 1)

    def test_concurrent_first_balance_creation_is_unique_and_correct(self):
        barrier = Barrier(2)

        def add(amount, doc):
            barrier.wait(timeout=15)
            return self._post(amount=amount, doc_id=doc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda args: add(*args), [("4", "FIRST-A"), ("5", "FIRST-B")]))
        self.assertEqual(len(set(outcomes)), 2)
        self.assertEqual(self._balance(), Decimal("9.000000"))
        with self.Session() as db:
            self.assertEqual(db.query(SiteBalance).filter_by(site_id=self.site_id, product_id=self.product_id).count(), 1)

    def test_concurrent_outgoing_movements_cannot_overdraw(self):
        self._post(amount="100", doc_id="OUTGOING-BASE")
        barrier = Barrier(2)

        def remove(doc):
            barrier.wait(timeout=15)
            try:
                self._post(amount="60", movement_type="outgoing", doc_id=doc)
                return "ok"
            except HTTPException as exc:
                return f"http-{exc.status_code}"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(remove, ["OUTGOING-A", "OUTGOING-B"]))
        self.assertEqual(sorted(results), ["http-409", "ok"])
        self.assertEqual(self._balance(), Decimal("40.000000"))

    def test_negative_balance_is_rejected_atomically(self):
        with self.assertRaises(HTTPException) as error:
            self._post(amount="1", movement_type="outgoing", doc_id="NO-STOCK")
        self.assertEqual(error.exception.status_code, 409)
        self.assertIsNone(self._balance())
        with self.Session() as db:
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(site_id=self.site_id).count(), 0)

    def test_negative_initial_opening_and_recovery_are_atomic_on_postgres(self):
        with self.Session() as db:
            with db.begin():
                request = inventory.create_opening_balance_request(
                    db, program="FTTH", site_id=self.site_id, product_id=self.product_id,
                    quantity="-5", uom="M", effective_date="2026-11-06",
                    reference="NEGATIVE-LEGACY", note="Synthetic cutover deficit",
                    entered_by="test-requester", request_token=f"negative-{uuid4().hex}",
                )
                request_id = request.id
        with self.Session() as db:
            with db.begin():
                inventory.approve_opening_balance(db, request_id, "FTTH", "test-approver")
        self.assertEqual(self._balance(), Decimal("-5.000000"))
        with self.assertRaises(HTTPException) as debit:
            self._post(amount="1", movement_type="outgoing", doc_id="NEGATIVE-OUT")
        self.assertEqual(debit.exception.status_code, 409)
        self.assertEqual(self._balance(), Decimal("-5.000000"))
        self._post(amount="3", doc_id="NEGATIVE-IN-1")
        self.assertEqual(self._balance(), Decimal("-2.000000"))
        self._post(amount="20", doc_id="NEGATIVE-IN-2")
        self.assertEqual(self._balance(), Decimal("18.000000"))
        with self.Session() as db:
            self.assertEqual(
                inventory.reconciliation_rows(db, "FTTH", self.site_id, self.product_id)[0]["status"],
                "MATCH",
            )

    def test_rollback_after_ledger_balance_and_before_commit(self):
        with self.Session() as db:
            initial_audit_count = db.query(AuditLog).filter_by(entity_type="site_inventory").count()
        for stage in ("after_movement", "after_balance", "after_audit"):
            with patch.object(inventory, "_stage", side_effect=lambda current, expected=stage: (_ for _ in ()).throw(RuntimeError("fault")) if current == expected else None):
                with self.assertRaises(RuntimeError):
                    self._post(amount="7", token=f"rollback-{stage}-token-01", doc_id=f"ROLLBACK-{stage}")
            self.assertIsNone(self._balance())
            with self.Session() as db:
                self.assertEqual(db.query(SiteMaterialMovement).filter_by(
                    site_id=self.site_id, product_id=self.product_id
                ).count(), 0)
                self.assertEqual(
                    db.query(AuditLog).filter_by(entity_type="site_inventory").count(), initial_audit_count
                )
        with self.Session() as db:
            with self.assertRaises(RuntimeError):
                with db.begin():
                    inventory.post_site_movement(
                        db, program="FTTH", site_id=self.site_id, product_id=self.product_id,
                        movement_type="incoming", event_type="rollback_precommit", quantity_delta=Decimal("7"),
                        uom="M", document_type="pg_test", document_id="ROLLBACK-COMMIT", document_line_id="1",
                        reference="ROLLBACK-COMMIT", note="synthetic rollback", created_by="test",
                        approved_by="test", effective_date="2026-11-06", request_token="rollback-commit-token-01",
                    )
                    raise RuntimeError("before commit")
        self.assertIsNone(self._balance())

    def test_append_only_and_reversal_history(self):
        original_id = self._post(amount="15", doc_id="REVERSAL-ORIGINAL")
        with self.Session() as db:
            with self.assertRaises(DBAPIError):
                with db.begin():
                    row = db.get(SiteMaterialMovement, original_id)
                    row.note = "must not update"
        with self.Session() as db:
            with self.assertRaises(DBAPIError):
                with db.begin():
                    db.delete(db.get(SiteMaterialMovement, original_id))
        with self.Session() as db:
            with db.begin():
                reversal = inventory.reverse_site_movement(
                    db, original_id, "FTTH", "test-approver", "REVERSAL-REF", "Correction reversal",
                    "reversal-token-postgres-001",
                )
                reversal_id = reversal.id
        self.assertEqual(self._balance(), Decimal("0.000000"))
        with self.Session() as db:
            rows = db.query(SiteMaterialMovement).filter(SiteMaterialMovement.id.in_([original_id, reversal_id])).all()
            self.assertEqual(len(rows), 2)
            reversal = next(row for row in rows if row.id == reversal_id)
            self.assertEqual(reversal.reverses_movement_id, original_id)
            self.assertEqual(reversal.quantity_delta, Decimal("-15.000000"))

    def test_reconciliation_preview_apply_and_audit(self):
        self._post(amount="21", doc_id="RECONCILE-BASE")
        with self.Session() as db:
            with db.begin():
                balance = db.query(SiteBalance).filter_by(site_id=self.site_id, product_id=self.product_id).one()
                balance.on_hand_qty = Decimal("18")
        with self.Session() as db:
            report = inventory.reconciliation_rows(db, "FTTH", self.site_id, self.product_id)
            preview = inventory.rebuild_site_balances(db, "FTTH")
        self.assertEqual(report[0]["status"], "MISMATCH")
        self.assertEqual(self._balance(), Decimal("18.000000"))
        movement_count = None
        with self.Session() as db:
            movement_count = db.query(SiteMaterialMovement).filter_by(site_id=self.site_id).count()
        with self.Session() as db:
            with db.begin():
                applied = inventory.rebuild_site_balances(
                    db, "FTTH", apply=True, expected_digest=preview["preview_digest"], actor="test-admin"
                )
        self.assertEqual(applied["mode"], "applied")
        self.assertEqual(self._balance(), Decimal("21.000000"))
        with self.Session() as db:
            self.assertEqual(db.query(SiteMaterialMovement).filter_by(site_id=self.site_id).count(), movement_count)
            self.assertEqual(db.query(AuditLog).filter_by(action="site_inventory_projection_rebuilt", entity_id="FTTH").count(), 1)
            self.assertEqual(inventory.reconciliation_rows(db, "FTTH", self.site_id, self.product_id)[0]["status"], "MATCH")

    def test_warehouse_inventory_in_temp_schema_remained_untouched(self):
        self.assertEqual(self._schema_inventory_counts(), self.warehouse_snapshot)
        self.assertEqual(self.warehouse_snapshot, {})


if __name__ == "__main__":
    main(verbosity=2)
