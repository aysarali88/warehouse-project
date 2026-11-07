"""Versioned, additive Site Inventory Foundation migration.

The migration function targets a verified explicit schema, or the active
PostgreSQL schema when omitted. Its command-line runner remains test-only.
"""

import argparse
import re
from datetime import datetime, timezone

from sqlalchemy import inspect, text

from migrations.site_inventory_migration_utils import (
    ensure_version_table as ensure_postgres_version_table,
    qualified,
    record_version as record_postgres_version,
    require_table_columns,
    resolve_postgres_schema,
    validate_model_table,
    version_applied as postgres_version_applied,
)

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
    if value and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise ValueError("Invalid PostgreSQL schema identifier")
    return value or None


def _table_exists(connection, schema: str | None, name: str) -> bool:
    return inspect(connection).has_table(name, schema=schema)


def _qualified(connection, schema: str, table: str) -> str:
    return qualified(connection, schema, table)


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


def _install_postgres_guards(connection, schema: str) -> None:
    q_movements = _qualified(connection, schema, "site_material_movements")
    q_function = _qualified(connection, schema, "reject_site_movement_mutation")
    q_trigger = connection.dialect.identifier_preparer.quote("tr_site_movement_append_only")
    function_row = connection.execute(text(
        "SELECT pg_get_functiondef(p.oid) FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname=:schema AND p.proname='reject_site_movement_mutation' AND p.pronargs=0"
    ), {"schema": schema}).first()
    if function_row:
        if "site_material_movements are append-only" not in function_row[0]:
            raise RuntimeError("Unexpected append-only guard function; migration stopped")
    else:
        connection.exec_driver_sql(
            f"CREATE FUNCTION {q_function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'site_material_movements are append-only; post a reversal instead'; END; $$"
        )
    trigger_row = connection.execute(text(
        "SELECT t.tgtype, p.proname FROM pg_trigger t "
        "JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_proc p ON p.oid=t.tgfoid "
        "WHERE n.nspname=:schema AND c.relname='site_material_movements' AND t.tgname='tr_site_movement_append_only' AND NOT t.tgisinternal"
    ), {"schema": schema}).first()
    if trigger_row:
        if trigger_row[1] != "reject_site_movement_mutation" or int(trigger_row[0]) != 27:
            raise RuntimeError("Unexpected append-only movement trigger; migration stopped")
    else:
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


def migrate(bind, schema: str | None = None) -> dict:
    """Apply Foundation DDL to an explicit schema or the active PostgreSQL schema."""
    from models import AuditLog, Product, Site, SiteAlias, SiteBalance, SiteMaterialMovement, SiteOpeningBalanceRequest

    dialect = bind.dialect.name
    schema_name = _validate_schema(schema, dialect)
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError(f"Unsupported migration dialect: {dialect}")
    with bind.connect() as connection:
        transaction = connection.begin()
        try:
            if dialect == "postgresql":
                schema_name = resolve_postgres_schema(connection, schema_name)
                translated = connection.execution_options(schema_translate_map={None: schema_name})
                require_table_columns(connection, schema_name, {
                    "sites": {"id", "program", "name"},
                    "products": {"id", "program", "unit"},
                    "audit_logs": {"id", "action", "entity_type", "entity_id", "actor", "details", "created_at"},
                })
                ensure_postgres_version_table(connection, schema_name)
                managed_tables = (SiteAlias.__table__, SiteBalance.__table__, SiteMaterialMovement.__table__, SiteOpeningBalanceRequest.__table__)
                if postgres_version_applied(connection, schema_name, VERSION):
                    for table in managed_tables:
                        validate_model_table(connection, schema_name, table)
                    _install_postgres_guards(connection, schema_name)
                    transaction.commit()
                    return {"version": VERSION, "schema": schema_name, "already_applied": True, "tables": list(FOUNDATION_TABLES)}
                existing = {name for name in FOUNDATION_TABLES if _table_exists(connection, schema_name, name)}
                if existing:
                    if existing != set(FOUNDATION_TABLES):
                        raise RuntimeError("Partial unversioned Site Inventory schema found; migration stopped without guessing")
                    for table in managed_tables:
                        validate_model_table(connection, schema_name, table)
                else:
                    for table in managed_tables:
                        table.create(bind=translated, checkfirst=False)
                for table in managed_tables:
                    validate_model_table(connection, schema_name, table)
                _install_postgres_guards(connection, schema_name)
                record_postgres_version(connection, schema_name, VERSION)
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
    if APP_ENV != "test" or not identity["safe"] or args.schema.casefold() == "public":
        raise SystemExit("Migration requires verified Warehouse-Test credentials and a non-public schema")
    test_engine = create_database_engine(TEST_DATABASE_URL)
    try:
        print(migrate(test_engine, args.schema))
    finally:
        test_engine.dispose()


if __name__ == "__main__":
    main()
