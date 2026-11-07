"""Versioned, additive Site Inventory Foundation migration.

The command-line runner intentionally supports only the isolated test target.
Production schema changes require a separately reviewed migration process.
"""

import argparse
import re
from datetime import datetime, timezone

from sqlalchemy import inspect, text

VERSION = "site-inventory-foundation-v1"
FOUNDATION_TABLES = (
    "site_aliases",
    "site_balances",
    "site_material_movements",
    "site_opening_balance_requests",
)
PREREQUISITE_TABLES = ("sites", "products", "audit_logs")


def _validate_schema(schema: str, dialect: str) -> str | None:
    if dialect == "sqlite":
        if schema in (None, "", "main"):
            return None
        raise ValueError("SQLite Foundation migrations use the default isolated database schema")
    value = str(schema or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise ValueError("An explicit valid PostgreSQL schema name is required")
    if value.casefold() == "public":
        raise ValueError("Site Inventory Foundation migrations cannot target the public schema")
    return value


def _table_exists(connection, schema: str | None, name: str) -> bool:
    return inspect(connection).has_table(name, schema=schema)


def _qualified(connection, schema: str, table: str) -> str:
    preparer = connection.dialect.identifier_preparer
    return f"{preparer.quote_schema(schema)}.{preparer.quote(table)}"


def _ensure_version_table(connection, schema: str | None) -> None:
    if connection.dialect.name == "postgresql":
        qualified = _qualified(connection, schema, "site_inventory_schema_versions")
        connection.exec_driver_sql(
            f"CREATE TABLE IF NOT EXISTS {qualified} ("
            "version VARCHAR(100) PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
    else:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS site_inventory_schema_versions ("
            "version VARCHAR(100) PRIMARY KEY, applied_at DATETIME NOT NULL)"
        )


def _version_applied(connection, schema: str | None) -> bool:
    if connection.dialect.name == "postgresql":
        qualified = _qualified(connection, schema, "site_inventory_schema_versions")
        return connection.execute(
            text(f"SELECT 1 FROM {qualified} WHERE version = :version"), {"version": VERSION}
        ).first() is not None
    return connection.execute(
        text("SELECT 1 FROM site_inventory_schema_versions WHERE version = :version"), {"version": VERSION}
    ).first() is not None


def _record_version(connection, schema: str | None) -> None:
    if connection.dialect.name == "postgresql":
        qualified = _qualified(connection, schema, "site_inventory_schema_versions")
        connection.execute(
            text(f"INSERT INTO {qualified} (version) VALUES (:version)"), {"version": VERSION}
        )
    else:
        connection.execute(
            text("INSERT INTO site_inventory_schema_versions (version, applied_at) VALUES (:version, :at)"),
            {"version": VERSION, "at": datetime.now(timezone.utc).isoformat()},
        )


def _upgrade_known_phase1(connection, schema: str, translated_connection) -> None:
    from models import SiteAlias, SiteBalance, SiteMaterialMovement, SiteOpeningBalanceRequest
    from site_inventory_service import source_event_key

    q_movements = _qualified(connection, schema, "site_material_movements")
    q_openings = _qualified(connection, schema, "site_opening_balance_requests")
    q_products = _qualified(connection, schema, "products")

    connection.exec_driver_sql(f"ALTER TABLE {q_movements} ADD COLUMN IF NOT EXISTS event_type VARCHAR(64)")
    connection.exec_driver_sql(f"ALTER TABLE {q_movements} ADD COLUMN IF NOT EXISTS uom VARCHAR(24)")
    connection.exec_driver_sql(f"ALTER TABLE {q_movements} ADD COLUMN IF NOT EXISTS source_event_key VARCHAR(64)")
    connection.exec_driver_sql(f"ALTER TABLE {q_movements} ADD COLUMN IF NOT EXISTS reverses_movement_id INTEGER")
    connection.exec_driver_sql(f"ALTER TABLE {q_movements} ADD COLUMN IF NOT EXISTS effective_date VARCHAR(10) NOT NULL DEFAULT ''")
    connection.exec_driver_sql(f"ALTER TABLE {q_openings} ADD COLUMN IF NOT EXISTS uom VARCHAR(24)")

    legacy_rows = connection.execute(text(
        f"SELECT id, program, site_id, product_id, movement_type, document_type, document_id, "
        f"document_line_id, effective_date, uom, event_type FROM {q_movements} ORDER BY id"
    )).mappings().all()
    for row in legacy_rows:
        event_type = str(row["event_type"] or row["movement_type"] or "legacy_movement").strip().lower()
        document_type = str(row["document_type"] or "legacy_site_movement").strip().lower()
        document_id = str(row["document_id"] or f"legacy-movement-{row['id']}").strip()
        line_id = str(row["document_line_id"] or "1").strip()
        product_unit = connection.execute(
            text(f"SELECT COALESCE(NULLIF(unit, ''), 'PCS') FROM {q_products} WHERE id = :id"),
            {"id": row["product_id"]},
        ).scalar()
        if not product_unit:
            raise RuntimeError(f"Legacy movement {row['id']} has no resolvable product unit; migration stopped")
        key = source_event_key(row["program"], event_type, document_type, document_id, line_id)
        connection.execute(text(
            f"UPDATE {q_movements} SET event_type=:event_type, document_type=:document_type, "
            "document_id=:document_id, document_line_id=:line_id, uom=:uom, source_event_key=:event_key "
            "WHERE id=:id"
        ), {
            "event_type": event_type,
            "document_type": document_type,
            "document_id": document_id,
            "line_id": line_id,
            "uom": row["uom"] or str(product_unit).strip().upper(),
            "event_key": key,
            "id": row["id"],
        })

    opening_rows = connection.execute(text(
        f"SELECT id, product_id, uom FROM {q_openings} ORDER BY id"
    )).mappings().all()
    for row in opening_rows:
        if row["uom"]:
            continue
        product_unit = connection.execute(
            text(f"SELECT COALESCE(NULLIF(unit, ''), 'PCS') FROM {q_products} WHERE id = :id"),
            {"id": row["product_id"]},
        ).scalar()
        if not product_unit:
            raise RuntimeError(f"Legacy opening request {row['id']} has no resolvable product unit; migration stopped")
        connection.execute(text(f"UPDATE {q_openings} SET uom=:uom WHERE id=:id"), {
            "uom": str(product_unit).strip().upper(), "id": row["id"]
        })

    connection.exec_driver_sql(f"ALTER TABLE {q_movements} ALTER COLUMN event_type SET NOT NULL")
    connection.exec_driver_sql(f"ALTER TABLE {q_movements} ALTER COLUMN uom SET NOT NULL")
    connection.exec_driver_sql(f"ALTER TABLE {q_movements} ALTER COLUMN source_event_key SET NOT NULL")
    connection.exec_driver_sql(f"ALTER TABLE {q_openings} ALTER COLUMN uom SET NOT NULL")
    connection.exec_driver_sql(f"ALTER TABLE {q_movements} DROP CONSTRAINT IF EXISTS uq_site_movement_request_token_hash")
    connection.exec_driver_sql(f"ALTER TABLE {q_movements} DROP CONSTRAINT IF EXISTS uq_site_movement_idempotency_key")
    connection.exec_driver_sql(f"ALTER TABLE {q_openings} DROP CONSTRAINT IF EXISTS uq_site_opening_request_token_hash")

    for table in (SiteAlias.__table__,):
        table.create(bind=translated_connection, checkfirst=True)

    connection.exec_driver_sql(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_site_movement_program_source_event "
        f"ON {q_movements} (program, source_event_key)"
    )
    connection.exec_driver_sql(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_site_movement_program_idempotency "
        f"ON {q_movements} (program, idempotency_key)"
    )
    connection.exec_driver_sql(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_site_movement_program_request_token "
        f"ON {q_movements} (program, request_token_hash) WHERE request_token_hash IS NOT NULL"
    )
    connection.exec_driver_sql(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_site_opening_program_request_token "
        f"ON {q_openings} (program, request_token_hash)"
    )
    connection.exec_driver_sql(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_site_movement_single_reversal "
        f"ON {q_movements} (reverses_movement_id) WHERE reverses_movement_id IS NOT NULL"
    )
    connection.exec_driver_sql(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_site_opening_balance_once "
        f"ON {q_movements} (program, site_id, product_id) WHERE movement_type = 'opening_balance'"
    )

    fk_name = "fk_site_movement_reverses_movement"
    has_reversal_fk = connection.execute(text(
        "SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
        "JOIN pg_namespace n ON n.oid=t.relnamespace "
        "WHERE n.nspname=:schema AND t.relname='site_material_movements' AND c.conname=:name"
    ), {"schema": schema, "name": fk_name}).first()
    if not has_reversal_fk:
        connection.exec_driver_sql(
            f"ALTER TABLE {q_movements} ADD CONSTRAINT {fk_name} FOREIGN KEY (reverses_movement_id) "
            f"REFERENCES {q_movements}(id) ON DELETE RESTRICT"
        )


def _install_postgres_guards(connection, schema: str) -> None:
    q_movements = _qualified(connection, schema, "site_material_movements")
    q_function = _qualified(connection, schema, "reject_site_movement_mutation")
    q_trigger = connection.dialect.identifier_preparer.quote("tr_site_movement_append_only")
    connection.exec_driver_sql(
        f"CREATE OR REPLACE FUNCTION {q_function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'site_material_movements are append-only; post a reversal instead'; END; $$"
    )
    connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {q_trigger} ON {q_movements}")
    connection.exec_driver_sql(
        f"CREATE TRIGGER {q_trigger} BEFORE UPDATE OR DELETE ON {q_movements} "
        f"FOR EACH ROW EXECUTE FUNCTION {q_function}()"
    )
    for table in FOUNDATION_TABLES:
        qualified = _qualified(connection, schema, table)
        connection.exec_driver_sql(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
        for role in ("anon", "authenticated"):
            exists = connection.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}
            ).first()
            if exists:
                role_name = connection.dialect.identifier_preparer.quote(role)
                connection.exec_driver_sql(f"REVOKE ALL ON TABLE {qualified} FROM {role_name}")


def _install_sqlite_guards(connection) -> None:
    connection.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS tr_site_movement_append_only_update "
        "BEFORE UPDATE ON site_material_movements BEGIN "
        "SELECT RAISE(ABORT, 'site_material_movements are append-only; post a reversal instead'); END"
    )
    connection.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS tr_site_movement_append_only_delete "
        "BEFORE DELETE ON site_material_movements BEGIN "
        "SELECT RAISE(ABORT, 'site_material_movements are append-only; post a reversal instead'); END"
    )


def migrate(bind, schema: str) -> dict:
    """Apply the Foundation migration to one explicitly named schema."""
    from models import SiteAlias, SiteBalance, SiteMaterialMovement, SiteOpeningBalanceRequest

    dialect = bind.dialect.name
    schema_name = _validate_schema(schema, dialect)
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError(f"Unsupported migration dialect: {dialect}")
    with bind.connect() as connection:
        transaction = connection.begin()
        try:
            if dialect == "postgresql":
                connection.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS {connection.dialect.identifier_preparer.quote_schema(schema_name)}")
                translated = connection.execution_options(schema_translate_map={None: schema_name})
                for table_name in PREREQUISITE_TABLES:
                    if not _table_exists(connection, schema_name, table_name):
                        raise RuntimeError(f"Required pre-existing table {schema_name}.{table_name} is missing")
                _ensure_version_table(connection, schema_name)
                if _version_applied(connection, schema_name):
                    transaction.commit()
                    return {"version": VERSION, "schema": schema_name, "already_applied": True, "tables": list(FOUNDATION_TABLES)}
                existing = {name for name in FOUNDATION_TABLES if _table_exists(connection, schema_name, name)}
                known_phase1 = {
                    "site_balances",
                    "site_material_movements",
                    "site_opening_balance_requests",
                }
                if existing and existing != known_phase1 and existing != set(FOUNDATION_TABLES):
                    raise RuntimeError("Partial unversioned Site Inventory schema found; migration stopped without guessing")
                if existing:
                    _upgrade_known_phase1(connection, schema_name, translated)
                else:
                    for table in (SiteAlias.__table__, SiteBalance.__table__, SiteMaterialMovement.__table__, SiteOpeningBalanceRequest.__table__):
                        table.create(bind=translated, checkfirst=False)
                _install_postgres_guards(connection, schema_name)
                _record_version(connection, schema_name)
            else:
                for table_name in PREREQUISITE_TABLES:
                    if not _table_exists(connection, None, table_name):
                        raise RuntimeError(f"Required pre-existing table {table_name} is missing")
                _ensure_version_table(connection, None)
                if _version_applied(connection, None):
                    transaction.commit()
                    return {"version": VERSION, "schema": "main", "already_applied": True, "tables": list(FOUNDATION_TABLES)}
                existing = {name for name in FOUNDATION_TABLES if _table_exists(connection, None, name)}
                if existing:
                    raise RuntimeError("Unversioned SQLite Site Inventory tables require explicit review; migration stopped")
                for table in (SiteAlias.__table__, SiteBalance.__table__, SiteMaterialMovement.__table__, SiteOpeningBalanceRequest.__table__):
                    table.create(bind=connection, checkfirst=False)
                _install_sqlite_guards(connection)
                _record_version(connection, None)
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise
    return {"version": VERSION, "schema": schema_name or "main", "already_applied": False, "tables": list(FOUNDATION_TABLES)}


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(".env.test", override=False)
    from database import APP_ENV, TEST_DATABASE_URL, create_database_engine, test_database_identity

    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=("test",), required=True)
    parser.add_argument("--schema", required=True)
    args = parser.parse_args()
    identity = test_database_identity(TEST_DATABASE_URL)
    if APP_ENV != "test" or not identity["safe"] or args.schema == "public":
        raise SystemExit("Migration requires verified Warehouse-Test credentials and a non-public schema")
    test_engine = create_database_engine(TEST_DATABASE_URL)
    try:
        print(migrate(test_engine, args.schema))
    finally:
        test_engine.dispose()


if __name__ == "__main__":
    main()
